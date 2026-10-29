"""Alpaca OPRA option stream, translated to the internal option-stream format.

FALLBACK OPTION STREAM (2026-10-15, Audit P1; updated 2026-10-17):
This is the FALLBACK, not the primary. The PRIMARY is compass/option_stream.py
(Massive/Polygon). This Alpaca stream is disabled by default.
- Enabled via: ALPACA_OPTION_STREAM_ENABLED=true AND Alpaca keys set
- Health key: health:alpaca_option_stream
- As of 2026-10-15, shows 0 subscribed (dark). This is EXPECTED when disabled.
  Do NOT treat 0-subscribed as a failure - it's the fallback, not the primary.
- SAFE to run both simultaneously (2026-10-17): Dedup logic prevents
  double-counting. Quotes are stored by symbol key, put_quote uses timestamp
  check (only updates if new ts >= existing ts). If Massive and Alpaca both
  deliver same contract at same timestamp, second write overwrites with
  identical data — no double-counting. If Massive misses a contract, Alpaca's
  quote fills the gap. Running both is RECOMMENDED for redundancy.
- FALLBACK (dynamic since 2026-10-19): If option_symbols is empty for
  30s, the stream subscribes to a generated SPY/QQQ ladder for the
  current expiry (fallback_symbols). The earlier hard-coded list
  expired 2026-10-19 and would have subscribed dead contracts forever.

Runs side-by-side with the Massive stream during the migration evaluation.
Every translated item carries 'src': 'alpaca' so downstream persistence can
tag rows by source and the comparison can measure parity.

Alpaca protocol (wss://stream.data.alpaca.markets/v1beta1/opra):
  NOTE: OPRA requires MessagePack (binary) encoding, NOT JSON text.
  auth:      {"action": "auth", "key": "...", "secret": "..."} (msgpack)
  subscribe: {"action": "subscribe", "trades": [...], "quotes": [...]} (msgpack)
  trade:     {"T":"t","S":"SPY251004C00745000","x":"Q","p":1.25,"s":10,
              "t":"2026-10-05T13:30:00.123Z","c":[...],"i":12345}
  quote:     {"T":"q","S":"...","ax":"Q","ap":1.30,"as":5,"bx":"Q","bp":1.20,
              "bs":10,"t":"...","c":[...]}

Internal format (matches option_stream.OptionBuffer.offer expectations):
  trade: {'sym','ev':'T','t','p','s','i','q','src':'alpaca'}
  quote: {'sym','ev':'Q','t','bp','ap','bs','as','src':'alpaca'}

Symbol mapping: internal/Massive form is 'O:SPY251004C00745000';
Alpaca's wire form strips the 'O:' prefix.
"""
import asyncio
import json
import logging
import time

import msgpack

from .market import ts, number
from .diagnostics import redacted_detail
from .option_stream import (OptionBuffer, OptionStreamError, persist_batch,
                            drops_in_window, snapshot_hour,
                            write_health_snapshot, DROP_WINDOW)

ALPACA_OPRA_WS = "wss://stream.data.alpaca.markets/v1beta1/opra"


def to_alpaca_symbol(sym):
    """Internal 'O:...' form -> Alpaca wire form (strip O: prefix)."""
    sym = str(sym or "")
    return sym[2:] if sym.startswith("O:") else sym


def to_internal_symbol(alpaca_sym):
    """Alpaca wire form -> internal 'O:...' form."""
    sym = str(alpaca_sym or "")
    return sym if sym.startswith("O:") else "O:" + sym


def subscription_diff(wanted, have):
    """(added, removed) wire-form symbols between two internal sets."""
    wire = {to_alpaca_symbol(s) for s in wanted}
    have_wire = {to_alpaca_symbol(s) for s in have}
    return sorted(wire - have_wire), sorted(have_wire - wire)


def _occ(root, expiry, right, strike):
    return "O:%s%s%s%08d" % (root, expiry.strftime("%y%m%d"), right,
                             int(round(strike * 1000)))


