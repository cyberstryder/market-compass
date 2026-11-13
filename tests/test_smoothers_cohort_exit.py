"""Smoothers Monday-mark cohort x exit-horizon rule cell
(frozen registration 2026-11-02, Josh-approved creative-search
Candidate 2).

Every Monday-board pick is scored under two exit rules on the board's
executable ruler (EOD chain-archive bid out, x100, $1.32 fee):
BASELINE exits everything at Wednesday's bid (the standing flat
standard); RULE exits the deep Monday-mark cohort (<0.70 of entry ask)
at Monday's bid — the mark itself — and shallow/middle exactly as
baseline. The per-pick delta is therefore identically 0 outside the
deep cohort, and the weekly summary checks that identity.

Pins:
- both arms priced from the archive, never fabricated: a missing
  Wednesday snapshot inside its retry grace defers (None); past
  grace, a missing contract / bid records an explicit unavailable
  reason;
- cohort boundaries come from mark_cohort (>=0.80 shallow, <0.70
  deep); a missing Monday mark leaves the cohort 'unknown', the
  baseline arm still scores, and no rule arm is assigned;
- the deep rule can LOSE (Wednesday rally) — delta is signed, not
  floored;
- the cell covers EVERY board pick, including one that already
  resolved WIN on its underlying target;
- the baseline arm equals the Tuesday cell's Monday arm on identical
  inputs (the registration's consistency check);
- storage only: no outbox notice is queued for a cell;
- the weekly summary: primary population excludes unknown cohorts,
  per-cohort and pooled means, identity_ok flag.
"""
from sqlalchemy import select
from test_projects import db  # noqa: F401  (shared Store fixture)
from compass.native_outbox import outbox as native_outbox
from compass.native_smoothers import (VERSION, weekly, save, monitor,
                                      cohort_exit_score, cohort_exit_summary,
                                      tuesday_cell_score,
                                      COHORT_EXIT_STANDARD_FROM)
from compass.smoothers_handoff import sessions
from compass.chain_eod import event_key as chain_event_key

WEEK = '2026-09-14'
CAL = sessions(WEEK)
MON, TUE, WED = CAL[0], CAL[1], CAL[2]
# The first board under the adopted scoring standard (Josh, 2026-11-12)
# and the board immediately before it.
WEEK_STD = COHORT_EXIT_STANDARD_FROM            # '2026-11-16'
CAL_STD = sessions(WEEK_STD)
MON_STD, WED_STD = CAL_STD[0], CAL_STD[2]
WEEK_PRE = '2026-11-09'
CAL_PRE = sessions(WEEK_PRE)
ENTRY = MON['open'] + 3600
CONTRACT = {'symbol': 'DEMO260918C00102000', 'multiplier': 100,
            'strike': 102, 'expiration': '2026-09-18', 'type': 'CALL'}


def _quote(bid, ask):
    return dict(status='available', bid=bid, ask=ask, midpoint=(bid + ask) / 2,
                quote_at_ms=int(ENTRY * 1000))


def _pick(ticker='DEMO', pid=None, **over):
    p = dict(id=pid or ticker, week=WEEK, ticker=ticker, status='WIN',
             direction='CALL', entry_price=100.0, target_price=102.0,
             model_entry_time=ENTRY, created_at=ENTRY,
             resolution_time=MON['close'] - 3600, exit_underlying=102.0,
             contract=dict(CONTRACT), quote=_quote(1.9, 2.0), cost_key=10.0)
    p.update(over)
    return p


def _chain(db, date, ticker, contracts):
    close = next(s['close'] for s in CAL if s['date'] == date)
    with db.tx() as c:
        db.append(c, 'chain_eod', 'massive', ticker, close + 600,
                  {'date': date, 'symbol': ticker, 'contracts': contracts},
                  key=chain_event_key(date, ticker))


def _row(symbol, bid, ask):
    return {'symbol': symbol, 'quote': {'bid': bid, 'ask': ask}}


def _seed_week(db, picks):
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + WEEK,
               {'week': WEEK, 'sessions': CAL, 'state': 'complete'})
        for p in picks:
            save(db, c, p)


def _stored(db, pid):
    with db.tx() as c:
        return c.execute(select(weekly.c.payload)
                         .where(weekly.c.id == pid)).scalar_one()


