"""Tests for compass/alpaca_option_stream.py (Alpaca OPRA translation)."""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.alpaca_option_stream import (
    fallback_symbols, subscription_diff, translate, to_alpaca_symbol,
    to_internal_symbol)


def test_symbol_conversion():
    assert to_alpaca_symbol("O:SPY251004C00745000") == "SPY251004C00745000"
    assert to_alpaca_symbol("SPY251004C00745000") == "SPY251004C00745000"
    assert to_internal_symbol("SPY251004C00745000") == "O:SPY251004C00745000"
    assert to_internal_symbol("O:SPY251004C00745000") == "O:SPY251004C00745000"


def test_translate_trade():
    msg = {"T": "t", "S": "SPY251004C00745000", "x": "Q", "p": 1.25,
           "s": 10, "t": "2026-10-05T13:30:00.123Z", "c": ["@"], "i": 12345}
    out = translate(msg)
    assert out is not None
    assert out["sym"] == "O:SPY251004C00745000"
    assert out["ev"] == "T"
    assert out["p"] == 1.25
    assert out["s"] == 10
    assert out["i"] == 12345
    assert out["src"] == "alpaca"


def test_translate_quote():
    msg = {"T": "q", "S": "SPY251004C00745000", "ax": "Q", "ap": 1.30,
           "as": 5, "bx": "Q", "bp": 1.20, "bs": 10,
           "t": "2026-10-05T13:30:00.123Z", "c": ["R"]}
    out = translate(msg)
    assert out is not None
    assert out["sym"] == "O:SPY251004C00745000"
    assert out["ev"] == "Q"
    assert out["bp"] == 1.20
    assert out["ap"] == 1.30
    assert out["bs"] == 10
    assert out["as"] == 5
    assert out["src"] == "alpaca"


def test_translate_trade_with_msgpack_timestamp():
    """Live failure 2026-10-20: Alpaca OPRA TRADE messages carry 't'
    as a msgpack Timestamp object (quotes use ISO strings). ts() fed
    str(Timestamp) into fromisoformat -> ValueError, killing the
    stream on the first trade print after the open. The exact value
    from the production error: Timestamp(seconds=1792503033,
    nanoseconds=335444196) = 2026-10-20 13:30:33 UTC (the open)."""
    import msgpack
    from compass.market import ts
    stamp = msgpack.Timestamp(1792503033, 335444196)
    assert ts(stamp) == 1792503033 + 335444196 / 1e9
    msg = {"T": "t", "S": "SPY261020C00745000", "x": "Q", "p": 1.25,
           "s": 10, "t": stamp, "c": ["@"], "i": 999}
    out = translate(msg)
    assert out is not None
    assert out["ev"] == "T"
    assert out["sym"] == "O:SPY261020C00745000"
    # The raw msgpack Timestamp must NOT ride along in the output: the
    # commit path JSON-serializes the payload, and a Timestamp object
    # raises TypeError there (the second 2026-10-20 Alpaca failure).
    import json
    assert out["t"] == 1792503033 + 335444196 / 1e9
    json.dumps(out)


def test_ts_accepts_datetime_and_iso():
    from compass.market import ts
    dt = datetime(2026, 10, 20, 13, 30, 33, tzinfo=timezone.utc)
    assert ts(dt) == dt.timestamp()
    assert ts("2026-10-20T13:30:33Z") == dt.timestamp()


def test_translate_rejects():
    assert translate(None) is None
    assert translate({}) is None
    assert translate({"T": "success", "msg": "authenticated"}) is None
    assert translate({"T": "t"}) is None  # no symbol
    assert translate({"T": "t", "S": "X"}) is None  # no timestamp
    # Missing price/size on trade
    assert translate({"T": "t", "S": "SPY251004C00745000",
                      "t": "2026-10-05T13:30:00Z"}) is None
    # Missing bid/ask on quote
    assert translate({"T": "q", "S": "SPY251004C00745000",
                      "t": "2026-10-05T13:30:00Z"}) is None