def _spot(db, symbol, now=None):
    """Last usable spot for a fallback ladder: fresh quote mid, else last
    bar close (mirrors the providers chain-reference logic).

    Freshness is judged against the caller's `now` (fallback_symbols'
    own clock), not the wall clock: with a wall-clock check, a quote
    that is fresh relative to the evaluated instant silently ages out
    7 days later, which both broke the deterministic unit test from
    2026-10-29 (its 2026-10-21 quotes crossed the window) and would
    misjudge any replayed/backdated evaluation."""
    if now is None:
        now = time.time()
    try:
        with db.tx() as c:
            q = db.get(c, "quote:" + symbol)
            if q and 0 < q.get("bid", 0) <= q.get("ask", 0) \
                    and now - q.get("ts", 0) < 7 * 86400:
                return (q["bid"] + q["ask"]) / 2
            bars = db.recent(c, "bar", symbol, limit=1)
            if bars:
                return number(bars[0]["payload"].get("c"))
    except Exception:
        return None
    return None


def fallback_symbols(db, now=None):
    """Dynamic SPY/QQQ fallback ladder.

    Replaces the hard-coded contracts that expired 2026-10-19 (audit
    #10): from the next day the old list subscribed only dead contracts.
    SPY/QQQ list options expiring every weekday, so the expiry is today
    (ET) before the 16:15 ET close, else the next weekday. Strikes are
    a 5-point ladder centered on the last known spot. Empty when no spot
    is known — a blind ladder would subscribe dead strikes and fake
    coverage."""
    from datetime import datetime, timedelta
    from .market import NY
    now = now or time.time()
    et = datetime.fromtimestamp(now, NY)
    expiry = et.date()
    if expiry.weekday() >= 5 or (et.hour, et.minute) >= (16, 15):
        expiry += timedelta(days=1)
        while expiry.weekday() >= 5:
            expiry += timedelta(days=1)
    out = []
    for root in ("SPY", "QQQ"):
        spot = _spot(db, root, now)
        if not spot:
            continue
        atm = round(spot / 5.0) * 5
        for off in (-10, -5, 0, 5, 10):
            for right in ("C", "P"):
                out.append(_occ(root, expiry, right, atm + off))
    return out


def translate(item):
    """Pure function: one Alpaca WS message -> internal format. None if unusable."""
    if not isinstance(item, dict):
        return None
    kind = item.get("T")
    if kind not in ("t", "q"):
        return None
    sym = to_internal_symbol(item.get("S"))
    stamp = ts(item.get("t"))
    if not sym or stamp is None:
        return None
    # Store the parsed epoch, NOT the raw wire value: for trades the raw
    # 't' is a msgpack Timestamp object, which is not JSON serializable and
    # killed the commit path on the first trade print of 2026-10-20 even
    # after ts() learned to parse it. Downstream re-runs ts() on 't',
    # which accepts epoch floats, so the epoch is equivalent everywhere.
    out = {"sym": sym, "t": stamp, "src": "alpaca"}
    if kind == "t":
        # Trade: price, size, trade id. 'q' (quantity) has no Alpaca
        # equivalent; downstream uses it only for identity, size suffices.
        if number(item.get("p")) is None or number(item.get("s")) is None:
            return None
        out.update(ev="T", p=item.get("p"), s=item.get("s"),
                   i=item.get("i"), q=item.get("s"))
    else:
        if number(item.get("bp")) is None or number(item.get("ap")) is None:
            return None
        out.update(ev="Q", bp=item.get("bp"), ap=item.get("ap"),
                   bs=item.get("bs", 0), as_=item.get("as", 0))
        # 'as' is a keyword; OptionBuffer reads item.get('as').
        out["as"] = out.pop("as_")
    return out


