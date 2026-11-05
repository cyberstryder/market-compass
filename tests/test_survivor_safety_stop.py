"""Survivor safety-stop overlay (2026-11-04, Josh's direction).

Josh, after this morning's live S3 gold trade ran with no resting
stop: "I don't want any orders that are solely market." Two layers:

1. S1/S3 live entries carry a broker-resting DISASTER stop at
   SAFETY_STOP_ATR_MULT x ATR (stop-only, no take-profit; the timed
   engine exit remains the normal exit). The distance comes from the
   frozen gauntlet trade sets' MAE distribution (would have bound on
   1/65 S1 trades and 6/334 S3 trades); these tests pin the MECHANICS
   and the distances, not the evidence.
2. A send-path invariant: any live entry whose payload would carry
   no stop is REFUSED — not queued — stamped 'no_stop_attached' on
   both record copies, projected 'not_sent' by fill_confirmation,
   and raised loudly on the live ICT Discord channel.

Also pinned: exits() detects a broker-side safety-stop fill for a
live-sent horizon position exactly like a bracket fill (quote cross
and completed-bar cross), and ignores the field entirely for
paper-only bookings (the frozen stop-less horizon construction).
"""
import pytest
from sqlalchemy import select

from compass import fill_confirmation as fc
from compass import ict_push, survivors, traderspost
from compass.config import Config
from compass.engine import Engine
from compass.store import Store, ict_push_outbox

URL = 'https://discord.com/api/webhooks/123456789/abcdef'

NOW = 1793371200.0  # Mon 2026-09-28 12:00 CT — a futures session


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 't.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    return c


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(cfg, db, trade):
        calls.append(dict(trade))
        return True, 'ok'

    monkeypatch.setattr(traderspost, 'send_signal', fake)
    return calls


def _sig(strat, **kw):
    # Mirrors tests/test_survivors.py's helper (the production feed's
    # signal shape).
    base = {'strategy': strat, 'dir': survivors.DIRECTION[strat],
            'close': 100.0, 'ts': int(NOW) - 60, 'tod': 720,
            'sdate': '2026-09-28', 'atr_t': 100.0, 'bar_index': 0}
    if strat in ('S2', 'S4'):
        base.update({'stop_ticks': 25.0, 'target_ticks': 50.0})
    if strat in survivors.HORIZON:
        base['hold'] = survivors.HORIZON[strat]
    base.update(kw)
    return base


def _ict_rows(db):
    with db.engine.begin() as conn:
        return [dict(r._mapping) for r in
                conn.execute(select(ict_push_outbox))]


# ---------------------------------------------------------------------
# Layer 1 — payload shape (stop-only, absolute and relative forms)
# ---------------------------------------------------------------------
def _stop_only_trade(**over):
    t = {'detector': 'survivor_s3', 'symbol': 'MGC.v.0', 'side': 'long',
         'signal_price': 100.0, 'entry': 100.0, 'stop': 95.0,
         'target': None, 'qty': 1, 'is_first_leg': True,
         'safety_stop_only': True, 'no_bracket': False}
    t.update(over)
    return t


def test_payload_stop_only_absolute():
    p = traderspost.build_payload(None, _stop_only_trade())
    assert p['stopLoss'] == {'type': 'stop', 'stopPrice': 95.0}
    assert 'takeProfit' not in p
    assert p['quantity'] == 1 and p['orderType'] == 'market'


def test_payload_stop_only_short():
    p = traderspost.build_payload(
        None, _stop_only_trade(side='short', symbol='MNQ.c.0',
                               signal_price=20000.0, entry=20000.0,
                               stop=20012.5))
    assert p['stopLoss'] == {'type': 'stop', 'stopPrice': 20012.5}
    assert 'takeProfit' not in p


def test_payload_stop_only_relative_when_quote_booked():
    # Booked off a quote at 100.5, signal at 100.0: the stop distance
    # is what converts (5.0), anchored on the booked price, exactly
    # like the full-bracket relative conversion.
    p = traderspost.build_payload(
        None, _stop_only_trade(entry=100.5,
                               entry_price_source='quote'))
    assert p['stopLoss'] == {'type': 'stop', 'amount': 5.0}
    assert p['price'] == 100.5 and p['signalPrice'] == 100.5
    assert 'takeProfit' not in p


@pytest.mark.parametrize('over', [
    {'stop': None},                       # nothing to attach
    {'stop': 105.0},                      # wrong side for a long
    {'stop': 100.0},                      # at the entry
    {'qty': 0},                           # no quantity
])
def test_payload_stop_only_fails_closed(over):
    assert traderspost.build_payload(None, _stop_only_trade(**over)) \
        is None


def test_bracket_record_describes_stop_only():
    p = traderspost.build_payload(None, _stop_only_trade())
    form, rec = traderspost._bracket_record(p)
    assert form == 'absolute'
    assert rec['stop_price'] == 95.0
    assert rec['take_profit_price'] is None


