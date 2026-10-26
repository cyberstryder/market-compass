"""Freshness guard for live ICT futures sends (Josh 2026-10-26).

Fitted in files/freshness-guard-refit-2026-10-26.md, approved for
go-live 2026-10-26 09:30 CT. At the TradersPost send-queue decision
in ict_paper.submit() — after every existing gate, detector not
paper-only — the guard reads a FRESH resolved quote and skips the
SEND ONLY (the paper trade books normally) when:

1. the quote is already beyond the trade's APPROVED stop or target
   (guard_level_stop / guard_level_target), or
2. the quote has moved adversely from the BOOKED entry by strictly
   more than 10% of |target - booked entry| (guard_adverse_cap).

Fail-open: kill switch off (FRESHNESS_GUARD_ENABLED=false), no fresh
quote readable, or any guard error -> the send proceeds and the
verdict on the trade record shows the guard was not evaluated.
"""
from datetime import datetime, timezone

import pytest

from compass.store import Store
from compass.config import Config
from compass import freshness_guard, ict_paper

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT
SYM = 'MCL.v.0'  # aoi_fade live book: {MCL, MES}
POS = 'position:ict:aoi_fade:%s:t1' % SYM


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'guard.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.ict_aoi_fade = True
    c.ict_turtle_soup = True
    c.traderspost_enabled = True
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


def trade(**kw):
    base = {'side': 'long', 'symbol': SYM, 'entry': 100.0,
            'stop': 98.0, 'target': 104.0}
    base.update(kw)
    return base


def evaluate(db, cfg, t, q=None):
    with db.tx() as c:
        if q is not None:
            db.put(c, 'quote:' + SYM, q)
        return freshness_guard.evaluate(db, c, cfg, t, NOW)


def submit(db, cfg, q=None, detector='aoi_fade', **kw):
    with db.tx() as c:
        if q is not None:
            db.put(c, 'quote:' + SYM, q)
        r = ict_paper.submit(db, c, cfg, NOW, detector, SYM, sig(**kw))
        assert r['submitted'], r
        pos = db.get(c, 'position:ict:%s:%s:t1' % (detector, SYM))
        tr = db.get(c, 'trade:' + pos['id'])
        outbox = db.prefix(c, 'traderspost_outbox:')
    return r, tr, pos, outbox


def declines(db):
    with db.tx() as c:
        return db.recent(c, 'ict_decline', limit=50)


# ---------- evaluate(): the level rule ----------

def test_level_target_trips_long(db, cfg):
    v = evaluate(db, cfg, trade(), quote(NOW, 104.20, 104.25))
    assert v['decision'] == 'skip'
    assert v['reason'] == 'guard_level_target'
    assert v['evaluated'] is True
    assert v['fresh_quote'] == pytest.approx(104.25)  # ask for a long
    assert v['booked_entry'] == pytest.approx(100.0)


def test_level_stop_trips_long(db, cfg):
    v = evaluate(db, cfg, trade(), quote(NOW, 97.85, 97.90))
    assert v['decision'] == 'skip'
    assert v['reason'] == 'guard_level_stop'
    assert v['fresh_quote'] == pytest.approx(97.90)


def test_level_target_trips_short(db, cfg):
    t = trade(side='short', stop=102.0, target=96.0)
    v = evaluate(db, cfg, t, quote(NOW, 95.90, 95.95))
    assert v['decision'] == 'skip'
    assert v['reason'] == 'guard_level_target'
    assert v['fresh_quote'] == pytest.approx(95.90)  # bid for a short


def test_level_stop_trips_short(db, cfg):
    t = trade(side='short', stop=102.0, target=96.0)
    v = evaluate(db, cfg, t, quote(NOW, 102.10, 102.15))
    assert v['decision'] == 'skip'
    assert v['reason'] == 'guard_level_stop'
    assert v['fresh_quote'] == pytest.approx(102.10)


def test_inside_levels_passes(db, cfg):
    v = evaluate(db, cfg, trade(), quote(NOW, 99.95, 100.0))
    assert v['decision'] == 'send'
    assert v['evaluated'] is True
    assert v['reason'] is None


# ---------- evaluate(): the residual adverse cap ----------

def test_cap_trips_above_ten_percent_long(db, cfg):
    # Booked 100, target distance 4.0; quote 99.5 = 0.5 adverse = 12.5%.
    v = evaluate(db, cfg, trade(), quote(NOW, 99.45, 99.50))
    assert v['decision'] == 'skip'
    assert v['reason'] == 'guard_adverse_cap'
    assert v['adverse_pct'] == pytest.approx(12.5)


