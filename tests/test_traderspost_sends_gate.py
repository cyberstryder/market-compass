"""Master TradersPost send gate + tracking alert copy (2026-11-19).

Josh's decision: nothing goes live ever again — every book is
tracking / measurement only. These tests pin the consequences:

1. The master gate (TRADERSPOST_SENDS_ENABLED, default OFF) makes
   the send functions return before ANY network call, while a
   survivor signal still books its tracking trade and queues exactly
   as before. The transport (urllib.request.urlopen) is asserted
   never called — not merely stubbed at the send-function seam.
2. The gate can still be opened explicitly (env / cfg field), so the
   switch is a switch, not a deletion.
3. The ICT/survivor alert copy no longer claims LIVE: no 'LIVE',
   no 'queued to TradersPost', no 'closed via TradersPost' in entry,
   exit, fill-verdict, or refusal alerts.
4. A timed (horizon) exit is labeled as a timed exit, not as the
   15:45 CT session flatten, in both alert formatters.
"""
import types
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from compass.store import Store
from compass.config import Config
from compass import alert_format, ict_push, survivors, traderspost

CT = ZoneInfo('America/Chicago')
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'gate.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.traderspost_enabled = True
    c.traderspost_webhook_url = 'https://example.invalid/hook'
    return c


def _sig(strat, **kw):
    base = {'strategy': strat,
            'dir': survivors.DIRECTION[strat],
            'close': 100.0, 'ts': NOW - 60, 'tod': 720,
            'sdate': '2026-09-28', 'atr_t': 100.0,
            'bar_index': 0}
    if strat in ('S2', 'S4'):
        base.update({'stop_ticks': 25.0, 'target_ticks': 50.0})
    if strat in survivors.HORIZON:
        base['hold'] = survivors.HORIZON[strat]
    base.update(kw)
    return base


class _Resp:
    def __init__(self, body=b'{"ok":true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture
def transport(monkeypatch):
    """Record every urlopen; the gate tests assert it stays empty."""
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        return _Resp()

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    return calls


# ---------------------------------------------------------------------
# The switch itself
# ---------------------------------------------------------------------

def test_config_default_is_off(monkeypatch):
    monkeypatch.delenv('TRADERSPOST_SENDS_ENABLED', raising=False)
    assert Config(local=True, risk=100).traderspost_sends_enabled is False


def test_config_env_can_open_the_gate(monkeypatch):
    monkeypatch.setenv('TRADERSPOST_SENDS_ENABLED', 'true')
    assert Config(local=True, risk=100).traderspost_sends_enabled is True


def test_cfg_without_the_field_is_off():
    bare = types.SimpleNamespace(traderspost_enabled=True)
    assert traderspost.sends_master_enabled(bare) is False


def test_send_functions_return_at_the_gate(db, transport):
    cfg = types.SimpleNamespace(
        traderspost_enabled=True,
        traderspost_webhook_url='https://example.invalid/hook',
        traderspost_webhook_url_2='')
    trade = {'id': 'ict-x-MNQ-c-0-1-t1', 'detector': 'x',
             'symbol': 'MNQ.c.0', 'side': 'long', 'entry': 100.0,
             'stop': 98.0, 'target': 104.0, 'qty': 1,
             'is_first_leg': True}
    ok, msg = traderspost.send_signal(cfg, db, trade)
    assert ok is False and 'gated OFF' in msg
    ok, msg = traderspost.send_close_signal(cfg, db, 'MNQ.c.0', ref='p1')
    assert ok is False and 'gated OFF' in msg
    assert transport == []


def test_gate_open_transmits(db, transport):
    cfg = types.SimpleNamespace(
        traderspost_enabled=True, traderspost_sends_enabled=True,
        traderspost_webhook_url='https://example.invalid/hook',
        traderspost_webhook_url_2='')
    trade = {'id': 'ict-x-MNQ-c-0-1-t1', 'detector': 'x',
             'symbol': 'MNQ.c.0', 'side': 'long', 'entry': 100.0,
             'stop': 98.0, 'target': 104.0, 'qty': 1,
             'is_first_leg': True}
    ok, _msg = traderspost.send_signal(cfg, db, trade)
    assert ok is True and len(transport) == 1


# ---------------------------------------------------------------------
# Survivor end-to-end: booked + queued, never transmitted
# ---------------------------------------------------------------------

def test_survivor_books_and_queues_but_never_transmits(db, cfg, transport):
    assert cfg.traderspost_sends_enabled is False  # production posture
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S1', 'MNQ.c.0',
                                      _sig('S1', close=20000.0))
    # Booking and queueing are unchanged by the gate.
    assert r['submitted'] and r['traderspost_queued']

    traderspost.dispatch_pending(cfg, db)

    # The transport was never touched.
    assert transport == []
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['id'])
        pos = db.get(c, 'position:ict:survivor_s1:MNQ.c.0:t1')
        outbox = db.prefix(c, traderspost.OUTBOX_PREFIX)
        health = db.get(c, 'health:traderspost_webhook')
    # The tracking trade stands, marked honestly as not sent.
    assert pos is not None and trade is not None
    assert trade['traderspost_sent'] is False
    assert 'gated OFF' in trade['traderspost_skip_reason']
    # The queued row was processed and recorded, not left dangling.
    assert outbox and all(
        row['status'] == 'done' and row['ok'] is False
        and 'gated OFF' in row['detail'] for row in outbox.values())
    # And the gate left a visible state note.
    assert health['status'] == 'skipped'
    assert 'gated OFF' in health['detail']