# ---------------------------------------------------------------------
# Layer 1 — submit paths: S1/S3 sends carry the stop; records don't move
# ---------------------------------------------------------------------
def test_submit_s3_live_send_carries_safety_stop(db, cfg, sent):
    cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S3', 'MGC.v.0',
                                      _sig('S3'))
    assert r['submitted'] and r['traderspost_queued']
    assert r['safety_stop'] == 95.0  # 0.5 * 100 ticks * 0.1, long
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1
    copy = sent[0]
    assert copy['safety_stop_only'] is True and copy['stop'] == 95.0
    payload = traderspost.build_payload(None, dict(copy))
    assert payload['stopLoss'] == {'type': 'stop', 'stopPrice': 95.0}
    assert 'takeProfit' not in payload
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['id'])
        pos = db.get(c, 'position:ict:survivor_s3:MGC.v.0:t1')
    for rec in (trade, pos):
        # The booked construction is untouched: horizon, stop-less,
        # with the safety stop recorded as evidence only.
        assert rec['stop'] is None and rec['target'] is None
        assert rec['no_bracket'] is True
        assert rec['safety_stop'] == 95.0
        assert rec['safety_stop_ticks'] == 50
        assert rec['traderspost_sent'] is True
        assert rec['live_send'] is True


def test_submit_s2_record_has_no_safety_fields(db, cfg):
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s2:MCL.v.0:t1')
    assert 'safety_stop' not in pos and 'safety_stop_ticks' not in pos


def test_submit_s3_paper_books_safety_stamp_but_never_sends(db, cfg, sent):
    # Paper mode: the stamp is recorded (evidence), nothing is sent,
    # and the invariant never fires — it lives in the live path only.
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S3', 'MGC.v.0',
                                      _sig('S3'))
    assert r['submitted'] and not r['traderspost_queued']
    assert 'send_refused' not in r
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s3:MGC.v.0:t1')
    assert pos['safety_stop'] == 95.0 and pos['stop'] is None
    traderspost.dispatch_pending(cfg, db)
    assert sent == []


# ---------------------------------------------------------------------
# Layer 2 — the invariant refuses a stop-less live entry
# ---------------------------------------------------------------------
def test_invariant_refuses_stopless_live_entry(db, cfg, sent,
                                               monkeypatch):
    # A horizon strategy whose safety stop cannot be computed (here:
    # the multiple table emptied) must not send — and must say so,
    # loudly, everywhere the record is read.
    monkeypatch.setattr(survivors, 'SAFETY_STOP_ATR_MULT', {})
    cfg.traderspost_enabled = True
    cfg.ict_push_enabled = True
    cfg.ict_push_webhook = URL
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S3', 'MGC.v.0',
                                      _sig('S3'))
    assert r['submitted']                       # paper booking stands
    assert r['traderspost_queued'] is False
    assert r['send_refused'] == 'no_stop_attached'
    assert 'no_stop_attached' in r['traderspost_skip_reason']
    traderspost.dispatch_pending(cfg, db)
    assert sent == []                           # nothing reached the wire
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['id'])
        pos = db.get(c, 'position:ict:survivor_s3:MGC.v.0:t1')
        state = survivors.account_live(
            db, c, db.get(c, survivors.STATE_KEY), NOW)
    for rec in (trade, pos):
        assert rec['traderspost_sent'] is False
        assert 'no_stop_attached' in rec['traderspost_skip_reason']
        assert rec['live_send'] is False
    # Fill confirmation: a refused entry is not_sent by construction
    # and invisible to the live accounting.
    assert fc.derive_live(trade, {})['live_status'] == 'not_sent'
    assert state['live_trades_closed'] == 0
    assert state['live_fills'] == 0 and state['live_pending'] == 0
    # The refusal alert queued as a LIVE (pending) row on the ICT
    # channel — the paper-silence gate must not swallow it.
    rows = [x for x in _ict_rows(db)
            if x['payload'].get('type') == 'send_refusal']
    assert len(rows) == 1
    assert rows[0]['status'] == 'pending'
    assert rows[0]['payload']['live'] is True
    assert rows[0]['payload']['reason'] == 'no_stop_attached'
    assert rows[0]['payload']['symbol'] == 'MGC.v.0'


def test_refusal_message_is_loud():
    msg = ict_push.format_refusal_message(
        {'detector': 'survivor_s3', 'side': 'LONG', 'symbol': 'MGC.v.0',
         'entry': 4044.1, 'reason': 'no_stop_attached',
         'time_str': 'Wed 07:31 CT'})
    content = msg['content']
    assert 'LIVE SEND REFUSED' in content
    assert 'no_stop_attached' in content
    assert 'NOT sent to TradersPost' in content
    assert '4044.10' in content and '🔴' in content


