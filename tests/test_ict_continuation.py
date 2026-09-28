from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import continuation as ct

NOW = 1_700_000_000.0
SYM = 'NQ.c.0'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'cont.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def quiet(start_ts, n=20, base=99.5, rng=0.6):
    return [{'ts': start_ts + i * 60, 'o': base, 'h': base + rng / 2,
             'l': base - rng / 2, 'c': base} for i in range(n)]


def break_long_with_retest():
    """Close above 100 on a displacement bar, then a retest touch of 100."""
    bars = quiet(1000.0, 20)
    t = 1000.0 + 20 * 60
    bars.append({'ts': t, 'o': 99.6, 'h': 101.5, 'l': 99.4, 'c': 101.2})      # displacement break
    bars.append({'ts': t + 60, 'o': 100.8, 'h': 100.9, 'l': 99.9, 'c': 100.4})  # narrow retest touch
    return bars, t


def break_long_no_retest():
    bars = quiet(1000.0, 20)
    t = 1000.0 + 20 * 60
    bars.append({'ts': t, 'o': 99.6, 'h': 101.5, 'l': 99.4, 'c': 101.2})
    for i in range(1, 7):
        bars.append({'ts': t + i * 60, 'o': 101.2, 'h': 101.5, 'l': 100.7, 'c': 101.3})
    return bars, t


class Cfg:
    ict_continuation = True
    ict_symbols = (SYM,)


def seed_levels(db, c, levels):
    db.put(c, 'ict_session_liquidity:latest',
           {'at': NOW, 'levels': levels})


# --- detect_break ---

def test_break_with_retest_entry_at_level():
    bars, disp_ts = break_long_with_retest()
    sig = ct.detect_break(bars, 100.0, 'long', now=NOW)
    assert sig is not None
    assert sig['direction'] == 'long'
    assert sig['entry'] == 100.0
    assert sig['retested'] is True
    assert sig['signal_ts'] == disp_ts + 60
    assert sig['disp_ts'] == disp_ts
    assert sig['stop'] < 99.4  # origin extreme minus buffer
    assert sig['rr'] == pytest.approx(2.0)  # no opposing liquidity -> 2R target
    assert sig['params']['target_source'] == '2R'
    assert sig['level_price'] == 100.0
    assert sig['at'] == NOW


def test_break_without_retest_entry_at_displacement_close():
    bars, disp_ts = break_long_no_retest()
    sig = ct.detect_break(bars, 100.0, 'long')
    assert sig is not None
    assert sig['entry'] == 101.2
    assert sig['retested'] is False
    assert sig['signal_ts'] == disp_ts


def test_break_short_direction():
    bars = quiet(1000.0, 20, base=100.5)
    t = 1000.0 + 20 * 60
    bars.append({'ts': t, 'o': 100.4, 'h': 100.6, 'l': 98.5, 'c': 98.8})  # displacement break below 100
    bars.append({'ts': t + 60, 'o': 99.0, 'h': 100.05, 'l': 99.7, 'c': 99.2})  # narrow retest touch
    sig = ct.detect_break(bars, 100.0, 'short')
    assert sig is not None
    assert sig['direction'] == 'short'
    assert sig['entry'] == 100.0
    assert sig['stop'] > 100.6
    assert sig['target'] < sig['entry']
    assert sig['rr'] == pytest.approx(2.0)


def test_shift_fresh_vs_extended():
    fresh = quiet(1000.0, 20)
    t = 1000.0 + 20 * 60
    fresh.append({'ts': t, 'o': 100.0, 'h': 101.0, 'l': 100.0, 'c': 100.5})  # close-to-origin 0.5
    fresh.append({'ts': t + 60, 'o': 100.5, 'h': 100.7, 'l': 100.2, 'c': 100.6})
    sig = ct.detect_break(fresh, 100.0, 'long')
    assert sig is not None and sig['shift'] == 'fresh'

    extended = quiet(2000.0, 20)
    t = 2000.0 + 20 * 60
    extended.append({'ts': t, 'o': 100.0, 'h': 102.2, 'l': 100.1, 'c': 102.0})  # close-to-origin 1.9
    extended.append({'ts': t + 60, 'o': 102.0, 'h': 102.1, 'l': 101.7, 'c': 101.5})
    sig = ct.detect_break(extended, 100.0, 'long')
    assert sig is not None and sig['shift'] == 'extended'


def test_opposing_liquidity_target():
    bars, _ = break_long_with_retest()
    sig = ct.detect_break(bars, 100.0, 'long', opposing=101.5)
    assert sig is not None
    assert sig['target'] == 101.5
    assert sig['params']['target_source'] == 'opposing_liquidity'
    assert sig['rr'] > 1.5


def test_rr_filter_rejects():
    bars, _ = break_long_no_retest()
    # Entry 101.2, stop ~99.34: risk ~1.86; opposing at 101.4 gives rr < 1.5.
    sig = ct.detect_break(bars, 100.0, 'long', opposing=101.4, min_rr=1.5)
    assert sig is None