# ---------------------------------------------------------------------
# Alert copy: tracking, never LIVE
# ---------------------------------------------------------------------

def _entry_payload(**over):
    p = {'detector': 'survivor_s3_london_mr60', 'strategy': 'S3',
         'symbol': 'MGC.v.0', 'side': 'LONG', 'entry': 4044.1,
         'stop': None, 'target': None, 'rr': 0, 'qty': 1,
         'time_str': 'Thu 07:40 CT', 'live': True}
    p.update(over)
    return p


def _exit_payload(**over):
    p = {'type': 'exit', 'detector': 'survivor_s3_london_mr60',
         'strategy': 'S3', 'symbol': 'MGC.v.0', 'side': 'LONG',
         'entry': 4044.1, 'exit': 4050.0, 'pnl': 59.0,
         'pnl_str': '+$59.00', 'result': 'TIMED EXIT', 'emoji': '⏰',
         'time_str': 'Thu 07:56 CT', 'live': True}
    p.update(over)
    return p


def _assert_tracking(content):
    assert 'TRACKING' in content
    assert 'LIVE' not in content
    assert 'queued to TradersPost' not in content
    assert 'closed via TradersPost' not in content


def test_entry_copy_is_tracking():
    content = ict_push.format_message(_entry_payload())['content']
    _assert_tracking(content)
    assert 'TradersPost' not in content


def test_exit_copy_is_tracking():
    content = ict_push.format_exit_message(_exit_payload())['content']
    _assert_tracking(content)
    assert 'TradersPost' not in content


def test_fill_verdict_footers_are_tracking():
    for state in ict_push.FILL_FOOTERS:
        content = ict_push.format_exit_message(
            _exit_payload(fill_state=state))['content']
        _assert_tracking(content)


def test_refusal_copy_is_tracking():
    content = ict_push.format_refusal_message(
        {'detector': 'survivor_s3', 'side': 'LONG', 'symbol': 'MGC.v.0',
         'entry': 4044.1, 'reason': 'no_stop_attached',
         'time_str': 'Wed 07:31 CT'})['content']
    assert 'LIVE' not in content
    assert 'SEND REFUSED' in content


def test_paper_copy_unchanged():
    content = ict_push.format_message(_entry_payload(live=False))['content']
    assert '_Paper trade — watch only_' in content
    content = ict_push.format_exit_message(_exit_payload(live=False))['content']
    assert '_Paper trade — watch only_' in content


# ---------------------------------------------------------------------
# Exit-reason labels: a timed exit is not the session flatten
# ---------------------------------------------------------------------

def _ct(day, hh, mm):
    return datetime(2026, 9, day, hh, mm, tzinfo=CT).timestamp()


def _futures_exit_msg(flatten_at):
    row = {'payload': {
        'status': 'closed', 'asset': 'future', 'side': 'long',
        'strategy': 'survivor_s3_london_mr60', 'entry': 100.0,
        'exit': 100.5, 'pnl': 10.0, 'exit_reason': 'session_flatten',
        'qty': 1, 'flatten_at': flatten_at}}
    identity = {'symbol': 'MGC.v.0', 'direction': 'LONG', 'setup': '',
                'mode': 'tracking', 'horizon': 'session'}
    return alert_format.futures_message(row, identity, NOW)


def test_timed_exit_is_not_labeled_session_flatten():
    msg = _futures_exit_msg(_ct(28, 7, 56))  # S3-style morning timed exit
    assert 'Timed exit' in msg
    assert 'Session flatten' not in msg
    assert 'Survivor S3 · MGC London MR60' in msg
    assert '(live)' not in msg


def test_real_session_flatten_keeps_its_label():
    msg = _futures_exit_msg(_ct(28, 15, 45))
    assert 'Session flatten (15:45 CT)' in msg


def _queue_exit_result(db, flatten_at, tag):
    cfg = types.SimpleNamespace(ict_push_enabled=True,
                                push_outbox_unified=False)
    trade_id = 'ict-survivor_s3_london_mr60-MGC-v-0-%s-t1' % tag
    trade = {'id': trade_id,
             'detector': 'survivor_s3_london_mr60', 'strategy': 'S3',
             'source': 'survivors', 'symbol': 'MGC.v.0', 'side': 'long',
             'entry': 100.0, 'exit': 100.5, 'pnl': 10.0,
             'exit_reason': 'session_flatten',
             'flatten_at': flatten_at, 'exited_at': flatten_at}
    with db.tx() as c:
        row_id = ict_push.maybe_queue_exit(db, c, cfg, trade)
        assert row_id
        rows = c.execute(select(ict_push.outbox)).mappings().all()
    (row,) = [r for r in rows if r['id'] == row_id]
    return row['payload']['result']


def test_ict_exit_label_splits_timed_from_session(db):
    assert _queue_exit_result(db, _ct(28, 7, 56), 'timed') == 'TIMED EXIT'
    assert _queue_exit_result(db, _ct(28, 15, 45), 'sess') == 'FLATTENED'
