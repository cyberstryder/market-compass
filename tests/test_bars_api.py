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
    import time as _time
    client, db = make_client(tmp_path, monkeypatch)
    try:
        base = _time.time() - 3600  # inside the resolver's 14-day window
        seed_bars(db, 'MES.c.0@111', [base, base + 60])
        seed_bars(db, 'MES.c.0@222', [base + 3600])
        r = client.get('/api/bars', params={'symbol': 'MES.c.0', 'limit': 10})
        assert r.status_code == 200
        rows = r.json()
        # Newest variant (@222, latest ts) wins.
        assert [row['ts'] for row in rows] == [base + 3600]
        assert rows[0]['symbol'] == 'MES.c.0@222'
    finally:
        client.__exit__(None, None, None)


def test_alias_resolves_to_dated_contract_key(tmp_path, monkeypatch):
    """Production shape: futures bars live under the dated contract key
    (MESZ6@<iid>); the alias resolves via the bar_window keys, the same
    path the ICT detectors use (ict_common.resolve_bar_key)."""
    import time as _time
    client, db = make_client(tmp_path, monkeypatch)
    try:
        base = _time.time() - 3600
        seed_bars(db, 'MESZ6@4242', [base, base + 60])
        with db.tx() as c:
            db.put(c, 'bar_window:MESZ6@4242',
                   [[base, 100.0, 101.0, 99.0, 100.5, 10],
                    [base + 60, 100.5, 101.5, 100.0, 101.0, 12]])
        r = client.get('/api/bars', params={'symbol': 'MES.c.0', 'limit': 10})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        assert all(row['symbol'] == 'MESZ6@4242' for row in rows)
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


def test_chart_markers_filter_and_normalize(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        with db.tx() as c:
            db.append(c, 'ict_bos_fvg_signal', 'ict_futures', 'MNQ.c.0',
                      5000.0, {'direction': 'long', 'entry': 21000.5,
                               'stop': 20990.0, 'target': 21021.5})
            db.append(c, 'ict_bos_fvg_signal', 'ict_futures', 'MES.c.0',
                      5000.0, {'direction': 'short', 'entry': 6500.0})
            db.append(c, 'alert', 'mu_scalp', 'MU', 6000.0,
                      {'detector': 'mu_spike_fade', 'direction': 'short',
                       'entry': 512.3, 'stop': 515.0, 'target': 508.0})
            db.append(c, 'alert', 'someone_else', 'MU', 7000.0,
                      {'detector': 'mu_spike_fade', 'direction': 'long',
                       'entry': 1.0})

        r = client.get('/api/chart-markers', params={'symbol': 'MNQ.c.0'})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]['label'] == 'BOS+FVG'
        assert rows[0]['direction'] == 'long'
        assert rows[0]['entry'] == 21000.5
        assert rows[0]['ts'] == 5000.0

        r = client.get('/api/chart-markers', params={'symbol': 'MU'})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1  # the other-source alert is excluded
        assert rows[0]['label'] == 'Spike Fade'
        assert rows[0]['direction'] == 'short'
    finally:
        client.__exit__(None, None, None)
