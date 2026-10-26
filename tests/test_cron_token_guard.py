"""Endpoint lockdown (audit #3): COMPASS_CRON_TOKEN bearer guard.

The guard middleware in compass/app.py requires
"Authorization: Bearer <token>" on the state-mutating cron/agent endpoints
(CRON_TOKEN_PATHS) for EVERY HTTP method. 2026-10-23: the token is no
longer optional at the guard — an unset token answers 503 (the 10-22
lockdown shipped but was never activated, and the (method,path) match
let the other verb through). 2026-10-24 adoption note: the audit set's
Config.validate() start-refusal is NOT adopted (held for Josh), so no
test asserts it here. /api/pick-check/auto stays public by Josh's
2026-10-09 direction regardless.
"""
from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app

TOKEN = 'test-cron-token-' + 'x' * 40  # >= 32 chars (Config.validate rule)

# The nine locked (method, path) pairs from compass.app.CRON_TOKEN_ENDPOINTS.
LOCKED_POSTS = [
    '/api/tradermatrix-intel/web-scrape',
    '/api/tradermatrix-intel/refresh',
    '/api/admin/archive-trigger',
    '/api/admin/copy-archive-chunk',
    '/api/admin/simple-archive-chunk',
    '/api/admin/archive-verify-samples',
    '/api/admin/archive-prune-chunk',
]
LOCKED_GETS = [
    '/api/admin/backtest-futures',
    '/api/admin/backtest-scalp',
]


def make_client(tmp_path, name, monkeypatch, token=None):
    db_path = str(tmp_path / (name + '.db'))
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    if token is None:
        monkeypatch.delenv('COMPASS_CRON_TOKEN', raising=False)
    else:
        monkeypatch.setenv('COMPASS_CRON_TOKEN', token)
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


def test_token_unset_locked_paths_are_503_not_public(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'unset', monkeypatch)
    try:
        for path in LOCKED_POSTS + LOCKED_GETS:
            for call in (client.post, client.get):
                r = call(path, json=valid_payload()) if call is client.post else call(path)
                assert r.status_code == 503, (path, call.__name__, r.status_code)
    finally:
        client.__exit__(None, None, None)


def test_token_set_other_verb_is_also_locked(tmp_path, monkeypatch):
    # Regression: handlers register both GET and POST; the old guard matched
    # (method, path) so the unlisted verb bypassed the token.
    client = make_client(tmp_path, 'verbs', monkeypatch, token=TOKEN)
    try:
        for path in LOCKED_POSTS:
            assert client.get(path).status_code == 401, path
        for path in LOCKED_GETS:
            assert client.post(path).status_code == 401, path
    finally:
        client.__exit__(None, None, None)


def test_token_set_no_header_is_401(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'locked', monkeypatch, token=TOKEN)
    try:
        for path in LOCKED_POSTS:
            r = client.post(path, json=valid_payload())
            assert r.status_code == 401, (path, r.status_code, r.text[:200])
        for path in LOCKED_GETS:
            r = client.get(path)
            assert r.status_code == 401, (path, r.status_code, r.text[:200])
    finally:
        client.__exit__(None, None, None)


def test_token_set_wrong_bearer_is_401(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'wrong', monkeypatch, token=TOKEN)
    try:
        r = client.post('/api/tradermatrix-intel/web-scrape',
                        json=valid_payload(),
                        headers={'Authorization': 'Bearer not-the-token'})
        assert r.status_code == 401, (r.status_code, r.text[:200])
        # The raw token without the Bearer scheme is also rejected: the
        # guard compares the full "Bearer <token>" value.
        r = client.post('/api/tradermatrix-intel/web-scrape',
                        json=valid_payload(),
                        headers={'Authorization': TOKEN})
        assert r.status_code == 401, (r.status_code, r.text[:200])
    finally:
        client.__exit__(None, None, None)


def test_token_set_correct_bearer_passes_guard(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'ok', monkeypatch, token=TOKEN)
    try:
        headers = {'Authorization': 'Bearer ' + TOKEN}
        r = client.post('/api/tradermatrix-intel/web-scrape',
                        json=valid_payload(), headers=headers)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        assert r.json()["ok"] is True
        r = client.post('/api/admin/archive-prune-chunk', headers=headers)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        r = client.get('/api/admin/backtest-futures', headers=headers)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        r = client.get('/api/admin/backtest-scalp', headers=headers)
        assert r.status_code == 200, (r.status_code, r.text[:300])
    finally:
        client.__exit__(None, None, None)


def test_pick_check_auto_stays_public_with_token_set(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'pick', monkeypatch, token=TOKEN)
    try:
        # No header: the guard must not 401 this deliberately public route
        # (a 422 for missing query params or a 200 both prove it passed).
        r = client.get('/api/pick-check/auto')
        assert r.status_code != 401, (r.status_code, r.text[:200])
    finally:
        client.__exit__(None, None, None)


# ---------- /health cron_token presence flag (desk item 9) ----------

def test_health_reports_cron_token_configured(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'health_set', monkeypatch, token=TOKEN)
    try:
        r = client.get('/health')
        assert r.status_code == 200
        assert r.json()['cron_token'] is True
        # Presence only: the token value must never appear in the body.
        assert TOKEN not in r.text
    finally:
        client.__exit__(None, None, None)


def test_health_reports_cron_token_unconfigured(tmp_path, monkeypatch):
    client = make_client(tmp_path, 'health_unset', monkeypatch, token=None)
    try:
        r = client.get('/health')
        assert r.status_code == 200
        assert r.json()['cron_token'] is False
    finally:
        client.__exit__(None, None, None)
