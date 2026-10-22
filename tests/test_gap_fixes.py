"""Gap-fix regression tests (2026-10-20).

Covers the four mechanisms behind recurring observation_gap exits on
off-core names (BABA 110C gap-exited 3 minutes after entry):
- cadence-scaled gap tolerance instead of a flat 15s rule,
- open 0DTE paper trades pinned in the subscription inventory,
- subscription requests that include open (not only pending) positions,
- 0DTE paper exits on stale marks flagged data_gap (excluded from
  measured stats) and quoteless trades force-closed after the flatten.
"""
import time

import pytest
from sqlalchemy import select

from compass.config import Config
from compass.store import Store
from compass.gap_tolerance import intervals_between, scaled_tolerance
from compass.active_observations import inventory
from compass import zero_dte_paper
from compass import simple
# Register every table inventory() reads on the shared metadata before any
# Store initializes (table creation happens at Store.initialize()).
import compass.option_ideas  # noqa: F401
import compass.swing_ideas  # noqa: F401
import compass.spy_study  # noqa: F401
import compass.spy_timeframes  # noqa: F401
import compass.setup_study  # noqa: F401
import compass.smoothers_daily  # noqa: F401


# ---------------------------------------------------------------- helper units

def test_intervals_between_sorts_and_drops_nonpositive():
    assert intervals_between([30, 10, 10, 25]) == [15, 5]
    assert intervals_between([]) == []
    assert intervals_between([None, 5]) == []


def test_scaled_tolerance_unproven_cadence_gets_no_grace():
    # Too few observed intervals -> the legacy floor, unchanged. A missing
    # path with unproven cadence stays fatal at the flat threshold.
    assert scaled_tolerance([]) == 15
    assert scaled_tolerance([60, 90]) == 15
    assert scaled_tolerance([], floor=60) == 60


def test_scaled_tolerance_liquid_contract_stays_at_floor():
    # 2s median cadence -> 8s scaled, floored at the legacy 15s.
    assert scaled_tolerance([2, 2, 3, 2]) == 15


def test_scaled_tolerance_sparse_contract_scales_and_caps():
    assert scaled_tolerance([60, 60, 60]) == 180
    assert scaled_tolerance([200, 200, 200]) == 300  # capped


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'gap.db'))
    db.initialize()
    yield db
    db.engine.dispose()


# ---------------------------------------------------------------- inventory pin

def test_inventory_pins_open_0dte_paper_trades(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-1', {'status': 'open',
                                        'symbol': 'O:BABA251023C00110000',
                                        'underlying': 'BABA', 'kind': 'scanner'})
        db.put(c, 'trade:0dte-scan-2', {'status': 'closed',
                                        'symbol': 'O:SHOP251023C00180000',
                                        'underlying': 'SHOP', 'kind': 'scanner'})
        inv = inventory(db, c, now)
    assert 'O:BABA251023C00110000' in inv['options']
    assert 'O:SHOP251023C00180000' not in inv['options']
    assert 'BABA' in inv['stocks']


# ---------------------------------------------------------------- zero_dte exits

T0 = 1792000000.0
SYM = 'O:BABA251023C00110000'


def _trade(**kw):
    base = {'id': 'trade:0dte-scan-t', 'kind': 'scanner', 'symbol': SYM,
            'underlying': 'BABA', 'side': 'long', 'qty': 1, 'multiplier': 100,
            'entry': 1.36, 'entered_at': T0, 'status': 'open', 'fee': 0.65,
            'flatten_at': T0 + 3600}
    base.update(kw)
    return base


def test_0dte_flatten_on_stale_mark_is_data_gap(db):
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        # Only quote available at the flatten is 200s old: a blind mark.
        db.put(c, 'quote:' + SYM, {'ts': T0 + 3410, 'bid': 1.02, 'ask': 1.05})
        zero_dte_paper._update_open(db, c, T0 + 3610)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'closed'
    assert t['data_gap'] is True
    assert t['exit_reason'] == 'session flatten'


def test_0dte_flatten_on_fresh_mark_is_measured(db):
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        db.put(c, 'quote:' + SYM, {'ts': T0 + 1795, 'bid': 1.50, 'ask': 1.53})
        zero_dte_paper._update_open(db, c, T0 + 1800)  # stamps the quote, no exit
        t = db.get(c, 'trade:0dte-scan-t')
        assert t['status'] == 'open' and t['last_quote_ts'] == T0 + 1795
        db.put(c, 'quote:' + SYM, {'ts': T0 + 3605, 'bid': 1.03, 'ask': 1.06})
        zero_dte_paper._update_open(db, c, T0 + 3610)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'closed'
    assert t['data_gap'] is False
    assert t['pnl'] == pytest.approx(-35.30)


