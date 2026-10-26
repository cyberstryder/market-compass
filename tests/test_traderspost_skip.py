"""TradersPost skip-logic regression tests.

2026-10-19 audit findings covered here:
1. Commit 8bb36d6 (same-detector stacking fix) made every signal skip
   ITSELF: the duplicate check runs after the new position is written and
   did not exclude it, so TradersPost received nothing from that deploy on.
   There were zero tests exercising the TradersPost path.
2. The duplicate/hedging scan used a hand-maintained detector list that
   missed 5 of the 12 detectors in STRATEGY_TAGS.
3. Unmapped symbols (absent from traderspost.SYMBOL_MAP) would
   have been POSTed verbatim to TradersPost as an unknown ticker.

2026-10-25: Golden Zone and Rumers Box became paper-only (never
mirrored — see tests/test_paper_only_carve_out.py), so the "sent"
roles below are played by mirrored detectors (aoi_fade, turtle_soup,
trend_rider). Each test's original purpose is unchanged.

2026-10-26: Turtle Soup became paper-only too (the live book is AOI
Fade {MCL, MES} only), so its second-detector role below moved to
trend_rider — mirrored, with no symbol allowlist restriction.
"""
from datetime import datetime, timezone

import pytest

from compass.store import Store
from compass.config import Config
from compass import ict_paper
import compass.traderspost as traderspost

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'tp.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.ict_golden_zone = True
    c.ict_aoi_fade = True
    c.ict_turtle_soup = True
    c.ict_trend_rider = True
    c.ict_rumers_box = True
    c.traderspost_enabled = True
    return c


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(cfg, db, trade):
        calls.append(trade)
        return True, 'ok'

    monkeypatch.setattr(traderspost, 'send_signal', fake_send)
    return calls


def sig(**kw):
    base = {'direction': 'long', 'entry': 100.0, 'stop': 98.0,
            'target': 104.0, 'rr': 2.0, 'signal_ts': NOW - 60,
            'level_kind': 'test'}
    base.update(kw)
    return base


def submit(db, cfg, detector, symbol, **kw):
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, detector, symbol, sig(**kw))
        assert r['submitted'], r
    # Audit #4: the send is queued inside the submit transaction and
    # dispatched after commit — engine.tick does this in production;
    # the helper does it here so the assertions below observe the same
    # post-dispatch state they always did.
    traderspost.dispatch_pending(cfg, db)
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['trade_id'] + '-t1')
    return r, trade


def test_first_signal_is_sent_not_self_skipped(db, cfg, sent):
    _, trade = submit(db, cfg, 'aoi_fade', 'MCL.v.0')
    assert len(sent) == 1
    assert trade['traderspost_sent'] is True


def test_second_detector_same_symbol_is_skipped(db, cfg, sent):
    submit(db, cfg, 'aoi_fade', 'MCL.v.0')
    _, trade2 = submit(db, cfg, 'trend_rider', 'MCL.v.0', signal_ts=NOW - 30)
    assert len(sent) == 1  # only the first signal went out
    assert trade2['traderspost_sent'] is False
    assert 'already open for aoi_fade' in trade2['traderspost_skip_reason']


def test_same_detector_reentry_blocked_at_paper_layer(db, cfg, sent):
    submit(db, cfg, 'aoi_fade', 'MCL.v.0')
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, 'aoi_fade', 'MCL.v.0',
                             sig(signal_ts=NOW - 30))
    assert r['submitted'] is False
    assert r['reason'] == 'existing_position'
    assert len(sent) == 1


def test_correlated_opposite_direction_is_hedging_skip(db, cfg, sent):
    # Post-prune vehicles (2026-10-24): aoi_zones/bos_fvg are disabled with
    # empty allowlists; golden_zone|MNQ and rumers_box|MYM are live combos
    # in the same equity correlation group.
    # 2026-10-25: golden_zone/rumers_box are paper-only now, so the pair is
    # aoi_fade|MES (long, equity group) vs trend_rider|MNQ (short) —
    # trend_rider has no allowlist entry, hence no symbol restriction.
    submit(db, cfg, 'aoi_fade', 'MES.c.0')  # long, equity group
    _, trade2 = submit(db, cfg, 'trend_rider', 'MNQ.c.0', signal_ts=NOW - 30,
                       direction='short', entry=100.0, stop=102.0, target=96.0)
    assert len(sent) == 1
    assert trade2['traderspost_sent'] is False
    assert 'hedging' in trade2['traderspost_skip_reason']


def test_sent_flag_survives_exits_copy_over(db, cfg, sent):
    """2026-10-20 visibility bug: engine.exits() closes a trade by
    copying the POSITION record over the TRADE record. The position
    copy is written at submit time, before the TradersPost hook runs
    — a flag stored only on the trade record was erased at close, so
    /api/traderspost-skips and the Simple live split showed zero
    sends while live trades filled. The hook must mark both copies."""
    r, trade = submit(db, cfg, 'aoi_fade', 'MCL.v.0')
    assert trade['traderspost_sent'] is True
    pos_key = 'position:ict:aoi_fade:MCL.v.0:t1'
    with db.tx() as c:
        pos = db.get(c, pos_key)
        assert pos['traderspost_sent'] is True
        # Simulate exits(): management writes the position object back
        # over BOTH keys when the trade closes.
        pos = dict(pos)
        pos['status'] = 'closed'
        db.put(c, pos_key, pos)
        db.put(c, 'trade:' + r['trade_id'] + '-t1', pos)
        after = db.get(c, 'trade:' + r['trade_id'] + '-t1')
    assert after['traderspost_sent'] is True


def test_unmapped_symbol_fails_closed(db, cfg, sent):
    # aoi_fade's allowlist is root-based, so MCL.c.0 is a legal paper
    # symbol, but SYMBOL_MAP has no MCL.c.0 entry (it maps MCL.v.0):
    # the raw Compass symbol must never be POSTed to TradersPost.
    _, trade = submit(db, cfg, 'aoi_fade', 'MCL.c.0')
    assert sent == []
    assert trade['traderspost_sent'] is False
    assert 'no TradersPost symbol mapping' in trade['traderspost_skip_reason']
