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
