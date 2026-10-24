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


def test_record_call_increments_counters_on_store(tmp_path):
    """record_call used `db.conn`, which Store never had, so every call raised
    (swallowed by providers.get) and no usage was ever recorded. It now opens
    its own transaction via db.tx()."""
    from datetime import datetime, timezone
    from compass.provider_monitor import record_call, get_daily_usage, get_weekly_summary

    db = Store('sqlite:///' + str(tmp_path / 'monitor.db'))
    db.initialize()
    record_call(db, 'massive', '/v3/snapshot', True, 100.0)
    record_call(db, 'massive', '/v3/snapshot', True, 300.0)
    record_call(db, 'massive', '/v2/aggs', False, 50.0)

    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    base = f'provider_usage:massive:{day}'
    with db.tx() as c:
        assert db.get(c, base + ':total') == 3
        assert db.get(c, base + ':success') == 2
        assert db.get(c, base + ':fail') == 1
        assert db.get(c, base + ':latency_n') == 3
        assert db.get(c, base + ':endpoint:/v3/snapshot') == 2
        assert db.get(c, base + ':endpoint:/v2/aggs') == 1
        assert db.get(c, 'provider_usage:massive:last_seen') > 0

    usage = get_daily_usage(db, 'massive')
    assert usage['total_calls'] == 3 and usage['success'] == 2 and usage['fail'] == 1
    assert usage['avg_latency_ms'] == 150.0
    assert get_weekly_summary(db)['massive']['weekly_calls'] == 3
    assert get_daily_usage(db, 'alpaca')['total_calls'] == 0
