"""POST /api/tradermatrix-intel/web-scrape stores the scheduled web-scrape payload.

Regression context: the tradermatrix-web-scrape cron (3x daily) scrapes the three
web-only TraderMatrix tools (Sector Flow, Earnings Flow, Reversal Finder) and
stores them under DB key intel:web_scrape so the engine/Simple page can serve
them via GET /api/tradermatrix-intel. The store endpoint must accept a valid
payload without login (public for cron) and reject malformed payloads.
"""
from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app
CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}


def make_client(tmp_path, name, monkeypatch):
    db_path = str(tmp_path / (name + '.db'))
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    cfg = Config()
    cfg.cron_token = CRON  # 2026-10-23: locked endpoints always require the bearer
    cfg.db = 'sqlite:///' + db_path
    cfg.password = 'test-password-12345678'
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    return client, db


def valid_payload():
    return {
        "date": "2026-10-18",
        "run": "morning",
        "scraped_at": "2026-10-18T12:15:00+00:00",
        "sector_flow": [{"sector": "Technology", "bias": "bullish"}],
        "earnings_flow": [],
        "reversal_finder": [{"ticker": "XYZ", "change_pct": -4.2}],
        "errors": [],
    }


def test_web_scrape_store_roundtrip(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, 'ws1', monkeypatch)
    try:
        # No login: endpoint is public for cron.
        r = client.post('/api/tradermatrix-intel/web-scrape', headers=AUTH, json=valid_payload())
        assert r.status_code == 200, (r.status_code, r.text[:300])
        body = r.json()
        assert body["ok"] is True
        assert body["counts"] == {"sector_flow": 1, "earnings_flow": 0,
                                 "reversal_finder": 1}
        with db.tx() as c:
            stored = db.get(c, "intel:web_scrape")
        assert stored["run"] == "morning"
        assert stored["sector_flow"][0]["sector"] == "Technology"
        # Served back by the public intel endpoint.
        r2 = client.get('/api/tradermatrix-intel')
        assert r2.json()["web_scrape"]["run"] == "morning"
    finally:
        client.__exit__(None, None, None)


def test_web_scrape_rejects_bad_run(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path, 'ws2', monkeypatch)
    try:
        bad = valid_payload()
        bad["run"] = "midnight"
        r = client.post('/api/tradermatrix-intel/web-scrape', headers=AUTH, json=bad)
        assert r.json()["ok"] is False
        assert "run" in r.json()["error"]
    finally:
        client.__exit__(None, None, None)


def test_web_scrape_rejects_non_list_rows(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path, 'ws3', monkeypatch)
    try:
        bad = valid_payload()
        bad["sector_flow"] = {"sector": "Technology"}
        r = client.post('/api/tradermatrix-intel/web-scrape', headers=AUTH, json=bad)
        assert r.json()["ok"] is False
        assert "sector_flow" in r.json()["error"]
    finally:
        client.__exit__(None, None, None)


def test_web_scrape_rejects_invalid_json(tmp_path, monkeypatch):
    client, _db = make_client(tmp_path, 'ws4', monkeypatch)
    try:
        r = client.post('/api/tradermatrix-intel/web-scrape', content=b'not json', headers={**AUTH, 'content-type': 'application/json'})
        assert r.json()["ok"] is False
    finally:
        client.__exit__(None, None, None)
