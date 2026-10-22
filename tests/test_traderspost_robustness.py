"""TradersPost send robustness tests (audit #4, 2026-10-21).

Covers the six audited defects:
1. No idempotency: a re-send of the same signal must never POST twice
   (durable send-once claim recorded before posting; the deterministic
   key rides in the payload's documented "extras" field — TradersPost
   itself documents no dedupe field).
2. POST inside the engine tick transaction: submit/exits only queue
   outbox rows in-transaction; dispatch_pending() performs the POST
   after commit.
3. `ok = any(...)` hid one firm's failure: per-firm outcomes are
   recorded and a split result is audited as 'partial'.
4. HTTP 200 bodies were never parsed: an error payload in a 200 body
   is a failure.
5. The audit log hardcoded qty=1 while sends carry qty=2.
6. The dormant action='add' branch now fails closed.
"""
import io
import json
import types
import urllib.error
import urllib.request

import pytest

from compass.store import Store
from compass import traderspost


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'tp4.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    return types.SimpleNamespace(
        traderspost_enabled=True,
        traderspost_webhook_url='https://example.invalid/hook-primary',
        traderspost_webhook_url_2='https://example.invalid/hook-secondary')


def trade(**over):
    t = {'id': 'ict-bos_fvg-MNQ-c-0-123-t1', 'detector': 'bos_fvg',
         'symbol': 'MNQ.c.0', 'side': 'long', 'entry': 100.0,
         'stop': 98.0, 'target': 104.0, 'qty': 2, 'is_first_leg': True}
    t.update(over)
    return t


