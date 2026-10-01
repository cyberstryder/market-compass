from datetime import datetime, timezone
import pytest
from compass.store import Store
from compass.market import session
from compass import apex_magnet as am

DAY = '2026-09-25'  # Friday
OPEN, CLOSE = session(DAY)
NOW = OPEN + 3 * 3600  # mid-session


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'apex.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def apex_payload(received=NOW, source_ts=NOW - 60, spot=100.0, levels=None):
    return {
        'symbol': 'SPY', 'source': 'tradermatrix', 'source_ts': source_ts,
        'received': received, 'spot': spot,
        'levels': levels if levels is not None else [
            {'price': 102.0, 'score': 90, 'kind': 'apex', 'net_gex': 1.0, 'oi': 5000},
            {'price': 98.0, 'score': 70, 'kind': 'apex', 'net_gex': 0.5, 'oi': 3000},
            {'price': 99.0, 'score': None, 'kind': 'gamma_flip'},
        ],
    }


def minute_bars(start_ts, closes, base=100.0):
    """Window rows [ts,o,h,l,c,v]; closes drive the shape."""
    rows = []
    for i, c in enumerate(closes):
        ts = start_ts + i * 60
        rows.append([ts, base, max(base, c), min(base, c), c, 1000])
        base = c
    return rows


def daily_closes(n=25, start=90.0):
    return [start + i * 0.5 for i in range(n)]


# --- select_magnet ---

def test_select_magnet_picks_highest_score():
    magnet, flip = am.select_magnet(apex_payload())
    assert magnet['price'] == 102.0 and magnet['score'] == 90
    assert flip == 99.0


def test_select_magnet_no_levels():
    magnet, reason = am.select_magnet(apex_payload(levels=[]))
    assert magnet is None and reason == 'no_apex_levels'


# --- detect_signal ---

def test_detect_broke_through():
    bars = am._bars_from_window(minute_bars(OPEN, [99.0, 99.5, 100.5, 103.0]))
    signal, tests = am.detect_signal(bars, 102.0, OPEN)
    assert signal == 'broke_through'


def test_detect_tested_holding_counts_tests():
    # touches 102 band (101.9-102.1) twice, closes back below both times
    rows = []
    closes = [100.0, 101.95, 100.5, 102.05, 100.8]
    for i, c in enumerate(closes):
        ts = OPEN + i * 60
        rows.append([ts, 100.0, max(100.0, c) + 0.3, min(100.0, c) - 0.3, c, 1000])
    bars = am._bars_from_window(rows)
    signal, tests = am.detect_signal(bars, 102.0, OPEN)
    assert signal == 'tested_holding' and tests == 2


def test_detect_approaching():
    bars = am._bars_from_window(minute_bars(OPEN, [95.0, 96.0, 97.0, 98.0, 99.0, 99.5, 99.8]))
    signal, tests = am.detect_signal(bars, 102.0, OPEN)
    assert signal == 'approaching' and tests == 0


def test_detect_none_when_drifting_away():
    bars = am._bars_from_window(minute_bars(OPEN, [99.5, 99.0, 98.0, 97.0, 96.0, 95.0]))
    signal, tests = am.detect_signal(bars, 102.0, OPEN)
    assert signal is None


# --- classify_row ---

def test_classify_row_builds_full_row():
    bars = am._bars_from_window(minute_bars(OPEN, [99.0, 99.5, 100.0, 100.5, 101.0, 101.2]))
    row = am.classify_row('SPY', apex_payload(spot=101.2), bars, daily_closes(),
                          'Technology', NOW)
    assert row['symbol'] == 'SPY'
    assert row['magnet'] == 102.0
    assert row['role'] == 'resistance'
    assert row['vs_flip'] == 'above'
    assert row['distance_pct'] == pytest.approx(abs(101.2 - 102.0) / 101.2)
    assert row['sma20'] == pytest.approx(sum(daily_closes()[-20:]) / 20)
    assert row['signal'] in ('approaching', 'tested_holding', 'broke_through', None)


def test_classify_row_excluded_outside_radius():
    bars = am._bars_from_window(minute_bars(OPEN, [90.0] * 6))
    row = am.classify_row('SPY', apex_payload(spot=90.0), bars, daily_closes(),
                          None, NOW, radius=0.02)
    assert row == {'excluded': 'outside_radius'}


def test_classify_row_excluded_stale_apex():
    bars = am._bars_from_window(minute_bars(OPEN, [101.0] * 6))
    stale = apex_payload(received=NOW - 3600, source_ts=NOW - 3600, spot=101.0)
    row = am.classify_row('SPY', stale, bars, daily_closes(), None, NOW)
    assert row == {'excluded': 'apex_not_fresh'}


# --- magnet drift (level first-seen history) ---

