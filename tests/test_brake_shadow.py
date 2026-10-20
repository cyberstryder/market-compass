"""Brake shadow logging (2026-10-20): candidate futures brakes are
measured, never enforced.

Covers strike counting (08:30 CT anchor scope, win resets the streak,
data_gap never counts), correlation-group state, the submit() hook
(a shadow record is written and the trade still submits), and outcome
scoring in summarize().
"""
from datetime import datetime, timezone
import pytest
from compass.store import Store
from compass.config import Config
from compass import brake_shadow, ict_paper

# Mon 2026-09-28 12:00 CT = 17:00 UTC. 08:30 CT anchor = 13:30 UTC.
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()
H = 3600.0


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'brake.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.ict_golden_zone = True
    return c


def seed_closed(db, detector, exited_at, pnl, data_gap=False, n=0):
    tid = 'ict-%s-MES-c-0-%d-t1' % (detector, int(exited_at) + n)
    trade = {'id': tid, 'detector': detector, 'symbol': 'MES.c.0',
             'side': 'long', 'status': 'closed', 'pnl': pnl,
             'exited_at': exited_at, 'exit_reason': 'stop'}
    if data_gap:
        trade['data_gap'] = True
    with db.tx() as c:
        db.put(c, 'trade:' + tid, trade)
    return tid


def seed_open(db, detector, symbol, side, entry=100.0, stop=98.0):
    tid = 'ict-%s-%s-open-t1' % (detector, symbol.replace('.', '-'))
    pos = {'id': tid, 'detector': detector, 'symbol': symbol,
           'side': side, 'status': 'open', 'entry': entry, 'stop': stop}
    with db.tx() as c:
        db.put(c, 'position:ict:%s:%s:t1' % (detector, symbol), pos)
    return tid


# ---------- strike_state ----------

def test_three_losses_since_anchor_blocks(db):
    for i in range(3):
        seed_closed(db, 'aoi_zones', NOW - (3 - i) * H, -50.0, n=i)
    with db.tx() as c:
        st = brake_shadow.strike_state(db, c, 'aoi_zones', NOW)
    assert st['strikes'] == 3 and st['would_block'] is True


def test_win_breaks_the_streak(db):
    seed_closed(db, 'aoi_zones', NOW - 4 * H, -50.0)
    seed_closed(db, 'aoi_zones', NOW - 3 * H, -50.0)
    seed_closed(db, 'aoi_zones', NOW - 2 * H, 80.0)
    seed_closed(db, 'aoi_zones', NOW - 1 * H, -50.0)
    with db.tx() as c:
        st = brake_shadow.strike_state(db, c, 'aoi_zones', NOW)
    assert st['strikes'] == 1 and st['would_block'] is False


def test_overnight_losses_before_anchor_do_not_count(db):
    # Three losses at 05:00-07:00 CT (before the 08:30 anchor) must not
    # bench the detector for the NY session (Josh's overnight rule).
    for i in range(3):
        seed_closed(db, 'aoi_zones', NOW - (10 + i) * H, -50.0, n=i)
    with db.tx() as c:
        st = brake_shadow.strike_state(db, c, 'aoi_zones', NOW)
    assert st['strikes'] == 0 and st['would_block'] is False


def test_data_gap_loss_never_counts(db):
    for i in range(3):
        seed_closed(db, 'aoi_zones', NOW - (3 - i) * H, -50.0,
                    data_gap=True, n=i)
    with db.tx() as c:
        st = brake_shadow.strike_state(db, c, 'aoi_zones', NOW)
    assert st['strikes'] == 0 and st['would_block'] is False


# ---------- corr_state ----------

def test_corr_blocks_same_group_same_direction(db):
    seed_open(db, 'aoi_zones', 'MES.c.0', 'long')
    with db.tx() as c:
        st = brake_shadow.corr_state(db, c, 'MNQ.c.0', 'long')
    assert st['would_block'] is True and st['group'] == 'EQUITY'
    assert st['blockers'][0]['symbol'] == 'MES.c.0'


def test_corr_allows_opposite_direction_and_other_groups(db):
    seed_open(db, 'aoi_zones', 'MES.c.0', 'long')
    with db.tx() as c:
        opp = brake_shadow.corr_state(db, c, 'MNQ.c.0', 'short')
        other = brake_shadow.corr_state(db, c, 'MCL.v.0', 'long')
    assert opp['would_block'] is False
    assert other['would_block'] is False and other['group'] is None


# ---------- submit() hook ----------

def test_submit_writes_shadow_record_and_still_trades(db, cfg):
    seed_open(db, 'aoi_zones', 'MES.c.0', 'long')
    sig = {'direction': 'long', 'entry': 100.0, 'stop': 98.0,
           'target': 104.0, 'rr': 2.0, 'signal_ts': NOW - 60,
           'level_kind': 'test'}
    with db.tx() as c:
        res = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', 'NQ.c.0', sig)
    assert res['submitted'] is True  # shadow never blocks
    with db.tx() as c:
        recs = db.prefix(c, 'brake_shadow:')
    assert len(recs) == 1
    rec = next(iter(recs.values()))
    assert rec['detector'] == 'golden_zone'
    assert rec['corr_cap1']['would_block'] is False  # NQ not in a group
    assert rec['strike3_0830']['strikes'] == 0
    assert 'open_marks' in rec and 'vwap' in rec
    assert rec['trade_id'] == res['legs'][0]


def test_shadow_disabled_by_config(db, cfg):
    cfg.ict_brake_shadow = False
    sig = {'direction': 'long', 'entry': 100.0, 'stop': 98.0,
           'target': 104.0, 'rr': 2.0, 'signal_ts': NOW - 60,
           'level_kind': 'test'}
    with db.tx() as c:
        res = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', 'NQ.c.0', sig)
    assert res['submitted'] is True
    with db.tx() as c:
        assert db.prefix(c, 'brake_shadow:') == {}


# ---------- summarize ----------

def test_summarize_scores_would_blocks(db):
    loser = seed_closed(db, 'aoi_zones', NOW - H, -120.0)
    winner = seed_closed(db, 'bos_fvg', NOW - H, 90.0)
    recs = [
        {'ts': NOW - 2 * H, 'detector': 'aoi_zones', 'trade_id': loser,
         'strike3_0830': {'would_block': True},
         'corr_cap1': {'would_block': False}},
        {'ts': NOW - 2 * H, 'detector': 'bos_fvg', 'trade_id': winner,
         'strike3_0830': {'would_block': False},
         'corr_cap1': {'would_block': True}},
    ]
    with db.tx() as c:
        for r in recs:
            db.put(c, 'brake_shadow:' + r['trade_id'], r)
        out = brake_shadow.summarize(db, c, NOW - 86400)
    assert out['records'] == 2 and out['trades_closed'] == 2
    assert out['baseline_pnl'] == -30.0
    assert out['strike3_0830']['losses_avoided'] == 120.0
    assert out['strike3_0830']['net'] == 120.0
    assert out['corr_cap1']['wins_missed'] == 90.0
    assert out['corr_cap1']['net'] == -90.0