def test_no_displacement_no_signal():
    bars = quiet(1000.0, 20)
    t = 1000.0 + 20 * 60
    # Closes above 100 but the bar is not a displacement bar.
    bars.append({'ts': t, 'o': 99.6, 'h': 100.2, 'l': 99.5, 'c': 100.1})
    assert ct.detect_break(bars, 100.0, 'long') is None


def test_no_close_beyond_level_no_signal():
    bars = quiet(1000.0, 20)
    t = 1000.0 + 20 * 60
    bars.append({'ts': t, 'o': 99.6, 'h': 102.5, 'l': 99.4, 'c': 99.9})  # big bar, close inside
    assert ct.detect_break(bars, 100.0, 'long') is None


def test_defensive_bad_inputs():
    bars, _ = break_long_with_retest()
    assert ct.detect_break(bars, None, 'long') is None
    assert ct.detect_break(bars, 100.0, 'sideways') is None
    assert ct.detect_break([], 100.0, 'long') is None
    assert ct.detect_break(bars[:5], 100.0, 'long') is None


# --- scan / display ---

def test_scan_disabled_flag(db):
    with db.tx() as c:
        assert ct.scan(db, c, SimpleNamespace(ict_continuation=False, ict_symbols=(SYM,)), NOW) == \
            {'ran': False, 'reason': 'disabled'}
        assert ct.scan(db, c, SimpleNamespace(), NOW) == {'ran': False, 'reason': 'disabled'}


def test_scan_throttle(db):
    with db.tx() as c:
        seed_levels(db, c, [{'symbol': SYM, 'session': 'ny_am', 'high': 100.0,
                             'low': 95.0, 'complete': True}])
        assert ct.scan(db, c, Cfg(), NOW)['ran'] is True
        assert ct.scan(db, c, Cfg(), NOW + 10) == {'ran': False, 'reason': 'throttled'}


def test_scan_no_levels_excluded(db):
    with db.tx() as c:
        result = ct.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'symbols': 0, 'excluded': {'no_levels': 1}}


def test_scan_no_bars_excluded(db):
    with db.tx() as c:
        seed_levels(db, c, [{'symbol': SYM, 'session': 'ny_am', 'high': 100.0,
                             'low': 95.0, 'complete': True}])
        result = ct.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'symbols': 0, 'excluded': {'no_bars': 1}}


def test_scan_integration_break_signal(db):
    with db.tx() as c:
        seed_levels(db, c, [{'symbol': SYM, 'session': 'ny_am', 'high': 100.0,
                             'low': 95.0, 'complete': True}])
        bars, disp_ts = break_long_with_retest()
        db.put(c, 'bar_window:' + SYM,
               [[b['ts'], b['o'], b['h'], b['l'], b['c'], 100] for b in bars])
        result = ct.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'symbols': 1, 'excluded': {}}
    with db.tx() as c:
        latest = db.get(c, 'ict_continuation:latest')
        sigs = latest['rows'][SYM]['signals']
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig['direction'] == 'long' and sig['entry'] == 100.0
        assert sig['level_kind'] == 'session_high'
        assert sig['rr'] >= 1.5
        events = db.recent(c, 'ict_continuation_signal', limit=10)
        assert len(events) == 1
        assert events[0]['payload']['concept'] == 'continuation'
        # Re-scan after throttle clears: same break must not double-append.
        db.put(c, 'ict_continuation:scanned_at', 0)
        ct.scan(db, c, Cfg(), NOW + 200)
        assert len(db.recent(c, 'ict_continuation_signal', limit=10)) == 1


def test_scan_htf_level_opposing_target(db):
    with db.tx() as c:
        db.put(c, 'ict_htf_levels:latest',
               {'at': NOW, 'levels': [{'symbol': SYM, 'price': 100.0, 'kind': 'pdh'},
                                      {'symbol': SYM, 'price': 101.5, 'kind': 'pwh'}]})
        bars, _ = break_long_with_retest()
        db.put(c, 'bar_window:' + SYM,
               [[b['ts'], b['o'], b['h'], b['l'], b['c'], 100] for b in bars])
        result = ct.scan(db, c, Cfg(), NOW)
    assert result['ran'] is True and result['symbols'] == 1
    with db.tx() as c:
        sigs = db.get(c, 'ict_continuation:latest')['rows'][SYM]['signals']
        by_kind = {s['level_kind']: s for s in sigs}
        assert by_kind['pdh']['target'] == 101.5  # pwh is the next opposing liquidity


def test_display_payload_shape(db):
    with db.tx() as c:
        seed_levels(db, c, [{'symbol': SYM, 'session': 'ny_am', 'high': 100.0,
                             'low': 95.0, 'complete': True}])
        bars, _ = break_long_with_retest()
        db.put(c, 'bar_window:' + SYM,
               [[b['ts'], b['o'], b['h'], b['l'], b['c'], 100] for b in bars])
        ct.scan(db, c, Cfg(), NOW)
        payload = ct.display(db, c, NOW)
    assert payload['asof'] == NOW
    assert len(payload['rows']) == 1
    assert len(payload['signals']) == 1
    assert payload['note'] == 'Research evidence only. No auto-admission, no alerts, no trades.'