def test_0dte_quoteless_trade_force_closes_after_flatten(db):
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade(last_bid=1.02, last_quote_ts=T0 + 100))
        zero_dte_paper._update_open(db, c, T0 + 3600 + 1000)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'closed'
    assert t['data_gap'] is True
    assert t['exit'] == pytest.approx(1.01)
    assert t['exit_reason'] == 'session flatten (data gap)'


def test_0dte_quoteless_trade_without_any_mark_closes_with_no_pnl(db):
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        zero_dte_paper._update_open(db, c, T0 + 3600 + 1000)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'closed'
    assert t['data_gap'] is True
    assert t['exit'] is None and t['pnl'] is None


def test_0dte_open_trade_before_force_close_deadline_stays_open(db):
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        zero_dte_paper._update_open(db, c, T0 + 3600 + 300)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'open'


# ---------------------------------------------------------------- simple stats

def test_simple_0dte_excludes_data_gap_trades(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-g', {'kind': 'scanner', 'underlying': 'BABA',
                                        'symbol': SYM, 'status': 'closed',
                                        'entry': 1.36, 'exit': 1.02, 'pnl': -35.3,
                                        'data_gap': True, 'exit_reason': 'session flatten',
                                        'entered_at': now - 7200, 'exited_at': now - 3600})
        db.put(c, 'trade:0dte-scan-c', {'kind': 'scanner', 'underlying': 'BABA',
                                        'symbol': SYM, 'status': 'closed',
                                        'entry': 1.00, 'exit': 1.51, 'pnl': 49.7,
                                        'data_gap': False, 'exit_reason': 'target hit (+50%)',
                                        # Post-enrichment era (audit #8): a measured
                                        # scanner trade carries an evidence snapshot.
                                        'evidence': {'version': '0dte-evidence-v1'},
                                        'entered_at': now - 7200, 'exited_at': now - 3500})
        out = simple.summary(db, c, now)
        d = simple.detail(db, c, now, '0dte', '0DTE BABA')
    z = out['types']['0dte']
    assert z['stats']['tracked'] == 1
    assert z['stats']['wins'] == 1
    assert z['stats']['data_gaps'] == 1
    assert z['stats']['data_gap_pnl'] == pytest.approx(-35.3)
    gapped = [i for i in d['items'] if '⚠️' in i['result']]
    assert len(gapped) == 1


# ---------------------------------------------------------------- setup_study trials

@pytest.fixture
def study_cfg():
    return Config(local=True, stocks=('SPY',), futures=('MES.c.0',), extra_futures=(),
                  databento='', alpaca_key='', alpaca_secret='', matrix='', massive='', discord='')


def _trial(t0, **kw):
    p = {'id': 'trial-gap', 'source_id': 's-gap', 'version': 'test', 'symbol': SYM,
         'underlying': 'BABA', 'side': 'long', 'status': 'open', 'started': t0,
         'last_quote_ts': t0, 'flatten_at': t0 + 7200, 'max_gap_seconds': 0,
         'samples': 0, 'entry': 1.36, 'stop': 0.95, 'target': 2.19,
         'multiplier': 100, 'fee': 0.65, 'tick': 0.01, 'initial_risk': 41.0,
         'mfe_r': 0.0, 'mae_r': 0.0, 'alerted': False, 'primary': False}
    p.update(kw)
    return p


def _insert_trial(db, c, p):
    from compass.setup_study import trials
    c.execute(db.insert(trials).values(
        id=p['id'], source_id=p['source_id'], symbol=p['symbol'], strategy='fixture',
        side=p['side'], version=p['version'], status=p['status'], started=p['started'],
        payload=p))


def _sq(ts, bid=1.36, ask=1.40):
    return {'ts': ts, 'bid': bid, 'ask': ask, 'bid_size': 10, 'ask_size': 10}