def test_subscription_diff():
    wanted = {"O:SPY261019C00770000", "O:QQQ261019P00720000"}
    have = {"O:SPY261019C00770000", "O:AAPL261019C00342500"}
    added, removed = subscription_diff(wanted, have)
    assert added == ["QQQ261019P00720000"]
    assert removed == ["AAPL261019C00342500"]
    assert subscription_diff(wanted, wanted) == ([], [])


class _FakeDB:
    """Minimal Store stand-in for fallback_symbols: tx/get/recent."""

    def __init__(self, quotes=None, bars=None):
        self._quotes = quotes or {}
        self._bars = bars or {}

    @contextmanager
    def tx(self):
        yield object()

    def get(self, c, key, default=None):
        return self._quotes.get(key, default)

    def recent(self, c, kind, symbol, limit=1):
        return self._bars.get(symbol, [])


# Wed 2026-10-21 18:00 UTC = 14:00 ET (market open).
WED_OPEN = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc).timestamp()
# Fri 2026-10-23 21:30 UTC = 17:30 ET (after the close).
FRI_LATE = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc).timestamp()


def test_fallback_symbols_dynamic_ladder():
    db = _FakeDB(quotes={
        "quote:SPY": {"bid": 600.0, "ask": 602.0, "ts": WED_OPEN},
        "quote:QQQ": {"bid": 500.0, "ask": 502.0, "ts": WED_OPEN},
    })
    syms = fallback_symbols(db, now=WED_OPEN)
    assert len(syms) == 20  # 5 strikes x C/P x 2 roots
    # Expiry is today (Wednesday) for both roots.
    assert all("261021" in s for s in syms)
    # SPY spot 601 -> ATM 600; ladder 590..610.
    assert "O:SPY261021C00600000" in syms
    assert "O:SPY261021P00590000" in syms
    assert "O:SPY261021C00610000" in syms
    # QQQ spot 501 -> ATM 500.
    assert "O:QQQ261021C00500000" in syms


def test_fallback_symbols_rolls_past_friday_close():
    db = _FakeDB(quotes={
        "quote:SPY": {"bid": 600.0, "ask": 602.0, "ts": FRI_LATE},
    })
    syms = fallback_symbols(db, now=FRI_LATE)
    assert syms and all("261026" in s for s in syms)  # Monday 2026-10-26


def test_fallback_symbols_bar_spot_and_no_spot():
    db = _FakeDB(bars={"SPY": [{"payload": {"c": 604.0}, "ts": WED_OPEN}]})
    syms = fallback_symbols(db, now=WED_OPEN)
    assert syms and all(s.startswith("O:SPY") for s in syms)  # QQQ skipped
    assert "O:SPY261021C00605000" in syms  # ATM rounds 604 -> 605
    assert fallback_symbols(_FakeDB(), now=WED_OPEN) == []


