"""The dashboard banner must reflect the engine's live operating policy.

Regression test: the dashboard service's own PAPER_TRADING_ENABLED flag is
unset/false, but the engine writes a fresh `operating_policy` record to the
shared DB when it is paper trading. /api/state must prefer that record so
the banner stops claiming "Paper trading paused" while the engine trades.
"""
import time

from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app


def make_client(tmp_path, name, monkeypatch):
    db_path = str(tmp_path / (name + '.db'))
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    cfg = Config()
    cfg.db = 'sqlite:///' + db_path
    cfg.password = 'test-password-12345678'
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    client.post('/login', json={'password': 'test-password-12345678'})
    return client, db


def banner_policy(client):
    r = client.get('/api/state')
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return r.json()['operating_policy'].get('paper_entries_enabled')


def test_banner_falls_back_without_engine_policy(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path, 'banner1', monkeypatch)
    try:
        # Dashboard-local config has paper trading off; no engine record.
        assert banner_policy(client) is False
    finally:
        client.__exit__(None, None, None)


def test_banner_prefers_fresh_engine_policy(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, 'banner2', monkeypatch)
    try:
        with db.tx() as c:
            db.put(c, 'operating_policy',
                   {'mode': 'paper', 'paper_entries_enabled': True,
                    'at': time.time()})
        assert banner_policy(client) is True
    finally:
        client.__exit__(None, None, None)


def test_banner_ignores_stale_engine_policy(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, 'banner3', monkeypatch)
    try:
        with db.tx() as c:
            db.put(c, 'operating_policy',
                   {'mode': 'paper', 'paper_entries_enabled': True,
                    'at': time.time() - 3600})
        assert banner_policy(client) is False
    finally:
        client.__exit__(None, None, None)
