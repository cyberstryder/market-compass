from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import trendline_liquidity as tl

NOW = 1700000000.0
SYM = 'NQ.c.0'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'tl.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def zigzag_lows(anchors):
    """Linear interpolation between (idx, low) anchors."""
    last_idx = anchors[-1][0]
    lows = [0.0] * (last_idx + 1)
    for (i0, l0), (i1, l1) in zip(anchors, anchors[1:]):
        for i in range(i0, i1 + 1):
            lows[i] = l0 + (l1 - l0) * (i - i0) / (i1 - i0)
    return lows


def base_lows():
    """58 bars (idx 0..57): three rising swing lows at (5,98),(15,99),(25,100),
    then a strictly rising tail so no further fractals form."""
    lows = zigzag_lows([(0, 104.0), (5, 98.0), (10, 103.0), (15, 99.0),
                        (20, 104.0), (25, 100.0), (30, 102.2)])
    for i in range(31, 58):
        lows.append(102.2 + (105.0 - 102.2) * (i - 30) / 27)
    return lows


def bars_from_lows(start_ts, lows, rng=0.4):
    """Window rows [ts,o,h,l,c,v]; o = prior close, h = l+rng."""
    rows = []
    prev_c = None
    for i, lo in enumerate(lows):
        ts = start_ts + i * 60
        hi = lo + rng
        cl = lo + rng * 0.5
        op = prev_c if prev_c is not None else cl
        rows.append([ts, op, hi, lo, cl, 1000])
        prev_c = cl
    return rows


def watch_rows():
    """60 bars: rising trendline, 4th touch dips exactly to the line."""
    lows = base_lows()
    lows.append(103.3)   # idx 58: low exactly on line(58) = 98 + 0.1*53
    lows.append(103.5)   # idx 59: stays above
    return bars_from_lows(0, lows)


def signal_rows():
    """60 bars: bar 58 pokes below the line and closes back above it."""
    rows = bars_from_lows(0, base_lows())
    ts58 = 58 * 60
    rows.append([ts58, 105.0, 105.1, 103.11, 103.6, 1000])      # poke + reclaim
    rows.append([ts58 + 60, 103.6, 104.0, 103.4, 103.8, 1000])  # follow-through
    return rows


def no_reclaim_rows():
    """60 bars: bar 58 pokes below the line but never closes back above."""
    rows = bars_from_lows(0, base_lows())
    ts58 = 58 * 60
    rows.append([ts58, 105.0, 105.1, 103.11, 103.0, 1000])
    rows.append([ts58 + 60, 103.0, 103.2, 102.8, 102.9, 1000])
    return rows


def cfg(**kw):
    base = {'ict_trendline_liquidity': True, 'ict_symbols': (SYM,)}
    base.update(kw)
    return SimpleNamespace(**base)


# --- find_swings ---

def test_find_swings_fractal_highs_and_lows():
    lows = [12.0, 11.0, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0,
            11.0, 9.0, 11.0, 12.0]
    bars = tl._bars_from_window(bars_from_lows(0, lows))
    sw = tl.find_swings(bars, 2)
    assert [i for i, _, _ in sw['lows']] == [2, 10]
    assert [i for i, _, _ in sw['highs']] == [8]
    assert sw['lows'][0][2] == pytest.approx(10.0)


def test_find_swings_ignores_flat_series():
    bars = tl._bars_from_window(bars_from_lows(0, [10.0] * 10))
    sw = tl.find_swings(bars, 2)
    assert sw == {'highs': [], 'lows': []}


# --- fit_line ---

def test_fit_line_through_first_last():
    line = tl.fit_line([(2, 1000.0, 98.0), (15, 2000.0, 99.0), (25, 3000.0, 100.0)])
    assert line['slope'] == pytest.approx((100.0 - 98.0) / (25 - 2))
    assert tl._line_at(line, 25) == pytest.approx(100.0)
    assert tl._line_at(line, 2) == pytest.approx(98.0)


