"""Tests for compass/alpaca_option_stream.py (Alpaca OPRA translation)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.alpaca_option_stream import (
    translate, to_alpaca_symbol, to_internal_symbol)


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
