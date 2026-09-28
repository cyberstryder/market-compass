from datetime import datetime
from types import SimpleNamespace
import pytest
from compass.store import Store
from compass.market import CT
from compass import htf_levels as ht

SYMBOL = 'ES.c.0'
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=CT).timestamp()  # Monday


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'icthtf.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def cfg(**kw):
    base = dict(ict_htf_levels=True, ict_symbols=(SYMBOL,), ict_htf_fractal_n=2)
    base.update(kw)
    return SimpleNamespace(**base)


def seed_daily(db, c, symbol, closes, start_y, start_m, start_d, step_days=1,
               ohlc_spread=1.0):
    """Append daily rows (ts at 16:00 CT); returns list of (ts, payload)."""
    out = []
    for i, cl in enumerate(closes):
        d = datetime(start_y, start_m, start_d, 16, 0, tzinfo=CT)
        ts = (d.timestamp() + i * step_days * 86400)
        payload = {'o': cl - 0.5, 'h': cl + ohlc_spread,
                   'l': cl - ohlc_spread, 'c': cl, 'v': 1000}
        db.append(c, 'daily', 'test', symbol, ts, payload,
                  key='daily:%s:%d' % (symbol, i))
        out.append((ts, payload))
    return out


# --- scan plumbing ---

def test_scan_disabled():
    assert ht.scan(None, None, cfg(ict_htf_levels=False), NOW) == {
        'ran': False, 'reason': 'disabled'}


def test_scan_no_symbols():
    assert ht.scan(None, None, cfg(ict_symbols=()), NOW) == {
        'ran': False, 'reason': 'no_symbols'}


def test_scan_no_daily(db):
    with db.tx() as c:
        res = ht.scan(db, c, cfg(), NOW)
        assert res['ran'] is False
        assert res['reason'] == 'no_bars'
        assert res['excluded'][SYMBOL] == 'no_daily'


def test_scan_throttles(db):
    with db.tx() as c:
        seed_daily(db, c, SYMBOL, [100, 101, 102, 103, 104, 105],
                   2026, 9, 21)
        assert ht.scan(db, c, cfg(), NOW)['ran'] is True
        assert ht.scan(db, c, cfg(), NOW + 60) == {
            'ran': False, 'reason': 'throttled'}


def test_scan_writes_snapshot_and_display(db):
    with db.tx() as c:
        seed_daily(db, c, SYMBOL, [100, 101, 102, 103, 104, 105],
                   2026, 9, 21)
        res = ht.scan(db, c, cfg(), NOW)
        assert res['ran'] is True and res['rows'] > 0
        latest = db.get(c, 'ict_htf_levels:latest')
        kinds = {r['kind'] for r in latest['levels']}
        assert {'pdh', 'pdl'} <= kinds
    with db.tx() as c:
        payload = ht.display(db, c, NOW)
        assert SYMBOL in payload['map']
        assert payload['note'] == (
            'Research evidence only. No auto-admission, no alerts, no trades.')


def test_missing_daily_for_one_symbol_excluded(db):
    other = 'NQ.c.0'
    with db.tx() as c:
        seed_daily(db, c, SYMBOL, [100, 101, 102], 2026, 9, 25)
        res = ht.scan(db, c, cfg(ict_symbols=(SYMBOL, other)), NOW)
        assert res['ran'] is True
        assert res['excluded'][other] == 'no_daily'
        latest = db.get(c, 'ict_htf_levels:latest')
        assert {r['symbol'] for r in latest['levels']} == {SYMBOL}


# --- prior day / prior week ---

def test_prior_day_high_low(db):
    with db.tx() as c:
        # Oldest -> newest: prior day is the second-to-last bar.
        seed_daily(db, c, SYMBOL, [100, 101, 102], 2026, 9, 24)
        ht.scan(db, c, cfg(), NOW)
        latest = db.get(c, 'ict_htf_levels:latest')
        by_kind = {r['kind']: r for r in latest['levels']}
        assert by_kind['pdh']['price'] == pytest.approx(102.0)  # h = 101+1
        assert by_kind['pdl']['price'] == pytest.approx(100.0)  # l = 101-1
        assert by_kind['pdh']['age_sessions'] == 1