def test_fit_line_needs_two_swings():
    assert tl.fit_line([(1, 0.0, 5.0)]) is None
    assert tl.fit_line([]) is None


# --- analyze_symbol ---

def test_rising_trendline_third_touch_is_watch():
    bars = tl._bars_from_window(watch_rows())
    watch, signals = tl.analyze_symbol(SYM, bars, NOW, {}, None, None)
    assert signals == []
    assert len(watch) == 1
    w = watch[0]
    assert w['state'] == 'watch' and w['direction'] == 'long'
    assert w['touches'] >= 3
    assert w['level_kind'] == 'trendline'
    assert w['line_slope'] == pytest.approx(0.1)
    assert w['entry'] is None and w['stop'] is None


def test_sweep_and_reclaim_emits_long_signal():
    bars = tl._bars_from_window(signal_rows())
    watch, signals = tl.analyze_symbol(SYM, bars, NOW, {}, None, None)
    assert watch == []  # signal supersedes the watch on the rising line
    assert len(signals) == 1
    s = signals[0]
    assert s['state'] == 'signal' and s['direction'] == 'long'
    assert s['concept'] == 'trendline'
    assert s['entry'] == pytest.approx(103.6)
    assert s['stop'] == pytest.approx(103.11 - s['params']['sweep_ticks'])
    assert s['stop'] < 103.11
    assert s['target'] == pytest.approx(103.6 + 2 * (103.6 - s['stop']))
    assert s['rr'] == pytest.approx(2.0)
    assert s['touches'] >= 3
    assert s['level_price'] == pytest.approx(103.3)


def test_sweep_without_reclaim_gives_no_signal():
    bars = tl._bars_from_window(no_reclaim_rows())
    watch, signals = tl.analyze_symbol(SYM, bars, NOW, {}, None, None)
    assert signals == []
    assert len(watch) == 1 and watch[0]['state'] == 'watch'


def test_analyze_needs_atr():
    bars = tl._bars_from_window(bars_from_lows(0, [100.0] * 5))
    watch, signals = tl.analyze_symbol(SYM, bars, NOW, {}, None, None)
    assert watch == [] and signals == []


# --- scan ---

def test_scan_stores_watch_snapshot_and_throttles(db):
    with db.tx() as c:
        db.put(c, 'bar_window:' + SYM, watch_rows())
        out = tl.scan(db, c, cfg(), NOW)
    assert out['ran'] is True and out['symbols'] == 1
    with db.tx() as c:
        latest = db.get(c, 'ict_trendline_liquidity:latest')
        assert latest['rows'][SYM][0]['state'] == 'watch'
        assert latest['rows'][SYM][0]['direction'] == 'long'
        # Signal appends use the same idempotency path even with no signal.
        assert tl.scan(db, c, cfg(), NOW + 10) == {'ran': False, 'reason': 'throttled'}


def test_scan_logs_signal_idempotently(db):
    with db.tx() as c:
        db.put(c, 'bar_window:' + SYM, signal_rows())
        assert tl.scan(db, c, cfg(), NOW)['ran'] is True
        db.put(c, 'ict_trendline_liquidity:scanned_at', 0)
        assert tl.scan(db, c, cfg(), NOW + 200)['ran'] is True
        sigs = db.recent(c, 'ict_trendline_liquidity_signal', limit=10)
    assert len(sigs) == 1
    assert sigs[0]['payload']['direction'] == 'long'
    assert sigs[0]['payload']['rr'] == pytest.approx(2.0)


def test_scan_fewer_than_60_bars_is_no_bars(db):
    with db.tx() as c:
        db.put(c, 'bar_window:' + SYM, bars_from_lows(0, [100.0 + i * 0.1 for i in range(10)]))
        assert tl.scan(db, c, cfg(), NOW) == {'ran': False, 'reason': 'no_bars'}


def test_scan_disabled_flag(db):
    with db.tx() as c:
        assert tl.scan(db, c, cfg(ict_trendline_liquidity=False), NOW) == {'ran': False, 'reason': 'disabled'}