def _score(db, p, now):
    with db.tx() as c:
        return cohort_exit_score(c, p, MON, WED, now)


class _Provider:
    def quote(self, contract):
        return dict(status='available', bid=0.4, ask=0.5,
                    quote_at_ms=int((WED['close'] + 300) * 1000))


# ---------------------------------------------------------------- arms

def test_shallow_pick_rule_equals_baseline(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.70, 1.80)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'scored'
    assert cell['cohort'] == 'shallow'           # 1.70 / 2.00 = 0.85
    assert cell['monday_mark_ratio'] == 0.85
    # Baseline: (2.20 - 2.00) * 100 - 1.32; rule identical by construction.
    assert cell['baseline_net'] == 18.68
    assert cell['rule_net'] == 18.68
    assert cell['delta_net'] == 0.0
    assert cell['rule_exit'] == 'wednesday_close'
    assert cell['fee'] == 1.32


def test_middle_pick_rule_equals_baseline(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.50, 1.80)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['cohort'] == 'middle'            # 1.50 / 2.00 = 0.75
    assert cell['rule_net'] == cell['baseline_net'] == 18.68
    assert cell['delta_net'] == 0.0
    assert cell['rule_exit'] == 'wednesday_close'


def test_deep_pick_exits_at_monday_bid(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 1.10, 1.20)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['cohort'] == 'deep'              # 1.30 / 2.00 = 0.65
    # Baseline: (1.10 - 2.00) * 100 - 1.32; rule: (1.30 - 2.00) * 100 - 1.32.
    assert cell['baseline_net'] == -91.32
    assert cell['rule_net'] == -71.32
    assert cell['delta_net'] == 20.00
    assert cell['rule_exit'] == 'monday_close'
    assert cell['monday_bid'] == 1.30


def test_deep_rule_loses_when_wednesday_rallies(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 1.60, 1.70)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['baseline_net'] == -41.32
    assert cell['rule_net'] == -71.32
    assert cell['delta_net'] == -30.00           # signed, never floored


def test_baseline_matches_tuesday_cell_monday_arm(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain(db, '2026-09-15', 'DEMO', [_row(CONTRACT['symbol'], 1.25, 1.35)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 1.10, 1.20)])
    with db.tx() as c:
        tue = tuesday_cell_score(c, p, MON, TUE, WED, WED['close'] + 600)
        cell = cohort_exit_score(c, p, MON, WED, WED['close'] + 600)
    assert tue['status'] == cell['status'] == 'scored'
    assert cell['baseline_net'] == tue['monday_net']
    assert cell['monday_mark_ratio'] == tue['monday_mark_ratio']
    assert cell['cohort'] == tue['cohort']


# ----------------------------------------------------------- Monday mark

def test_missing_monday_mark_scores_baseline_cohort_unknown(db):
    p = _pick()
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'scored'
    assert cell['cohort'] == 'unknown'
    assert cell['monday_mark_ratio'] is None
    assert cell['mark_reason'] == 'monday_chain_missing'
    assert cell['baseline_net'] == 18.68
    assert cell['rule_net'] is None and cell['delta_net'] is None