def test_refusal_delivers_end_to_end(db, cfg):
    cfg.ict_push_enabled = True
    cfg.ict_push_webhook = URL
    with db.tx() as c:
        row_id = ict_push.maybe_queue_send_refusal(
            db, c, cfg, {'id': 't-ref', 'detector': 'survivor_s3',
                         'symbol': 'MGC.v.0', 'side': 'long',
                         'entry': 4044.1, 'entered_at': NOW},
            'no_stop_attached')
    assert row_id

    class FakeResp:
        status_code = 204
        text = ''

    class FakeClient:
        def __init__(self):
            self.posts = []

        def post(self, url, **kw):
            self.posts.append((url, kw))
            return FakeResp()

    fake = FakeClient()
    assert ict_push.deliver_one(db, fake, cfg.ict_push_webhook,
                                now=NOW) is True
    assert len(fake.posts) == 1
    assert 'LIVE SEND REFUSED' in fake.posts[0][1]['json']['content']


# ---------------------------------------------------------------------
# exits() — a broker-side safety fill is detected like a bracket fill
# ---------------------------------------------------------------------
def _booked_s3(db, cfg, sent, live):
    """Book a real S3 position through submit_survivor (entry 100.0,
    safety stop 95.0, flatten_at NOW+960 — signal ts NOW-60 plus the
    (15 + 2)-minute horizon exit since the 2026-11-05 amendment) so
    the record shape is the production one, not a hand-built
    approximation."""
    if live:
        cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S3', 'MGC.v.0',
                                      _sig('S3'))
    assert r['submitted']
    if live:
        traderspost.dispatch_pending(cfg, db)
        assert len(sent) == 1
    return 'position:ict:survivor_s3:MGC.v.0:t1'


def _quote(db, bid, ask, ts=NOW):
    with db.tx() as c:
        db.put(c, 'quote:MGC.v.0',
               {'ts': ts, 'bid': bid, 'ask': ask, 'bid_size': 1,
                'ask_size': 1, 'source': 'fixture'})


def test_exits_detects_safety_stop_quote_cross(db, cfg, sent):
    key = _booked_s3(db, cfg, sent, live=True)
    _quote(db, 94.9, 95.0)              # bid through the safety stop
    e = Engine(db, cfg)
    with db.tx() as c:
        e.exits(c, NOW)
        p = db.get(c, key)
        closes = [r for r in db.prefix(c, 'traderspost_outbox:').values()
                  if r.get('kind') == 'close']
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'stop'
    assert p['safety_stop_hit'] is True
    # The fail-closed close signal fires, exactly as for a bracket
    # fill (a no-op at the broker if the stop already flattened it).
    assert len(closes) == 1


def test_exits_detects_safety_stop_in_completed_bar(db, cfg, sent):
    key = _booked_s3(db, cfg, sent, live=True)
    # A completed 1-minute bar (ts >= entered_at, ts+60 <= eval time)
    # whose low crossed the safety stop, while the live quote at eval
    # time is back above it — the intrabar detection path.
    _quote(db, 96.0, 96.1, ts=NOW + 120)  # live quote still above it
    with db.tx() as c:
        db.append(c, 'bar', 'fixture', 'MGC.v.0', NOW,
                  {'o': 97.0, 'h': 97.5, 'l': 94.5, 'c': 96.0,
                   'v': 10})
    e = Engine(db, cfg)
    with db.tx() as c:
        e.exits(c, NOW + 120)
        p = db.get(c, key)
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'stop_detected_in_bar'
    assert p['safety_stop_hit'] is True


def test_exits_ignores_safety_stop_when_never_sent(db, cfg, sent):
    # Paper-only booking: the field is stamped but the frozen
    # construction exits on flatten_at only — a quote through the
    # level must NOT close it.
    key = _booked_s3(db, cfg, sent, live=False)
    _quote(db, 94.9, 95.0)
    e = Engine(db, cfg)
    with db.tx() as c:
        e.exits(c, NOW)
        p = db.get(c, key)
    assert p['status'] == 'open'
    assert 'safety_stop_hit' not in p


def test_exits_time_exit_unchanged_for_live_horizon(db, cfg, sent):
    # Past flatten_at, the timed exit fires as before (and the close
    # signal's cancel retires the resting stop at the broker).
    key = _booked_s3(db, cfg, sent, live=True)
    _quote(db, 101.0, 101.1, ts=NOW + 1900)
    e = Engine(db, cfg)
    with db.tx() as c:
        e.exits(c, NOW + 1900)
        p = db.get(c, key)
        closes = [r for r in db.prefix(c, 'traderspost_outbox:').values()
                  if r.get('kind') == 'close']
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'session_flatten'
    assert 'safety_stop_hit' not in p
    assert len(closes) == 1
