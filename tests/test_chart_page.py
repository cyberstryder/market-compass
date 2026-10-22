"""Compass Chart prototype page: /chart serves the static chart shell and
its assets, behind the same session gate as /detailed. The page itself is
client-side (Lightweight Charts + /api/bars + /api/gamma-levels); these
tests only pin the route and asset wiring."""
from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app


def make_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'chart.db')
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
    return client


def login(client):
    r = client.post('/login', json={'password': 'test-password-12345678'})
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_chart_page_served_with_assets(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    try:
        login(client)
        r = client.get('/chart')
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert 'id="chart"' in r.text
        assert '/static/chart.js' in r.text
        assert '/static/vendor/lightweight-charts.standalone.production.js' in r.text
        # Free-form symbol entry (any ticker, not just the preset buttons).
        assert 'id="customSymbol"' in r.text

        js = client.get('/static/chart.js')
        assert js.status_code == 200
        assert '/api/bars' in js.text
        assert 'resolveCustomSymbol' in js.text
        assert '/api/gamma-levels' in js.text
        assert '/api/gamma-flip' in js.text

        lib = client.get('/static/vendor/lightweight-charts.standalone.production.js')
        assert lib.status_code == 200
        assert 'Lightweight Charts' in lib.text
    finally:
        client.__exit__(None, None, None)


def test_chart_page_not_public(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    try:
        r = client.get('/chart')
        # Same gate as /detailed: no session, no chart markup.
        assert r.status_code != 200 or 'id="chart"' not in r.text
    finally:
        client.__exit__(None, None, None)