def test_cap_passes_at_exactly_ten_percent_long(db, cfg):
    # Quote 99.6 = 0.4 adverse = exactly 10.0% — strictly-greater rule.
    v = evaluate(db, cfg, trade(), quote(NOW, 99.55, 99.60))
    assert v['decision'] == 'send'
    assert v['evaluated'] is True
    assert v['adverse_pct'] == pytest.approx(10.0)


def test_cap_passes_below_ten_percent_long(db, cfg):
    v = evaluate(db, cfg, trade(), quote(NOW, 99.65, 99.70))
    assert v['decision'] == 'send'
    assert v['adverse_pct'] == pytest.approx(7.5)


def test_cap_trips_above_ten_percent_short(db, cfg):
    t = trade(side='short', stop=102.0, target=96.0)
    v = evaluate(db, cfg, t, quote(NOW, 100.50, 100.55))
    assert v['decision'] == 'skip'
    assert v['reason'] == 'guard_adverse_cap'
    assert v['adverse_pct'] == pytest.approx(12.5)


def test_favorable_move_never_trips(db, cfg):
    v = evaluate(db, cfg, trade(), quote(NOW, 100.95, 101.0))
    assert v['decision'] == 'send'
    assert v['adverse_pct'] == pytest.approx(-25.0)


# ---------- evaluate(): fail-open paths ----------

def test_no_quote_not_evaluated(db, cfg):
    v = evaluate(db, cfg, trade())
    assert v['decision'] == 'send'
    assert v['evaluated'] is False
    assert v['note'] == 'no_fresh_quote'


def test_stale_quote_not_evaluated(db, cfg):
    # MCL freshness age is 15s; a 60s-old quote is not readable.
    v = evaluate(db, cfg, trade(), quote(NOW - 60, 104.20, 104.25))
    assert v['decision'] == 'send'
    assert v['evaluated'] is False
    assert v['note'] == 'no_fresh_quote'


def test_kill_switch_disables(db, cfg):
    cfg.freshness_guard = False
    v = evaluate(db, cfg, trade(), quote(NOW, 104.20, 104.25))
    assert v['decision'] == 'send'
    assert v['evaluated'] is False
    assert v['note'] == 'disabled'


