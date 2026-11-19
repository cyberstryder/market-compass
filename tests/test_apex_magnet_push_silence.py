"""Apex-magnet push silence (Josh, 2026-11-05).

The magnet-break verdict (files/apex-magnet-verdict-2026-11-05.md)
found the pushed transitions NOT TRADEABLE — post-signal returns
decay to negative by the close, the SPY-adjusted +60m return is
exactly zero, and the move precedes the alert — and the push text's
"broke above/below" wording disagrees with the detector's own break
direction in 56% of events. Josh directed the pushes stop.

Pins:
- a magnet transition still writes its outbox row (legacy and
  unified tables), payload byte-identical, but as 'suppressed'
  under gate apex-magnet-push-silence-v1 with the gate id on its
  delivery record;
- recording is byte-identical with the gate on: the
  apex_magnet_signal event payload and the per-symbol signal state
  row for the same transition match the pre-gate shapes exactly;
- a magnet row queued before the gate existed ('pending', pre-gate
  delivery record) never drains: deliver_one terminally resolves it
  as suppressed under the same gate, in both tables, without a POST;
- the unified-table belt is scoped to the magnet channel: another
  channel's pending row beside it is untouched;
- a row under an active send lease is left to that attempt, while a
  'sending' row whose lease expired is suppressed;
- the live survivor alert path (ict_push) is unaffected: a live
  entry still queues 'pending' and delivers byte-identical;
- format_message is untouched by this build — the direction wording
  defect is moot on a silenced surface, not fixed here.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from compass.store import Store, push_outbox
from compass.market import session
from compass import apex_magnet as am
from compass import magnet_push as mp
from compass import ict_push

DAY = '2026-09-25'  # Friday
OPEN, CLOSE = session(DAY)
NOW = OPEN + 3 * 3600  # mid-session
URL = 'https://discord.com/api/webhooks/123/abcDEF_.-'
GATE = 'apex-magnet-push-silence-v1'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'magnet_silence.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class PushCfg:
    apex_magnet = True
    apex_magnet_radius = 0.02
    apex_magnet_tolerance = 0.001
    magnet_push_enabled = True
    magnet_push_signals = ('broke_through',)


def apex_payload(received=NOW, spot=101.5):
    return {
        'symbol': 'SPY', 'source': 'tradermatrix', 'source_ts': received - 60,
        'received': received, 'spot': spot,
        'levels': [
            {'price': 102.0, 'score': 90, 'kind': 'apex', 'net_gex': 1.0, 'oi': 5000},
            {'price': 98.0, 'score': 70, 'kind': 'apex', 'net_gex': 0.5, 'oi': 3000},
            {'price': 99.0, 'score': None, 'kind': 'gamma_flip'},
        ],
    }


def minute_bars(start_ts, closes, base=100.0):
    rows = []
    for i, c in enumerate(closes):
        ts = start_ts + i * 60
        rows.append([ts, base, max(base, c), min(base, c), c, 1000])
        base = c
    return rows


BROKE = [99.0, 99.5, 100.5, 103.0]
APPROACH = [95.0, 96.0, 97.0, 98.0, 99.0, 99.5, 99.8]


def seed_daily(db, c, symbol='SPY'):
    for i in range(5):
        cl = 100.0 + i * 0.2
        db.append(c, 'daily', 'test', symbol, NOW - (i + 1) * 86400,
                  {'o': cl - 1, 'h': cl + 1, 'l': cl - 2, 'c': cl, 'v': 1000},
                  key='daily:%s:push:%d' % (symbol, i))


def run_scan(db, c, closes, received, now, cfg=None, spot=101.5):
    db.put(c, 'apex:SPY', apex_payload(received=received, spot=spot))
    db.put(c, 'bar_window:SPY', minute_bars(OPEN, closes))
    db.put(c, 'apex_magnet:scanned_at', 0)
    seed_daily(db, c)
    return am.scan(db, c, cfg or PushCfg(), now)


def _rows(db, c):
    return c.execute(select(mp.outbox)).mappings().all()


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, response=None):
        self.response = response
        self.posts = []

    def post(self, url, params=None, json=None):
        self.posts.append({'url': url, 'params': params, 'json': json})
        return self.response


EXPECTED_PAYLOAD = {
    'symbol': 'SPY', 'signal': 'broke_through', 'session': DAY,
    'magnet': 102.0, 'spot': 101.5, 'distance_pct': 0.5 / 101.5,
    'role': 'resistance', 'vs_flip': 'above', 'gamma_flip': 99.0,
    'magnet_score': 90, 'tests': 1, 'previous_signal': 'approaching',
}


# --- enqueue gate ---

def test_transition_row_suppressed_at_enqueue(db):
    with db.tx() as c:
        run_scan(db, c, APPROACH, NOW, NOW)           # cold start, no row
        run_scan(db, c, BROKE, NOW + 200, NOW + 200)  # -> broke_through
        rows = _rows(db, c)
    assert len(rows) == 1
    row = rows[0]
    assert row['status'] == 'suppressed'
    assert row['payload'] == dict(EXPECTED_PAYLOAD, at=NOW + 200)
    d = row['delivery']
    assert d['attempts'] == 0
    assert d['gate'] == GATE == mp.APEX_MAGNET_PUSH_GATE
    assert d['error'] == 'suppressed_by_gate'
    assert d['suppressed_at'] == NOW + 200
    # The drainer never selects it: no POST, row stays suppressed.
    client = FakeClient(FakeResponse(200, {'id': '987654321'}))
    assert mp.deliver_one(db, client, URL, now=NOW + 400) is False
    assert client.posts == []
    with db.tx() as c:
        assert _rows(db, c)[0]['status'] == 'suppressed'


def test_transition_row_suppressed_at_enqueue_unified(db):
    cfg = SimpleNamespace(magnet_push_enabled=True,
                          magnet_push_signals=('broke_through',),
                          push_outbox_unified=True)
    row = {'signal': 'broke_through', 'magnet': 102.0, 'spot': 101.5,
           'distance_pct': 0.5 / 101.5, 'role': 'resistance',
           'vs_flip': 'above', 'gamma_flip': 99.0, 'magnet_score': 90,
           'tests': 0}
    with db.tx() as c:
        row_id = mp.maybe_queue(db, c, cfg, 'SPY', row, 'approaching', DAY, NOW)
        rows = c.execute(select(push_outbox)).mappings().all()
        legacy = _rows(db, c)
    assert row_id
    assert legacy == []
    assert len(rows) == 1
    assert rows[0]['id'] == row_id and rows[0]['channel'] == 'magnet'
    assert rows[0]['status'] == 'suppressed'
    assert rows[0]['payload'] == dict(EXPECTED_PAYLOAD, tests=0, at=NOW)
    assert rows[0]['delivery']['gate'] == GATE


# --- recording byte-identical ---

def test_recording_byte_identical_with_gate(db):
    with db.tx() as c:
        run_scan(db, c, APPROACH, NOW, NOW)
        run_scan(db, c, BROKE, NOW + 200, NOW + 200)
        events = db.recent(c, 'apex_magnet_signal', limit=10)
        state = db.get(c, 'apex_magnet:signal:SPY')
    by_signal = {e['payload']['signal']: e['payload'] for e in events}
    assert by_signal['broke_through'] == {
        'session': DAY, 'signal': 'broke_through', 'magnet': 102.0,
        'spot': 101.5, 'distance_pct': 0.5 / 101.5, 'role': 'resistance',
        'vs_flip': 'above', 'previous_signal': 'approaching'}
    assert by_signal['approaching']['previous_signal'] is None
    assert state == {'signal': 'broke_through', 'at': NOW + 200,
                     'magnet': 102.0, 'spot': 101.5, 'session': DAY}


# --- send-time belt: pre-gate backlog ---

def _insert_pending(db, c, table=None, channel=None, delivery=None,
                    row_id='magnet-push-v1:backlog', status='pending'):
    values = dict(id=row_id, status=status, created=NOW,
                  payload=dict(EXPECTED_PAYLOAD, at=NOW),
                  delivery={'attempts': 0} if delivery is None else delivery)
    if channel is not None:
        values['channel'] = channel
    c.execute(db.insert(table if table is not None else mp.outbox)
              .values(**values))


def test_backlog_pending_row_suppressed_at_drain(db):
    with db.tx() as c:
        _insert_pending(db, c)
    client = FakeClient(FakeResponse(200, {'id': '987654321'}))
    assert mp.deliver_one(db, client, URL, now=NOW + 60) is True
    assert client.posts == []
    with db.tx() as c:
        row = _rows(db, c)[0]
    assert row['status'] == 'suppressed'
    assert row['delivery']['gate'] == GATE
    assert row['delivery']['error'] == 'suppressed_by_gate'
    assert row['delivery']['suppressed_at'] == NOW + 60
    # Terminal: a later pass finds nothing to do.
    assert mp.deliver_one(db, client, URL, now=NOW + 120) is False
    assert client.posts == []


def test_backlog_suppressed_in_unified_table_scoped_to_magnet(db):
    with db.tx() as c:
        _insert_pending(db, c, table=push_outbox, channel='magnet',
                        row_id='magnet-push-v1:u-backlog')
        _insert_pending(db, c, table=push_outbox, channel='ict',
                        row_id='ict-push-v1:other')
    client = FakeClient(FakeResponse(200, {'id': '987654321'}))
    assert mp.deliver_one(db, client, URL, now=NOW + 60,
                          table=push_outbox, channel='magnet') is True
    assert client.posts == []
    with db.tx() as c:
        rows = {r['id']: r for r in
                c.execute(select(push_outbox)).mappings().all()}
    assert rows['magnet-push-v1:u-backlog']['status'] == 'suppressed'
    assert rows['magnet-push-v1:u-backlog']['delivery']['gate'] == GATE
    assert rows['ict-push-v1:other']['status'] == 'pending'
    assert 'gate' not in rows['ict-push-v1:other']['delivery']


def test_backlog_sending_row_lease_rules(db):
    with db.tx() as c:
        _insert_pending(db, c, row_id='magnet-push-v1:expired',
                        status='sending',
                        delivery={'attempts': 1, 'lease_until': NOW - 1})
        _insert_pending(db, c, row_id='magnet-push-v1:active',
                        status='sending',
                        delivery={'attempts': 1, 'lease_until': NOW + 600})
    client = FakeClient(FakeResponse(200, {'id': '987654321'}))
    assert mp.deliver_one(db, client, URL, now=NOW) is True
    assert client.posts == []
    with db.tx() as c:
        rows = {r['id']: r for r in _rows(db, c)}
    assert rows['magnet-push-v1:expired']['status'] == 'suppressed'
    assert rows['magnet-push-v1:expired']['delivery']['gate'] == GATE
    # Under an active lease: left to that attempt's own resolution.
    assert rows['magnet-push-v1:active']['status'] == 'sending'
    assert 'gate' not in rows['magnet-push-v1:active']['delivery']


# --- other surfaces untouched ---

def test_live_survivor_alert_path_unaffected(db):
    cfg = SimpleNamespace(push_outbox_unified=False,
                          ict_push_enabled=True, ict_push_webhook=URL)
    trade = {'id': 'ict-survivor_s2-MCL.v.0-1-t1', 'detector': 'survivor_s2',
             'strategy': 'survivor_s2_asia_fade', 'symbol': 'MCL.v.0',
             'side': 'long', 'entry': 100.0, 'stop': 99.0, 'target': 102.0,
             'qty': 1, 'entered_at': NOW, 'source': 'survivors',
             'live_send': True}
    with db.tx() as c:
        assert ict_push.maybe_queue(db, c, cfg, trade)
        rows = c.execute(select(ict_push.outbox)).mappings().all()
    assert len(rows) == 1 and rows[0]['status'] == 'pending'
    assert rows[0]['payload']['live'] is True

    class Resp:
        status_code = 200

        def json(self):
            return {'id': '123'}

    class Client:
        def __init__(self):
            self.calls = []

        def post(self, url, params=None, json=None):
            self.calls.append(json)
            return Resp()

    client = Client()
    assert ict_push.deliver_one(db, client, URL, now=NOW) is True
    assert client.calls[0] == ict_push.format_message(rows[0]['payload'])
    # The send-set footer: since 2026-11-19 the survivors are a
    # tracking book, so the delivered alert carries the TRACKING
    # footer (it previously pinned the LIVE footer).
    assert 'TRACKING' in client.calls[0]['content']
    assert 'LIVE' not in client.calls[0]['content']


def test_formatter_untouched_by_gate():
    # The "broke above" wording defect is NOT fixed by this build;
    # silencing makes it moot on the push surface. Pin the pre-gate
    # formatter output so the scope boundary is explicit.
    msg = mp.format_message(dict(EXPECTED_PAYLOAD, at=NOW))
    assert msg['content'] == (
        '**SPY** broke above magnet **102.00**\n'
        'spot 101.50 (0.49% away) · resistance\n'
        'gamma flip 99.00 (above)\n'
        'was: approaching')
    assert msg['allowed_mentions'] == {'parse': []}


def test_snapshot_counts_suppressed_rows(db):
    with db.tx() as c:
        run_scan(db, c, APPROACH, NOW, NOW)
        run_scan(db, c, BROKE, NOW + 200, NOW + 200)
        snap = mp.snapshot(db, c)
    assert snap == {'counts': {'suppressed': 1}}
