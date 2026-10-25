"""Live-combo scorecard (2026-10-24, futures plan phase 1): trailing
28-day per-combo paper stats for the post-prune live book, with the
PF < 1.0 kill-rule watch flagged. Counting follows the headline rules:
closed ICT paper trades only, data_gap excluded.
"""
from datetime import datetime, timezone
import pytest
from compass.store import Store
from compass import live_combo_scorecard as sc

NOW = datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc).timestamp()
D = 86400.0


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'scorecard.db'))
    s.initialize()
    yield s
    s.engine.dispose()


_seed_n = [0]


def seed(db, detector, symbol, pnl, exited_at, **kw):
    _seed_n[0] += 1
    tid = 'ict-%s-%s-%d-%d' % (detector, symbol.replace('.', '-'),
                               int(exited_at), _seed_n[0])
    trade = {'id': tid, 'detector': detector, 'symbol': symbol,
             'source': 'ict_paper', 'status': 'closed', 'pnl': pnl,
             'exited_at': exited_at}
    trade.update(kw)
    with db.tx() as c:
        db.put(c, 'trade:' + tid, trade)
    return tid


def combo(out, detector, root):
    return next(x for x in out['combos']
                if x['detector'] == detector and x['symbol'] == root)


# ---------- pure aggregation ----------

def test_combo_stats_pf_and_win_rate():
    trades = [{'pnl': 300.0}, {'pnl': 100.0}, {'pnl': -150.0}]
    s = sc.combo_stats(trades)
    assert s['trades'] == 3 and s['wins'] == 2 and s['losses'] == 1
    assert s['win_rate'] == pytest.approx(2 / 3, abs=1e-4)
    assert s['pf'] == pytest.approx(400.0 / 150.0, abs=1e-3)
    assert s['net'] == pytest.approx(250.0)
    assert s['kill_watch'] is False


def test_combo_stats_kill_flag_below_one():
    s = sc.combo_stats([{'pnl': 50.0}, {'pnl': -120.0}])
    assert s['pf'] == pytest.approx(50.0 / 120.0, abs=1e-3)
    assert s['kill_watch'] is True


def test_combo_stats_no_losses_pf_none_never_flagged():
    s = sc.combo_stats([{'pnl': 75.0}, {'pnl': 25.0}])
    assert s['pf'] is None and s['kill_watch'] is False
    empty = sc.combo_stats([])
    assert empty['trades'] == 0 and empty['pf'] is None
    assert empty['kill_watch'] is False


def test_combo_stats_scratch_counts_as_trade_only():
    s = sc.combo_stats([{'pnl': 0.0}, {'pnl': -10.0}])
    assert s['trades'] == 2 and s['wins'] == 0 and s['losses'] == 1
    assert s['pf'] == 0.0 and s['kill_watch'] is True


# ---------- summarize over the ledger ----------

def test_summarize_groups_by_combo_and_flags(db):
    seed(db, 'aoi_fade', 'MCL.v.0', 300.0, NOW - 2 * D)
    seed(db, 'aoi_fade', 'MCL.v.0', -100.0, NOW - 1 * D)
    seed(db, 'turtle_soup', 'MCL.v.0', 50.0, NOW - 3 * D)
    seed(db, 'turtle_soup', 'MCL.v.0', -120.0, NOW - 1 * D)
    seed(db, 'golden_zone', 'MNQ.c.0', 80.0, NOW - 5 * D)
    with db.tx() as c:
        out = sc.summarize(db, c, NOW)
    af = combo(out, 'aoi_fade', 'MCL')
    assert af['trades'] == 2 and af['pf'] == pytest.approx(3.0)
    assert af['kill_watch'] is False
    ts = combo(out, 'turtle_soup', 'MCL')
    assert ts['kill_watch'] is True
    assert out['kill_watch'] == ['turtle_soup|MCL']
    gz_mnq = combo(out, 'golden_zone', 'MNQ')
    assert gz_mnq['trades'] == 1 and gz_mnq['net'] == pytest.approx(80.0)
    # A combo with no trades in the window is present but unflagged.
    rb = combo(out, 'rumers_box', 'MYM')
    assert rb['trades'] == 0 and rb['kill_watch'] is False


def test_summarize_includes_aoi_fade_mes(db):
    # 2026-10-25: AOI Fade|MES joined the tracked combos when MES was
    # activated (Josh) — its live record must be scorecard-visible.
    seed(db, 'aoi_fade', 'MES.c.0', 200.0, NOW - 1 * D)
    seed(db, 'aoi_fade', 'MES.c.0', -50.0, NOW - 2 * D)
    with db.tx() as c:
        out = sc.summarize(db, c, NOW)
    mes = combo(out, 'aoi_fade', 'MES')
    assert mes['trades'] == 2 and mes['net'] == pytest.approx(150.0)
    assert len(out['combos']) == 6


def test_summarize_window_and_exclusions(db):
    seed(db, 'aoi_fade', 'MCL.v.0', 500.0, NOW - 29 * D)      # too old
    seed(db, 'aoi_fade', 'MCL.v.0', 500.0, NOW - 27 * D)      # in window
    seed(db, 'aoi_fade', 'MCL.v.0', -999.0, NOW - 1 * D,
         data_gap=True)                                        # data gap
    seed(db, 'aoi_fade', 'MCL.v.0', -999.0, NOW - 1 * D,
         status='open')                                        # not closed
    seed(db, 'aoi_fade', 'MCL.v.0', -999.0, NOW - 1 * D,
         source='legacy')                                      # not ICT
    seed(db, 'bos_fvg', 'MNQ.c.0', -999.0, NOW - 1 * D)       # not live
    with db.tx() as c:
        out = sc.summarize(db, c, NOW)
    af = combo(out, 'aoi_fade', 'MCL')
    assert af['trades'] == 1 and af['net'] == pytest.approx(500.0)
    assert len(out['combos']) == len(sc.LIVE_COMBOS)