def test_consume_survives_auth_and_subscribes():
    """Regression for the zero-trades root cause (2026-10-19): the
    authenticate() worker returned the moment auth succeeded, and
    consume() ends on FIRST_COMPLETED — so every successful auth tore
    the stream down before subscriptions could land. The stream
    re-authed every ~15s forever: subscribed=0, zero trades, no error.
    This drives the real consume() over a fake websocket: after auth
    it must send subscribes AND still be running."""
    import asyncio

    import msgpack

    from compass.alpaca_option_stream import consume

    class FakeWS:
        def __init__(self):
            self.sent = []
            self.incoming = asyncio.Queue()

        async def send(self, data):
            self.sent.append(msgpack.unpackb(data, raw=False))

        async def recv(self):
            return await self.incoming.get()

    class FakeDB:
        def health(self, *a, **k):
            pass

    class FakeCollector:
        class cfg:
            alpaca_opra_key = "k"
            alpaca_opra_secret = "s"

        def __init__(self):
            self.option_symbols = {"O:SPY261021C00600000"}
            self.db = FakeDB()

        def quote_batch(self, src, rows):
            pass

        def option_trade_batch(self, rows):
            pass

    async def main():
        ws = FakeWS()
        task = asyncio.create_task(consume(ws, FakeCollector()))
        await ws.incoming.put(
            msgpack.packb([{"T": "success", "msg": "connected"}]))
        await ws.incoming.put(
            msgpack.packb([{"T": "success", "msg": "authenticated"}]))
        for _ in range(100):
            if any(m.get("action") == "subscribe" for m in ws.sent):
                break
            await asyncio.sleep(0.05)
        subs = [m for m in ws.sent if m.get("action") == "subscribe"]
        assert subs, f"no subscribe sent; sent={ws.sent}"
        assert "SPY261021C00600000" in subs[0]["trades"]
        assert not task.done()  # pre-fix: returned right after auth
        await asyncio.sleep(1.0)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def test_consume_survives_persistent_quote_batch_failure():
    """2026-10-21 incident: a quote_batch that always raises (statement
    timeouts against a loaded Postgres) killed the writer task, and
    consume() ending on FIRST_COMPLETED tore the whole stream down into
    a reconnect flap that committed ~zero all morning. The writer must
    now retry, drop the batch, and keep processing later batches."""
    import asyncio

    import msgpack

    from compass.alpaca_option_stream import consume

    class FakeWS:
        def __init__(self):
            self.sent = []
            self.incoming = asyncio.Queue()

        async def send(self, data):
            self.sent.append(msgpack.unpackb(data, raw=False))

        async def recv(self):
            return await self.incoming.get()

    class FakeDB:
        def health(self, *a, **k):
            pass

    class FakeCollector:
        class cfg:
            alpaca_opra_key = "k"
            alpaca_opra_secret = "s"

        def __init__(self):
            self.option_symbols = {"O:SPY261021C00600000"}
            self.db = FakeDB()
            self.quote_calls = 0

        def quote_batch(self, src, rows):
            self.quote_calls += 1
            raise RuntimeError("statement timeout")

        def option_trade_batch(self, rows):
            pass

    async def main():
        ws = FakeWS()
        collector = FakeCollector()
        task = asyncio.create_task(consume(ws, collector))
        await ws.incoming.put(
            msgpack.packb([{"T": "success", "msg": "connected"}]))
        await ws.incoming.put(
            msgpack.packb([{"T": "success", "msg": "authenticated"}]))
        for _ in range(100):
            if any(m.get("action") == "subscribe" for m in ws.sent):
                break
            await asyncio.sleep(0.05)
        assert any(m.get("action") == "subscribe" for m in ws.sent)
        await ws.incoming.put(msgpack.packb(
            [{"T": "q", "S": "SPY261021C00600000", "bp": 2.4, "ap": 2.6,
              "t": "2026-10-21T14:30:00Z"}]))
        # First batch: 3 attempts, all fail, batch dropped, stream alive.
        for _ in range(120):
            if collector.quote_calls >= 3:
                break
            await asyncio.sleep(0.05)
        assert collector.quote_calls >= 3
        assert not task.done()
        # A later batch is still attempted — the queue head did not wedge.
        await ws.incoming.put(msgpack.packb(
            [{"T": "q", "S": "SPY261021C00600000", "bp": 2.5, "ap": 2.7,
              "t": "2026-10-21T14:32:00Z"}]))
        for _ in range(120):
            if collector.quote_calls >= 6:
                break
            await asyncio.sleep(0.05)
        assert collector.quote_calls >= 6
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def test_translate_flows_through_buffer():
    """Translated items must satisfy OptionBuffer.offer's expectations."""
    from compass.option_stream import OptionBuffer
    buf = OptionBuffer()
    trade = translate({"T": "t", "S": "SPY251004C00745000", "p": 2.5,
                       "s": 4, "t": "2026-10-05T13:30:00Z", "i": 1})
    quote = translate({"T": "q", "S": "SPY251004C00745000", "bp": 2.4,
                       "ap": 2.6, "t": "2026-10-05T13:30:00Z"})
    wanted = {"O:SPY251004C00745000"}
    import time
    buf.offer(trade, wanted, time.time(), time.monotonic())
    buf.offer(quote, wanted, time.time(), time.monotonic())
    assert len(buf.trades) == 1
    assert len(buf.quotes) == 1
    # Source tag survives into the stored quote dict.
    sym, q, record = buf.quotes[0]
    assert q["src"] == "alpaca"
    # And on the stored trade item.
    sym, stamp, item = buf.trades[0]
    assert item["src"] == "alpaca"


