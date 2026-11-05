"""Regression: the chain-contract symbol seam (defect repair 2026-11-04,
files/smoothers-wednesday-chain-gaps-2026-11-04.md).

Picks store the Alpaca bare OCC symbol (ARM261106C00207500); the
Massive-sourced chain_eod archive stores the same contract with an
'O:' prefix (O:ARM261106C00207500). The Wednesday-horizon scorer, the
Tuesday-entry cell and the cohort-exit rule all joined the two by
exact string, so on Massive archive days every lookup missed: all 13
still-open picks of the 2026-11-02 board stamped 'unavailable' on
2026-11-04 despite 11 of the contracts sitting in Wednesday's chain
with closing bids.

Pins:
- the exact failing case heals on all three pricing paths: a bare
  pick symbol finds the 'O:'-prefixed archive row and prices;
- matching stays strict: a different strike or a different expiry in
  the archive does NOT match (no fuzzy matching);
- a genuinely band-absent contract (COP/MU class: the day's ladder
  stops below the strike) still records unavailable — never a
  fabricated price;
- the reverse direction also works: a bare archive row (Alpaca-format
  day) matches the bare pick symbol.
"""
from sqlalchemy import select
from test_projects import db  # noqa: F401  (shared Store fixture)
from compass.native_outbox import outbox as native_outbox
from compass.native_smoothers import (VERSION, weekly, save, _occ_key,
                                      _chain_contract, horizon_score,
                                      tuesday_cell_score, cohort_exit_score,
                                      rescore_week_cells)
from compass.smoothers_handoff import sessions
from compass.smoothers_scorecard import FEE_PER_CONTRACT
from compass.chain_eod import event_key as chain_event_key

WEEK = '2026-09-14'
CAL = sessions(WEEK)
MON, TUE, WED = CAL[0], CAL[1], CAL[2]
AFTER_WED = WED['close'] + 3600

# The real 2026-11-04 ARM case from the diagnostic: bare pick symbol,
# Wednesday's Massive archive row 'O:ARM261106C00207500' at bid 3.90.
ARM_CONTRACT = {'symbol': 'ARM261106C00207500', 'multiplier': 100,
                'strike': 207.5, 'expiration': '2026-11-06', 'type': 'CALL'}


def _row(symbol, bid, ask):
    return {'symbol': symbol, 'quote': {'bid': bid, 'ask': ask}}


def _chain(db, date, ticker, contracts):
    close = next(s['close'] for s in CAL if s['date'] == date)
    with db.tx() as c:
        db.append(c, 'chain_eod', 'massive', ticker, close + 600,
                  {'date': date, 'symbol': ticker, 'contracts': contracts},
                  key=chain_event_key(date, ticker))


def _pick(ticker, contract, ask):
    return {'ticker': ticker, 'contract': dict(contract),
            'quote': {'ask': ask}}


# ------------------------------------------------------------- the key

def test_occ_key_canonicalizes_both_forms():
    assert _occ_key('O:ARM261106C00207500') == 'ARM261106C00207500'
    assert _occ_key('ARM261106C00207500') == 'ARM261106C00207500'
    assert _occ_key(None) is None
    assert _occ_key(207.5) is None


# ------------------------------------------------------- horizon path

def test_horizon_finds_prefixed_archive_row(db):
    """The exact failing case: ARM 207.5C, 2026-11-04. Diagnostic
    rescue arithmetic from the frozen source: bid 3.90, entry ask
    10.75 -> net (3.90-10.75)*100 - fee."""
    p = _pick('ARM', ARM_CONTRACT, 10.75)
    _chain(db, WED['date'], 'ARM', [_row('O:ARM261106C00207500', 3.90, 4.20)])
    with db.tx() as c:
        h = horizon_score(c, p, WED, AFTER_WED)
    assert h['status'] == 'scored'
    assert h['chain_bid'] == 3.90
    assert h['premium_net'] == round((3.90 - 10.75) * 100 - FEE_PER_CONTRACT, 2)


def test_horizon_finds_bare_archive_row(db):
    p = _pick('ARM', ARM_CONTRACT, 10.75)
    _chain(db, WED['date'], 'ARM', [_row('ARM261106C00207500', 3.90, 4.20)])
    with db.tx() as c:
        h = horizon_score(c, p, WED, AFTER_WED)
    assert h['status'] == 'scored'
    assert h['chain_bid'] == 3.90


