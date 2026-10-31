"""Events-export endpoint (2026-10-31, Smoothers plan).

Until this endpoint, NO HTTP surface at any auth level served raw
event payloads: the chain_eod archive could be counted (manifest) but
not read, and the native_premium quote paths existed only inside
production Postgres. That blocked the Smoothers archive re-score and
the measured Wednesday-cutoff curve (archive-rescore.md §2/§4).

Pins: cron-token gating exactly like the other admin endpoints
(CRON_TOKEN_PATHS pattern from test_db_maintenance.py), kind-required
validation, and id-cursor paging over (kind, symbol, ts-range) filters
via Store.export_events.
"""
import pytest
from fastapi.testclient import TestClient

from compass.app import CRON_TOKEN_ENDPOINTS, CRON_TOKEN_PATHS, create_app
from compass.config import Config
from compass.store import Store

CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
PATH = '/api/admin/events-export'


def make_client(tmp_path, monkeypatch, token=CRON):
    db_path = str(tmp_path / 'export.db')
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    if token is None:
        monkeypatch.delenv('COMPASS_CRON_TOKEN', raising=False)
    else:
        monkeypatch.setenv('COMPASS_CRON_TOKEN', token)
    cfg = Config()
    cfg.cron_token = token
    cfg.db = 'sqlite:///' + db_path
    cfg.password = 'test-password-12345678'
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    return client, db


def seed(db):
    with db.tx() as c:
        for i in range(5):
            db.append(c, 'chain_eod', 'massive', 'AAA', 1000.0 + i,
                      {'date': '2026-10-26', 'n': i},
                      key=f'chain_eod:2026-10-26:AAA:{i}')
        db.append(c, 'chain_eod', 'massive', 'BBB', 1000.0,
                  {'date': '2026-10-26'}, key='chain_eod:2026-10-26:BBB')
        db.append(c, 'native_premium', 'smoothers', 'AAA', 1001.0,
                  {'quote': {'bid': 1.0}}, key='native_premium:AAA:1')


def test_export_path_is_cron_token_locked():
    assert PATH in CRON_TOKEN_PATHS
    assert ('GET', PATH) in CRON_TOKEN_ENDPOINTS
    assert ('POST', PATH) in CRON_TOKEN_ENDPOINTS


def test_export_rejects_missing_and_wrong_token(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        assert client.get(PATH, params={'kind': 'chain_eod'}).status_code == 401
        assert client.get(PATH, params={'kind': 'chain_eod'},
                          headers={'Authorization': 'Bearer wrong'}).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_export_503_when_token_unconfigured(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch, token=None)
    try:
        assert client.get(PATH, params={'kind': 'chain_eod'}).status_code == 503
    finally:
        client.__exit__(None, None, None)


def test_export_requires_a_valid_kind(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        assert client.get(PATH, headers=AUTH).status_code == 400
        assert client.get(PATH, params={'kind': 'Bad Kind!'}, headers=AUTH).status_code == 400
        assert client.get(PATH, params={'kind': 'chain_eod', 'limit': 201},
                          headers=AUTH).status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_export_pages_by_id_cursor_with_filters(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    seed(db)
    try:
        # Page the 5 AAA chain events two at a time, in id order.
        seen, after = [], 0
        while True:
            r = client.get(PATH, params={'kind': 'chain_eod', 'symbol': 'AAA',
                                         'limit': 2, 'after_id': after},
                           headers=AUTH)
            assert r.status_code == 200
            body = r.json()
            assert body['ok'] is True and body['kind'] == 'chain_eod'
            seen.extend(row['payload']['n'] for row in body['rows'])
            if body['next_after_id'] is None:
                break
            after = body['next_after_id']
        assert seen == [0, 1, 2, 3, 4]
        # Symbol filter excludes BBB; kind filter excludes native_premium.
        r = client.get(PATH, params={'kind': 'chain_eod'}, headers=AUTH)
        assert r.json()['count'] == 6
        r = client.get(PATH, params={'kind': 'native_premium'}, headers=AUTH)
        rows = r.json()['rows']
        assert len(rows) == 1 and rows[0]['symbol'] == 'AAA'
        assert rows[0]['payload']['quote']['bid'] == 1.0
        # ts range narrows the chain set (ts 1002..1003 -> n 2,3).
        r = client.get(PATH, params={'kind': 'chain_eod', 'symbol': 'AAA',
                                     'since': 1002.0, 'until': 1003.0},
                       headers=AUTH)
        assert [row['payload']['n'] for row in r.json()['rows']] == [2, 3]
    finally:
        client.__exit__(None, None, None)