def _levels_with_known_at(known_at):
    return [
        {'price': 102.0, 'score': 90, 'kind': 'apex', 'net_gex': 1.0,
         'oi': 5000, 'known_at': known_at},
        {'price': 98.0, 'score': 70, 'kind': 'apex', 'net_gex': 0.5,
         'oi': 3000, 'known_at': known_at},
        {'price': 99.0, 'score': None, 'kind': 'gamma_flip'},
    ]


def test_magnet_basis_stable_when_level_known_before_open():
    bars = am._bars_from_window(minute_bars(OPEN, [99.0, 99.5, 100.0, 100.5, 101.0, 101.2]))
    payload = apex_payload(spot=101.2, levels=_levels_with_known_at(OPEN - 3600))
    row = am.classify_row('SPY', payload, bars, daily_closes(), 'Technology', NOW)
    assert row['magnet_basis'] == 'stable'
    assert row['magnet_known_at'] == OPEN - 3600
    assert row['magnet_age_s'] == pytest.approx(NOW - (OPEN - 3600), abs=0.2)


def test_magnet_basis_intraday_when_level_appeared_today():
    bars = am._bars_from_window(minute_bars(OPEN, [99.0, 99.5, 100.0, 100.5, 101.0, 101.2]))
    payload = apex_payload(spot=101.2, levels=_levels_with_known_at(OPEN + 3600))
    row = am.classify_row('SPY', payload, bars, daily_closes(), 'Technology', NOW)
    assert row['magnet_basis'] == 'intraday'
    assert row['magnet_known_at'] == OPEN + 3600


def test_magnet_basis_current_without_known_at():
    # Snapshots predating the known_at stamp degrade to the old label.
    bars = am._bars_from_window(minute_bars(OPEN, [99.0, 99.5, 100.0, 100.5, 101.0, 101.2]))
    row = am.classify_row('SPY', apex_payload(spot=101.2), bars, daily_closes(),
                          'Technology', NOW)
    assert row['magnet_basis'] == 'current'
    assert row['magnet_known_at'] is None
    assert row['magnet_age_s'] is None


# --- scan end to end ---

class Cfg:
    apex_magnet = True
    apex_magnet_radius = 0.02
    apex_magnet_tolerance = 0.001


def _seed(db, c, symbol='SPY', spot=101.2, signal_closes=None):
    closes = signal_closes or [99.0, 99.5, 100.0, 100.5, 101.0, 101.2]
    db.put(c, 'apex:' + symbol, apex_payload(spot=spot))
    db.put(c, 'bar_window:' + symbol, minute_bars(OPEN, closes))
    for i, cl in enumerate(reversed(daily_closes())):
        db.append(c, 'daily', 'test', symbol, NOW - (i + 1) * 86400,
                  {'o': cl - 1, 'h': cl + 1, 'l': cl - 2, 'c': cl, 'v': 1000},
                  key='daily:%s:%d' % (symbol, i))


def test_scan_stores_snapshot_and_signal(db):
    with db.tx() as c:
        _seed(db, c)
        result = am.scan(db, c, Cfg(), NOW)
    assert result['ran'] is True and result['symbols'] == 1
    with db.tx() as c:
        latest = db.get(c, 'apex_magnet:latest')
        assert 'SPY' in latest['rows']
        assert latest['rows']['SPY']['magnet'] == 102.0
        signals = db.recent(c, 'apex_magnet_signal', limit=10)
        assert len(signals) == 1
        assert signals[0]['payload']['signal'] in ('approaching', 'tested_holding')


def test_scan_throttles_and_is_idempotent(db):
    with db.tx() as c:
        _seed(db, c)
        assert am.scan(db, c, Cfg(), NOW)['ran'] is True
        assert am.scan(db, c, Cfg(), NOW + 10) == {'ran': False, 'reason': 'throttled'}
        # New apex receipt re-runs but the unchanged signal does not double-append.
        db.put(c, 'apex:SPY', apex_payload(received=NOW + 200, spot=101.2))
        assert am.scan(db, c, Cfg(), NOW + 200)['ran'] is True
        assert len(db.recent(c, 'apex_magnet_signal', limit=10)) == 1


