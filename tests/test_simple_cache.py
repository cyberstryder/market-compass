"""Task 003 item 7: /api/simple goes through SnapshotCache.

summary() runs five prefix scans plus a Config() rebuild per call, and
the route is public — every hit used to rescan. These tests pin:
- the payload shape is unchanged (same top-level keys the page reads);
- a second hit inside the cache TTL does NOT re-run summary() (the
  cache engages — asserted with a counting stub), so bursts coalesce
  into one scan set exactly like /api/state's dashboard_cache.
"""
from fastapi.testclient import TestClient

import compass.simple as simple_mod
from compass.app import create_app
from compass.config import Config
from compass.store import Store


def make_client(tmp_path, name, monkeypatch):
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / (name + '.db'))
    cfg.password = 'test-password-12345678'
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    return client


def test_simple_route_payload_shape_unchanged(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'shape', monkeypatch)
    r = client.get('/api/simple')
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {'asof', 'types', 'tracking'}
    assert set(body['tracking'].keys()) == {
        'futures_book', 'scanner', 'iceberg'}


def test_simple_route_caches_within_ttl(tmp_path, monkeypatch):
    calls = {'n': 0}
    sentinel = {'asof': 1.0, 'types': {}, 'tracking': {}}

    def stub(db, c, now):
        calls['n'] += 1
        return dict(sentinel)

    monkeypatch.setattr(simple_mod, 'summary', stub)
    client = make_client(tmp_path, 'cache', monkeypatch)
    r1 = client.get('/api/simple')
    r2 = client.get('/api/simple')
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == sentinel and r2.json() == sentinel
    # Second hit served from the SnapshotCache — summary ran once.
    assert calls['n'] == 1
