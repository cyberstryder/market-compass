"""/api/state poison health row (2026-10-30 outage).

snapshot_fast builds the dashboard health list from EVERY health:* row in
state, and decorate_health read h["name"]/h["status"]/h["detail"]
unguarded. Four engine error boundaries wrote bare {'error','at'} dicts
(no name/status/detail) to health:levels_error, health:ice_shadow_error,
health:pre_ict_error and health:detector_*_error. The first time any of
them fired, the row persisted in state and every /api/state poll 500'd
with KeyError: 'name' — while /health and /api/simple stayed green.

These tests pin both halves of the fix:
- decorate_health tolerates a health row missing any subset of
  name/status/detail (legacy *_error shapes included), and
- the engine's *_error writers now emit the full Store.health shape
  (name/status/detail/checked_at) while preserving the symbol/error/at
  fields existing readers assert on.
"""
import time

from fastapi.testclient import TestClient

from compass.app import create_app
from compass.config import Config
from compass.engine import Engine
from compass.readiness import decorate_health
from compass.store import Store

NOW = 1760000000.0
MARKETS = dict(futures=True, equities=True)

# The exact shapes the four boundaries wrote before the fix.
LEGACY_ROWS = [
    {'symbol': 'SPY', 'error': 'levels boom', 'at': NOW},   # levels_error
    {'error': 'ice boom', 'at': NOW},                       # ice_shadow_error
    {'error': 'pre-ict boom', 'at': NOW},                   # pre_ict_error
    {'error': 'detector boom', 'at': NOW},                  # detector_*_error
]


def test_decorate_health_tolerates_bare_error_rows():
    rows = [dict(r) for r in LEGACY_ROWS] + [{}, {'name': 'engine'}]
    result = decorate_health(rows, {}, MARKETS, NOW)
    assert len(result) == len(rows)
    for entry in result:
        assert 'name' in entry and 'status' in entry and 'detail' in entry
        assert isinstance(entry['name'], str)
        assert isinstance(entry['detail'], str)


def test_decorate_health_tolerates_error_rows_with_live_workers():
    workers = {'worker:web': {'at': NOW}, 'worker:engine': {'at': NOW},
               'worker:collector': {'at': NOW}, 'worker:all': {'at': NOW}}
    result = decorate_health([dict(r) for r in LEGACY_ROWS], workers, MARKETS, NOW)
    assert len(result) == len(LEGACY_ROWS)
    for entry in result:
        assert entry['status'] in {'unknown', 'stale', 'error'}


def test_decorate_health_wellformed_error_row_stays_error():
    row = {'name': 'levels_error', 'status': 'error', 'detail': 'levels boom',
           'checked_at': NOW, 'symbol': 'SPY', 'error': 'levels boom', 'at': NOW}
    result = decorate_health([row], {'worker:web': {'at': NOW}}, MARKETS, NOW)
    assert result[0]['status'] == 'error'
    assert result[0]['recorded_status'] == 'error'
    assert result[0]['detail'] == 'levels boom'
    assert result[0]['symbol'] == 'SPY'


def test_decorate_health_mixed_list_healthy_row_unchanged():
    healthy = dict(name='databento_comex_MGC', status='receiving', detail='quote',
                   source_ts=NOW - 50, checked_at=NOW)
    rows = [healthy] + [dict(r) for r in LEGACY_ROWS]
    result = decorate_health(rows, {'worker:collector': {'at': NOW}}, MARKETS, NOW)
    assert len(result) == len(rows)
    # Healthy row decorates exactly as before (see test_extra_futures).
    assert result[0]['status'] == 'stale' and result[0]['heartbeat_age'] == 0
    assert result[0]['name'] == 'databento_comex_MGC'


# --- Writer shape: the error boundaries now emit full health rows ---

def make_engine(tmp_path, stocks=('SPY',)):
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'levels.db')
    cfg.local = True
    cfg.paper_trading = False
    cfg.orb_setups = False
    cfg.setup_study = False
    cfg.stocks = tuple(stocks)
    db = Store(cfg.db)
    db.initialize()
    return Engine(db, cfg), db


def seed_bars(db, symbol, n=60, end=NOW):
    items = []
    for i in range(n):
        ts = end - (n - 1 - i) * 60
        px = 100.0 + i * 0.1
        items.append((symbol, ts, {'o': px, 'h': px + 0.2, 'l': px - 0.2,
                                   'c': px + 0.1, 'v': 1000}))
    with db.tx() as c:
        db.append_bars(c, 'bar', 'fixture', items)
        db.put(c, 'latestbar:' + symbol, end)


def assert_wellformed(err, name):
    assert err['name'] == name
    assert err['status'] == 'error'
    assert err['detail'] and err['detail'] == err['error'][:300]
    assert err['checked_at'] == err['at']


def test_levels_error_row_is_wellformed(tmp_path, monkeypatch):
    engine, db = make_engine(tmp_path, stocks=('TSLA', 'SPY'))
    seed_bars(db, 'TSLA')
    seed_bars(db, 'SPY')
    real_recent = Store.recent

    def poisoned(self, c, kind, symbol=None, limit=100, since=None):
        if symbol == 'TSLA':
            raise RuntimeError('transaction timeout (simulated)')
        return real_recent(self, c, kind, symbol, limit=limit, since=since)

    monkeypatch.setattr(Store, 'recent', poisoned)
    engine._levels_pass(NOW + 60)  # must not raise
    with db.tx() as c:
        err = db.get(c, 'health:levels_error')
        assert err and err['symbol'] == 'TSLA'  # preserved fields
        assert 'transaction timeout' in err['error']
        assert_wellformed(err, 'levels_error')


def test_pre_ict_error_row_is_wellformed(tmp_path, monkeypatch):
    cfg = Config(paper_trading=True, stock_paper_trades=True,
                 db='sqlite:///' + str(tmp_path / 'tick.db'), local=True,
                 role='web', password='<redacted>', stocks=('SPY',), futures=(),
                 alpaca_key='', alpaca_secret='', massive='', databento='',
                 matrix='', discord='', ict_bos_fvg=True)
    db = Store(cfg.db)
    db.initialize()
    engine = Engine(db, cfg)

    def boom(*args, **kwargs):
        raise RuntimeError('statement timeout (simulated)')

    monkeypatch.setattr(engine.scanner, 'scan', boom)
    monkeypatch.setattr(engine.scanner, 'retry_options', boom)
    engine.tick(time.time())  # must not raise
    with db.tx() as c:
        err = db.get(c, 'health:pre_ict_error')
        assert err and 'statement timeout' in err['error']  # preserved field
        assert_wellformed(err, 'pre_ict_error')


# --- Snapshot level: a poison row in state must not 500 /api/state ---

def test_api_state_survives_poison_health_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'state.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    with db.tx() as c:
        db.put(c, 'health:levels_error',
               {'symbol': 'SPY', 'error': 'levels boom', 'at': time.time()})
        db.put(c, 'health:ice_shadow_error',
               {'error': 'ice boom', 'at': time.time()})
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    try:
        client.post('/login', json={'password': 'test-password-12345678'})
        r = client.get('/api/state')
        assert r.status_code == 200, (r.status_code, r.text[:200])
        health = r.json()['health']
        assert all('name' in e and 'status' in e for e in health)
        errors = [e for e in health if e.get('error') in {'levels boom', 'ice boom'}]
        assert len(errors) == 2
    finally:
        client.__exit__(None, None, None)