# --- subscribe-path fix (2026-11-27) ------------------------------------
# Diagnosis (opra-cap-check 2026-11-23): the reconcile sent subscribes
# before unsubscribes (transient cap overshoot -> Alpaca refused whole
# 20-symbol batches with a 405), sent additions alphabetically instead
# of in the selection's priority order, and then marked the whole
# wanted set subscribed locally regardless of refusals. The tests below
# pin the fixed behavior against the real consume().

_FIX_A = "O:AAA261121C00100000"
_FIX_B = "O:BBB261121C00100000"
_FIX_A_WIRE = "AAA261121C00100000"
_FIX_B_WIRE = "BBB261121C00100000"


def test_subscription_diff_ordered_keeps_priority():
    from compass.alpaca_option_stream import (
        subscription_diff_ordered, wanted_in_priority_order)
    added, removed = subscription_diff_ordered(
        ["O:MMM261121C00100000", "O:AAA261121C00100000",
         "O:ZZZ261121C00100000"],
        {"O:AAA261121C00100000", "O:QQQ261121C00100000"})
    # Priority order (MMM before ZZZ), NOT alphabetical (ZZZ last here
    # only because it is last in priority; MMM precedes it despite
    # sorting after AAA).
    assert added == ["MMM261121C00100000", "ZZZ261121C00100000"]
    assert removed == ["QQQ261121C00100000"]

    class WithOrder:
        option_symbols = {"O:B", "O:A", "O:C"}
        option_symbols_ordered = ["O:C", "O:A"]

    # Ordered symbols first in their order; the rest sorted after.
    assert wanted_in_priority_order(WithOrder()) == ["O:C", "O:A", "O:B"]

    class Bare:
        option_symbols = {"O:B", "O:A"}

    # No ordered list: plain sorted order (the pre-fix behavior).
    assert wanted_in_priority_order(Bare()) == ["O:A", "O:B"]


class _FixDB:
    def __init__(self):
        self.health_calls = []

    def health(self, *a, **k):
        self.health_calls.append((a, k))


class _FixWS:
    """Fake Alpaca WS; can refuse subscribes with a 405 error message,
    exactly as Alpaca does when a subscribe would exceed the cap."""

    def __init__(self):
        import asyncio
        self.sent = []
        self.incoming = asyncio.Queue()
        self.refusals_left = 0

    async def send(self, data):
        import msgpack
        msg = msgpack.unpackb(data, raw=False)
        self.sent.append(msg)
        if msg.get("action") == "subscribe" and self.refusals_left > 0:
            self.refusals_left -= 1
            self.incoming.put_nowait(msgpack.packb(
                [{"T": "error", "code": 405,
                  "msg": "symbol limit exceeded"}]))

    async def recv(self):
        return await self.incoming.get()


class _FixCollector:
    class cfg:
        alpaca_opra_key = "k"
        alpaca_opra_secret = "s"

    def __init__(self, symbols, ordered=None):
        self.option_symbols = set(symbols)
        if ordered is not None:
            self.option_symbols_ordered = list(ordered)
        self.db = _FixDB()
        self.quote_calls = 0

    def quote_batch(self, src, rows):
        self.quote_calls += 1

    def option_trade_batch(self, rows):
        pass


async def _fix_start(ws, collector):
    import asyncio

    import msgpack
    from compass.alpaca_option_stream import consume
    task = asyncio.create_task(consume(ws, collector))
    await ws.incoming.put(msgpack.packb([{"T": "success", "msg": "connected"}]))
    await ws.incoming.put(
        msgpack.packb([{"T": "success", "msg": "authenticated"}]))
    return task