def test_monday_contract_absent_mark_reason(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row('OTHER261016C00100000', 1.0, 1.1)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['cohort'] == 'unknown'
    assert cell['mark_reason'] == 'monday_contract_not_in_chain'


# ------------------------------------------------------ missing snapshots

def test_missing_wednesday_snapshot_defers_inside_grace(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    assert _score(db, p, WED['close'] + 600) is None


def test_missing_wednesday_snapshot_unavailable_past_grace(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    cell = _score(db, p, WED['close'] + 36 * 3600 + 60)
    assert cell['status'] == 'unavailable'
    assert cell['reason'] == 'wednesday_chain_missing'


def test_contract_absent_from_wednesday_chain(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain(db, '2026-09-16', 'DEMO', [_row('OTHER261016C00100000', 1.0, 1.1)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'unavailable'
    assert cell['reason'] == 'wednesday_contract_not_in_chain'
    assert 'baseline_net' not in cell


def test_wednesday_bid_missing(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain(db, '2026-09-16', 'DEMO',
           [{'symbol': CONTRACT['symbol'], 'quote': {'ask': 1.20}}])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'unavailable'
    assert cell['reason'] == 'chain_bid_missing'


def test_direct_unavailable_paths(db):
    now = WED['close'] + 600
    cell = _score(db, _pick(contract=None), now)
    assert cell['status'] == 'unavailable' and cell['reason'] == 'no_contract_recorded'
    cell = _score(db, _pick(quote=None), now)
    assert cell['status'] == 'unavailable' and cell['reason'] == 'entry_ask_missing'
    bad = dict(CONTRACT, multiplier=10)
    cell = _score(db, _pick(contract=bad), now)
    assert cell['status'] == 'unavailable' and cell['reason'] == 'unsupported_contract_multiplier'


# ------------------------------------------------------------- summary

def _cell(cohort, baseline, rule):
    return {'status': 'scored', 'cohort': cohort, 'baseline_net': baseline,
            'rule_net': rule,
            'delta_net': None if rule is None else round(rule - baseline, 2)}


def test_summary_primary_cohorts_and_identity():
    rows = [
        {'cohort_exit': _cell('shallow', 18.68, 18.68)},
        {'cohort_exit': _cell('middle', -10.0, -10.0)},
        {'cohort_exit': _cell('deep', -91.32, -71.32)},   # delta +20
        {'cohort_exit': _cell('deep', -41.32, -71.32)},   # delta -30
        {'cohort_exit': _cell('unknown', 5.0, None)},
        {'cohort_exit': {'status': 'unavailable', 'reason': 'wednesday_chain_missing'}},
        {},
    ]
    s = cohort_exit_summary(rows)
    assert s['picks'] == 7 and s['scored'] == 5 and s['unavailable'] == 2
    assert s['primary']['n'] == 4
    assert s['primary']['delta_sum'] == -10.0
    assert s['primary']['delta_mean'] == -2.5
    assert s['cohorts']['deep'] == {'n': 2, 'baseline_mean': -66.32,
                                    'rule_mean': -71.32, 'delta_mean': -5.0,
                                    'baseline_sum': -132.64, 'rule_sum': -142.64,
                                    'delta_sum': -10.0}
    assert s['cohorts']['shallow']['delta_sum'] == 0
    assert s['cohorts']['middle']['delta_sum'] == 0
    assert s['identity_ok'] is True
    assert s['unknown_cohort'] == {'n': 1, 'baseline_mean': 5.0}


def test_summary_identity_flags_a_moved_shallow_delta():
    rows = [{'cohort_exit': _cell('shallow', 10.0, 12.0)},   # impossible by construction
            {'cohort_exit': _cell('deep', -90.0, -70.0)}]
    s = cohort_exit_summary(rows)
    assert s['identity_ok'] is False


def test_summary_empty_week():
    s = cohort_exit_summary([])
    assert s['picks'] == 0 and s['primary']['n'] == 0
    assert s['primary']['delta_mean'] is None
    assert s['identity_ok'] is False


# ----------------------------------------------------- monitor end-to-end

def test_monitor_scores_every_pick_and_writes_summary(db):
    deep = _pick('DEEPX')                                   # resolved WIN
    open_pick = _pick('OPEN1', pid='OPEN1', status='OPEN',
                      resolution_time=None, exit_underlying=None)
    _seed_week(db, [deep, open_pick])
    for t in ('DEEPX', 'OPEN1'):
        _chain(db, '2026-09-14', t, [_row(CONTRACT['symbol'], 1.30, 1.80)])
        _chain(db, '2026-09-15', t, [_row(CONTRACT['symbol'], 1.25, 1.35)])
        _chain(db, '2026-09-16', t, [_row(CONTRACT['symbol'], 1.10, 1.20)])
    # OPEN1 is shallow by cost only in name; both picks share the deep
    # Monday mark here (1.30/2.00) — the summary math below uses that.
    monitor(db, _Provider(), CAL[-1]['close'] + 600)
    saved = _stored(db, 'DEEPX')
    assert saved['cohort_exit']['status'] == 'scored'
    assert saved['cohort_exit']['cohort'] == 'deep'
    assert saved['cohort_exit']['delta_net'] == 20.00
    assert _stored(db, 'OPEN1')['cohort_exit']['status'] == 'scored'
    with db.tx() as c:
        keys = list(c.execute(select(native_outbox.c.event_key)).scalars())
        summary = db.get(c, VERSION + ':week:' + WEEK + ':cohort_exit')
    assert not any('cohort' in k for k in keys)             # storage only
    assert summary['picks'] == 2 and summary['scored'] == 2
    assert summary['primary']['n'] == 2
    assert summary['primary']['delta_sum'] == 40.0
    assert summary['cohorts']['deep']['n'] == 2
    assert summary['identity_ok'] is True
    # Idempotent: a later pass does not rescore or move the summary.
    monitor(db, _Provider(), CAL[-1]['close'] + 1200)
    assert _stored(db, 'DEEPX')['cohort_exit']['scored_at'] == saved['cohort_exit']['scored_at']


# --------------------------------------- adopted scoring standard
# Josh 2026-11-12: from the Monday 2026-11-16 board forward, the
# week's headline total books each pick at its STANDARD net (deep at
# rule_net, all other scored picks at baseline_net); the flat
# Wednesday total remains stored as the comparator. Earlier boards
# keep the study-era summary and cell shapes exactly.

def _std_rows(week):
    return [{'week': week, 'cohort_exit': _cell('shallow', 18.68, 18.68)},
            {'week': week, 'cohort_exit': _cell('middle', -10.0, -10.0)},
            {'week': week, 'cohort_exit': _cell('deep', -91.32, -71.32)},
            {'week': week, 'cohort_exit': _cell('deep', -41.32, -71.32)},
            {'week': week, 'cohort_exit': _cell('unknown', 5.0, None)},
            {'week': week, 'cohort_exit': {'status': 'unavailable',
                                           'reason': 'wednesday_chain_missing'}},
            {'week': week}]


def test_standard_headline_is_sum_of_standard_nets():
    s = cohort_exit_summary(_std_rows(WEEK_STD), WEEK_STD)
    assert s['headline'] == 'standard'
    # deep at rule_net (-71.32 x2); shallow/middle/unknown at baseline.
    assert s['standard']['n'] == 5
    assert s['standard']['net_sum'] == -128.96
    assert s['standard']['net_mean'] == round(-128.96 / 5, 2)


def test_standard_comparator_is_the_flat_wednesday_total():
    s = cohort_exit_summary(_std_rows(WEEK_STD), WEEK_STD)
    # The comparator is the old flat standard over the same scored
    # population: every pick out at Wednesday's bid.
    assert s['standard']['baseline_sum'] == -118.96
    assert s['standard']['baseline_mean'] == round(-118.96 / 5, 2)
    # The frozen study figures are still produced, unchanged.
    assert s['primary']['baseline_sum'] == -123.96
    assert s['primary']['rule_sum'] == -133.96
    assert s['identity_ok'] is True


def test_standard_identity_only_the_deep_cohort_moves():
    s = cohort_exit_summary(_std_rows(WEEK_STD), WEEK_STD)
    st = s['standard']
    assert st['deep_delta_sum'] == -10.0          # +20 and -30
    assert abs((st['net_sum'] - st['baseline_sum']) - st['deep_delta_sum']) <= 0.005
    assert st['identity_ok'] is True


def test_pre_standard_week_summary_shape_unchanged():
    s = cohort_exit_summary(_std_rows(WEEK_PRE), WEEK_PRE)
    assert 'standard' not in s and 'headline' not in s
    assert set(s) == {'picks', 'scored', 'unavailable', 'primary',
                      'cohorts', 'unknown_cohort', 'identity_ok'}
    # And the study figures themselves are the same as ever.
    assert s['primary']['delta_sum'] == -10.0


def _chain_on(db, cal, date, ticker, contracts):
    close = next(s['close'] for s in cal if s['date'] == date)
    with db.tx() as c:
        db.append(c, 'chain_eod', 'massive', ticker, close + 600,
                  {'date': date, 'symbol': ticker, 'contracts': contracts},
                  key=chain_event_key(date, ticker))


def _score_std(db, p, now):
    with db.tx() as c:
        return cohort_exit_score(c, p, MON_STD, WED_STD, now)


def test_standard_cell_stamp_from_cutoff_week(db):
    deep = _pick('DEEP1', week=WEEK_STD)
    _chain_on(db, CAL_STD, MON_STD['date'], 'DEEP1',
              [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain_on(db, CAL_STD, WED_STD['date'], 'DEEP1',
              [_row(CONTRACT['symbol'], 1.10, 1.20)])
    cell = _score_std(db, deep, WED_STD['close'] + 600)
    assert cell['cohort'] == 'deep'
    assert cell['standard_net'] == cell['rule_net'] == -71.32
    assert cell['standard_exit'] == 'monday_close'
    shallow = _pick('SHAL1', week=WEEK_STD)
    _chain_on(db, CAL_STD, MON_STD['date'], 'SHAL1',
              [_row(CONTRACT['symbol'], 1.70, 1.80)])
    _chain_on(db, CAL_STD, WED_STD['date'], 'SHAL1',
              [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score_std(db, shallow, WED_STD['close'] + 600)
    assert cell['standard_net'] == cell['baseline_net'] == 18.68
    assert cell['standard_exit'] == 'wednesday_close'
    unknown = _pick('UNKN1', week=WEEK_STD)
    _chain_on(db, CAL_STD, WED_STD['date'], 'UNKN1',
              [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score_std(db, unknown, WED_STD['close'] + 600)
    assert cell['cohort'] == 'unknown' and cell['rule_net'] is None
    assert cell['standard_net'] == cell['baseline_net'] == 18.68
    assert cell['standard_exit'] == 'wednesday_close'


def test_no_standard_stamp_before_cutoff(db):
    p = _pick('PRE1', week=WEEK_PRE)
    cal = CAL_PRE
    mon, wed = cal[0], cal[2]
    _chain_on(db, cal, mon['date'], 'PRE1', [_row(CONTRACT['symbol'], 1.30, 1.80)])
    _chain_on(db, cal, wed['date'], 'PRE1', [_row(CONTRACT['symbol'], 1.10, 1.20)])
    with db.tx() as c:
        cell = cohort_exit_score(c, p, mon, wed, wed['close'] + 600)
    assert cell['status'] == 'scored' and cell['cohort'] == 'deep'
    assert 'standard_net' not in cell and 'standard_exit' not in cell


def _seed_week_on(db, week, cal, picks):
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + week,
               {'week': week, 'sessions': cal, 'state': 'complete'})
        for p in picks:
            save(db, c, p)


def test_monitor_writes_standard_headline_for_cutoff_week(db):
    picks = [_pick('DEEPX', week=WEEK_STD),
             _pick('OPEN1', pid='OPEN1', week=WEEK_STD, status='OPEN',
                   resolution_time=None, exit_underlying=None)]
    _seed_week_on(db, WEEK_STD, CAL_STD, picks)
    for t in ('DEEPX', 'OPEN1'):
        for s in CAL_STD[:3]:
            bid = 1.30 if s is CAL_STD[0] else 1.25 if s is CAL_STD[1] else 1.10
            _chain_on(db, CAL_STD, s['date'], t,
                      [_row(CONTRACT['symbol'], bid, bid + 0.10)])
    monitor(db, _Provider(), CAL_STD[-1]['close'] + 600)
    with db.tx() as c:
        summary = db.get(c, VERSION + ':week:' + WEEK_STD + ':cohort_exit')
    # Both picks deep (Mon mark 1.30/2.00): standard books the Monday
    # exit (-71.32 each); the comparator keeps Wednesday (-91.32 each).
    assert summary['headline'] == 'standard'
    assert summary['standard']['net_sum'] == -142.64
    assert summary['standard']['baseline_sum'] == -182.64
    assert summary['standard']['deep_delta_sum'] == 40.0
    assert summary['standard']['identity_ok'] is True
    assert _stored(db, 'DEEPX')['cohort_exit']['standard_net'] == -71.32


def test_monitor_pre_cutoff_week_has_no_standard_block(db):
    picks = [_pick('DEEPX', week=WEEK_PRE)]
    _seed_week_on(db, WEEK_PRE, CAL_PRE, picks)
    for s in CAL_PRE[:3]:
        bid = 1.30 if s is CAL_PRE[0] else 1.25 if s is CAL_PRE[1] else 1.10
        _chain_on(db, CAL_PRE, s['date'], 'DEEPX',
                  [_row(CONTRACT['symbol'], bid, bid + 0.10)])
    monitor(db, _Provider(), CAL_PRE[-1]['close'] + 600)
    with db.tx() as c:
        summary = db.get(c, VERSION + ':week:' + WEEK_PRE + ':cohort_exit')
    assert summary['primary']['delta_sum'] == 20.0
    assert 'standard' not in summary and 'headline' not in summary
    assert 'standard_net' not in _stored(db, 'DEEPX')['cohort_exit']
