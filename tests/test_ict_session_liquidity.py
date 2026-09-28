from datetime import datetime
from types import SimpleNamespace
import pytest
from compass.store import Store
from compass.market import CT
from compass import session_liquidity as sl


def ct(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CT).timestamp()


# Monday 2026-09-28 09:00 CT — Asia window (Sun 18:00 -> Mon 01:00) complete.
NOW = ct(2026, 9, 28, 9, 0)
SYMBOL = 'NQ.c.0'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'ictsess.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def cfg(**kw):
    base = dict(ict_session_liquidity=True, ict_symbols=(SYMBOL,),
                ict_sweep_ticks_mult=2.0)
    base.update(kw)
    return SimpleNamespace(**base)


def bars_5m(start, minutes, base=100.0, step=0.0, wiggle=0.0):
    """5-minute [ts,o,h,l,c,v] rows; closes drift by `step` per bar."""
    rows = []
    c = base
    for i in range(minutes):
        ts = start + i * 300
        h = c + abs(wiggle)
        l = c - abs(wiggle)
        rows.append([ts, c, h, l, c + step, 100])
        c = c + step
    return rows


def seed_window(db, c, symbol, rows):
    db.put(c, 'bar_window:' + symbol, rows)


# --- scan plumbing ---

def test_scan_disabled():
    assert sl.scan(None, None, cfg(ict_session_liquidity=False), NOW) == {
        'ran': False, 'reason': 'disabled'}


def test_scan_no_symbols():
    assert sl.scan(None, None, cfg(ict_symbols=()), NOW) == {
        'ran': False, 'reason': 'no_symbols'}


def test_scan_no_bars(db):
    with db.tx() as c:
        res = sl.scan(db, c, cfg(), NOW)
        assert res['ran'] is False and res['reason'] == 'no_bars'


def test_scan_throttles(db):
    with db.tx() as c:
        seed_window(db, c, SYMBOL,
                    bars_5m(ct(2026, 9, 27, 16, 0), 180))
        assert sl.scan(db, c, cfg(), NOW)['ran'] is True
        assert sl.scan(db, c, cfg(), NOW + 10) == {
            'ran': False, 'reason': 'throttled'}


def test_scan_writes_snapshot_and_display(db):
    with db.tx() as c:
        # Sun 00:00 -> Mon 06:00 covers the last complete window of every kind.
        seed_window(db, c, SYMBOL, bars_5m(ct(2026, 9, 27, 0, 0), 360))
        res = sl.scan(db, c, cfg(), NOW)
        assert res['ran'] is True and res['rows'] > 0
        latest = db.get(c, 'ict_session_liquidity:latest')
        kinds = {r['session'] for r in latest['levels']}
        assert {'asia', 'london', 'ny_am', 'ny_pm'} <= kinds
    with db.tx() as c:
        payload = sl.display(db, c, NOW)
        assert SYMBOL in payload['map']
        assert payload['note'] == (
            'Research evidence only. No auto-admission, no alerts, no trades.')


# --- session windowing across midnight ---

def test_asia_window_spans_midnight():
    start, end = sl.last_complete_window('asia', NOW)
    assert start == ct(2026, 9, 27, 18, 0)
    assert end == ct(2026, 9, 28, 1, 0)


def test_ny_am_window_same_day():
    # At 12:00 CT Monday, the last complete NY AM is 07:00-11:30 Monday.
    start, end = sl.last_complete_window('ny_am', ct(2026, 9, 28, 12, 0))
    assert start == ct(2026, 9, 28, 7, 0)
    assert end == ct(2026, 9, 28, 11, 30)


def test_incomplete_session_falls_back_to_prior():
    # At 08:00 CT Monday NY AM is still live -> the prior day's complete window.
    start, end = sl.last_complete_window('ny_am', ct(2026, 9, 28, 8, 0))
    assert start == ct(2026, 9, 27, 7, 0)
    assert end == ct(2026, 9, 27, 11, 30)
    # London at 08:00 Monday IS complete (01:00-07:00 Monday).
    lstart, lend = sl.last_complete_window('london', ct(2026, 9, 28, 8, 0))
    assert lstart == ct(2026, 9, 28, 1, 0)
    assert lend == ct(2026, 9, 28, 7, 0)


def test_asia_excludes_bars_outside_window(db):
    # A 5m grid 16:00 Sun -> 02:00 Mon; inject an extreme at 17:00 Sun
    # (before the Asia window opens) and one at 20:00 Sun (inside).
    rows = bars_5m(ct(2026, 9, 27, 16, 0), 120, base=100.0, wiggle=0.1)
    idx_pre = 12   # 17:00 Sun, outside
    idx_in = 48    # 20:00 Sun, inside
    rows[idx_pre][2] = 200.0   # pre-window spike: must be ignored
    rows[idx_pre][4] = 100.0
    rows[idx_in][2] = 105.0    # in-window high: the real session high
    rows[idx_in][4] = 100.0
    bars = sl._bars_from_window(rows)
    row = sl.classify_session(SYMBOL, 'asia', bars,
                              ct(2026, 9, 27, 18, 0), ct(2026, 9, 28, 1, 0),
                              sweep_ticks=0.5, now=NOW)
    assert row['high'] == 105.0
    assert row['high_ts'] == rows[idx_in][0]
    assert row['complete'] is True


def test_session_with_no_bars_is_excluded(db):
    with db.tx() as c:
        # Bars only after the NY PM window; Asia/London rows excluded.
        seed_window(db, c, SYMBOL,
                    bars_5m(ct(2026, 9, 28, 6, 0), 60))
        res = sl.scan(db, c, cfg(), ct(2026, 9, 28, 20, 0))
        assert res['ran'] is True
        latest = db.get(c, 'ict_session_liquidity:latest')
        excluded = latest['excluded']
        assert any(k.startswith(SYMBOL + ':') and v == 'no_bars'
                   for k, v in excluded.items())


