from datetime import timezone
import pytest
from compass.store import Store
from compass.market import session
from compass import apex_magnet as am
from compass import magnet_push as mp

DAY = '2026-09-25'  # Friday
OPEN, CLOSE = session(DAY)
NOW = OPEN + 3 * 3600  # mid-session


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'magnet_push.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class PushCfg:
    apex_magnet = True
    apex_magnet_radius = 0.02
    apex_magnet_tolerance = 0.001
    magnet_push_enabled = True
    magnet_push_signals = ('broke_through',)


class AllSignalsCfg(PushCfg):
    magnet_push_signals = ('broke_through', 'tested_holding', 'approaching')


class DisabledCfg(PushCfg):
    magnet_push_enabled = False


def apex_payload(received=NOW, source_ts=None, spot=101.5):
    if source_ts is None:
        source_ts = received - 60
    return {
        'symbol': 'SPY', 'source': 'tradermatrix', 'source_ts': source_ts,
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


BROKE = [99.0, 99.5, 100.5, 103.0]                    # origin below, closes above magnet
APPROACH = [95.0, 96.0, 97.0, 98.0, 99.0, 99.5, 99.8]  # walks toward magnet
HELD = [100.0, 101.95, 100.5, 102.05, 100.8]          # touches, holds below


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
    from sqlalchemy import select
    return c.execute(select(mp.outbox)).mappings().all()


# --- scan hook ---

def test_broke_through_transition_queues_row(db):
    with db.tx() as c:
        assert run_scan(db, c, BROKE, NOW, NOW)['ran'] is True
    with db.tx() as c:
        rows = _rows(db, c)
    # Cold start seeds silently: first scan writes state but no push.
    assert len(rows) == 0


def test_second_scan_transition_pushes(db):
    with db.tx() as c:
        run_scan(db, c, APPROACH, NOW, NOW)          # cold start: approaching, no push
        run_scan(db, c, BROKE, NOW + 200, NOW + 200)  # approaching -> broke_through
        rows = _rows(db, c)
    assert len(rows) == 1
    p = rows[0]['payload']
    assert rows[0]['status'] == 'pending'
    assert p['symbol'] == 'SPY' and p['signal'] == 'broke_through'
    assert p['magnet'] == 102.0 and p['previous_signal'] == 'approaching'
    assert p['role'] in ('support', 'resistance')
    assert rows[0]['delivery']['attempts'] == 0


def test_filter_excludes_approaching(db):
    with db.tx() as c:
        run_scan(db, c, BROKE, NOW, NOW)              # cold start: broke, no push
        run_scan(db, c, APPROACH, NOW + 200, NOW + 200,
                 cfg=PushCfg(), spot=101.5)           # broke -> approaching: filtered
        rows = _rows(db, c)
    assert len(rows) == 0


def test_disabled_writes_nothing(db):
    with db.tx() as c:
        run_scan(db, c, APPROACH, NOW, NOW, cfg=DisabledCfg())
        run_scan(db, c, BROKE, NOW + 200, NOW + 200, cfg=DisabledCfg())
        rows = _rows(db, c)
    assert len(rows) == 0


def test_restart_with_unchanged_state_emits_nothing(db):
    with db.tx() as c:
        run_scan(db, c, APPROACH, NOW, NOW)
        run_scan(db, c, BROKE, NOW + 200, NOW + 200)
        assert len(_rows(db, c)) == 1
        # New apex receipt, same signal: no new transition, no new push.
        run_scan(db, c, BROKE, NOW + 400, NOW + 400)
        assert len(_rows(db, c)) == 1


def test_oscillation_produces_unique_rows(db):
    with db.tx() as c:
        cfg = AllSignalsCfg()
        run_scan(db, c, APPROACH, NOW, NOW, cfg=cfg)            # cold start
        run_scan(db, c, HELD, NOW + 200, NOW + 200, cfg=cfg)    # -> tested_holding
        run_scan(db, c, BROKE, NOW + 400, NOW + 400, cfg=cfg)   # -> broke_through
        run_scan(db, c, APPROACH, NOW + 600, NOW + 600, cfg=cfg)  # -> approaching
        rows = _rows(db, c)
    assert len(rows) == 3
    assert len({r['id'] for r in rows}) == 3
    assert [r['payload']['signal'] for r in rows] == \
        ['tested_holding', 'broke_through', 'approaching']


# --- formatter ---

def test_format_message_discord_safe():
    msg = mp.format_message({'symbol': 'SPY', 'signal': 'broke_through',
                             'magnet': 102.0, 'spot': 101.5, 'distance_pct': 0.005,
                             'role': 'resistance', 'vs_flip': 'above',
                             'gamma_flip': 99.0, 'previous_signal': 'approaching'})
    assert len(msg['content']) < 2000
    assert msg['allowed_mentions'] == {'parse': []}
    assert '@everyone' not in msg['content'] and '@here' not in msg['content']
    assert 'SPY' in msg['content'] and '102.00' in msg['content']


# --- drainer ---

class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.posts = []

    def post(self, url, params=None, json=None):
        self.posts.append({'url': url, 'params': params, 'json': json})
        if self.exc:
            raise self.exc
        return self.response


URL = 'https://discord.com/api/webhooks/123/abcDEF_.-'


def _queue_row(db, c, signal='broke_through'):
    db.put(c, 'apex:SPY', apex_payload())
    row = {'signal': signal, 'magnet': 102.0, 'spot': 101.5, 'distance_pct': 0.005,
           'role': 'resistance', 'vs_flip': 'above', 'gamma_flip': 99.0,
           'magnet_score': 90, 'tests': 0}
    cfg = PushCfg()
    return mp.maybe_queue(db, c, cfg, 'SPY', row, 'approaching', DAY, NOW)


def test_drainer_marks_delivered(db):
    with db.tx() as c:
        row_id = _queue_row(db, c)
    client = FakeClient(FakeResponse(200, {'id': '987654321'}))
    assert mp.deliver_one(db, client, URL) is True
    assert client.posts and 'wait' in client.posts[0]['params']
    with db.tx() as c:
        rows = _rows(db, c)
    assert rows[0]['id'] == row_id
    assert rows[0]['status'] == 'delivered'
    assert rows[0]['delivery']['message_id'] == '987654321'


def test_429_backoff_and_retry(db):
    import httpx
    with db.tx() as c:
        _queue_row(db, c)
    client = FakeClient(FakeResponse(429, {'retry_after': 60}))
    assert mp.deliver_one(db, client, URL, now=NOW) is True
    with db.tx() as c:
        rows = _rows(db, c)
    assert rows[0]['status'] == 'pending'
    assert rows[0]['delivery']['error'] == 'rate_limited'
    assert rows[0]['delivery']['next_attempt'] == NOW + 60
    assert rows[0]['delivery']['attempts'] == 1
    # Not attempted again before next_attempt.
    assert mp.deliver_one(db, client, URL, now=NOW + 10) is False


def test_retry_budget_exhausted(db):
    with db.tx() as c:
        _queue_row(db, c)
    client = FakeClient(FakeResponse(429, {'retry_after': 1}))
    for i in range(mp.MAX_ATTEMPTS):
        mp.deliver_one(db, client, URL, now=NOW + i * 120)
    with db.tx() as c:
        rows = _rows(db, c)
    assert rows[0]['status'] == 'failed'
    assert rows[0]['delivery']['error'] == 'retry_budget_exhausted'


def test_4xx_fails_without_retry(db):
    with db.tx() as c:
        _queue_row(db, c)
    client = FakeClient(FakeResponse(404))
    assert mp.deliver_one(db, client, URL) is True
    with db.tx() as c:
        rows = _rows(db, c)
    assert rows[0]['status'] == 'failed'
    assert rows[0]['delivery']['error'] == 'http_404'


def test_network_error_leaves_ambiguous(db):
    import httpx
    with db.tx() as c:
        _queue_row(db, c)
    client = FakeClient(exc=httpx.ConnectError('boom'))
    assert mp.deliver_one(db, client, URL) is True
    with db.tx() as c:
        rows = _rows(db, c)
    assert rows[0]['status'] == 'ambiguous'
    assert rows[0]['delivery']['error'] == 'network_outcome_unknown'


def test_invalid_webhook_url_skips(db):
    with db.tx() as c:
        _queue_row(db, c)
    client = FakeClient(FakeResponse(200, {'id': '1'}))
    assert mp.deliver_one(db, client, 'not-a-url') is False
    assert mp.deliver_one(db, client, '') is False
    assert client.posts == []


def test_no_pending_rows_returns_false(db):
    client = FakeClient(FakeResponse(200, {'id': '1'}))
    assert mp.deliver_one(db, client, URL) is False
