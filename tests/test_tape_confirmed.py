import pytest
from sqlalchemy import insert
from compass.store import Store
from compass.setup_study import trials
from compass import tape_confirmed as tc

NOW = 1_790_000_000.0
SIG = NOW - 2000  # trigger with a closed +/-15min window


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'tape.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def flow(symbol='SPY', ts=SIG + 100, option_type='call', premium=300000, score=90):
    return {'symbol': symbol, 'source_ts': ts, 'option_type': option_type,
            'premium': premium, 'score': score, 'vendor_id': 'v1'}


# --- confirm_trial ---

def test_long_confirmed_on_call_premium_and_score():
    r = tc.confirm_trial('SPY', 'long', SIG, [flow(), flow(premium=50000, score=70)])
    assert r['confirmed'] is True
    assert r['call_premium'] == 350000 and r['put_premium'] == 0
    assert r['max_score'] == 90 and r['void_reason'] is None


def test_short_confirmed_on_puts():
    r = tc.confirm_trial('SPY', 'short', SIG, [flow(option_type='put')])
    assert r['confirmed'] is True


def test_unconfirmed_when_premium_too_low():
    r = tc.confirm_trial('SPY', 'long', SIG, [flow(premium=100000)])
    assert r['confirmed'] is False and r['void_reason'] is None


def test_unconfirmed_when_no_high_score_print():
    r = tc.confirm_trial('SPY', 'long', SIG, [flow(score=80)])
    assert r['confirmed'] is False


def test_mixed_direction_voids():
    rows = [flow(premium=300000), flow(option_type='put', premium=200000)]
    r = tc.confirm_trial('SPY', 'long', SIG, rows)
    assert r['confirmed'] is False and r['void_reason'] == 'direction_mixed'


def test_invalidation_before_window_close_voids():
    r = tc.confirm_trial('SPY', 'long', SIG, [flow()], invalidated_ts=SIG + 100)
    assert r['confirmed'] is False
    assert r['void_reason'] == 'setup_invalidated_before_window_close'


def test_invalidation_after_window_close_does_not_void():
    r = tc.confirm_trial('SPY', 'long', SIG, [flow()], invalidated_ts=SIG + 901)
    assert r['confirmed'] is True


def test_ignores_other_symbols_and_out_of_window_rows():
    rows = [flow(symbol='QQQ'), flow(ts=SIG - 901), flow(ts=SIG + 901)]
    r = tc.confirm_trial('SPY', 'long', SIG, rows)
    assert r['confirmed'] is False and r['flow_count'] == 0


# --- scan ---

class Cfg:
    tape_confirmed = True


def seed_trial(c, trial_id='t1', symbol='SPY', side='long', signal_ts=SIG,
               status='open', r_multiple=None):
    payload = {'id': 'sig1', 'source_id': 'sig1', 'symbol': symbol, 'side': side, 'signal_time': signal_ts,
               'signal_price': 100.0, 'strategy': 'intraday', 'rule': 'trend_pullback',
               'r_multiple': r_multiple}
    c.execute(insert(trials).values(id=trial_id, source_id='sig1', symbol=symbol,
                                    strategy='intraday', side=side, version='v4',
                                    status=status, started=signal_ts, finished=None,
                                    payload=payload))


def test_scan_confirms_and_appends_once(db):
    with db.tx() as c:
        seed_trial(c)
        db.put(c, 'matrix:unusual_activity', {'rows': [flow()]})
        result = tc.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'evaluated': 1, 'confirmed': 1}
    with db.tx() as c:
        verdict = db.get(c, 'tape_confirm:t1')
        assert verdict['confirmed'] is True and verdict['call_premium'] == 300000
        events = db.recent(c, 'tape_confirmation', limit=10)
        assert len(events) == 1 and events[0]['payload']['trial_id'] == 't1'
        # Second pass: throttled, then idempotent.
        assert tc.scan(db, c, Cfg(), NOW + 10)['reason'] == 'throttled'
        db.put(c, 'matrix:unusual_activity', {'rows': [flow(premium=999999)]})
        assert tc.scan(db, c, Cfg(), NOW + 200)['evaluated'] == 0
        assert len(db.recent(c, 'tape_confirmation', limit=10)) == 1


def test_scan_skips_open_window_and_voids_invalidated(db):
    with db.tx() as c:
        seed_trial(c, trial_id='t2', signal_ts=NOW - 100)  # window still open
        seed_trial(c, trial_id='t3', signal_ts=SIG - 5000)
        db.append(c, 'alert', 'scanner', 'SPY', SIG - 4900,
                  {'status': 'setup_invalidated', 'setup_id': 'sig1'}, key='inv1')
        db.put(c, 'matrix:unusual_activity', {'rows': [flow(ts=SIG - 4900)]})
        result = tc.scan(db, c, Cfg(), NOW)
    assert result['evaluated'] == 1  # t2 skipped: window open
    with db.tx() as c:
        verdict = db.get(c, 'tape_confirm:t3')
        assert verdict['confirmed'] is False
        assert verdict['void_reason'] == 'setup_invalidated_before_window_close'


def test_display_scorecard_joins_outcomes(db):
    with db.tx() as c:
        seed_trial(c, trial_id='t1', status='closed', r_multiple=1.5)
        seed_trial(c, trial_id='t2', status='closed', r_multiple=-1.0)
        seed_trial(c, trial_id='t3', status='open')
        db.put(c, 'tape_confirm:t1', {'confirmed': True, 'evaluated_at': NOW})
        db.put(c, 'tape_confirm:t2', {'confirmed': False, 'evaluated_at': NOW})
        card = tc.display(db, c, NOW)['scorecard']
    assert card['confirmed']['n'] == 1 and card['confirmed']['mean_r'] == 1.5
    assert card['unconfirmed']['n'] == 1 and card['unconfirmed']['mean_r'] == -1.0
    assert card['confirmed']['win_rate'] == 1.0