def test_prior_week_high_low():
    # Two ISO weeks; newest bar sits in the current (latest) week alone.
    daily = [
        {'ts': datetime(2026, 9, 14, 16, tzinfo=CT).timestamp(), 'h': 110, 'l': 90, 'o': 100, 'c': 100},
        {'ts': datetime(2026, 9, 15, 16, tzinfo=CT).timestamp(), 'h': 112, 'l': 92, 'o': 101, 'c': 101},
        {'ts': datetime(2026, 9, 16, 16, tzinfo=CT).timestamp(), 'h': 111, 'l': 91, 'o': 100, 'c': 100},
        {'ts': datetime(2026, 9, 17, 16, tzinfo=CT).timestamp(), 'h': 113, 'l': 89, 'o': 102, 'c': 102},
        {'ts': datetime(2026, 9, 18, 16, tzinfo=CT).timestamp(), 'h': 112, 'l': 90, 'o': 101, 'c': 101},
        {'ts': datetime(2026, 9, 21, 16, tzinfo=CT).timestamp(), 'h': 115, 'l': 95, 'o': 105, 'c': 105},
    ]
    rows = ht.levels_for_symbol(SYMBOL, daily, 2, NOW)
    by_kind = {r['kind']: r for r in rows}
    # Prior complete week = Sept 14-18: high 113, low 89.
    assert by_kind['pwh']['price'] == 113.0
    assert by_kind['pwl']['price'] == 89.0
    assert by_kind['pwh']['age_sessions'] == 1


# --- fractals ---

def test_fractal_swings_unit():
    closes = [10, 11, 12, 13, 11, 10, 9, 11, 12, 13, 14, 12, 11]
    highs, lows = ht.fractal_swings(closes, n=2)
    assert highs == [3, 10]  # 13 and 14 each > everything 2 bars each side
    assert lows == [6]       # 9 < everything 2 bars each side


def test_fractal_edges_not_counted():
    # Peak at the very start cannot be confirmed (no left bars).
    closes = [15, 12, 11, 10, 9, 8, 7]
    highs, _ = ht.fractal_swings(closes, n=2)
    assert highs == []


def test_fractal_n_respected():
    closes = [10, 12, 11, 13, 11, 12, 10]
    highs1, _ = ht.fractal_swings(closes, n=1)
    highs2, _ = ht.fractal_swings(closes, n=2)
    assert highs1 == [1, 3, 5]
    assert highs2 == [3]


def test_swing_rows_emitted(db):
    with db.tx() as c:
        seed_daily(db, c, SYMBOL,
                   [10, 11, 12, 13, 11, 10, 9, 11, 12, 13, 14, 12, 11],
                   2026, 9, 10)
        ht.scan(db, c, cfg(), NOW)
        latest = db.get(c, 'ict_htf_levels:latest')
        swings = [r for r in latest['levels'] if r['kind'].startswith('swing')]
        sh = [r for r in swings if r['kind'] == 'swing_high']
        slw = [r for r in swings if r['kind'] == 'swing_low']
        assert [r['price'] for r in sh] == [13.0, 14.0]
        assert [r['price'] for r in slw] == [9.0]
        # 13 bars; swings at ascending indices 3, 10, 6 -> ages 9, 2, 6.
        assert [r['age_sessions'] for r in sh] == [9, 2]
        assert slw[0]['age_sessions'] == 6


def test_fractal_too_few_bars_no_swings(db):
    with db.tx() as c:
        seed_daily(db, c, SYMBOL, [100, 101, 102], 2026, 9, 24)
        ht.scan(db, c, cfg(), NOW)
        latest = db.get(c, 'ict_htf_levels:latest')
        kinds = {r['kind'] for r in latest['levels']}
        assert 'swing_high' not in kinds and 'swing_low' not in kinds
        assert {'pdh', 'pdl'} <= kinds  # daily levels still emitted
