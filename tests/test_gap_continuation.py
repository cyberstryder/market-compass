import pytest
from datetime import datetime
from compass.store import Store
from compass.market import session, CT
from compass import gap_continuation as gc

DAY = '2026-09-25'
SESS_OPEN, SESS_CLOSE = session(DAY)
CAPS = {'SPY', 'QQQ'}


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'gap.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class Cfg:
    gap_continuation = True
    gap_min_gap = 0.015
    gap_large_caps = 'SPY,QQQ'
    stocks = ['SPY']


def daily(ts, close):
    return {'id': 'd1', 'ts': ts, 'payload': {'o': close - 1, 'h': close + 1, 'l': close - 2, 'c': close}}


def bar(ts, o, h, l, c):
    return {'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c}


# --- identify ---

def test_identify_gap_up():
    rec, reason = gc.identify('SPY', [daily(SESS_OPEN - 86400, 100.0)],
                              bar(SESS_OPEN, 102, 102.5, 101.8, 102.2), 0.015, CAPS)
    assert reason is None and rec['direction'] == 'up'
    assert abs(rec['gap_pct'] - 0.02) < 1e-9


def test_identify_rejects_small_gap_and_unknown_cap():
    rec, reason = gc.identify('SPY', [daily(SESS_OPEN - 86400, 100.0)],
                              bar(SESS_OPEN, 101, 101.2, 100.8, 101.0), 0.015, CAPS)
    assert (rec, reason) == (None, 'gap_below_minimum')
    rec, reason = gc.identify('XYZ', [daily(SESS_OPEN - 86400, 100.0)],
                              bar(SESS_OPEN, 102, 102.5, 101.8, 102.2), 0.015, CAPS)
    assert (rec, reason) == (None, 'no_market_cap_record')


# --- opening range / break / hold ---

def test_or_range_needs_fifteen_bars():
    bars = [bar(SESS_OPEN + 60 * i, 102, 103, 101.5, 102) for i in range(15)]
    assert gc.or_range(bars, SESS_OPEN) == (103, 101.5)
    assert gc.or_range(bars[:10], SESS_OPEN) is None


def test_check_break():
    bars = [bar(SESS_OPEN + 900 + 60 * i, 102, 102.5, 101.8,
                103.5 if i == 5 else 102.2) for i in range(10)]
    direction, bts, level = gc.check_break(bars, 103, 101.5, SESS_OPEN + 900)
    assert (direction, bts, level) == ('up', SESS_OPEN + 900 + 300, 103.5)


def test_check_hold_confirmed_failed_and_incomplete():
    or_high, or_low = 103, 101.5
    bts = SESS_OPEN + 1200
    held = [bar(bts + 60 * i, 103.2, 103.6, 103.1, 103.4) for i in range(1, 12)]
    assert gc.check_hold(held, 'up', or_high, or_low, bts)[0] == 'confirmed'
    failed = [bar(bts + 60 * i, 103.2, 103.6, 103.1,
                  102.0 if i == 5 else 103.4) for i in range(1, 12)]
    outcome, fail_ts = gc.check_hold(failed, 'up', or_high, or_low, bts)
    assert outcome == 'failed' and fail_ts == bts + 300
    assert gc.check_hold(held[:5], 'up', or_high, or_low, bts) is None


# --- pillars ---

def test_dealer_pillar_supportive_opposing_unknown():
    assert gc.dealer_pillar({'levels': [{'kind': 'apex', 'price': 105.0, 'score': 90}]},
                            'up', 102.0, 103.5)['status'] == 'supportive'
    opp = gc.dealer_pillar({'levels': [{'kind': 'apex', 'price': 102.8, 'score': 90}]},
                           'up', 102.0, 103.5)
    assert opp['status'] == 'opposing' and opp['veto'] is True
    assert gc.dealer_pillar({}, 'up', 102.0, 103.5)['status'] == 'unknown'


def test_flow_pillar():
    rows = [{'symbol': 'SPY', 'source_ts': SESS_OPEN + 600, 'option_type': 'call',
             'premium': 300000, 'score': 90}]
    assert gc.flow_pillar(rows, 'SPY', SESS_OPEN, 'up')['status'] == 'supportive'
    assert gc.flow_pillar([], 'SPY', SESS_OPEN, 'up')['status'] == 'opposing'


def test_sector_pillar_unknown_without_data():
    assert gc.sector_pillar({}, {}, 'SPY', 'up')['status'] == 'unknown'


def test_forward_marks():
    bars = [bar(SESS_OPEN + 60 * i, 100, 101, 99, 100 + i * 0.1) for i in range(200)]
    marks = gc.forward_marks(bars, SESS_OPEN + 3600, 'up', SESS_CLOSE)
    assert marks['m15'] > 0 and marks['close'] is not None


# --- end to end ---

def test_scan_full_session_qualifies(db):
    with db.tx() as c:
        db.append(c, 'daily', 'test', 'SPY', SESS_OPEN - 86400,
                  {'o': 99, 'h': 101, 'l': 98, 'c': 100.0})
        # 09:30-09:45 OR: high 103, low 101.5. Open 102 (+2% gap).
        for i in range(15):
            ts = SESS_OPEN + 60 * i
            o = 102.0 if i == 0 else 102.2
            db.append(c, 'bar', 'test', 'SPY', ts, {'o': o, 'h': 103.0, 'l': 101.5, 'c': 102.2})
        # Drift then break: 09:50 close 103.5 (> or_high).
        for i in range(15, 75):
            ts = SESS_OPEN + 60 * i
            if i < 20:
                px = 102.4
            elif i == 20:
                px = 103.5
            else:
                px = 103.8
            db.append(c, 'bar', 'test', 'SPY', ts,
                      {'o': px - 0.1, 'h': px + 0.2, 'l': px - 0.2, 'c': px})
        # Late bars for forward marks + session close.
        for ts in (SESS_OPEN + 3600 + 60 * i for i in range(10)):
            db.append(c, 'bar', 'test', 'SPY', ts, {'o': 104, 'h': 104.2, 'l': 103.9, 'c': 104.0})
        db.append(c, 'bar', 'test', 'SPY', SESS_CLOSE - 60,
                  {'o': 104.5, 'h': 104.6, 'l': 104.4, 'c': 104.5})
        db.put(c, 'apex:SPY', {'levels': [{'kind': 'apex', 'price': 106.0, 'score': 92}],
                               'received': SESS_OPEN})
        db.put(c, 'matrix:unusual_activity',
               {'rows': [{'symbol': 'SPY', 'source_ts': SESS_OPEN + 600,
                          'option_type': 'call', 'premium': 300000, 'score': 90}]})
        result = gc.scan(db, c, Cfg(), SESS_CLOSE - 3600)
    assert result == {'ran': True, 'symbols': 1, 'qualified': 1}
    with db.tx() as c:
        state = db.get(c, 'gap_cont:SPY:' + DAY)
        assert state['stage'] == 'done' and state['hold'] == 'confirmed'
        assert state['qualified'] is True and state['veto'] is False
        assert state['pillars']['dealer']['status'] == 'supportive'
        assert state['pillars']['flow']['status'] == 'supportive'
        assert state['marks']['m15'] is not None
        events = db.recent(c, 'gap_continuation', limit=10)
        assert len(events) == 1
        # Idempotent: second pass adds no new event.
        assert gc.scan(db, c, Cfg(), SESS_CLOSE - 3590)['reason'] == 'throttled'
        db.put(c, 'gap_continuation:scanned_at', 0)
        gc.scan(db, c, Cfg(), SESS_CLOSE - 3400)
        assert len(db.recent(c, 'gap_continuation', limit=10)) == 1


def test_scan_excluded_symbol_stays_excluded(db):
    with db.tx() as c:
        db.append(c, 'daily', 'test', 'SPY', SESS_OPEN - 86400,
                  {'o': 99, 'h': 101, 'l': 98, 'c': 100.0})
        db.append(c, 'bar', 'test', 'SPY', SESS_OPEN,
                  {'o': 101.0, 'h': 101.2, 'l': 100.8, 'c': 101.0})  # +1% < min
        gc.scan(db, c, Cfg(), SESS_OPEN + 3600)
        state = db.get(c, 'gap_cont:SPY:' + DAY)
    assert state['stage'] == 'excluded' and state['exclusion'] == 'gap_below_minimum'


def test_display_board_and_weekly_table(db):
    with db.tx() as c:
        db.put(c, 'gap_cont:SPY:' + DAY,
               {'symbol': 'SPY', 'day': DAY, 'stage': 'done', 'gap_pct': 0.02,
                'break_direction': 'up', 'qualified': True, 'veto': False,
                'pillars': {'price': {'status': 'supportive'},
                            'dealer': {'status': 'supportive'},
                            'flow': {'status': 'supportive'},
                            'sector': {'status': 'unknown'}},
                'marks': {'close': 0.01}})
        out = gc.display(db, c, SESS_CLOSE)
    assert len(out['board']) == 1 and out['board'][0]['symbol'] == 'SPY'
    assert out['note'].startswith('Research evidence only')
