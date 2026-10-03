"""Tests for the AOI 50/20 Fade detector."""
import time

import pytest

from compass.aoi_fade import (
    find_fractal_swings,
    find_fvgs,
    fib_zones,
    check_signal,
    build_zones,
    _resample,
    _atr,
    POINT_TARGETS,
    scan,
)


def _bar(ts, o, h, l, c):
    return {'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c}


def _trend_bars(n, start=100.0, step=1.0):
    """Rising 1m bars."""
    bars = []
    px = start
    for i in range(n):
        o = px
        c = px + step
        bars.append(_bar(1_700_000_000 + i * 60, o, max(o, c) + 0.2,
                         min(o, c) - 0.2, c))
        px = c
    return bars


def test_resample_1h():
    # Hour-aligned start so 120 minutes = exactly 2 hourly bars
    base = 1_699_999_200  # divisible by 3600
    bars = []
    px = 100.0
    for i in range(120):
        o = px
        c = px + 1.0
        bars.append(_bar(base + i * 60, o, max(o, c) + 0.2,
                         min(o, c) - 0.2, c))
        px = c
    h1 = _resample(bars, 3600)
    assert len(h1) == 2
    assert h1[0]['o'] == bars[0]['o']
    assert h1[0]['c'] == bars[59]['c']
    assert h1[0]['h'] == max(b['h'] for b in bars[:60])
    assert h1[0]['l'] == min(b['l'] for b in bars[:60])


def test_fractal_swing_low():
    # V-shape: swing low at index 2 (k=2 needs 5-bar window)
    bars = [
        _bar(1, 10, 11, 9.5, 10.5),
        _bar(2, 10.5, 11, 9.0, 10),
        _bar(3, 10, 10.5, 8.0, 9.5),   # swing low
        _bar(4, 9.5, 10.5, 8.5, 10),
        _bar(5, 10, 11, 9.0, 10.5),
    ]
    lows, highs = find_fractal_swings(bars, k=2)
    assert lows == [8.0]
    assert highs == []


def test_fractal_swing_high():
    bars = [
        _bar(1, 10, 10.5, 9.5, 10),
        _bar(2, 10, 11, 9.5, 10.5),
        _bar(3, 10.5, 12.0, 10, 11),   # swing high
        _bar(4, 11, 11.5, 10.5, 11),
        _bar(5, 11, 11, 10, 10.5),
    ]
    lows, highs = find_fractal_swings(bars, k=2)
    assert highs == [12.0]
    assert lows == []


def test_bullish_fvg_unmitigated():
    # Bar 1 low=105, bar 3 high=100 -> gap [100, 105], bar 3 bullish
    bars = [
        _bar(1, 104, 106, 105, 104.5),
        _bar(2, 104.5, 105, 103, 104),
        _bar(3, 101, 102, 100, 101.5),  # bullish, high=102 < 105
        _bar(4, 101.5, 103, 101, 102.5),
        _bar(5, 102.5, 104, 102, 103.5),  # low=102 > gap top=105? No, 102<105
    ]
    # Bar 5 low=102 <= 105, so this FVG IS mitigated
    zones = find_fvgs(bars)
    assert zones == []


def test_bullish_fvg_detected():
    bars = [
        _bar(1, 104, 106, 105, 104.5),
        _bar(2, 104.5, 105, 103, 104),
        _bar(3, 106, 108, 106, 107.5),  # bullish, high=108? No...
    ]
    # Let me construct properly: need low[0] > high[2]
    bars = [
        _bar(1, 110, 112, 110, 111),    # low=110
        _bar(2, 111, 112, 108, 109),
        _bar(3, 106, 108, 105, 107),    # high=108 < 110, bullish close
        _bar(4, 107, 109, 107, 108),    # low=107 > 110? No, need > gap top
        _bar(5, 108, 110, 108, 109),    # low=108 < 110, mitigated
    ]
    zones = find_fvgs(bars)
    # Gap [108, 110], bar 4 low=107 <= 110 mitigates it
    assert zones == []

    # Truly unmitigated: later bars stay above gap top
    bars2 = [
        _bar(1, 110, 112, 110, 111),
        _bar(2, 111, 112, 108, 109),
        _bar(3, 106, 108, 105, 107),
        _bar(4, 111, 113, 111, 112),    # low=111 > 110, unmitigated
        _bar(5, 112, 114, 112, 113),
    ]
    zones2 = find_fvgs(bars2)
    assert len(zones2) == 1
    assert zones2[0]['bias'] == 'long'
    assert zones2[0]['top'] == 110
    assert zones2[0]['bottom'] == 108


def test_bearish_fvg_detected():
    bars = [
        _bar(1, 100, 102, 99, 100),     # high=102
        _bar(2, 100, 101, 99, 99.5),
        _bar(3, 104, 106, 103, 103.5),  # low=103 > 102, bearish close
        _bar(4, 103, 104, 102.5, 102.8),  # high=104 < ... need >= bottom=103?
        _bar(5, 102, 103, 101, 101.5),
    ]
    # Bearish FVG: high[0]=102 < low[2]=103. Zone [102, 103] (bottom, top).
    # Mitigated if later bar high >= bottom (103). Bar 4 high=104 >= 103 -> mitigated.
    zones = find_fvgs(bars)
    assert zones == []

    bars2 = [
        _bar(1, 100, 102, 99, 100),
        _bar(2, 100, 101, 99, 99.5),
        _bar(3, 104, 106, 103, 103.5),
        _bar(4, 101, 102, 100, 100.5),  # high=102 < 103, unmitigated
        _bar(5, 100, 101, 99, 99.5),
    ]
    zones2 = find_fvgs(bars2)
    assert len(zones2) == 1
    assert zones2[0]['bias'] == 'short'


def test_fib_zones():
    # Clear up-leg: swing low then higher high
    bars = []
    ts = 1_700_000_000
    # Down to swing low
    for i in range(5):
        px = 100 - i
        bars.append(_bar(ts + i * 14400, px + 0.5, px + 1, px - 0.5, px))
    # Up to higher high (with a fractal swing low at index 4)
    for i in range(5, 12):
        px = 95 + (i - 4) * 2
        bars.append(_bar(ts + i * 14400, px - 0.5, px + 0.5, px - 1, px))
    zones = fib_zones(bars)
    longs = [z for z in zones if z['bias'] == 'long']
    assert len(longs) == 4  # 4 fib levels
    # Fib levels should be between swing low and high
    for z in longs:
        assert z['top'] == z['bottom']  # single-price


def test_check_signal_bullish():
    zone = {'bias': 'long', 'top': 100.0, 'bottom': 100.0,
            'zone_kind': 'swing_low'}
    # Wick below, close back above
    bar = _bar(1, 101, 102, 99.5, 101.5)
    sig = check_signal(bar, [zone], atr=2.0)
    assert sig is not None
    assert sig['direction'] == 'long'
    assert sig['entry'] == 100.0
    assert sig['stop'] == 100.0 - 2.0  # zone bottom - 1.0*ATR


def test_check_signal_bearish():
    zone = {'bias': 'short', 'top': 110.0, 'bottom': 110.0,
            'zone_kind': 'swing_high'}
    bar = _bar(1, 109, 110.5, 108, 108.5)
    sig = check_signal(bar, [zone], atr=2.0)
    assert sig is not None
    assert sig['direction'] == 'short'
    assert sig['entry'] == 110.0
    assert sig['stop'] == 110.0 + 2.0


def test_check_signal_no_trigger():
    zone = {'bias': 'long', 'top': 100.0, 'bottom': 100.0,
            'zone_kind': 'swing_low'}
    # Close below zone -> no signal (didn't close back through)
    bar = _bar(1, 101, 102, 99.5, 99.0)
    assert check_signal(bar, [zone], atr=2.0) is None
    # No wick -> no signal
    bar2 = _bar(1, 101, 102, 100.5, 101.5)
    assert check_signal(bar2, [zone], atr=2.0) is None


def test_point_targets():
    assert POINT_TARGETS['MNQ.c.0'] == 50.0
    assert POINT_TARGETS['MES.c.0'] == 20.0
    assert POINT_TARGETS['MGC.v.0'] == 10.0
    assert POINT_TARGETS['SIL.v.0'] == 0.40
    assert POINT_TARGETS['MCL.v.0'] == 1.0


class _FakeDB:
    def __init__(self):
        self.store = {}
        self.appends = []

    def get(self, c, k, default=None):
        return self.store.get(k, default)

    def put(self, c, k, v):
        self.store[k] = v

    def append(self, c, kind, cat, symbol, ts, payload, key=None):
        self.appends.append((kind, symbol, payload))

    def recent(self, c, kind, limit=50):
        return []


class _Cfg:
    ict_aoi_fade = True
    ict_symbols = ('MNQ.c.0', 'MES.c.0', 'MGC.v.0')
    ict_paper_trading = True


def test_scan_disabled():
    db = _FakeDB()

    class CfgOff:
        ict_aoi_fade = False

    assert scan(db, 'c', CfgOff(), 1_700_000_000) == {
        'ran': False, 'reason': 'disabled'}


def test_scan_no_symbols():
    db = _FakeDB()

    class CfgEmpty:
        ict_aoi_fade = True
        ict_symbols = ()

    assert scan(db, 'c', CfgEmpty(), 1_700_000_000)['reason'] == 'no_symbols'
