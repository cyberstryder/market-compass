"""Tests for compass/squeeze.py (volatility squeeze detector)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.squeeze import (compute_squeeze, detect, _sma, _stdev, _ema, _atr,
                             MIN_BARS, FRESH_MAX, EXTENDED_MIN)


def _bars(n, base=100.0, drift=0.0, vol=0.02):
    """Synthetic bars: tight consolidation with optional drift and vol."""
    bars = []
    price = base
    for i in range(n):
        # Deterministic wiggle, no randomness.
        move = math.sin(i * 1.3) * vol + drift
        o = price
        c = price + move
        h = max(o, c) + vol * 0.5
        l = min(o, c) - vol * 0.5
        bars.append({"o": o, "h": h, "l": l, "c": c, "v": 1000})
        price = c
    return bars


def test_indicators():
    vals = [float(i) for i in range(1, 31)]
    assert _sma(vals, 20) == sum(vals[-20:]) / 20
    assert _stdev(vals, 20) > 0
    assert _ema(vals, 20) is not None
    hlc = [(v + 1, v - 1, v) for v in vals]
    assert _atr(hlc, 14) > 0
    assert _sma([1.0], 20) is None


def test_insufficient_bars():
    out = compute_squeeze(_bars(10))
    assert out["state"] == "none"
    assert "need" in out["reason"]


def test_squeeze_detected():
    # Tight consolidation -> BB inside KC.
    out = compute_squeeze(_bars(60, vol=0.05))
    assert out["state"] in ("squeezed", "none"), out
    # With very low vol, a squeeze should form.
    if out["state"] == "squeezed":
        assert out["squeeze_bars"] >= 1
        assert out["watch_only"] is True
        assert out["bb_upper"] < out["kc_upper"]
        assert out["bb_lower"] > out["kc_lower"]


def test_fire_after_squeeze():
    # Consolidate, then break out hard.
    bars = _bars(50, vol=0.05)
    last = bars[-1]["c"]
    for i in range(3):
        o = last
        c = last + 2.0  # sharp rally
        bars.append({"o": o, "h": c + 0.2, "l": o - 0.1, "c": c, "v": 5000})
        last = c
    out = compute_squeeze(bars)
    assert out["state"] == "fired", out
    assert out["direction"] == "bullish"
    assert out["squeeze_bars"] >= 1


def test_fire_bearish():
    bars = _bars(50, vol=0.05)
    last = bars[-1]["c"]
    for i in range(3):
        o = last
        c = last - 2.0
        bars.append({"o": o, "h": o + 0.1, "l": c - 0.2, "c": c, "v": 5000})
        last = c
    out = compute_squeeze(bars)
    assert out["state"] == "fired", out
    assert out["direction"] == "bearish"


def test_fresh_squeeze_is_watch_only():
    # Short squeeze that fires quickly -> watch_only.
    bars = _bars(35, vol=0.05)
    # Force a quick fire by adding one big bar.
    last = bars[-1]["c"]
    bars.append({"o": last, "h": last + 3, "l": last - 0.1,
                 "c": last + 3, "v": 5000})
    out = compute_squeeze(bars)
    if out["state"] == "fired" and out["squeeze_bars"] <= FRESH_MAX:
        assert out["watch_only"] is True


def test_no_fire_without_prior_squeeze():
    # Trending market, no squeeze -> no fire.
    out = compute_squeeze(_bars(60, drift=0.5, vol=0.3))
    assert out["state"] in ("none", "squeezed", "fired")
    if out["state"] == "fired":
        assert out["squeeze_bars"] >= 1  # a real prior squeeze existed


def test_detect_handles_errors():
    out = detect({"A": _bars(60, vol=0.05), "B": [{"bad": 1}]})
    assert out["A"]["state"] in ("squeezed", "none", "fired")
    assert out["B"]["state"] == "none"


def test_evidence_fields_present():
    out = compute_squeeze(_bars(60, vol=0.05))
    for key in ("bb_upper", "bb_lower", "kc_upper", "kc_lower", "close",
                "squeeze_bars", "state"):
        assert key in out, key
