from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import turtle_soup as ts

OPEN = 1_700_000_000  # arbitrary fixed ts
SYM = 'NQ.c.0'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'turtle.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def row(ts, o, h, l, c):
    return [ts, o, h, l, c, 1000]


def flat_pad(start_ts, n, price=101.0, wiggle=0.1):
    """ATR-capable padding: >= 15 bars so ATR(14) is computable."""
    return [row(start_ts + i * 60, price, price + wiggle, price - wiggle, price)
            for i in range(n)]


def sweep_long_rows(start_ts, level=100.0):
    """Sweep of the 100 low (wick to 99.5), reclaim close 100.2 two bars later."""
    return [
        row(start_ts + 0 * 60, 101.0, 101.2, 100.8, 101.0),
        row(start_ts + 1 * 60, 101.0, 101.1, 100.6, 100.7),
        row(start_ts + 2 * 60, 100.7, 100.8, 99.5, 99.9),   # sweep
        row(start_ts + 3 * 60, 99.9, 100.3, 99.8, 100.2),   # reclaim
        row(start_ts + 4 * 60, 100.2, 100.6, 100.1, 100.5),
    ]


def liq_snapshot(levels):
    return {'at': OPEN, 'levels': levels}


def _seed(db, c, bars, levels):
    db.put(c, 'bar_window:' + SYM, bars)
    db.put(c, 'ict_session_liquidity:latest', liq_snapshot(levels))


def _cfg(**kw):
    base = {'ict_turtle_soup': True, 'ict_symbols': (SYM,)}
    base.update(kw)
    return SimpleNamespace(**base)


# --- detect_sweep: long ---

def test_detect_long_sweep_and_reclaim():
    bars = ts._bars_from_window(sweep_long_rows(OPEN))
    sig = ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25,
                          opposing=[105.0])
    assert sig is not None
    assert sig['direction'] == 'long'
    assert sig['sweep_ts'] == OPEN + 2 * 60
    assert sig['signal_ts'] == OPEN + 3 * 60
    assert sig['entry'] == pytest.approx(100.2)
    assert sig['stop'] == pytest.approx(99.5 - 0.25)
    assert sig['target'] == pytest.approx(105.0)
    assert sig['target_kind'] == 'session_extreme'
    assert sig['rr'] == pytest.approx(4.8 / 0.95)
    assert sig['level_price'] == 100.0


def test_detect_short_sweep_and_reclaim():
    bars = ts._bars_from_window([
        row(OPEN + 0 * 60, 99.0, 99.2, 98.8, 99.0),
        row(OPEN + 1 * 60, 99.0, 99.4, 98.9, 99.3),
        row(OPEN + 2 * 60, 99.3, 100.6, 99.2, 99.8),   # sweep of 100 high
        row(OPEN + 3 * 60, 99.8, 99.9, 99.5, 99.6),    # reclaim close < 100
        row(OPEN + 4 * 60, 99.6, 99.7, 99.0, 99.2),
    ])
    sig = ts.detect_sweep(bars, 100.0, 'short', sweep_ticks=0.25,
                          opposing=[95.0])
    assert sig is not None
    assert sig['direction'] == 'short'
    # Sweep bar closes back below the level itself -> same-bar confirm.
    assert sig['sweep_ts'] == sig['signal_ts'] == OPEN + 2 * 60
    assert sig['entry'] == pytest.approx(99.8)
    assert sig['stop'] == pytest.approx(100.6 + 0.25)
    assert sig['target'] == pytest.approx(95.0)
    assert sig['rr'] >= 1.5


def test_sweep_without_reclaim_is_none():
    bars = ts._bars_from_window([
        row(OPEN + 0 * 60, 101.0, 101.2, 100.8, 101.0),
        row(OPEN + 1 * 60, 101.0, 101.1, 99.5, 99.8),   # sweep
        row(OPEN + 2 * 60, 99.8, 99.9, 99.4, 99.6),     # stays below level
        row(OPEN + 3 * 60, 99.6, 99.9, 99.3, 99.5),
        row(OPEN + 4 * 60, 99.5, 99.8, 99.2, 99.4),
    ])
    assert ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25) is None


def test_reclaim_after_confirm_window_is_none():
    bars = ts._bars_from_window([
        row(OPEN + 0 * 60, 101.0, 101.2, 100.8, 101.0),
        row(OPEN + 1 * 60, 101.0, 101.1, 99.5, 99.7),   # sweep at i=1
        row(OPEN + 2 * 60, 99.7, 99.9, 99.8, 99.6),
        row(OPEN + 3 * 60, 99.6, 99.9, 99.8, 99.5),
        row(OPEN + 4 * 60, 99.5, 99.9, 99.8, 99.4),
        row(OPEN + 5 * 60, 99.4, 100.4, 99.8, 100.2),   # reclaim 4 bars later
    ])
    assert ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25,
                           confirm_bars=3) is None


def test_same_bar_wick_and_reclaim_confirms():
    bars = ts._bars_from_window([
        row(OPEN + 0 * 60, 101.0, 101.2, 100.8, 101.0),
        row(OPEN + 1 * 60, 100.8, 101.0, 99.5, 100.3),  # wick below, close above
    ])
    sig = ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25,
                          opposing=[105.0])
    assert sig is not None
    assert sig['sweep_ts'] == sig['signal_ts'] == OPEN + 60
    assert sig['confirm_lag_bars'] == 0