def test_horizon_different_strike_does_not_match(db):
    """Strictness: the archive holds the same underlying/expiry at a
    different strike — that is not the pick's contract."""
    p = _pick('ARM', ARM_CONTRACT, 10.75)
    _chain(db, WED['date'], 'ARM', [_row('O:ARM261106C00210000', 3.10, 3.40)])
    with db.tx() as c:
        h = horizon_score(c, p, WED, AFTER_WED)
    assert h['status'] == 'unavailable'
    assert h['reason'] == 'contract_not_in_wednesday_chain'


def test_horizon_band_absent_stays_unavailable(db):
    """COP/MU class: the event exists but the day's ladder stops below
    the strike — genuinely absent, still unavailable, no fabrication."""
    cop = {'symbol': 'COP261106C00150000', 'multiplier': 100,
           'strike': 150, 'expiration': '2026-11-06', 'type': 'CALL'}
    p = _pick('COP', cop, 5.00)
    _chain(db, WED['date'], 'COP', [_row('O:COP261106C00149000', 0.10, 0.14),
                                    _row('O:COP261106C00148000', 0.16, 0.20)])
    with db.tx() as c:
        h = horizon_score(c, p, WED, AFTER_WED)
    assert h['status'] == 'unavailable'
    assert h['reason'] == 'contract_not_in_wednesday_chain'


# --------------------------------------------------- the shared seam

def test_chain_contract_expiry_mismatch_does_not_match(db):
    _chain(db, WED['date'], 'DEMO', [_row('O:DEMO260925C00102000', 1.0, 1.1)])
    with db.tx() as c:
        row, why = _chain_contract(c, WED['date'], 'DEMO',
                                   'DEMO260918C00102000')
    assert row is None
    assert why == 'contract_not_in_chain'


# --------------------------------------------------- Tuesday cell path

DEMO_CONTRACT = {'symbol': 'DEMO260918C00102000', 'multiplier': 100,
                 'strike': 102, 'expiration': '2026-09-18', 'type': 'CALL'}


def _seed_three_days(db, ticker, mon, tue, wed):
    _chain(db, MON['date'], ticker, [mon])
    _chain(db, TUE['date'], ticker, [tue])
    _chain(db, WED['date'], ticker, [wed])


def test_tuesday_cell_scores_through_prefixed_rows(db):
    p = _pick('DEMO', DEMO_CONTRACT, 2.0)
    sym = 'O:' + DEMO_CONTRACT['symbol']
    _seed_three_days(db, 'DEMO', _row(sym, 1.90, 2.00),
                     _row(sym, 1.55, 1.60), _row(sym, 1.20, 1.30))
    with db.tx() as c:
        cell = tuesday_cell_score(c, p, MON, TUE, WED, AFTER_WED)
    assert cell['status'] == 'scored'
    assert cell['monday_net'] == round((1.20 - 2.0) * 100 - FEE_PER_CONTRACT, 2)
    assert cell['tuesday_net'] == round((1.20 - 1.6) * 100 - FEE_PER_CONTRACT, 2)
    # The Monday mark also comes through the seam: 1.90/2.00 = shallow.
    assert cell['monday_mark_ratio'] == 0.95
    assert cell['cohort'] == 'shallow'


# -------------------------------------------------- cohort-exit path

def test_cohort_exit_scores_through_prefixed_rows(db):
    p = _pick('DEMO', DEMO_CONTRACT, 2.0)
    sym = 'O:' + DEMO_CONTRACT['symbol']
    # Monday mark deep (bid 1.00 / ask 2.00 = 0.50): the rule arm exits
    # at Monday's bid; baseline at Wednesday's.
    _chain(db, MON['date'], 'DEMO', [_row(sym, 1.00, 1.10)])
    _chain(db, WED['date'], 'DEMO', [_row(sym, 1.20, 1.30)])
    with db.tx() as c:
        cell = cohort_exit_score(c, p, MON, WED, AFTER_WED)
    assert cell['status'] == 'scored'
    assert cell['cohort'] == 'deep'
    assert cell['mark_reason'] is None
    assert cell['baseline_net'] == round((1.20 - 2.0) * 100 - FEE_PER_CONTRACT, 2)
    assert cell['rule_net'] == round((1.00 - 2.0) * 100 - FEE_PER_CONTRACT, 2)
    assert cell['rule_exit'] == 'monday_close'


