"""Fill-only daily-bar backfill (2026-11-03, Connors depth repair).

The write path behind POST /api/admin/daily-backfill. Pins the hard
rules the production load relies on:

- fill ONLY: a session holding any collector 'daily' event is skipped
  and its payload survives byte-identical (daily_series resolves a
  session by latest event id, so an append into an occupied session
  would silently replace the collector's bar);
- completed XNYS sessions only (today / future / non-session dates
  refused), every bar held to spy_daily.valid_daily;
- idempotent: a second identical call inserts nothing;
- provenance: inserted payloads carry the marker + splice factor;
- endpoint: cron-token gated, watchlist-only symbols, GET reads back
  fill summaries + the stored-session count.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from compass import daily_backfill as dfb
from compass.app import CRON_TOKEN_PATHS, create_app
from compass.config import Config
from compass.market import NY
from compass.store import Store
from compass.swing_signals import sessions_between
from compass.swings_c2 import daily_series

CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
PATH = '/api/admin/daily-backfill'

# Tuesday 2026-11-03, noon ET: sessions through Mon 2026-11-02 are
# completed; 2026-11-03 itself is not.
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=NY).timestamp()


def bar(d, o=100.0, h=101.0, l=99.0, c=100.5, v=1000.0):
    return {'date': d, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


@pytest.fixture()
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'backfill.db'))
    store.initialize()
    return store


def seed_collector_bar(db, symbol, date, close=55.25):
    """A collector-written daily bar (source 'alpaca'), ts at the
    session's NY midnight like the standing recovery's rows."""
    ts = datetime.fromisoformat(date).replace(tzinfo=NY).timestamp() + 4 * 3600
    payload = {'o': 55.0, 'h': 56.0, 'l': 54.5, 'c': close, 'v': 777.0,
               'vw': 55.4}
    with db.tx() as c:
        db.append(c, 'daily', 'alpaca', symbol, ts, payload,
                  key=f'seed:{symbol}:{date}')
    return payload


def series_by_date(db, symbol, first='2026-10-01', last='2026-11-03'):
    sessions = list(sessions_between(first, last))
    with db.tx() as c:
        rows = daily_series(db, c, symbol, sessions)
    return {r['date']: r for r in rows}


def test_fill_only_skips_occupied_session_and_preserves_payload(db):
    original = seed_collector_bar(db, 'AAPL', '2026-10-30')
    # A valid but WRONG bar for the occupied session: it must be
    # skipped, and the collector's payload must survive untouched.
    bars = [bar('2026-10-29'),
            bar('2026-10-30', o=1.0, h=1.1, l=0.9, c=1.0)]
    with db.tx() as c:
        result = dfb.fill_bars(db, c, 'AAPL', bars, NOW, factor=1.01)
    assert result['inserted'] == ['2026-10-29']
    assert result['skipped_existing'] == ['2026-10-30']
    assert result['refused'] == []
    series = series_by_date(db, 'AAPL')
    assert series['2026-10-30']['c'] == original['c']  # untouched
    assert series['2026-10-29']['c'] == 100.5


def test_inserted_payload_carries_provenance_marker(db):
    with db.tx() as c:
        dfb.fill_bars(db, c, 'AAPL', [bar('2026-10-29')], NOW, factor=0.987)
        rows = db.recent(c, 'daily', 'AAPL', limit=5)
    mine = [r for r in rows if r['source'] == dfb.SOURCE]
    assert len(mine) == 1
    assert mine[0]['payload']['backfilled'] == dfb.MARKER
    assert mine[0]['payload']['backfill_factor'] == 0.987


def test_second_identical_call_inserts_nothing(db):
    bars = [bar('2026-10-28'), bar('2026-10-29')]
    with db.tx() as c:
        first = dfb.fill_bars(db, c, 'AAPL', bars, NOW, factor=1.0)
        second = dfb.fill_bars(db, c, 'AAPL', bars, NOW, factor=1.0)
    assert len(first['inserted']) == 2
    assert second['inserted'] == []
    assert sorted(second['skipped_existing']) == ['2026-10-28', '2026-10-29']


def test_refuses_today_future_weekend_and_invalid(db):
    bars = [bar('2026-11-03'),          # today: not completed
            bar('2026-11-04'),          # future
            bar('2026-10-31'),          # Saturday: not a session
            bar('2026-10-29', h=98.0),  # h < l: invalid
            bar('2026-10-29')]          # duplicate date in one call
    with db.tx() as c:
        result = dfb.fill_bars(db, c, 'AAPL', bars, NOW)
    assert result['inserted'] == []
    reasons = {r['date']: r['reason'] for r in result['refused']}
    assert reasons['2026-11-03'] == 'not_a_completed_session'
    assert reasons['2026-11-04'] == 'not_a_completed_session'
    assert reasons['2026-10-31'] == 'not_an_xnys_session'
    assert reasons['2026-10-29'] in ('invalid_bar', 'duplicate_in_request')
    assert len(result['refused']) == 5


def test_fill_reaches_sma200_depth(db):
    dates = list(sessions_between('2024-10-22', '2026-10-28'))
    assert len(dates) >= 500
    fill_dates = dates[:250]
    with db.tx() as c:
        result = dfb.fill_bars(db, c, 'AAPL',
                               [bar(d) for d in fill_dates], NOW)
    assert len(result['inserted']) == 250
    sessions = list(sessions_between('2024-10-22', '2026-11-03'))
    with db.tx() as c:
        rows = daily_series(db, c, 'AAPL', sessions)
    assert len(rows) == 250  # >= SMA_PERIOD: the census turns eligible


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('COMPASS_CRON_TOKEN', CRON)
    cfg = Config()
    cfg.cron_token = CRON
    cfg.db = 'sqlite:///' + str(tmp_path / 'backfill-app.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    cfg.role = 'web'  # web-only: no background writers on the test file
    db = Store(cfg.db)
    db.initialize()
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    return client, db


def test_endpoint_path_is_cron_token_gated():
    assert PATH in CRON_TOKEN_PATHS


def test_endpoint_requires_cron_token(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    r = client.post(PATH, json={'symbol': 'AAPL', 'bars': [bar('2026-10-29')]})
    assert r.status_code == 401


def test_endpoint_rejects_non_watchlist_symbol(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    r = client.post(PATH, headers=AUTH,
                    json={'symbol': 'NOTREAL', 'bars': [bar('2026-10-29')]})
    assert r.status_code == 400


def test_endpoint_fill_and_readback(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    seed_collector_bar(db, 'AAPL', '2026-10-30')
    body = {'symbol': 'AAPL', 'factor': 1.0,
            'bars': [bar('2026-10-29'),
                     bar('2026-10-30', o=1.0, h=1.1, l=0.9, c=1.0)]}
    r = client.post(PATH, headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['ok'] and data['inserted'] == 1
    assert data['skipped_existing'] == 1 and data['refused'] == 0
    again = client.post(PATH, headers=AUTH, json=body).json()
    assert again['inserted'] == 0 and again['skipped_existing'] == 2
    read = client.get(PATH + '?symbol=AAPL', headers=AUTH).json()
    assert read['ok'] and read['stored_sessions'] >= 2
    assert sum(f['inserted'] for f in read['fills']) == 1