async def consume(ws, collector, buffer=None):
    """Alpaca OPRA consumer. Feeds the shared OptionBuffer; source-tagged."""
    from .option_subscriptions import SubscriptionTrace
    buffer = buffer or OptionBuffer()
    buffer.trade_observer = getattr(collector, 'iceberg', None)
    authenticated = asyncio.Event()
    subscribed = set()
    committed = {"quotes": 0, "trades": 0}
    trace = SubscriptionTrace(time.time(), "alpaca")
    # Diagnostics for the status endpoint: what we're actually receiving.
    diag = {"last_raw_type": None, "last_raw_len": 0, "last_msgpack_error": None,
            "msgpack_failures": 0, "messages_received": 0, "last_message_at": None}

    async def read():
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), 10)
            except asyncio.TimeoutError:
                if not authenticated.is_set():
                    raise OptionStreamError(
                        "Alpaca OPRA authentication timed out; reconnecting") from None
                continue
            now, mono = time.time(), time.monotonic()
            diag["messages_received"] += 1
            diag["last_message_at"] = now
            diag["last_raw_type"] = type(raw).__name__
            diag["last_raw_len"] = len(raw) if hasattr(raw, "__len__") else 0
            try:
                # OPRA uses MessagePack binary, not JSON text.
                msgs = msgpack.unpackb(raw, raw=False)
            except Exception as e:
                diag["msgpack_failures"] += 1
                diag["last_msgpack_error"] = str(e)[:200]
                # Try to see if it's actually JSON/text (wrong encoding)
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        preview = raw[:200].decode('utf-8', errors='replace')
                        diag["last_raw_preview"] = preview
                    except Exception:
                        diag["last_raw_preview"] = "<binary>"
                else:
                    diag["last_raw_preview"] = str(raw)[:200]
                continue
            if not isinstance(msgs, list):
                msgs = [msgs]
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                # Log every message type we see for diagnostics
                diag["last_msg_type"] = msg.get("T")
                diag["last_msg_detail"] = str(msg.get("msg") or "")[:100]
                if msg.get("T") == "success":
                    # Auth: {"T":"success","msg":"authenticated"}
                    # Sub confirm: {"T":"success","msg":"subscribed to trades: [...], quotes: [...]"}
                    msg_text = str(msg.get("msg") or "")
                    if msg_text == "authenticated":
                        authenticated.set()
                        diag["authenticated_at"] = now
                    elif "subscribed to" in msg_text:
                        diag["last_subscription"] = {
                            "at": now,
                            "detail": msg_text[:300],
                        }
                    else:
                        diag["last_success_msg"] = msg_text[:200]
                    continue
                if msg.get("T") == "subscription":
                    # Alpaca confirms subscriptions: {"T":"subscription","trades":[...],"quotes":[...]}
                    diag["last_subscription"] = {
                        "at": now,
                        "trades": len(msg.get("trades") or []),
                        "quotes": len(msg.get("quotes") or []),
                    }
                    continue
                if msg.get("T") == "error":
                    # Always record the error first — the old code raised
                    # without keeping the code/message, discarding the
                    # single most useful diagnostic.
                    diag["last_error"] = {
                        "at": now, "code": msg.get("code"),
                        "msg": str(msg.get("msg", "error"))[:200]}
                    if not authenticated.is_set():
                        raise OptionStreamError(
                            "Alpaca OPRA " + redacted_detail(str(msg.get("msg", "error"))))
                    # Post-auth errors are usually symbol-level (unknown
                    # or over-limit symbols in one batch). Tearing the
                    # whole stream down for one bad symbol is how a feed
                    # stays dark; record it in diag (visible in the
                    # health row) and keep streaming.
                    continue
                item = translate(msg)
                if item is None:
                    continue
                if item["sym"] in subscribed and item["ev"] in ("Q", "T"):
                    trace.data(item["ev"], item["sym"], now, ts(item.get("t")))
                buffer.offer(item, subscribed, now, mono)
            await asyncio.sleep(0)

    async def subscriptions():
        try:
            await asyncio.wait_for(authenticated.wait(), 15)
        except asyncio.TimeoutError:
            raise OptionStreamError(
                "Alpaca OPRA authentication timed out; reconnecting") from None
        # Alpaca silently drops large subscribe payloads. Batch into chunks
        # of 20 symbols to stay under undocumented per-message limits.
        SUBSCRIBE_BATCH_SIZE = 20
        empty_cycles = 0
        while True:
            wanted = set(collector.option_symbols)
            # If wanted is empty, the symbol selector hasn't populated yet
            # (e.g., collector just restarted, or chain fetch failing).
            # After 60 empty cycles (30s), fall back to a dynamic SPY/QQQ
            # ladder (fallback_symbols) so the stream doesn't sit idle.
            if not wanted:
                empty_cycles += 1
                if empty_cycles >= 60:
                    if empty_cycles == 60:
                        try:
                            collector.db.health('alpaca_option_stream', 'warning',
                                'option_symbols empty for 30s - using dynamic fallback ladder (SPY/QQQ)')
                        except:
                            pass
                    # Recomputed each cycle: a spot that appears later
                    # (or the next day's expiry) is picked up, unlike
                    # the old one-shot static list.
                    wanted = set(fallback_symbols(collector.db))
                    if not wanted:
                        await asyncio.sleep(0.5)
                        continue
                else:
                    await asyncio.sleep(0.5)
                    continue
            empty_cycles = 0
            added, removed = subscription_diff(wanted, subscribed)
            if added or removed:
                if added:
                    trace.request("subscribe",
                                  {to_internal_symbol(s) for s in added},
                                  time.time())
                    for i in range(0, len(added), SUBSCRIBE_BATCH_SIZE):
                        batch = added[i:i + SUBSCRIBE_BATCH_SIZE]
                        await ws.send(msgpack.packb({"action": "subscribe",
                                                     "trades": batch, "quotes": batch}))
                        # Small delay between batches to avoid rate limiting
                        if i + SUBSCRIBE_BATCH_SIZE < len(added):
                            await asyncio.sleep(0.1)
                    trace.sent("subscribe",
                               {to_internal_symbol(s) for s in added},
                               time.time())
                if removed:
                    # Alpaca holds server-side subscriptions until told;
                    # the old code never unsubscribed, so the server-side
                    # set drifted from ours and stale symbols kept flowing.
                    for i in range(0, len(removed), SUBSCRIBE_BATCH_SIZE):
                        batch = removed[i:i + SUBSCRIBE_BATCH_SIZE]
                        await ws.send(msgpack.packb({"action": "unsubscribe",
                                                     "trades": batch, "quotes": batch}))
                        if i + SUBSCRIBE_BATCH_SIZE < len(removed):
                            await asyncio.sleep(0.1)
                diag["last_subscribe_sent"] = {
                    "at": time.time(),
                    "added": len(added),
                    "removed": len(removed),
                    "count": len(wanted),
                    "sample": sorted(to_alpaca_symbol(s) for s in wanted)[:3],
                    "batches": (len(added) + SUBSCRIBE_BATCH_SIZE - 1) // SUBSCRIBE_BATCH_SIZE,
                    "deploy_marker": "DIFF_SUB_V3_20261019",
                }
            subscribed.clear()
            subscribed.update(wanted)
            buffer.prune(wanted)
            trace.prune(wanted)
            await asyncio.sleep(0.5)

    # Batches dropped after persist_batch exhausted its retries. The
    # pre-fix writer let a persistence exception kill the writer task,
    # which (via FIRST_COMPLETED) tore down the whole stream; combined
    # with the buffer raising on a full queue, a slow database converted
    # directly into a zero-data flap loop on 2026-10-21.
    dropped = {"quotes": 0, "trades": 0}
    drop_events = []
    snapshot_state = {"hour": None}

    async def note_drop(kind, rows):
        """Fix 4 (2026-10-26): a persistence drop is a health failure with
        its own degraded row (the stock-stream pattern), not a counter the
        stream reports past while claiming to receive."""
        dropped[kind] += rows
        drop_events.append((time.time(), kind, rows))
        try:
            await asyncio.to_thread(
                collector.db.health, "alpaca_option_stream_persistence",
                "degraded",
                "Alpaca %s batch dropped after a database failure; "
                "observation gap recorded" % kind,
                dropped_batches=dict(dropped))
        except Exception:
            logging.getLogger("uvicorn.error").exception(
                "Alpaca option stream drop health write failed")

    async def write(kind):
        queue = buffer.quotes if kind == "quotes" else buffer.trades
        while True:
            if kind == "quotes":
                buffer.flush(time.monotonic())
            batch = list(queue)[:64]
            if batch:
                if kind == "quotes":
                    # Split by source so rows are tagged correctly. Each
                    # group resolves independently: committed or dropped,
                    # never fatal to the stream.
                    by_src = {}
                    for row in batch:
                        sym, q, record = row
                        src = (q.get("src") if isinstance(q, dict) else None) or "alpaca"
                        by_src.setdefault(src, []).append(row)
                    for src, rows in by_src.items():
                        try:
                            ok = await persist_batch(
                                collector.quote_batch, src, rows,
                                label="Alpaca quote persistence")
                        except asyncio.CancelledError:
                            # persist_batch awaited the thread: committed.
                            for _ in rows:
                                queue.popleft()
                            committed[kind] += len(rows)
                            raise
                        if ok:
                            committed[kind] += len(rows)
                        else:
                            await note_drop(kind, len(rows))
                            logging.getLogger("uvicorn.error").warning(
                                "Alpaca %s persistence batch dropped after "
                                "retries (%s rows)", kind, len(rows))
                else:
                    try:
                        ok = await persist_batch(
                            collector.option_trade_batch, batch,
                            label="Alpaca trade persistence")
                    except asyncio.CancelledError:
                        for _ in batch:
                            queue.popleft()
                        committed[kind] += len(batch)
                        raise
                    if ok:
                        committed[kind] += len(batch)
                    else:
                        await note_drop(kind, len(batch))
                        logging.getLogger("uvicorn.error").warning(
                            "Alpaca %s persistence batch dropped after "
                            "retries (%s rows)", kind, len(batch))
                for _ in batch:
                    queue.popleft()
            else:
                await asyncio.sleep(0.05)

    async def health():
        await authenticated.wait()
        while True:
            now = time.time()
            latest = max((r.get("last_source_ts") for r in
                          buffer.diagnostics.values() if "last_source_ts" in r),
                         default=None)
            recent_batches, recent_rows = drops_in_window(drop_events, now)
            # 2026-10-14: Wrap health write in try/except so DB failures
            # don't crash the health task and trigger stream restart.
            try:
                await asyncio.to_thread(
                    collector.db.health, "alpaca_option_stream",
                    "degraded" if recent_batches else
                    "receiving" if latest else "waiting",
                    "Alpaca OPRA parallel run; source-tagged rows for parity check",
                    latest, subscribed=len(subscribed),
                    committed=dict(committed),
                    diag={**diag,
                          "persistence_dropped_batches": dict(dropped),
                          "persistence_dropped_recent_batches": recent_batches,
                          "persistence_dropped_recent_rows": recent_rows,
                          "persistence_drop_window_seconds": DROP_WINDOW,
                          "buffer_dropped_quotes": buffer.dropped_quotes,
                          "buffer_dropped_trades": buffer.dropped_trades})
            except Exception as e:
                logging.getLogger('uvicorn.error').warning(
                    'OPRA health write failed: %s', type(e).__name__)
            if snapshot_state["hour"] != snapshot_hour(now):
                snapshot_state["hour"] = snapshot_hour(now)
                try:
                    await asyncio.to_thread(
                        write_health_snapshot, collector.db,
                        "alpaca_option_stream", now,
                        persistence_dropped_batches=dict(dropped),
                        buffer_dropped_quotes=buffer.dropped_quotes,
                        buffer_dropped_trades=buffer.dropped_trades,
                        committed=dict(committed))
                except Exception:
                    logging.getLogger('uvicorn.error').exception(
                        'Alpaca option stream health snapshot failed')
            await asyncio.sleep(30)

    async def authenticate():
        await ws.send(msgpack.packb({"action": "auth",
                                     "key": collector.cfg.alpaca_opra_key,
                                     "secret": collector.cfg.alpaca_opra_secret}))
        # Wait for the auth confirmation; returning early would trigger
        # FIRST_COMPLETED and tear down the whole consumer.
        await authenticated.wait()
        # ROOT CAUSE of the zero-trades Alpaca stream (found 2026-10-19):
        # this task RETURNED as soon as auth succeeded, and consume()
        # ends when the FIRST worker completes — so every successful
        # auth tore the stream down. subscriptions() raced the teardown
        # and lost; the stream re-authed every ~15s forever with
        # subscribed=0 and no error anywhere. Park instead: if auth
        # fails, read() raises and ends the consumer properly.
        await asyncio.Event().wait()

    workers = [asyncio.create_task(fn())
               for fn in (authenticate, read, subscriptions, health)]
    workers += [asyncio.create_task(write(kind)) for kind in ("quotes", "trades")]
    try:
        done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if buffer.quotes or buffer.trades or buffer.pending:
            logging.getLogger("uvicorn.error").warning(
                "Alpaca option stream stopped with pending quotes=%s trades=%s",
                len(buffer.quotes), len(buffer.trades))


def compare_sources(db, hours=1, now=None):
    """Parity check: per-source trade counts over window.

    Returns {'alpaca': {...}, 'massive': {...}} for the dashboard or logs.
    Uses a short window and COUNT-only to stay fast on the large events table.
    """
    import time as _time
    from sqlalchemy import text
    now = now or _time.time()
    since = now - hours * 3600
    out = {}
    with db.tx() as c:
        # COUNT-only, no JSON payload math — just need to know if each
        # source is producing data.
        rows = c.execute(text("""
            SELECT COALESCE(payload->>'src', 'massive') AS src,
                   COUNT(*) AS n
            FROM events
            WHERE kind = 'option_trade' AND ts >= :since
            GROUP BY 1
        """), {"since": since}).mappings().all()
        by_src = {r["src"]: r for r in rows}
        for source in ("alpaca", "massive"):
            r = by_src.get(source, {})
            out[source] = {"trades": int(r.get("n") or 0)}
    return {"asof": now, "window_hours": hours, "sources": out}
