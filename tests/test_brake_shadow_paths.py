"""Bar-path counterfactuals in brake_shadow.summarize (2026-10-20).

Breakeven-after-+1R and the first-bar delayed-send check are scored by
replaying each closed trade's stored bars. Long MES geometry in most
tests: entry 100, stop 98 (risk 2), target 104 (target_r 2.0). Bars are
[ts, o, h, l, c, v] one-minute rows.
"""
from datetime import datetime, timezone
import pytest
from compass.store import Store
from compass import brake_shadow

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'brake.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def seed_pathed(db, tid, side='long', entry=100.0, stop=98.0, target=104.0,
                t0=NOW, span=180, pnl=-40.0, bars=(), data_gap=False):
    trade = {'id': tid, 'detector': 'bos_fvg', 'symbol': 'MES.c.0',
             'side': side, 'status': 'closed', 'pnl': pnl,
             'entry': entry, 'stop': stop, 'target': target,
             'entered_at': t0, 'exited_at': t0 + span,
             'exit_reason': 'target' if pnl > 0 else 'stop'}
    if data_gap:
        trade['data_gap'] = True
    with db.tx() as c:
        db.put(c, 'trade:' + tid, trade)
        db.put(c, 'brake_shadow:' + tid,
               {'ts': t0, 'detector': 'bos_fvg', 'trade_id': tid,
                'strike3_0830': {'would_block': False},
                'corr_cap1': {'would_block': False}})
        if bars:
            db.put(c, 'bar_window:MES.c.0', [list(b) for b in bars])
    return tid


def summarize(db):
    with db.tx() as c:
        return brake_shadow.summarize(db, c, NOW - 86400)


def test_breakeven_saves_armed_loser(db):
    bars = [
        [NOW, 100, 102.5, 99.5, 102, 10],        # runs +1.25R -> armed
        [NOW + 60, 101, 101.5, 99.8, 100.2, 10],  # back through entry
        [NOW + 120, 100, 100.4, 97.9, 98.2, 10],  # on to the real stop
    ]
    seed_pathed(db, 't-be-save', pnl=-40.0, bars=bars)
    out = summarize(db)
    be = out['breakeven']
    assert be['evaluated'] == 1 and be['armed'] == 1
    assert be['exits_at_zero'] == 1 and be['saved'] == 40.0
    assert be['cost'] == 0.0 and be['net'] == 40.0
    fb = out['first_bar']
    assert fb['evaluated'] == 1 and fb['would_skip'] == 0


def test_breakeven_costs_retracing_winner(db):
    bars = [
        [NOW, 100, 102.5, 99.5, 102, 10],         # armed
        [NOW + 60, 101, 101.0, 99.9, 100.1, 10],  # retraces to entry
        [NOW + 120, 100.5, 104.2, 100.4, 104, 10],  # then target
    ]
    seed_pathed(db, 't-be-cost', pnl=80.0, bars=bars)
    be = summarize(db)['breakeven']
    assert be['exits_at_zero'] == 1 and be['cost'] == 80.0
    assert be['saved'] == 0.0 and be['net'] == -80.0


def test_breakeven_leaves_clean_winner_alone(db):
    bars = [
        [NOW, 100, 102.5, 101.0, 102, 10],       # armed, no retrace
        [NOW + 60, 102, 104.2, 101.5, 104, 10],  # target
    ]
    seed_pathed(db, 't-be-clean', pnl=80.0, bars=bars)
    out = summarize(db)
    be = out['breakeven']
    assert be['armed'] == 1 and be['exits_at_zero'] == 0
    assert be['saved'] == 0.0 and be['cost'] == 0.0
    fb = out['first_bar']  # target done by the first recheck bar
    assert fb['would_skip'] == 1 and fb['skipped_pnl'] == 80.0
    assert fb['net'] == -80.0


def test_loser_that_never_arms_gets_no_save_but_first_bar_flags_it(db):
    bars = [
        [NOW, 100, 100.5, 98.5, 99, 10],         # fav only +0.25R
        [NOW + 60, 99, 99.2, 97.9, 98.1, 10],    # stop reached
    ]
    seed_pathed(db, 't-no-arm', pnl=-40.0, bars=bars)
    out = summarize(db)
    be = out['breakeven']
    assert be['armed'] == 0 and be['exits_at_zero'] == 0
    assert be['saved'] == 0.0
    fb = out['first_bar']
    assert fb['would_skip'] == 1 and fb['skipped_pnl'] == -40.0
    assert fb['net'] == 40.0


def test_short_side_breakeven_uses_direction_aware_extremes(db):
    bars = [
        [NOW, 100, 100.5, 97.5, 98, 10],         # short: low 97.5 = +1.25R
        [NOW + 60, 99, 100.2, 98.5, 99.8, 10],   # high back through entry
        [NOW + 120, 100, 102.1, 99.6, 101.8, 10],
    ]
    seed_pathed(db, 't-short', side='short', entry=100.0, stop=102.0,
                target=96.0, pnl=-40.0, bars=bars)
    be = summarize(db)['breakeven']
    assert be['armed'] == 1 and be['exits_at_zero'] == 1
    assert be['saved'] == 40.0


def test_data_gap_trade_is_excluded_from_path_scoring(db):
    bars = [
        [NOW, 100, 102.5, 99.5, 102, 10],
        [NOW + 60, 101, 101.5, 99.8, 100.2, 10],
    ]
    seed_pathed(db, 't-gap', pnl=-40.0, bars=bars, data_gap=True)
    out = summarize(db)
    assert out['breakeven']['evaluated'] == 0
    assert out['breakeven']['unevaluable'] == 0
    assert out['first_bar']['evaluated'] == 0


def test_missing_bars_score_unevaluable_not_guessed(db):
    seed_pathed(db, 't-nobars', pnl=-40.0, bars=())
    out = summarize(db)
    assert out['breakeven']['evaluated'] == 0
    assert out['breakeven']['unevaluable'] == 1
    assert out['first_bar']['unevaluable'] == 1
