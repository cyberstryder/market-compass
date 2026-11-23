"""Fast board read (2026-11-23): GET /api/admin/smoothers-board.

The Monday Weeklies board PDF pull used to go through the session-gated
/api/native/smoothers/history in a signed-in browser (~5-10 min). This
endpoint serves the same board behind the cron token so a local script
can fetch it in seconds. These tests pin: the path is cron-token gated
(no token and wrong token both rejected, mirroring the PR #167 backfill
endpoint), a valid token returns {week, state, rows} whose rows are the
history endpoint's own row objects (same native_reports.smoothers data
path, compared directly), and an explicit week param is honored."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from compass import native_reports, smoothers_history
from compass.app import CRON_TOKEN_PATHS, create_app
from compass.config import Config
from compass.native_smoothers import VERSION, schedule
from compass.projects import records
from compass.store import Store

FIRST = {'date': '2026-09-14',
         'open': datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc).timestamp(),
         'close': datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc).timestamp()}
NOW = FIRST['open'] + 3900
POPULATED = {'wins': 1, 'losses': 0, 'total': 1, 'win_rate': 100.0,
             'avg_return_pct': 50.0, 'avg_premium': 2.0,
             'avg_days_to_target': 2.2}


@pytest.fixture(autouse=True)
def _clear_stats_cache():
    smoothers_history.clear_cache()
    yield
    smoothers_history.clear_cache()


def hourly_fixture():
    dates = pd.bdate_range('2026-07-01', '2026-09-14', tz='America/New_York')
    idx = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=30) for d in dates]).tz_convert('UTC')
    values = [100 + i * .2 + (i % 3) * .1 for i in range(len(idx))]
    return pd.DataFrame(dict(open=values, high=[v + .5 for v in values], low=[v - .5 for v in values],
                             close=values, volume=100, parts=2), index=idx)


class Provider:
    def calendar(self, monday):
        return [FIRST, dict(date='2026-09-18', open=FIRST['open'] + 4 * 86400, close=FIRST['close'] + 4 * 86400)]

    def hourly(self, symbol, asof):
        return hourly_fixture()

    def daily(self, symbol, asof):
        return pd.DataFrame()

    def contract(self, *args):
        return None


def seed_config(db):
    config = dict(ticker='DEMO', s1=2, s2=2, s3=2, pm=.01, enabled=True, backtest_wr=.7)
    with db.tx() as c:
        db.put(c, VERSION + ':config', dict(configs=[config], alltime=[], revision='v1', received_at=NOW))


def seed_source_record(db):
    original = {'monday_date': '2026-09-07', 'status': 'WIN', 'entry_premium': 2.0,
                'value_at_target': 3.0,
                'resolution_time': '2026-09-09T20:00:00+00:00',
                'model_entry_time': '2026-09-07T14:30:00+00:00'}
    with db.tx() as c:
        c.execute(records.insert().values(
            project='smoothers', key='2026-09-07', source_id='2026-09-07', symbol='DEMO',
            source_ts=NOW, first_seen=NOW, updated=NOW, revision='r1',
            payload={'source_ts': NOW - 30 * 86400, 'original': original}, context={}))


def seed_completed_week(db):
    seed_source_record(db)
    seed_config(db)
    schedule(db, Provider(), NOW)
    schedule(db, Provider(), NOW + 1)
    with db.tx() as c:
        assert db.get(c, VERSION + ':week:2026-09-14')['state'] == 'complete'


CRON = 'test-cron-token-' + 'z' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
BOARD_PATH = '/api/admin/smoothers-board'


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('COMPASS_CRON_TOKEN', CRON)
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'board-app.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    cfg.role = 'web'  # web-only: no background writers on the test file
    store = Store(cfg.db)
    store.initialize()
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    return client, store


def test_board_path_is_cron_token_gated():
    assert BOARD_PATH in CRON_TOKEN_PATHS


def test_board_endpoint_rejects_missing_and_wrong_token(tmp_path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    seed_completed_week(store)
    r = client.get(BOARD_PATH, params={'week': '2026-09-14'})
    assert r.status_code == 401
    r = client.get(BOARD_PATH, params={'week': '2026-09-14'},
                   headers={'Authorization': 'Bearer not-the-token'})
    assert r.status_code == 401
    # The raw token without the Bearer scheme is also rejected: the guard
    # compares the full "Bearer <token>" value.
    r = client.get(BOARD_PATH, params={'week': '2026-09-14'},
                   headers={'Authorization': CRON})
    assert r.status_code == 401


def test_board_endpoint_returns_history_rows_for_explicit_week(tmp_path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    seed_completed_week(store)
    r = client.get(BOARD_PATH, params={'week': '2026-09-14'}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {'week', 'state', 'rows'}
    assert body['week'] == '2026-09-14'
    assert body['state'] == 'complete'
    # Rows are the history endpoint's own row objects: the history handler
    # returns native_reports.smoothers(...)['rows'] at limit=500, so the
    # admin read must match that call exactly.
    with store.tx() as c:
        expected = native_reports.smoothers(store, c, NOW, 500, '2026-09-14', '')
    assert body['rows'] == expected['rows']
    assert len(body['rows']) == 1
    native = body['rows'][0]['native']
    assert native['ticker'] == 'DEMO'
    assert native['historical_stats'] == POPULATED
    # The fields the board-cache/PDF mapping consumes.
    for key in ('status', 'direction', 'entry_price', 'target_price',
                'quality_tier', 'contract', 'entry_premium', 'quote'):
        assert key in native, key


def test_board_endpoint_default_week_is_current_ct_monday(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    r = client.get(BOARD_PATH, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    today = datetime.now(ZoneInfo('America/Chicago')).date()
    assert body['week'] == (today - timedelta(days=today.weekday())).isoformat()
    assert body['rows'] == []
    assert body['state'] is None


def test_board_endpoint_rejects_non_monday_week(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    r = client.get(BOARD_PATH, params={'week': '2026-09-15'}, headers=AUTH)
    assert r.status_code == 422
