"""Smoothers Tuesday-entry shadow cell (Josh-approved, 2026-11-02).

Every Monday-board pick is ALSO scored as if entered Tuesday: the
Tuesday EOD chain-archive ask in, the Wednesday snapshot bid out —
the identical ruler to the Monday arm (Monday entry ask in, Wednesday
bid out, same $1.32 fee). The cell exists because the graded exit-day
record (259 picks) showed shallow-underwater picks (Monday mark >=
80% of ask) peak on Tuesday while deep marks (<70%) decay after
Tuesday; the cell measures whether a Tuesday entry beats the Monday
entry per pick and in aggregate, split by that Monday-mark cohort and
by the board's executable-cost halves.

Pins:
- both arms priced from the archive, never fabricated: a missing
  snapshot inside its retry grace defers (None); past grace, a
  missing contract / ask / bid records an explicit unavailable reason;
- cohort boundaries: >=0.80 shallow, <0.70 deep, between them middle,
  unreadable mark 'unknown' (and the arms still score);
- the cell covers EVERY board pick, including one that already
  resolved WIN on its underlying target (the Monday arm's horizon
  pass only covers still-open picks — the premium path is in the
  archive either way);
- storage only: no outbox notice is queued for a cell;
- the weekly summary aggregates full population, cohorts, and the
  cost-key median halves.
"""
from sqlalchemy import select
from test_projects import db  # noqa: F401  (shared Store fixture)
from compass.native_outbox import outbox as native_outbox
from compass.native_smoothers import (VERSION, weekly, save, monitor,
                                      tuesday_session, mark_cohort,
                                      tuesday_cell_score, tuesday_cell_summary)
from compass.smoothers_handoff import sessions
from compass.chain_eod import event_key as chain_event_key

WEEK = '2026-09-14'
CAL = sessions(WEEK)
MON, TUE, WED = CAL[0], CAL[1], CAL[2]
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
        return tuesday_cell_score(c, p, MON, TUE, WED, now)


class _Provider:
    def quote(self, contract):
        return dict(status='available', bid=0.4, ask=0.5,
                    quote_at_ms=int((WED['close'] + 300) * 1000))


# ------------------------------------------------------------ sessions

def test_tuesday_session_picks_the_tuesday():
    assert tuesday_session(CAL)['date'] == '2026-09-15'
    assert tuesday_session([]) is None
    assert tuesday_session(CAL[:1]) is None


# -------------------------------------------------------------- cohorts

def test_mark_cohort_boundaries():
    assert mark_cohort(0.85) == 'shallow'
    assert mark_cohort(0.80) == 'shallow'    # boundary belongs to shallow
    assert mark_cohort(0.7999) == 'middle'
    assert mark_cohort(0.70) == 'middle'     # boundary belongs to middle
    assert mark_cohort(0.6999) == 'deep'
    assert mark_cohort(0.50) == 'deep'
    assert mark_cohort(None) == 'unknown'


# ---------------------------------------------------------------- arms