class Resp:
    def __init__(self, body=b'{"ok":true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture
def posts(monkeypatch):
    """Capture urlopen calls. `behavior` maps URL -> Resp or exception,
    or is a callable(url) -> Resp/exception."""
    calls = []
    behavior = {'handler': None}

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        handler = behavior['handler']
        if handler is not None:
            outcome = handler(req.full_url)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return Resp()

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    return calls, behavior


def http_error(url, code=500, body=b'broker rejected'):
    return urllib.error.HTTPError(url, code, 'err', {}, io.BytesIO(body))


def get(db, key):
    with db.tx() as c:
        return db.get(c, key)


# --- 1. Idempotency -------------------------------------------------------

def test_signal_key_is_stable_and_identity_based():
    assert traderspost.signal_key_for_trade(trade()) == \
        'entry:ict-bos_fvg-MNQ-c-0-123-t1'
    # Same signal re-submitted (e.g. tick rollback, detector re-fire)
    # reproduces the same key.
    assert traderspost.signal_key_for_trade(trade()) == \
        traderspost.signal_key_for_trade(trade())
    assert traderspost.signal_key_for_trade(trade(id='ict-x-9-t1')) != \
        traderspost.signal_key_for_trade(trade())


def test_resend_of_same_signal_posts_only_once(db, cfg, posts):
    calls, _ = posts
    ok1, _ = traderspost.send_signal(cfg, db, trade())
    ok2, msg2 = traderspost.send_signal(cfg, db, trade())
    assert ok1 is True and ok2 is True
    assert 'Duplicate suppressed' in msg2
    # Two firms, exactly one POST each — the second send never posted.
    assert len(calls) == 2
    claim = get(db, 'traderspost_send:entry:ict-bos_fvg-MNQ-c-0-123-t1')
    assert claim['status'] == 'sent'
    assert claim['ok'] is True


def test_signal_key_rides_in_extras(db, cfg, posts):
    calls, _ = posts
    traderspost.send_signal(cfg, db, trade())
    payload = json.loads(calls[0].data)
    assert payload['extras'] == {
        'signal_id': 'entry:ict-bos_fvg-MNQ-c-0-123-t1'}
    assert payload['quantity'] == 2


def test_failed_attempt_is_never_reposted(db, cfg, posts):
    calls, behavior = posts
    behavior['handler'] = lambda url: http_error(url)
    ok1, _ = traderspost.send_signal(cfg, db, trade())
    ok2, msg2 = traderspost.send_signal(cfg, db, trade())
    assert ok1 is False and ok2 is False
    assert 'Duplicate suppressed' in msg2
    assert len(calls) == 2  # one attempt per firm, total — no retries


def test_close_with_ref_is_send_once(db, cfg, posts):
    calls, _ = posts
    ok1, _ = traderspost.send_close_signal(cfg, db, 'MNQ.c.0', ref='pos-1')
    ok2, msg2 = traderspost.send_close_signal(cfg, db, 'MNQ.c.0', ref='pos-1')
    assert ok1 is True and ok2 is True
    assert 'Duplicate suppressed' in msg2
    assert len(calls) == 2
    # A DIFFERENT position's close is a different signal and posts.
    ok3, _ = traderspost.send_close_signal(cfg, db, 'MNQ.c.0', ref='pos-2')
    assert ok3 is True
    assert len(calls) == 4


# --- 2. Queue in transaction, dispatch after commit ------------------------

def test_queue_does_not_post_until_dispatch(db, cfg, posts):
    calls, _ = posts
    t = trade()
    with db.tx() as c:
        db.put(c, 'trade:' + t['id'], dict(t))
        db.put(c, 'position:ict:bos_fvg:MNQ.c.0:t1', dict(t))
        key = traderspost.queue_signal(
            db, c, t, trade_key='trade:' + t['id'],
            position_key='position:ict:bos_fvg:MNQ.c.0:t1')
    assert key == 'entry:ict-bos_fvg-MNQ-c-0-123-t1'
    assert calls == []  # nothing posted from inside the transaction
    row = get(db, 'traderspost_outbox:' + key)
    assert row['status'] == 'queued'

    assert traderspost.dispatch_pending(cfg, db) == 1
    assert len(calls) == 2  # both firms, after commit
    row = get(db, 'traderspost_outbox:' + key)
    assert row['status'] == 'done' and row['ok'] is True
    # Outcome written onto BOTH copies (830c646 visibility behavior).
    assert get(db, 'trade:' + t['id'])['traderspost_sent'] is True
    assert get(db, 'position:ict:bos_fvg:MNQ.c.0:t1')[
        'traderspost_sent'] is True
    # Dispatch is itself idempotent: nothing left queued.
    assert traderspost.dispatch_pending(cfg, db) == 0
    assert len(calls) == 2


def test_queued_close_dispatches_once(db, cfg, posts):
    calls, _ = posts
    with db.tx() as c:
        traderspost.queue_close_signal(db, c, 'MNQ.c.0', ref='pos-9')
    assert calls == []
    assert traderspost.dispatch_pending(cfg, db) == 1
    assert len(calls) == 2
    payload = json.loads(calls[0].data)
    assert payload['action'] == 'exit'
    assert payload['extras'] == {'signal_id': 'close:pos-9'}


# --- 3. Per-firm failure visibility ----------------------------------------

def test_partial_failure_is_visible_in_audit(db, cfg, posts):
    calls, behavior = posts

    def handler(url):
        if 'secondary' in url:
            return http_error(url, 500, b'firm2 down')
        return Resp()

    behavior['handler'] = handler
    ok, msg = traderspost.send_signal(cfg, db, trade())
    # Historical contract: a fill at >=1 firm means the signal went
    # live — but the other firm's failure must not be hidden.
    assert ok is True
    assert 'primary: ok' in msg
    assert 'secondary: FAILED' in msg
    assert 'firm2 down' in msg
    health = get(db, 'health:traderspost_webhook')
    assert health['status'] == 'partial'
    assert health['partial'] is True
    assert health['firms'] == {'primary': True, 'secondary': False}
    claim = get(db, 'traderspost_send:entry:ict-bos_fvg-MNQ-c-0-123-t1')
    assert claim['firms'] == {'primary': True, 'secondary': False}
    assert claim['partial'] is True


def test_total_failure_is_error_not_partial(db, cfg, posts):
    _, behavior = posts
    behavior['handler'] = lambda url: http_error(url, 503, b'down')
    ok, _ = traderspost.send_signal(cfg, db, trade())
    assert ok is False
    health = get(db, 'health:traderspost_webhook')
    assert health['status'] == 'error'
    assert health['firms'] == {'primary': False, 'secondary': False}


# --- 4. HTTP 200 with an error body ----------------------------------------

@pytest.mark.parametrize('body', [
    b'{"error": "unknown ticker"}',
    b'{"success": false, "message": "strategy paused"}',
    b'{"status": "rejected"}',
])
def test_200_with_error_body_is_a_failure(db, cfg, posts, body):
    _, behavior = posts
    behavior['handler'] = lambda url: Resp(body)
    ok, msg = traderspost.send_signal(cfg, db, trade())
    assert ok is False
    assert 'HTTP 200 error body' in msg
    claim = get(db, 'traderspost_send:entry:ict-bos_fvg-MNQ-c-0-123-t1')
    assert claim['status'] == 'failed'


def test_200_with_plain_ok_body_still_succeeds(db, cfg, posts):
    _, behavior = posts
    behavior['handler'] = lambda url: Resp(b'{"ok":true}')
    ok, _ = traderspost.send_signal(cfg, db, trade())
    assert ok is True


# --- 5. Real quantity in the audit log --------------------------------------

def test_audit_logs_real_quantity(db, cfg, posts):
    traderspost.send_signal(cfg, db, trade(qty=2))
    health = get(db, 'health:traderspost_webhook')
    assert 'qty=2' in health['detail']
    assert health['qty'] == 2
    claim = get(db, 'traderspost_send:entry:ict-bos_fvg-MNQ-c-0-123-t1')
    assert claim['qty'] == 2


# --- 6. 'add' branch fails closed -------------------------------------------

def test_add_leg_payload_is_refused(db, cfg, posts):
    calls, _ = posts
    assert traderspost.build_payload(cfg, trade(is_first_leg=False)) is None
    ok, msg = traderspost.send_signal(cfg, db, trade(is_first_leg=False))
    assert ok is False
    assert 'Invalid trade' in msg
    assert calls == []  # nothing reached the network
