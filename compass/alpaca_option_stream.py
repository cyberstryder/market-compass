"""Alpaca OPRA option stream, translated to the internal option-stream format.

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
from .option_stream import OptionBuffer, OptionStreamError

ALPACA_OPRA_WS = "wss://stream.data.alpaca.markets/v1beta1/opra"


def to_alpaca_symbol(sym):
    """Internal 'O:...' form -> Alpaca wire form (strip O: prefix)."""
    sym = str(sym or "")
    return sym[2:] if sym.startswith("O:") else sym


def to_internal_symbol(alpaca_sym):
    """Alpaca wire form -> internal 'O:...' form."""
    sym = str(alpaca_sym or "")
    return sym if sym.startswith("O:") else "O:" + sym


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
    out = {"sym": sym, "t": item.get("t"), "src": "alpaca"}
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
                    # Auth confirmation: {"T":"success","msg":"authenticated"}
                    # Subscription confirmation may also come as success
                    if msg.get("msg") == "authenticated":
                        authenticated.set()
                        diag["authenticated_at"] = now
                    else:
                        # Log non-auth success (likely subscription ack)
                        diag["last_success_msg"] = str(msg.get("msg") or "")[:200]
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
                    raise OptionStreamError(
                        "Alpaca OPRA " + redacted_detail(str(msg.get("msg", "error"))))
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
        while True:
            wanted = set(collector.option_symbols)
            wire = sorted(to_alpaca_symbol(s) for s in wanted)
            have_wire = sorted(to_alpaca_symbol(s) for s in subscribed)
            if wire != have_wire:
                trace.request("subscribe", wanted, time.time())
                await ws.send(msgpack.packb({"action": "subscribe",
                                             "trades": wire, "quotes": wire}))
                trace.sent("subscribe", wanted, time.time())
                # Log what we actually sent for diagnostics
                diag["last_subscribe_sent"] = {
                    "at": time.time(),
                    "count": len(wire),
                    "sample": wire[:3] if wire else [],
                }
            subscribed.clear()
            subscribed.update(wanted)
            buffer.prune(wanted)
            trace.prune(wanted)
            await asyncio.sleep(0.5)

    async def write(kind):
        queue = buffer.quotes if kind == "quotes" else buffer.trades
        while True:
            if kind == "quotes":
                buffer.flush(time.monotonic())
            batch = list(queue)[:64]
            if batch:
                if kind == "quotes":
                    # Split by source so rows are tagged correctly.
                    by_src = {}
                    for row in batch:
                        sym, q, record = row
                        src = (q.get("src") if isinstance(q, dict) else None) or "alpaca"
                        by_src.setdefault(src, []).append(row)
                    for src, rows in by_src.items():
                        work = asyncio.create_task(
                            asyncio.to_thread(collector.quote_batch, src, rows))
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            await work
                            for _ in rows:
                                queue.popleft()
                            committed[kind] += len(rows)
                            raise
                else:
                    work = asyncio.create_task(
                        asyncio.to_thread(collector.option_trade_batch, batch))
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        await work
                        for _ in batch:
                            queue.popleft()
                        committed[kind] += len(batch)
                        raise
                for _ in batch:
                    queue.popleft()
                committed[kind] += len(batch)
            else:
                await asyncio.sleep(0.05)

    async def health():
        await authenticated.wait()
        while True:
            now = time.time()
            latest = max((r.get("last_source_ts") for r in
                          buffer.diagnostics.values() if "last_source_ts" in r),
                         default=None)
            await asyncio.to_thread(
                collector.db.health, "alpaca_option_stream",
                "receiving" if latest else "waiting",
                "Alpaca OPRA parallel run; source-tagged rows for parity check",
                latest, subscribed=len(subscribed),
                committed=dict(committed), diag=dict(diag))
            await asyncio.sleep(30)

    async def authenticate():
        await ws.send(msgpack.packb({"action": "auth",
                                     "key": collector.cfg.alpaca_opra_key,
                                     "secret": collector.cfg.alpaca_opra_secret}))
        # Wait for the auth confirmation; returning early would trigger
        # FIRST_COMPLETED and tear down the whole consumer.
        await authenticated.wait()

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
