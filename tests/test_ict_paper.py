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


# --- prop-firm session rules: flat at end of day, never held overnight ---

def _friday_close():
    # Friday 2026-10-02 16:05 CT = 21:05 UTC, after the 15:45 CT flatten
    return datetime(2026, 10, 2, 21, 5, tzinfo=timezone.utc).timestamp()


def test_flatten_fires_with_stale_quotes(db, cfg):
    # The core bug: quotes go stale after the close, and the old exits loop
    # skipped the position entirely, leaving it open over the weekend.
    e = open_position(db, cfg)
    with db.tx() as c:
        p = db.get(c, POS)
        assert p['status'] == 'open'
        flat = p['flatten_at']
    # No fresh quote written at all; exits run well past flatten_at.
    with db.tx() as c:
        e.exits(c, flat + 3600)
    with db.tx() as c:
        p = db.get(c, POS)
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'session_flatten'


def test_no_entry_when_market_closed(db, cfg):
    e = Engine(db, cfg)
    closed = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc).timestamp()  # Fri 20:00 CT
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, closed, 'golden_zone', SYM, sig())
    assert not r['submitted'] and r['reason'] == 'market_closed'


def test_no_entry_past_flatten(db, cfg):
    e = Engine(db, cfg)
    # Friday 15:50 CT is inside the session but past the 15:45 flatten.
    late = datetime(2026, 10, 2, 20, 50, tzinfo=timezone.utc).timestamp()
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, late, 'golden_zone', SYM, sig())
    assert not r['submitted'] and r['reason'] == 'past_flatten'


def test_closed_market_flattens_overnight_position(db, cfg):
    # A position entered Friday evening (assigned by session rollover to
    # Monday's session, flatten_at in the future) must still be flattened
    # while the market is closed over the weekend: prop firms never hold
    # between sessions.
    e = Engine(db, cfg)
    fri_eve = datetime(2026, 10, 2, 23, 44, tzinfo=timezone.utc).timestamp()  # Fri 18:44 CT
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
        assert r['submitted'], r
        p = db.get(c, POS)
        # Simulate the rollover assigning a future flatten_at.
        p['flatten_at'] = fri_eve + 86400
        db.put(c, POS, p)
        db.put(c, 'trade:' + p['id'], p)
    sat = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()  # Sat 07:00 CT
    with db.tx() as c:
        e.exits(c, sat)
    with db.tx() as c:
        p = db.get(c, POS)
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'session_flatten'


def test_intraday_halt_does_not_flatten(db, cfg):
    # The 15:15-15:30 CT daily halt is not a close; positions survive it.
    e = open_position(db, cfg)
    halt = datetime(2026, 9, 28, 20, 20, tzinfo=timezone.utc).timestamp()  # Mon 15:20 CT
    with db.tx() as c:
        db.put(c, 'quote:' + SYM, quote(halt, 101.0, 101.01))
        e.exits(c, halt)
    with db.tx() as c:
        p = db.get(c, POS)
    assert p['status'] == 'open'


def test_exits_resolves_dated_contract_quote_for_alias(db, cfg):
    # Production: Databento writes futures quotes under dated-contract keys
    # (quote:MCLZ25@<iid>) while positions are stored under the alias
    # (MCL.v.0). exits() must resolve the alias or every open futures
    # position reports DATA BLOCKED ("No fresh exit quote").
    e = Engine(db, cfg)
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', 'MCL.v.0', sig())
        assert r['submitted'], r
        db.put(c, 'quote:MCLZ25@999', quote(NOW + 120, 101.0, 101.01))
        e.exits(c, NOW + 120)
    with db.tx() as c:
        p = db.get(c, 'position:ict:golden_zone:MCL.v.0')
    assert p['status'] == 'open'
    assert p['last_quote_ts'] == NOW + 120  # dated quote was seen
    assert p['mark'] == pytest.approx(101.0, abs=0.05)  # fill-adjusted bid


# --- churn guardrails (2026-10-06) ---

def seed_bars(c, db, n=20, lo=99.0, hi=101.0):
    db.put(c, 'bar_window:' + SYM,
           [[NOW - (n - i) * 60, 100.0, hi, lo, 100.0, 10] for i in range(n)])


def test_stop_too_tight_declined(db, cfg):
    with db.tx() as c:
        seed_bars(c, db)  # ATR ~2.0; 0.5x ATR = 1.0
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM,
                             sig(entry=100.0, stop=99.5, target=102.0))
    assert not r['submitted'] and r['reason'] == 'stop_too_tight'


def test_stop_ok_accepted(db, cfg):
    with db.tx() as c:
        seed_bars(c, db)  # ATR ~2.0; risk 2.0 >= 1.0
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
    assert r['submitted'], r


def test_no_bars_skips_atr_check(db, cfg):
    # No bar window: guardrail degrades to no-op rather than blocking.
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM,
                             sig(entry=100.0, stop=99.9, target=102.0))
    assert r['submitted'], r


def test_cooldown_blocks_immediate_reentry(db, cfg):
    e = Engine(db, cfg)
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
        assert r['submitted'], r
        db.put(c, 'quote:' + SYM, quote(NOW + 60, 97.0, 97.5))  # stop-out
        e.exits(c, NOW + 60)
    with db.tx() as c:
        p = db.get(c, POS)
        assert p['status'] == 'closed'
        r = ict_paper.submit(db, c, cfg, NOW + 120, 'golden_zone', SYM, sig())
    assert not r['submitted'] and r['reason'] == 'cooldown'


def test_cooldown_expires(db, cfg):
    e = Engine(db, cfg)
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', SYM, sig())
        assert r['submitted'], r
        db.put(c, 'quote:' + SYM, quote(NOW + 60, 97.0, 97.5))
        e.exits(c, NOW + 60)
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW + 16 * 60, 'golden_zone', SYM,
                             sig(signal_ts=NOW + 16 * 60 - 30))
    assert r['submitted'], r


def test_reset_paper_book(db, cfg):
    from compass import paper_risk
    with db.tx() as c:
        db.put(c, 'trade:ict-bos_fvg-MNQ-c-0-123',
               {'id': 'x', 'status': 'closed', 'pnl': -10.0, 'strategy': 'ict-bos-fvg'})
        db.put(c, 'position:ict:bos_fvg:MNQ.c.0', {'status': 'open'})
        db.put(c, 'paper_risk:v2:2026-09-27:futures', {'realized': -10.0})
        db.put(c, 'trade:legacy-1', {'status': 'closed'})
        counts = ict_paper.reset_paper_book(db, c, NOW)
    assert counts['trades'] == 1 and counts['positions'] == 1
    assert counts['ledgers'] == 2  # one deleted, one clean seeded
    with db.tx() as c:
        assert db.get(c, 'trade:ict-bos_fvg-MNQ-c-0-123') is None
        assert db.get(c, 'position:ict:bos_fvg:MNQ.c.0') is None
        assert db.get(c, 'trade:legacy-1') is not None  # untouched
        assert db.get(c, 'paper_risk:v2:2026-09-27:futures') is None
        ledger = db.get(c, paper_risk.key(NOW, 'future'))
        assert ledger['realized'] == 0.0 and ledger['ready'] is True