# ---------------------------------------------------- week-1 re-score
#
# The three cells skip already-stamped picks, so the seam's wholesale
# 'unavailable' stamps are terminal until deliberately cleared. The
# re-score clears ONLY chain-derived unavailable stamps and re-issues
# them under the frozen rules; everything else stands.

def _board_pick(pid, ticker, status, contract, ask, **over):
    p = {'id': pid, 'week': WEEK, 'ticker': ticker, 'status': status,
         'direction': 'CALL', 'entry_price': 100.0, 'target_price': 102.0,
         'model_entry_time': MON['open'] + 3600,
         'created_at': MON['open'] + 3600,
         'contract': dict(contract),
         'quote': {'status': 'available', 'bid': ask - 0.1, 'ask': ask,
                   'midpoint': ask - 0.05,
                   'quote_at_ms': int((MON['open'] + 3600) * 1000)},
         'cost_key': 10.0}
    p.update(over)
    return p


def _unavail(reason):
    return {'status': 'unavailable', 'reason': reason, 'scored_at': 1.0}


def test_rescore_week_cells_heals_only_chain_unavailable(db):
    a = _board_pick('A', 'ARM', 'OPEN', ARM_CONTRACT, 10.75,
                    horizon=_unavail('contract_not_in_wednesday_chain'),
                    tuesday_cell=_unavail('wednesday_contract_not_in_chain'),
                    cohort_exit=_unavail('wednesday_contract_not_in_chain'))
    b = _board_pick('B', 'DEMO', 'WIN', DEMO_CONTRACT, 2.0,
                    tuesday_cell=_unavail('tuesday_contract_not_in_chain'),
                    cohort_exit=_unavail('entry_ask_missing'))
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + WEEK,
               {'week': WEEK, 'sessions': CAL, 'state': 'complete'})
        for p in (a, b):
            save(db, c, p)
    arm = 'O:' + ARM_CONTRACT['symbol']
    _chain(db, MON['date'], 'ARM', [_row(arm, 9.00, 9.20)])
    _chain(db, TUE['date'], 'ARM', [_row(arm, 5.00, 5.20)])
    _chain(db, WED['date'], 'ARM', [_row(arm, 3.90, 4.20)])
    sym = 'O:' + DEMO_CONTRACT['symbol']
    _seed_three_days(db, 'DEMO', _row(sym, 1.90, 2.00),
                     _row(sym, 1.55, 1.60), _row(sym, 1.20, 1.30))

    report = rescore_week_cells(db, WEEK, AFTER_WED)

    assert report['ok'] is True
    assert report['cells']['horizon']['cleared'] == 1
    assert report['cells']['horizon']['after'] == {'scored': 1, 'unstamped': 1}
    assert report['cells']['tuesday_cell']['cleared'] == 2
    assert report['cells']['tuesday_cell']['after'] == {'scored': 2}
    assert report['cells']['cohort_exit']['cleared'] == 1
    assert report['cells']['cohort_exit']['still_unavailable'] == [
        {'ticker': 'DEMO', 'reason': 'entry_ask_missing'}]

    with db.tx() as c:
        rows = {p['id']: p for p in c.execute(
            select(weekly.c.payload).where(weekly.c.week == WEEK)).scalars()}
        tue_sum = db.get(c, VERSION + ':week:' + WEEK + ':tuesday_cell', {})
        coh_sum = db.get(c, VERSION + ':week:' + WEEK + ':cohort_exit', {})
        queued = c.execute(select(native_outbox.c.id)).scalars().all()
    # ARM horizon re-issued from the archived Wednesday bid, silently.
    assert rows['A']['horizon']['status'] == 'scored'
    assert rows['A']['horizon']['premium_net'] == \
        round((3.90 - 10.75) * 100 - FEE_PER_CONTRACT, 2)
    assert rows['A']['tuesday_cell']['status'] == 'scored'
    assert rows['A']['cohort_exit']['status'] == 'scored'
    assert rows['B']['tuesday_cell']['status'] == 'scored'
    # The non-chain unavailable stamp is not the seam's: untouched.
    assert rows['B']['cohort_exit']['reason'] == 'entry_ask_missing'
    # Summaries refreshed from the corrected cells; nothing queued.
    assert tue_sum['scored'] == 2
    assert coh_sum['identity_ok'] is True
    assert queued == []

    # Idempotent: a second pass clears nothing.
    again = rescore_week_cells(db, WEEK, AFTER_WED)
    assert all(again['cells'][f]['cleared'] == 0
               for f in ('horizon', 'tuesday_cell', 'cohort_exit'))