def test_cell_scores_both_arms_and_shallow_cohort(db):
    p = _pick()
    _chain(db, '2026-09-14', 'DEMO', [_row(CONTRACT['symbol'], 1.70, 1.80)])
    _chain(db, '2026-09-15', 'DEMO', [_row(CONTRACT['symbol'], 1.75, 1.85)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'scored'
    # Monday arm: (2.20 - 2.00) * 100 - 1.32; Tuesday arm: (2.20 - 1.85) * 100 - 1.32
    assert cell['monday_net'] == 18.68
    assert cell['tuesday_net'] == 33.68
    assert cell['delta_net'] == 15.00
    assert cell['monday_mark_ratio'] == 0.85  # 1.70 / 2.00
    assert cell['cohort'] == 'shallow'
    assert cell['fee'] == 1.32


def test_cell_deep_and_middle_cohorts_from_monday_mark(db):
    # Distinct tickers per case: the archive dedupes on (date, ticker),
    # so a second Monday snapshot for the same ticker would be a no-op.
    for ticker, bid, cohort in (('DEEP1', 1.30, 'deep'), ('MID1', 1.50, 'middle')):
        p = _pick(ticker)
        _chain(db, '2026-09-14', ticker, [_row(CONTRACT['symbol'], bid, 1.80)])
        _chain(db, '2026-09-15', ticker, [_row(CONTRACT['symbol'], 1.75, 1.85)])
        _chain(db, '2026-09-16', ticker, [_row(CONTRACT['symbol'], 2.20, 2.30)])
        cell = _score(db, p, WED['close'] + 600)
        assert cell['cohort'] == cohort


def test_cell_missing_monday_mark_still_scores_cohort_unknown(db):
    p = _pick()
    _chain(db, '2026-09-15', 'DEMO', [_row(CONTRACT['symbol'], 1.75, 1.85)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    # Monday's snapshot is past its grace by the scoring pass; its
    # absence must not block the arms.
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'scored'
    assert cell['cohort'] == 'unknown'
    assert cell['monday_mark_ratio'] is None
    assert cell['mark_reason'] == 'monday_chain_missing'


# ------------------------------------------------------ missing snapshots

def test_cell_missing_tuesday_snapshot_defers_inside_grace(db):
    p = _pick()
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    assert _score(db, p, WED['close'] + 600) is None


def test_cell_missing_tuesday_snapshot_unavailable_past_grace(db):
    p = _pick()
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, TUE['close'] + 36 * 3600 + 60)
    assert cell['status'] == 'unavailable'
    assert cell['reason'] == 'tuesday_chain_missing'


def test_cell_contract_absent_from_tuesday_chain(db):
    p = _pick()
    _chain(db, '2026-09-15', 'DEMO', [_row('OTHER261016C00100000', 1.0, 1.1)])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'unavailable'
    assert cell['reason'] == 'tuesday_contract_not_in_chain'
    assert 'tuesday_net' not in cell


def test_cell_tuesday_ask_missing(db):
    p = _pick()
    _chain(db, '2026-09-15', 'DEMO',
           [{'symbol': CONTRACT['symbol'], 'quote': {'bid': 1.75}}])
    _chain(db, '2026-09-16', 'DEMO', [_row(CONTRACT['symbol'], 2.20, 2.30)])
    cell = _score(db, p, WED['close'] + 600)
    assert cell['status'] == 'unavailable'
    assert cell['reason'] == 'tuesday_ask_missing'


def test_cell_direct_unavailable_paths(db):
    now = WED['close'] + 600
    cell = _score(db, _pick(contract=None), now)
    assert cell['status'] == 'unavailable' and cell['reason'] == 'no_contract_recorded'
    cell = _score(db, _pick(quote=None), now)
    assert cell['status'] == 'unavailable' and cell['reason'] == 'entry_ask_missing'
    bad = dict(CONTRACT, multiplier=10)
    cell = _score(db, _pick(contract=bad), now)
    assert cell['status'] == 'unavailable' and cell['reason'] == 'unsupported_contract_multiplier'


# ----------------------------------------------------- monitor end-to-end

def test_monitor_scores_resolved_pick_and_writes_summary(db):
    resolved = _pick('DEMO', cost_key=5.0)
    open_pick = _pick('OPEN1', pid='OPEN1', status='OPEN', cost_key=15.0,
                      resolution_time=None, exit_underlying=None)
    _seed_week(db, [resolved, open_pick])
    for t in ('DEMO', 'OPEN1'):
        _chain(db, '2026-09-14', t, [_row(CONTRACT['symbol'], 1.70, 1.80)])
        _chain(db, '2026-09-15', t, [_row(CONTRACT['symbol'], 1.75, 1.85)])
        _chain(db, '2026-09-16', t, [_row(CONTRACT['symbol'], 2.20, 2.30)])
    monitor(db, _Provider(), CAL[-1]['close'] + 600)
    # The resolved pick has no Monday-arm horizon (that pass is
    # still-open only) but DOES have a Tuesday cell.
    saved = _stored(db, 'DEMO')
    assert 'horizon' not in saved
    assert saved['tuesday_cell']['status'] == 'scored'
    assert saved['tuesday_cell']['delta_net'] == 15.00
    assert _stored(db, 'OPEN1')['tuesday_cell']['status'] == 'scored'
    # Storage only: no cell notice in the outbox.
    with db.tx() as c:
        keys = list(c.execute(select(native_outbox.c.event_key)).scalars())
        summary = db.get(c, VERSION + ':week:' + WEEK + ':tuesday_cell')
    assert not any('tuesday' in k for k in keys)
    assert summary['picks'] == 2 and summary['scored'] == 2
    assert summary['all']['monday_net'] == 37.36
    assert summary['all']['tuesday_net'] == 67.36
    assert summary['cohorts']['shallow']['n'] == 2
    assert summary['cost_halves']['cheap']['n'] == 1
    assert summary['cost_halves']['dear']['n'] == 1
    # Idempotent: a later pass does not rescore or move the summary.
    monitor(db, _Provider(), CAL[-1]['close'] + 1200)
    assert _stored(db, 'DEMO')['tuesday_cell']['scored_at'] == saved['tuesday_cell']['scored_at']


# ------------------------------------------------------------- summary

def test_summary_splits_and_unavailable_count():
    def cell(cohort, cost, mon, tue):
        return {'status': 'scored', 'cohort': cohort, 'cost_key': cost,
                'monday_net': mon, 'tuesday_net': tue, 'delta_net': round(tue - mon, 2)}
    rows = [
        {'tuesday_cell': cell('shallow', 1.0, 10.0, 12.0)},
        {'tuesday_cell': cell('shallow', 2.0, -5.0, -1.0)},
        {'tuesday_cell': cell('deep', 3.0, 20.0, 4.0)},
        {'tuesday_cell': cell('middle', 4.0, 0.0, 0.0)},
        {'tuesday_cell': {'status': 'unavailable', 'reason': 'tuesday_chain_missing'}},
        {},
    ]
    s = tuesday_cell_summary(rows)
    assert s['picks'] == 6 and s['scored'] == 4 and s['unavailable'] == 2
    assert s['all'] == {'n': 4, 'monday_net': 25.0, 'tuesday_net': 15.0, 'delta_net': -10.0}
    assert s['cohorts']['shallow'] == {'n': 2, 'monday_net': 5.0, 'tuesday_net': 11.0, 'delta_net': 6.0}
    assert s['cohorts']['deep']['n'] == 1 and s['cohorts']['unknown']['n'] == 0
    assert s['cost_median'] == 2.5
    assert s['cost_halves']['cheap']['n'] == 2   # keys 1.0, 2.0 (<= median)
    assert s['cost_halves']['dear']['n'] == 2    # keys 3.0, 4.0