async def _fix_wait(predicate, timeout=8.0):
    import asyncio
    waited = 0.0
    while not predicate():
        if waited >= timeout:
            raise AssertionError("timed out waiting for stream condition")
        await asyncio.sleep(0.05)
        waited += 0.05


async def _fix_stop(task):
    import asyncio
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _subscribes_for(ws, wire):
    return [m for m in ws.sent
            if m.get("action") == "subscribe" and wire in m.get("trades", [])]


def test_reconcile_sends_removals_before_additions(monkeypatch):
    import asyncio

    import compass.alpaca_option_stream as aos
    monkeypatch.setattr(aos, "REFUSAL_SETTLE_SECONDS", 0.02)

    async def main():
        ws = _FixWS()
        collector = _FixCollector({_FIX_A})
        task = await _fix_start(ws, collector)
        try:
            await _fix_wait(lambda: _subscribes_for(ws, _FIX_A_WIRE))
            marker = len(ws.sent)
            collector.option_symbols = {_FIX_B}
            collector.option_symbols_ordered = [_FIX_B]
            await _fix_wait(lambda: _subscribes_for(ws, _FIX_B_WIRE))
            later = ws.sent[marker:]
            actions = [m["action"] for m in later]
            # The removal of A is the FIRST wire message of the
            # reconcile and precedes the subscribe for B. Pre-fix the
            # subscribe went first (the cap-overshoot order).
            assert actions[0] == "unsubscribe"
            assert _FIX_A_WIRE in later[0]["trades"]
            assert actions.index("unsubscribe") < actions.index("subscribe")
        finally:
            await _fix_stop(task)

    asyncio.run(main())


def test_additions_follow_priority_order(monkeypatch):
    import asyncio

    import compass.alpaca_option_stream as aos
    monkeypatch.setattr(aos, "REFUSAL_SETTLE_SECONDS", 0.02)

    priority = ["O:MMM261121C00100000", "O:AAA261121C00100000",
                "O:ZZZ261121C00100000"]

    async def main():
        ws = _FixWS()
        collector = _FixCollector(set(priority), ordered=priority)
        task = await _fix_start(ws, collector)
        try:
            await _fix_wait(lambda: any(
                m.get("action") == "subscribe" for m in ws.sent))
            subs = [m for m in ws.sent if m.get("action") == "subscribe"]
            # One batch, in selection priority order — alphabetical
            # would be AAA, MMM, ZZZ.
            assert subs[0]["trades"] == [
                "MMM261121C00100000", "AAA261121C00100000",
                "ZZZ261121C00100000"]
            assert subs[0]["quotes"] == subs[0]["trades"]
        finally:
            await _fix_stop(task)

    asyncio.run(main())


def test_refused_batch_not_subscribed_and_retried(monkeypatch):
    import asyncio

    import msgpack

    import compass.alpaca_option_stream as aos
    monkeypatch.setattr(aos, "REFUSAL_SETTLE_SECONDS", 0.02)
    monkeypatch.setattr(aos, "RETRY_BACKOFF_BASE_SECONDS", 0.0)

    async def main():
        ws = _FixWS()
        ws.refusals_left = 10 ** 9  # refuse every subscribe for now
        collector = _FixCollector({_FIX_A})
        task = await _fix_start(ws, collector)
        try:
            await _fix_wait(lambda: _subscribes_for(ws, _FIX_A_WIRE))
            # Re-emission on a later reconcile proves the refused batch
            # was NOT marked subscribed locally (pre-fix it was marked
            # unconditionally and never re-sent).
            await _fix_wait(
                lambda: len(_subscribes_for(ws, _FIX_A_WIRE)) >= 2)
            # While un-subscribed locally, the buffer gate drops the
            # symbol's quotes: nothing reaches persistence.
            await ws.incoming.put(msgpack.packb(
                [{"T": "q", "S": _FIX_A_WIRE, "bp": 2.4, "ap": 2.6,
                  "t": "2026-11-21T14:30:00Z"}]))
            await asyncio.sleep(0.6)
            assert collector.quote_calls == 0
            # Refusals stop: the queued retry lands and confirms.
            ws.refusals_left = 0
            sent_before = len(_subscribes_for(ws, _FIX_A_WIRE))
            await _fix_wait(
                lambda: len(_subscribes_for(ws, _FIX_A_WIRE)) > sent_before)
            await asyncio.sleep(0.4)
            await ws.incoming.put(msgpack.packb(
                [{"T": "q", "S": _FIX_A_WIRE, "bp": 2.5, "ap": 2.7,
                  "t": "2026-11-21T14:31:00Z"}]))
            await _fix_wait(lambda: collector.quote_calls >= 1)
        finally:
            await _fix_stop(task)

    asyncio.run(main())