def test_scan_session_rollover_writes_outcome(db):
    prev_day = '2026-09-24'
    p_open, _ = session(prev_day)
    with db.tx() as c:
        # Prior session: tested the 102 magnet twice, closed below -> reject.
        rows = []
        closes = [100.0, 101.95, 100.5, 102.05, 100.8]
        for i, cl in enumerate(closes):
            ts = p_open + i * 60
            rows.append([ts, 100.0, max(100.0, cl) + 0.3, min(100.0, cl) - 0.3, cl, 1000])
        db.put(c, 'apex:SPY', apex_payload(received=p_open + 3600, source_ts=p_open + 3540))
        db.put(c, 'bar_window:SPY', rows)
        db.put(c, 'apex_magnet:scanned_at', 0)
        assert am.scan(db, c, Cfg(), p_open + 3600)['ran'] is True
        sig = db.get(c, 'apex_magnet:signal:SPY')
        assert sig['signal'] == 'tested_holding'
        # Persist prior-session bars for the outcome computation.
        for r in rows:
            ts, o, h, l, cl, v = r
            db.append(c, 'bar', 'test', 'SPY', ts,
                      {'o': o, 'h': h, 'l': l, 'c': cl, 'v': v}, key='bar:spy:%d' % ts)
    with db.tx() as c:
        # New session with fresh apex data triggers rollover close-out.
        db.put(c, 'apex:SPY', apex_payload(received=NOW, spot=101.0))
        db.put(c, 'bar_window:SPY', minute_bars(OPEN, [101.0] * 6))
        db.put(c, 'apex_magnet:scanned_at', 0)
        am.scan(db, c, Cfg(), NOW)
        outcomes = db.recent(c, 'apex_magnet_outcome', limit=10)
        assert len(outcomes) == 1
        assert outcomes[0]['payload']['session'] == prev_day
        assert outcomes[0]['payload']['outcome'] == 'reject'


# --- hit_rates ---

def _seed_signal_outcome(db, c, symbol, signal, outcome, ts):
    db.append(c, 'apex_magnet_signal', 'apex_magnet', symbol, ts,
              {'session': '2026-09-25', 'signal': signal, 'magnet': 100.0},
              key='apexmag:%s:sig:%s:%d' % (symbol, signal, ts))
    db.append(c, 'apex_magnet_outcome', 'apex_magnet', symbol, ts,
              {'session': '2026-09-25', 'signal': signal, 'magnet': 100.0,
               'outcome': outcome, 'tests': 2},
              key='apexmag:%s:out:%s:%d' % (symbol, signal, ts))


def test_hit_rates_per_state(db):
    with db.tx() as c:
        base = NOW - 86400
        # broke_through: 2 break (hits), 1 pin (miss)
        _seed_signal_outcome(db, c, 'SPY', 'broke_through', 'break', base)
        _seed_signal_outcome(db, c, 'SPY', 'broke_through', 'break', base + 1)
        _seed_signal_outcome(db, c, 'QQQ', 'broke_through', 'pin', base + 2)
        # tested_holding: 1 pin (hit), 1 reject (hit), 1 break (miss)
        _seed_signal_outcome(db, c, 'SPY', 'tested_holding', 'pin', base + 3)
        _seed_signal_outcome(db, c, 'SPY', 'tested_holding', 'reject', base + 4)
        _seed_signal_outcome(db, c, 'SPY', 'tested_holding', 'break', base + 5)
        # approaching: 1 pin (hit = tested), 1 no_test (excluded)
        _seed_signal_outcome(db, c, 'SPY', 'approaching', 'pin', base + 6)
        _seed_signal_outcome(db, c, 'SPY', 'approaching', 'no_test', base + 7)
        out = am.hit_rates(db, c, NOW, lookback_days=14)
    bt = out['states']['broke_through']
    assert bt['signals'] == 3
    assert bt['outcomes_recorded'] == 3
    assert bt['breakdown'] == {'break': 2, 'pin': 1}
    assert bt['tested'] == 3
    assert bt['hits'] == 2
    assert bt['hit_rate'] == pytest.approx(2 / 3)

    th = out['states']['tested_holding']
    assert th['signals'] == 3
    assert th['hits'] == 2  # pin + reject
    assert th['hit_rate'] == pytest.approx(2 / 3)

    ap = out['states']['approaching']
    assert ap['signals'] == 2
    assert ap['outcomes_recorded'] == 2
    assert ap['tested'] == 1  # no_test excluded
    assert ap['hits'] == 1
    assert ap['hit_rate'] == pytest.approx(1.0)


def test_hit_rates_empty_when_no_history(db):
    with db.tx() as c:
        out = am.hit_rates(db, c, NOW, lookback_days=14)
    for state in ('approaching', 'tested_holding', 'broke_through'):
        s = out['states'][state]
        assert s['signals'] == 0
        assert s['outcomes_recorded'] == 0
        assert s['hit_rate'] is None


def test_hit_rates_respects_lookback(db):
    with db.tx() as c:
        _seed_signal_outcome(db, c, 'SPY', 'broke_through', 'break', NOW - 86400)
        _seed_signal_outcome(db, c, 'SPY', 'broke_through', 'break', NOW - 30 * 86400)
        out = am.hit_rates(db, c, NOW, lookback_days=14)
    bt = out['states']['broke_through']
    # Only the recent one is inside the 14-day window.
    assert bt['signals'] == 1
    assert bt['outcomes_recorded'] == 1
