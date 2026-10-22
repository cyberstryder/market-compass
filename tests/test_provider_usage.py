"""GET /api/provider-usage regression (audit #3).

The handler imported flask's request inside the FastAPI app, so every call
raised ImportError and returned an error payload with a raw traceback. It
now uses the FastAPI request's query params and returns clean JSON.
Session-gated (not public), so the test logs in first.
"""
from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app

PASSWORD = 'test-password-12345678'


def make_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'provider-usage.db')
    monkeypatch.setenv('COMPASS_PASSWORD', PASSWORD)
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.delenv('COMPASS_CRON_TOKEN', raising=False)
    cfg = Config()
    cfg.db = 'sqlite:///' + db_path
    cfg.password = PASSWORD
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    return client


def test_provider_usage_returns_providers_not_traceback(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    try:
        r = client.post('/login', json={'password': PASSWORD})
        assert r.status_code == 200, (r.status_code, r.text[:200])
        for url in ('/api/provider-usage', '/api/provider-usage?days=7',
                    '/api/provider-usage?day=2026-10-18'):
            r = client.get(url)
            assert r.status_code == 200, (url, r.status_code, r.text[:300])
            body = r.json()
            assert 'providers' in body, (url, body)
            assert 'traceback' not in body, (url, body)
    finally:
        client.__exit__(None, None, None)