def test_successful_reconcile_marks_subscribed_no_resend(monkeypatch):
    import asyncio

    import msgpack

    import compass.alpaca_option_stream as aos
    monkeypatch.setattr(aos, "REFUSAL_SETTLE_SECONDS", 0.02)

    async def main():
        ws = _FixWS()
        collector = _FixCollector({_FIX_A, _FIX_B})
        task = await _fix_start(ws, collector)
        try:
            await _fix_wait(lambda: _subscribes_for(ws, _FIX_A_WIRE)
                            and _subscribes_for(ws, _FIX_B_WIRE))
            await asyncio.sleep(0.4)  # let the reconcile finish marking
            marker = len(ws.sent)
            await asyncio.sleep(1.6)  # ~3 reconcile cycles
            # Everything marked subscribed: no re-sends, no removals.
            assert [m for m in ws.sent[marker:]
                    if m.get("action") in ("subscribe", "unsubscribe")] == []
            # And the local set gates quotes IN for both symbols.
            await ws.incoming.put(msgpack.packb(
                [{"T": "q", "S": _FIX_A_WIRE, "bp": 2.4, "ap": 2.6,
                  "t": "2026-11-21T14:30:00Z"}]))
            await _fix_wait(lambda: collector.quote_calls >= 1)
        finally:
            await _fix_stop(task)

    asyncio.run(main())


# --- providers.py connection gate (2026-11-22 outage) -----------------------
# Wed 2026-11-18 a collector deploy restarted the process at 18:20:57Z with
# an empty option_symbols selection; alpaca_option_connection() waited for a
# non-empty selection with no bound, so the websocket was never opened and
# the stream's own SPY/QQQ fallback ladder (inside consume()) was
# unreachable. The wait is now bounded (~30s); these tests pin both paths.

class _HealthDB:
    def __init__(self):
        self.calls = []

    def health(self, name, status, detail):
        self.calls.append((name, status, detail))


def _run_gate(monkeypatch, symbols):
    import asyncio
    import compass.alpaca_option_stream as aos
    import compass.providers as providers
    from contextlib import asynccontextmanager

    consumed = []

    async def fake_consume(ws, collector):
        consumed.append(True)

    @asynccontextmanager
    async def fake_connect(*args, **kwargs):
        yield object()

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(aos, "consume", fake_consume)
    monkeypatch.setattr(providers.websockets, "connect", fake_connect)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    collectors = providers.Collectors.__new__(providers.Collectors)
    collectors.option_symbols = set(symbols)
    collectors.db = _HealthDB()
    asyncio.run(collectors.alpaca_option_connection())
    return collectors.db.calls, consumed, sleeps


def test_gate_connects_anyway_when_selection_stays_empty(monkeypatch):
    calls, consumed, sleeps = _run_gate(monkeypatch, set())
    assert consumed == [True]
    waiting = [c for c in calls if c[1] == "waiting"]
    connecting = [c for c in calls if c[1] == "connecting"]
    # Bounded wait: at most ~30s of 5s waiting cycles, then it connects.
    assert 0 < len(waiting) <= 6
    assert sum(sleeps) <= 30
    assert len(connecting) == 1


def test_gate_connects_immediately_when_selection_populated(monkeypatch):
    calls, consumed, sleeps = _run_gate(monkeypatch, {"O:SPY251204C00745000"})
    assert consumed == [True]
    assert [c for c in calls if c[1] == "waiting"] == []
    assert sleeps == []