def test_rr_filter_rejects_close_target():
    bars = ts._bars_from_window(sweep_long_rows(OPEN))
    # Opposing extreme too close: rr = 0.4/0.95 < 1.5 -> no signal.
    sig = ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25,
                          opposing=[100.6], min_rr=1.5)
    assert sig is None


def test_two_r_fallback_when_no_opposing_extreme():
    bars = ts._bars_from_window(sweep_long_rows(OPEN))
    sig = ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25,
                          opposing=[], min_rr=1.5)
    assert sig is not None
    assert sig['target_kind'] == 'two_r'
    assert sig['target'] == pytest.approx(100.2 + 2 * 0.95)
    assert sig['rr'] == pytest.approx(2.0)


def test_most_recent_sweep_wins():
    bars = ts._bars_from_window(
        sweep_long_rows(OPEN) + [
            row(OPEN + 5 * 60, 100.5, 100.7, 100.3, 100.6),
            row(OPEN + 6 * 60, 100.6, 100.8, 99.4, 99.8),   # second sweep
            row(OPEN + 7 * 60, 99.8, 100.4, 99.8, 100.3),   # second reclaim
        ])
    sig = ts.detect_sweep(bars, 100.0, 'long', sweep_ticks=0.25,
                          opposing=[105.0])
    assert sig['sweep_ts'] == OPEN + 6 * 60
    assert sig['signal_ts'] == OPEN + 7 * 60


# --- scan integration ---

def test_scan_emits_signal_and_snapshot(db):
    bars = flat_pad(OPEN - 15 * 60, 15) + sweep_long_rows(OPEN)
    levels = [{'symbol': SYM, 'session': 'asia', 'high': 105.0, 'low': 100.0,
               'complete': True}]
    with db.tx() as c:
        _seed(db, c, bars, levels)
        result = ts.scan(db, c, _cfg(), OPEN + 3600)
    assert result['ran'] is True
    assert result['signals'] == 1
    with db.tx() as c:
        latest = db.get(c, 'ict_turtle_soup:latest')
        row_ = latest['rows'][SYM]
        assert row_['direction'] == 'long'
        assert row_['concept'] == 'turtle_soup'
        assert row_['rr'] >= 1.5
        sigs = db.recent(c, 'ict_turtle_soup_signal', limit=10)
        assert len(sigs) == 1
        assert sigs[0]['payload']['level_price'] == 100.0


def test_scan_disabled_and_throttled(db):
    with db.tx() as c:
        assert ts.scan(db, c, SimpleNamespace(), OPEN) == \
            {'ran': False, 'reason': 'disabled'}
        _seed(db, c, flat_pad(OPEN - 15 * 60, 15) + sweep_long_rows(OPEN),
              [{'symbol': SYM, 'session': 'asia', 'high': 105.0, 'low': 100.0,
                'complete': True}])
        assert ts.scan(db, c, _cfg(), OPEN)['ran'] is True
        assert ts.scan(db, c, _cfg(), OPEN + 10) == \
            {'ran': False, 'reason': 'throttled'}


def test_scan_idempotent_signal(db):
    bars = flat_pad(OPEN - 15 * 60, 15) + sweep_long_rows(OPEN)
    levels = [{'symbol': SYM, 'session': 'asia', 'high': 105.0, 'low': 100.0,
               'complete': True}]
    with db.tx() as c:
        _seed(db, c, bars, levels)
        ts.scan(db, c, _cfg(), OPEN)
        db.put(c, 'ict_turtle_soup:scanned_at', 0)
        result = ts.scan(db, c, _cfg(), OPEN + 200)
        assert result['ran'] is True
        assert result['signals'] == 0  # same signal_ts -> no double-append
        assert len(db.recent(c, 'ict_turtle_soup_signal', limit=10)) == 1


def test_scan_missing_levels_graceful(db):
    with db.tx() as c:
        db.put(c, 'bar_window:' + SYM, flat_pad(OPEN - 15 * 60, 15))
        result = ts.scan(db, c, _cfg(), OPEN)
    assert result['ran'] is True
    assert result['excluded'] == {'no_levels': 1}
    assert result['signals'] == 0


def test_scan_missing_bars_graceful(db):
    with db.tx() as c:
        db.put(c, 'ict_session_liquidity:latest', liq_snapshot(
            [{'symbol': SYM, 'session': 'asia', 'high': 105.0, 'low': 100.0,
              'complete': True}]))
        result = ts.scan(db, c, _cfg(), OPEN)
    assert result['ran'] is True
    assert result['excluded'] == {'no_bars': 1}


def test_display_payload(db):
    bars = flat_pad(OPEN - 15 * 60, 15) + sweep_long_rows(OPEN)
    levels = [{'symbol': SYM, 'session': 'asia', 'high': 105.0, 'low': 100.0,
               'complete': True}]
    with db.tx() as c:
        _seed(db, c, bars, levels)
        ts.scan(db, c, _cfg(), OPEN)
        out = ts.display(db, c, OPEN)
    assert out['rows'][0]['direction'] == 'long'
    assert len(out['signals']) == 1
    assert out['note'].startswith('Research evidence only')
