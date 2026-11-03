"""ICE shadow fires export endpoint (2026-11-02, ICE long-side forward
registration).

Until this endpoint, NO HTTP surface at any auth level served rows of
ice_shadow_fires_v1: /api/ice-shadow-status is aggregates-only, and the
fires are a dedicated table (not events, not state rows). That blocked
the creative search's decisive ICE cuts (side x unit x session) and the
forward registration frozen on 2026-11-02
(files/ice-longside-forward-registration-2026-11-02.md), whose verdict
sample is defined over these rows.

Pins: cron-token gating exactly like the other admin endpoints
(CRON_TOKEN_PATHS pattern from test_events_export.py), parameter
validation, the flattened row shape (entry reference, ATR, vote counts,
per-horizon resolutions, ET session date), and (fire_ts, id) composite
cursor paging over (variant, symbol, side, ts-range) filters via
ice_shadow.export_fires.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from compass.app import CRON_TOKEN_ENDPOINTS, CRON_TOKEN_PATHS, create_app
from compass.config import Config
from compass.store import Store, ice_shadow_fires

CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
PATH = '/api/admin/ice-shadow-export'
ET = ZoneInfo('America/New_York')


def make_client(tmp_path, monkeypatch, token=CRON):
    db_path = str(tmp_path / 'ice_export.db')
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
    # Role 'web' + seed-before-start (the fbb8315 pattern, as in
    # test_events_export.py): role 'all' would spawn the engine and
    # background writers on the same sqlite file and starve seed writes.
    cfg.role = 'web'
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    return client, db


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


def _payload(fire_ts, entry_price=None, bull=4, bear=1, atr=0.6,
             h120_r=None, h120_exit=None):
    horizons = {
        '30': {'status': 'dropped', 'reason': 'gap'},
        '60': {'status': 'scored', 'r': 1.5, 'exit_ts': fire_ts + 3600,
               'exit': (entry_price or 100.0) + 0.9},
        '120': ({'status': 'scored', 'r': h120_r,
                 'exit_ts': fire_ts + 7200, 'exit': h120_exit}
                if h120_r is not None else {'status': 'pending'}),
        'session': {'status': 'pending'},
    }
    return {'version': 'ice-shadow-v1', 'variant': 'base',
            'symbol': 'AAPL', 'timeframe': '1M', 'side': 'long',
            'fire_ts': fire_ts, 'atr': atr, 'bull_votes': bull,
            'bear_votes': bear,
            'entry': ({'ts': fire_ts + 60, 'price': entry_price}
                      if entry_price is not None else None),
            'horizons': horizons, 'created': fire_ts + 5,
            'resolved_at': fire_ts + 7300 if h120_r is not None else None}


def _row(fid, variant, symbol, tf, side, fire_ts, **kw):
    p = _payload(fire_ts, **{k: v for k, v in kw.items()
                             if k in ('entry_price', 'bull', 'bear', 'atr',
                                      'h120_r', 'h120_exit')})
    p['variant'], p['symbol'], p['timeframe'], p['side'] = \
        variant, symbol, tf, side
    done = kw.get('h120_r') is not None
    return dict(id=fid, variant=variant, symbol=symbol, timeframe=tf,
                side=side, fire_ts=fire_ts,
                status='done' if done else 'open', created=fire_ts + 5,
                r60=1.5 if done else None, payload=p)


def seed(db):
    t0 = _ts(2026, 11, 2, 10, 0)
    rows = [
        # Two rows sharing a fire_ts: the composite cursor must order
        # (and page) them by id without skipping or repeating either.
        _row('b' * 64, 'base', 'AAPL', '1M', 'long', t0,
             entry_price=100.0, h120_r=2.0, h120_exit=101.2),
        _row('a' * 64, 'base', 'AAPL', '1M', 'long', t0,
             entry_price=100.0, h120_r=2.0, h120_exit=101.2),
        _row('c' * 64, 'max6rsi', 'NVDA', '5M', 'short', t0 + 600,
             entry_price=200.0, bull=1, bear=5,
             h120_r=0.5, h120_exit=199.7),
        _row('d' * 64, 'base', 'MSFT', '1M', 'short', t0 + 1200,
             entry_price=50.0, h120_r=-1.0, h120_exit=50.6),
        _row('e' * 64, 'max6rsi', 'AAPL', '1M', 'long', t0 + 1800),
        # 01:30 UTC on Nov 3 = 20:30 ET on Nov 2: the ET session date
        # must be Nov 2, not the UTC date.
        _row('f' * 64, 'base', 'AAPL', '1M', 'long',
             datetime(2026, 11, 3, 1, 30,
                      tzinfo=ZoneInfo('UTC')).timestamp(),
             entry_price=100.0, h120_r=1.0, h120_exit=100.6),
    ]
    with db.tx() as c:
        c.execute(db.insert(ice_shadow_fires).values(rows))


def test_export_path_is_cron_token_locked():
    assert PATH in CRON_TOKEN_PATHS
    assert ('GET', PATH) in CRON_TOKEN_ENDPOINTS
    assert ('POST', PATH) in CRON_TOKEN_ENDPOINTS


def test_export_rejects_missing_and_wrong_token(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    with client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers={
            'Authorization': 'Bearer wrong'}).status_code == 401


def test_export_503_when_token_unconfigured(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch, token=None)
    with client:
        assert client.get(PATH).status_code == 503


def test_export_validates_params(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    with client:
        for params in ({'variant': 'max6'}, {'variant': 'BASE'},
                       {'side': 'flat'}, {'limit': 0}, {'limit': 201},
                       {'after_ts': -1.0}):
            r = client.get(PATH, params=params, headers=AUTH)
            assert r.status_code == 400, params
            assert r.json()['ok'] is False


def test_export_row_shape_and_session(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    seed(db)  # before client startup: no app writer exists yet
    with client:
        r = client.get(PATH, params={'limit': 200}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is True and body['count'] == 6
        assert body['next_after_ts'] is None
        rows = {row['id']: row for row in body['rows']}
        done = rows['a' * 64]
        assert done['variant'] == 'base' and done['side'] == 'long'
        assert done['symbol'] == 'AAPL' and done['timeframe'] == '1M'
        assert done['status'] == 'done'
        assert done['session_date_et'] == '2026-11-02'
        assert done['entry_price'] == 100.0
        assert done['entry_ts'] == done['fire_ts'] + 60
        assert done['atr'] == 0.6
        assert done['bull_votes'] == 4 and done['bear_votes'] == 1
        assert done['h120']['status'] == 'scored'
        assert done['h120']['r'] == 2.0 and done['h120']['exit'] == 101.2
        assert done['h30'] == {'status': 'dropped', 'reason': 'gap'}
        assert done['r60'] == 1.5
        assert done['resolved_at'] is not None
        open_row = rows['e' * 64]
        assert open_row['status'] == 'open'
        assert open_row['entry_price'] is None
        assert open_row['h120'] == {'status': 'pending'}
        assert open_row['r60'] is None
        # ET session, not UTC date, for the Nov 3 01:30 UTC fire.
        assert rows['f' * 64]['session_date_et'] == '2026-11-02'
        assert rows['f' * 64]['fire_iso_utc'].startswith('2026-11-03T01:30')


def test_export_pages_by_composite_cursor_with_filters(tmp_path,
                                                       monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    seed(db)
    with client:
        # Page everything two at a time: the tied fire_ts pair must
        # both appear, in (fire_ts, id) order, exactly once.
        seen, after_ts, after_id = [], None, None
        while True:
            params = {'limit': 2}
            if after_ts is not None:
                params['after_ts'] = after_ts
                params['after_id'] = after_id
            r = client.get(PATH, params=params, headers=AUTH)
            assert r.status_code == 200
            body = r.json()
            seen.extend(row['id'] for row in body['rows'])
            if body['next_after_ts'] is None:
                assert body['next_after_id'] is None
                break
            after_ts, after_id = body['next_after_ts'], body['next_after_id']
        assert seen == ['a' * 64, 'b' * 64, 'c' * 64,
                        'd' * 64, 'e' * 64, 'f' * 64]
        # Filters narrow independently.
        r = client.get(PATH, params={'variant': 'max6rsi'}, headers=AUTH)
        assert [row['id'] for row in r.json()['rows']] == \
            ['c' * 64, 'e' * 64]
        r = client.get(PATH, params={'side': 'short'}, headers=AUTH)
        assert [row['id'] for row in r.json()['rows']] == \
            ['c' * 64, 'd' * 64]
        r = client.get(PATH, params={'symbol': 'AAPL', 'side': 'long'},
                       headers=AUTH)
        assert [row['id'] for row in r.json()['rows']] == \
            ['a' * 64, 'b' * 64, 'e' * 64, 'f' * 64]
        # since/until window on fire_ts: only the middle three rows.
        t0 = _ts(2026, 11, 2, 10, 0)
        r = client.get(PATH, params={'since': t0 + 600,
                                     'until': t0 + 1800}, headers=AUTH)
        assert [row['id'] for row in r.json()['rows']] == \
            ['c' * 64, 'd' * 64, 'e' * 64]