def test_guard_error_fails_open(db, cfg, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError('quote store exploded')
    monkeypatch.setattr(freshness_guard, '_read_decision_quote', boom)
    v = evaluate(db, cfg, trade(), quote(NOW, 104.20, 104.25))
    assert v['decision'] == 'send'
    assert v['evaluated'] is False
    assert v['note'] == 'error'
    assert 'quote store exploded' in v['error']


def test_config_default_on_and_env_kill_switch(monkeypatch):
    assert Config(local=True, risk=100).freshness_guard is True
    monkeypatch.setenv('FRESHNESS_GUARD_ENABLED', 'false')
    assert Config(local=True, risk=100).freshness_guard is False


# ---------- submit(): integration ----------

def test_submit_level_skip_books_paper_no_send_one_decline(db, cfg):
    # Quote beyond the approved target: booking crosses the level and
    # still books; the guard refuses only the live send.
    r, tr, pos, outbox = submit(db, cfg, quote(NOW, 104.40, 104.50))
    assert pos['status'] == 'open'  # paper position stands
    assert tr['traderspost_sent'] is False
    assert 'freshness guard (guard_level_target)' in \
        tr['traderspost_skip_reason']
    assert pos['traderspost_sent'] is False
    assert pos['traderspost_skip_reason'] == tr['traderspost_skip_reason']
    assert outbox == {}  # nothing queued for TradersPost
    g = tr['freshness_guard']
    assert g['decision'] == 'skip' and g['reason'] == 'guard_level_target'
    assert pos['freshness_guard']['reason'] == 'guard_level_target'
    rows = declines(db)
    assert len(rows) == 1
    p = rows[0]['payload']
    assert p['reason'] == 'guard_level_target'
    assert p['details']['fresh_quote'] == pytest.approx(104.50)
    assert p['details']['booked_entry'] == pytest.approx(104.50)
    assert p['details']['stop'] == pytest.approx(98.0)
    assert p['details']['target'] == pytest.approx(104.0)


def test_submit_cap_skip_via_moved_decision_quote(db, cfg, monkeypatch):
    # Booking reads the stored quote (ask 100.0); the decision-point
    # read sees the quote after a 0.5 adverse move (12.5% of the 4.0
    # target distance) — the flush the cap exists to refuse.
    def moved(db_, c_, symbol, side, booked, now):
        return {'price': 99.50, 'source': 'quote', 'quote_ts': NOW,
                'slip_ticks': None}
    monkeypatch.setattr(freshness_guard, '_read_decision_quote', moved)
    r, tr, pos, outbox = submit(db, cfg, quote(NOW, 99.95, 100.0))
    assert pos['status'] == 'open'
    assert tr['traderspost_sent'] is False
    assert 'guard_adverse_cap' in tr['traderspost_skip_reason']
    assert outbox == {}
    rows = declines(db)
    assert len(rows) == 1
    p = rows[0]['payload']
    assert p['reason'] == 'guard_adverse_cap'
    assert p['details']['adverse_pct'] == pytest.approx(12.5)
    assert p['details']['booked_entry'] == pytest.approx(100.0)
    assert p['details']['fresh_quote'] == pytest.approx(99.50)


def test_submit_fallback_no_quote_sends_and_marks_unevaluated(db, cfg):
    # No quote anywhere: booking falls back to the signal price and
    # the guard has nothing to read — fail-open, send proceeds, and
    # the record shows the guard was not evaluated.
    r, tr, pos, outbox = submit(db, cfg, None)
    assert tr['entry_price_source'] == 'signal_fallback'
    assert len(outbox) == 1  # send queued
    assert not tr.get('traderspost_skip_reason')
    g = tr['freshness_guard']
    assert g['evaluated'] is False and g['note'] == 'no_fresh_quote'
    assert declines(db) == []


def test_submit_fallback_level_tested_when_quote_readable(db, cfg,
                                                           monkeypatch):
    # Booked via signal_fallback (no stored quote), but a fresh quote
    # IS readable at the decision point, already beyond the target:
    # the level rule still applies, against the signal-price booking.
    def beyond(db_, c_, symbol, side, booked, now):
        return {'price': 104.50, 'source': 'quote', 'quote_ts': NOW,
                'slip_ticks': None}
    monkeypatch.setattr(freshness_guard, '_read_decision_quote', beyond)
    r, tr, pos, outbox = submit(db, cfg, None)
    assert tr['entry_price_source'] == 'signal_fallback'
    assert tr['traderspost_sent'] is False
    assert 'guard_level_target' in tr['traderspost_skip_reason']
    assert outbox == {}
    rows = declines(db)
    assert len(rows) == 1
    assert rows[0]['payload']['details']['booked_entry'] == \
        pytest.approx(100.0)


def test_submit_clean_pass_stamps_evaluated_verdict(db, cfg):
    r, tr, pos, outbox = submit(db, cfg, quote(NOW, 99.95, 100.0))
    assert len(outbox) == 1
    g = tr['freshness_guard']
    assert g['evaluated'] is True
    assert g['decision'] == 'send' and g['reason'] is None
    assert pos['freshness_guard']['evaluated'] is True
    assert declines(db) == []


def test_submit_kill_switch_off_sends_unguarded(db, cfg):
    cfg.freshness_guard = False
    r, tr, pos, outbox = submit(db, cfg, quote(NOW, 104.40, 104.50))
    assert len(outbox) == 1  # beyond-target quote sends anyway
    assert not tr.get('traderspost_skip_reason')
    assert tr['freshness_guard']['note'] == 'disabled'
    assert declines(db) == []


def test_submit_guard_exception_sends_anyway(db, cfg, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError('guard wiring exploded')
    monkeypatch.setattr(freshness_guard, 'evaluate', boom)
    r, tr, pos, outbox = submit(db, cfg, quote(NOW, 99.95, 100.0))
    assert len(outbox) == 1
    assert tr['freshness_guard']['note'] == 'error'
    assert declines(db) == []


def test_submit_paper_only_detector_never_guard_evaluated(db, cfg):
    # Turtle Soup is paper-only: the guard is a live-send filter and
    # must not run (or stamp, or persist declines) for it at all.
    r, tr, pos, outbox = submit(db, cfg, quote(NOW, 104.40, 104.50),
                                detector='turtle_soup')
    assert 'paper-only' in tr['traderspost_skip_reason']
    assert tr.get('freshness_guard') is None
    assert declines(db) == []