# --- sweep marking ---

def _sweep_rows():
    # Asia window (Sun 18:00 -> Mon 01:00): high 100.0 at 20:00 Sun. A later bar
    # at 03:00 Mon (after the window) wicks to 101.0 and closes back at 99.8
    # -> sweep (ticks for NQ = 0.25*2 = 0.5).
    rows = bars_5m(ct(2026, 9, 27, 16, 0), 150, base=99.0)
    for r in rows:
        r[1] = r[2] = r[3] = r[4] = 99.0
    hi_i = int((ct(2026, 9, 27, 20, 0) - rows[0][0]) / 300)
    sw_i = int((ct(2026, 9, 28, 3, 0) - rows[0][0]) / 300)
    rows[hi_i][2] = 100.0   # session high
    rows[sw_i][2] = 101.0   # wick beyond by 1.0 >= 0.5
    rows[sw_i][4] = 99.8    # close back inside
    return rows


SWEEP_TS = ct(2026, 9, 28, 3, 0)


def test_swept_high_marked(db):
    rows = _sweep_rows()
    bars = sl._bars_from_window(rows)
    row = sl.classify_session(SYMBOL, 'asia', bars,
                              ct(2026, 9, 27, 18, 0), ct(2026, 9, 28, 1, 0),
                              sweep_ticks=0.5, now=NOW)
    assert row['swept_high'] is True
    assert row['sweep_high_ts'] == SWEEP_TS
    assert row['swept_low'] is False


def test_swept_low_marked():
    rows = bars_5m(ct(2026, 9, 27, 16, 0), 150, base=99.0)
    for r in rows:
        r[1] = r[2] = r[3] = r[4] = 99.0
    lo_i = int((ct(2026, 9, 27, 20, 0) - rows[0][0]) / 300)
    sw_i = int((ct(2026, 9, 28, 3, 0) - rows[0][0]) / 300)
    rows[lo_i][3] = 98.0    # session low
    rows[sw_i][3] = 97.0    # wick beyond by 1.0 >= 0.5
    rows[sw_i][4] = 98.5    # close back inside
    bars = sl._bars_from_window(rows)
    row = sl.classify_session(SYMBOL, 'asia', bars,
                              ct(2026, 9, 27, 18, 0), ct(2026, 9, 28, 1, 0),
                              sweep_ticks=0.5, now=NOW)
    assert row['swept_low'] is True
    assert row['swept_high'] is False


def test_wick_below_sweep_ticks_not_swept():
    rows = _sweep_rows()
    bars = sl._bars_from_window(rows)
    # 1.0 wick with 2.0 required -> no sweep.
    row = sl.classify_session(SYMBOL, 'asia', bars,
                              ct(2026, 9, 27, 18, 0), ct(2026, 9, 28, 1, 0),
                              sweep_ticks=2.0, now=NOW)
    assert row['swept_high'] is False


def test_wick_without_close_back_inside_not_swept():
    rows = _sweep_rows()
    sw_i = int((SWEEP_TS - rows[0][0]) / 300)
    rows[sw_i][4] = 100.8   # close stays above the level: no reclaim
    bars = sl._bars_from_window(rows)
    row = sl.classify_session(SYMBOL, 'asia', bars,
                              ct(2026, 9, 27, 18, 0), ct(2026, 9, 28, 1, 0),
                              sweep_ticks=0.5, now=NOW)
    assert row['swept_high'] is False


def test_sweep_emits_idempotent_signal(db):
    with db.tx() as c:
        seed_window(db, c, SYMBOL, _sweep_rows())
        res = sl.scan(db, c, cfg(), NOW)
        assert res['sweep_signals'] >= 1
        sigs = db.recent(c, 'ict_session_liquidity_signal', limit=50)
        assert any(s['payload']['side'] == 'high' and
                   s['payload']['session'] == 'asia' for s in sigs)
        # Re-scan at a later now: no duplicate sweep signals.
        db.put(c, 'ict_session_liquidity:scanned_at', 0)
        sl.scan(db, c, cfg(), NOW + 400)
        sigs2 = db.recent(c, 'ict_session_liquidity_signal', limit=50)
        assert len(sigs2) == len(sigs)


# --- config surface ---

def test_symbols_not_hardcoded(db):
    other = 'MES.c.0'
    with db.tx() as c:
        seed_window(db, c, other, bars_5m(ct(2026, 9, 27, 16, 0), 180))
        res = sl.scan(db, c, cfg(ict_symbols=(other,)), NOW)
        assert res['ran'] is True
        latest = db.get(c, 'ict_session_liquidity:latest')
        assert {r['symbol'] for r in latest['levels']} == {other}


def test_sweep_ticks_from_tick_map():
    # NQ root -> 0.25 * 2.0
    assert sl.sweep_ticks_for('NQ.c.0', cfg()) == pytest.approx(0.5)
    # Unknown root falls back to DEFAULT_TICK * mult.
    assert sl.sweep_ticks_for('ZZZ.c.0', cfg()) == pytest.approx(0.02)
    # Absolute override wins.
    assert sl.sweep_ticks_for('NQ.c.0', cfg(ict_sweep_ticks=1.5)) == 1.5


def test_custom_session_defs():
    defs = sl._normalize_defs({'ny_am': ((7, 0), (12, 0))})
    assert defs['ny_am'] == (7 * 3600.0, 12 * 3600.0)
    # Unknown session kind -> None, never raises.
    assert sl.last_complete_window('mars', NOW) is None