def test_trial_sparse_quotes_survive_and_long_hole_still_gaps(db, study_cfg):
    from compass.setup_study import SetupStudy
    from test_setup_study import NOW as T0S
    study = SetupStudy(db, study_cfg)
    # Cadence already established (60-90s prints): tolerance scales to the cap.
    p = _trial(T0S, quote_gaps=[60, 90, 90])
    with db.tx() as c:
        _insert_trial(db, c, p)
        for off in (240, 480):
            study.observe(c, p, _sq(T0S + off), T0S + off)
            assert p['status'] == 'open', off
        assert p['quote_gaps'] == [60, 90, 90, 240, 240]
        # A 370s hole exceeds even the capped cadence tolerance (300s).
        study.observe(c, p, _sq(T0S + 850), T0S + 850)
    assert p['status'] == 'unresolved'
    assert p['exit_reason'] == 'observation_gap'


def test_trial_first_hole_without_cadence_is_still_fatal(db, study_cfg):
    # The suite's standing rule: no established cadence, no grace — a 20s
    # first hole on an unproven path is a missing observation, not a win.
    from compass.setup_study import SetupStudy
    from test_setup_study import NOW as T0S
    study = SetupStudy(db, study_cfg)
    p = _trial(T0S)
    with db.tx() as c:
        _insert_trial(db, c, p)
        study.observe(c, p, _sq(T0S + 20), T0S + 20)
    assert p['status'] == 'unresolved'
    assert p['exit_reason'] == 'observation_gap'


def test_trial_liquid_cadence_keeps_tight_tolerance(db, study_cfg):
    from compass.setup_study import SetupStudy
    from test_setup_study import NOW as T0S
    study = SetupStudy(db, study_cfg)
    p = _trial(T0S, quote_gaps=[2, 2, 2, 2])
    with db.tx() as c:
        _insert_trial(db, c, p)
        study.observe(c, p, _sq(T0S + 20), T0S + 20)  # 20s hole on a 2s-cadence name
    assert p['status'] == 'unresolved'
    assert p['exit_reason'] == 'observation_gap'


# ---------------------------------------------------------------- swing requests include open

def test_swing_stream_requests_default_includes_open(db):
    from compass.swing_ideas import stream_requests as swing_requests, swings
    now = time.time()
    p = {'status': 'open', 'contract': {'symbol': 'O:MSFT251121C00500000'},
         'candidates': [], 'expires_at': now + 3600}
    with db.tx() as c:
        c.execute(db.insert(swings).values(
            id='sw1', underlying='MSFT', status='open', created=now, updated=now, payload=p))
        assert 'O:MSFT251121C00500000' in swing_requests(c, now)
        assert 'O:MSFT251121C00500000' in swing_requests(c, now, 'open')


# ---------------------------------------------------------------- option_ideas end-to-end

@pytest.fixture
def ideas_cfg():
    return Config(local=True, stocks=('SPY',), futures=(), extra_futures=(), massive='fixture',
                  scanner_paper=False, alpaca_key='', alpaca_secret='', databento='', matrix='', discord='')


def test_idea_sparse_option_prints_survive_but_long_silence_gaps(db, ideas_cfg):
    from test_scanner import NOW
    from test_option_ideas import seed, signal, q, rows, CALL
    from compass.store import events
    from compass.option_ideas import OptionIdeas
    service = OptionIdeas(db, ideas_cfg)
    with db.tx() as c:
        seed(db, c)
        service.queue(c, signal(), NOW)
        service.tick(c, NOW)
        assert rows(c)[0]['status'] == 'open'

        def emit(symbol, ts, bid, ask):
            db.append(c, 'quote', 'massive', symbol, ts, q(ts, bid, ask))
            c.execute(events.update().where(events.c.symbol == symbol,
                                            events.c.ts == ts).values(received=ts + .1))

        # Underlying keeps printing every 30s; the option contract slows to
        # one print a minute after entry (the BABA pattern).
        for off in range(-60, 181, 30):
            emit('SPY', NOW + off, 100, 100.02)
        for off in (60, 120, 180):
            emit(CALL, NOW + off, 2.10, 2.15)
        db.put(c, 'quote:' + CALL, q(NOW + 180, 2.10, 2.15))
        db.put(c, 'quote:SPY', q(NOW + 180))
        # Tick just past +180: a quote is only visible once received (ts+.1).
        service.tick(c, NOW + 185)
        p = rows(c)[0]
        assert p['status'] == 'open'
        assert p['opt_gap_tol'] == 180  # median 60s cadence x3

        # A genuine 520s silence still closes it as a gap.
        service.tick(c, NOW + 700)
        p = rows(c)[0]
        assert p['status'] == 'unresolved'
        assert p['exit_reason'] == 'observation_gap'
