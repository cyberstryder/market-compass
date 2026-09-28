"""ICT paper-trade accounting: gating, submission, fills, exits, P&L,
and per-detector attribution.

Covers the signal -> paper-submission path for every detector wired into
ict_paper, plus stop-out/target-win accounting through the engine's exits()
loop. The dedicated ICT_FUTURES_PAPER_ENABLED gate is independent of the
global PAPER_TRADING_ENABLED (left off here on purpose).
"""
from datetime import datetime, timezone
import pytest
from compass.store import Store
from compass.config import Config
from compass.engine import Engine
from compass import ict_paper

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT
SYM = 'NQ.c.0'
POS = 'position:ict:golden_zone:' + SYM


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'ictpaper.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)  # paper_trading stays False
    c.ict_futures_paper = True
    c.ict_golden_zone = True
    return c


def sig(**kw):
    base = {'direction': 'long', 'entry': 100.0, 'stop': 98.0,
            'target': 104.0, 'rr': 2.0, 'signal_ts': NOW - 60,
            'level_kind': 'test'}
    base.update(kw)
    return base


def quote(ts, bid, ask):
    return {'ts': ts, 'bid': bid, 'ask': ask, 'bid_size': 10,
            'ask_size': 10, 'source': 'fixture'}


def open_position(db, cfg, detector='golden_zone', **kw):
    e = Engine(db, cfg)
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, detector, SYM, sig(**kw))
        assert r['submitted'], r
    return e


def exit_with(e, db, now, q, pos_key=POS):
    with db.tx() as c:
        db.put(c, 'quote:' + SYM, q)
        e.exits(c, now)
    with db.tx() as c:
        return db.get(c, pos_key)


# --- gating ---

def test_no_submission_when_paper_flag_off(db, cfg):
    cfg.ict_futures_paper = False
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
    assert r == {'submitted': False, 'reason': 'paper_disabled'}
    with db.tx() as c:
        assert db.get(c, POS) is None


def test_no_submission_when_detector_flag_off(db, cfg):
    cfg.ict_golden_zone = False
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
    assert r['reason'] == 'paper_disabled'
    with db.tx() as c:
        assert db.get(c, POS) is None


def test_no_submission_for_unknown_detector(db, cfg):
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'nope', SYM, sig())
    assert r == {'submitted': False, 'reason': 'unknown_detector'}


def test_submission_rejects_bad_levels(db, cfg):
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM,
                             sig(direction='long', stop=101.0))  # stop above entry
    assert r == {'submitted': False, 'reason': 'levels_inverted'}


# --- submission shape ---

def test_submission_tags_strategy_and_levels(db, cfg):
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
        assert r['submitted'] and r['strategy'] == 'ict-golden-zone'
        p = db.get(c, POS)
    assert p['symbol'] == SYM
    assert p['side'] == 'long'
    assert p['entry'] == pytest.approx(100.0)
    assert p['stop'] == pytest.approx(98.0)
    assert p['target'] == pytest.approx(104.0)  # detector target preserved
    assert p['qty'] == 1
    assert p['status'] == 'open'
    assert p['fill_version']  # same fill conventions as legacy paper
    assert p['flatten_at']  # session flatten still applies


def test_second_signal_same_detector_symbol_declined(db, cfg):
    open_position(db, cfg)
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
    assert r == {'submitted': False, 'reason': 'existing_position'}


def test_each_detector_submits_with_own_tag(db, cfg):
    for detector, flag in ict_paper.DETECTOR_FLAGS.items():
        setattr(cfg, flag, True)
    with db.tx() as c:
        for detector, tag in ict_paper.STRATEGY_TAGS.items():
            r = ict_paper.submit(db, c, cfg, NOW, detector, SYM, sig())
            assert r['submitted'], (detector, r)
            assert r['strategy'] == tag
            p = db.get(c, 'position:ict:%s:%s' % (detector, SYM))
            assert p['strategy'] == tag
            assert p['entry'] == pytest.approx(100.0)
            assert p['stop'] == pytest.approx(98.0)
            assert p['target'] == pytest.approx(104.0)


# --- exits / P&L ---

def test_stop_out_records_loss(db, cfg):
    e = open_position(db, cfg)
    p = exit_with(e, db, NOW + 120, quote(NOW + 120, 97.9, 97.91))
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'stop'
    assert p['pnl'] < 0


def test_target_hit_records_win(db, cfg):
    e = open_position(db, cfg)
    p = exit_with(e, db, NOW + 120, quote(NOW + 120, 104.1, 104.11))
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'target'
    assert p['exit'] == pytest.approx(104.0)  # exits at the detector target
    assert p['pnl'] > 0


def test_quiet_quote_leaves_position_open(db, cfg):
    e = open_position(db, cfg)
    p = exit_with(e, db, NOW + 120, quote(NOW + 120, 101.0, 101.01))
    assert p['status'] == 'open'


def test_attribution_splits_pnl_by_detector(db, cfg):
    cfg.ict_bos_fvg = True
    e = open_position(db, cfg, 'golden_zone')
    exit_with(e, db, NOW + 120, quote(NOW + 120, 104.1, 104.11))
    e2 = open_position(db, cfg, 'bos_fvg',
                       direction='short', entry=100.0, stop=102.0, target=96.0)
    p2 = exit_with(e2, db, NOW + 240, quote(NOW + 240, 102.1, 102.11),
                   pos_key='position:ict:bos_fvg:' + SYM)
    assert p2['exit_reason'] == 'stop' and p2['pnl'] < 0
    with db.tx() as c:
        attr = ict_paper.attribution(db, c)
    gz, bf = attr['ict-golden-zone'], attr['ict-bos-fvg']
    assert gz['trades'] == 1 and gz['wins'] == 1 and gz['realized'] > 0
    assert bf['trades'] == 1 and bf['losses'] == 1 and bf['realized'] < 0


def test_exits_log_no_alerts_only_paper_decisions(db, cfg):
    e = open_position(db, cfg)
    exit_with(e, db, NOW + 120, quote(NOW + 120, 104.1, 104.11))
    with db.tx() as c:
        decisions = db.recent(c, 'paper_decision', limit=10)
        alerts = db.recent(c, 'alert', limit=10)
    assert decisions, 'expected paper_decision rows'
    assert alerts == []
