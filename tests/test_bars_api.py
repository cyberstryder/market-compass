"""/api/bars futures alias resolution.

The Databento live writer keys futures OHLCV bars "<alias>@<instrument_id>"
(e.g. "MES.c.0@4242"), never the bare alias, so /api/bars?symbol=MES.c.0
returned [] even with bars stored. get_bars now resolves a bare alias to
its newest @-variant (mirroring ict_common.resolve_bar_key). Exact-symbol
lookups (equities) are unchanged.
"""
from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app

BAR = {'o': 100.0, 'h': 101.0, 'l': 99.0, 'c': 100.5, 'v': 10}


def make_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'bars.db')
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
    r = client.post('/login', json={'password': 'test-password-12345678'})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return client, db


def seed_bars(db, symbol, stamps):
    with db.tx() as c:
        db.append_bars(c, 'bar', 'databento',
                       [(symbol, ts, dict(BAR)) for ts in stamps])


def test_bare_alias_resolves_to_newest_variant(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        seed_bars(db, 'MES.c.0@111', [1000.0, 1060.0])
        seed_bars(db, 'MES.c.0@222', [2000.0])
        r = client.get('/api/bars', params={'symbol': 'MES.c.0', 'limit': 10})
        assert r.status_code == 200
        rows = r.json()
        # Newest variant (@222, latest ts) wins.
        assert [row['ts'] for row in rows] == [2000.0]
        assert rows[0]['symbol'] == 'MES.c.0@222'
    finally:
        client.__exit__(None, None, None)


def test_exact_symbol_unchanged(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        seed_bars(db, 'SPY', [1000.0, 1060.0])
        r = client.get('/api/bars', params={'symbol': 'SPY', 'limit': 10})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        assert all(row['symbol'] == 'SPY' for row in rows)
    finally:
        client.__exit__(None, None, None)


def test_unknown_symbol_still_empty(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        r = client.get('/api/bars', params={'symbol': 'ZZZZ', 'limit': 10})
        assert r.status_code == 200
        assert r.json() == []
    finally:
        client.__exit__(None, None, None)
