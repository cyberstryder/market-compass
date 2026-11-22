"""Record-gap observability (2026-11-22): the Nov 16 board stored empty
historical_stats board-wide because two bare except layers swallowed a
generation-time failure without logging. These tests pin the fix:
both layers log, stored values are unchanged on every path, and a board
that generates with zero populated records raises a health flag. They
also pin the repair pass behind POST
/api/admin/smoothers-history-backfill: it fills only empty records,
never overwrites a populated one, and is idempotent."""
import logging
from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_projects import db  # noqa: F401  (fixture)
from compass import smoothers_history
from compass.app import CRON_TOKEN_PATHS, create_app
from compass.config import Config
from compass.native_smoothers import (VERSION, backfill_week_historical_stats,
                                      save, schedule, weekly)
from compass.projects import records
from compass.store import Store

FIRST = {'date': '2026-09-14',
         'open': datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc).timestamp(),
         'close': datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc).timestamp()}
NOW = FIRST['open'] + 3900
ZEROS = {'wins': 0, 'losses': 0, 'total': 0, 'win_rate': 0,
         'avg_return_pct': 0, 'avg_premium': 0, 'avg_days_to_target': 0}
HEALTH_KEY = 'health:' + VERSION + '_historical_stats'


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


def saved_payloads(db):
    with db.tx() as c:
        return list(c.execute(select(weekly.c.payload)).scalars())


def health_row(db):
    with db.tx() as c:
        return db.get(c, HEALTH_KEY, {})


def test_get_ticker_stats_internal_failure_returns_zeros_and_logs(caplog):
    class FailingDB:
        def tx(self):
            raise RuntimeError('store unavailable')

    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        stats = smoothers_history.get_ticker_stats(FailingDB(), 'BOOM', '2026-11-23')
    assert stats == ZEROS
    logged = [r for r in caplog.records
              if 'get_ticker_stats failed for BOOM week 2026-11-23' in r.getMessage()]
    assert logged and logged[0].exc_info is not None


def test_attach_site_failure_stores_empty_and_logs(db, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError('stats blew up')

    monkeypatch.setattr(smoothers_history, 'get_ticker_stats', boom)
    seed_config(db)
    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        schedule(db, Provider(), NOW)
    payloads = saved_payloads(db)
    assert len(payloads) == 1
    assert payloads[0]['historical_stats'] == {}
    logged = [r for r in caplog.records
              if 'historical_stats attach failed for DEMO week 2026-09-14' in r.getMessage()]
    assert logged and logged[0].exc_info is not None


def test_all_empty_board_writes_health_flag(db):
    seed_config(db)
    schedule(db, Provider(), NOW)
    schedule(db, Provider(), NOW + 1)
    with db.tx() as c:
        assert db.get(c, VERSION + ':week:2026-09-14')['state'] == 'complete'
    row = health_row(db)
    assert row['name'] == VERSION + '_historical_stats'
    assert row['status'] == 'error'
    assert '2026-09-14' in row['detail'] and '0 populated historical_stats' in row['detail']
    assert row['checked_at'] > 0


def test_populated_board_writes_no_health_flag(db):
    seed_source_record(db)
    seed_config(db)
    schedule(db, Provider(), NOW)
    schedule(db, Provider(), NOW + 1)
    with db.tx() as c:
        assert db.get(c, VERSION + ':week:2026-09-14')['state'] == 'complete'
    assert health_row(db) == {}


def test_success_path_stores_stats_unchanged(db):
    seed_source_record(db)
    seed_config(db)
    schedule(db, Provider(), NOW)
    payloads = saved_payloads(db)
    assert len(payloads) == 1
    expected = {'wins': 1, 'losses': 0, 'total': 1, 'win_rate': 100.0,
                'avg_return_pct': 50.0, 'avg_premium': 2.0, 'avg_days_to_target': 2.2}
    assert payloads[0]['historical_stats'] == expected
    assert payloads[0]['historical_stats'] == smoothers_history.get_ticker_stats(
        db, 'DEMO', '2026-09-14', float('inf'))


POPULATED = {'wins': 3, 'losses': 2, 'total': 5, 'win_rate': 60.0,
             'avg_return_pct': 12.5, 'avg_premium': 4.25, 'avg_days_to_target': 1.5}
RECOMPUTED = {'wins': 1, 'losses': 0, 'total': 1, 'win_rate': 100.0,
              'avg_return_pct': 50.0, 'avg_premium': 2.0, 'avg_days_to_target': 2.2}


def seed_stored_week(db):
    """A generated week's stored payloads in the three shapes the gap
    produced: {} (attach failure), the zeros dict (internal failure),
    and one populated record that must survive untouched."""
    with db.tx() as c:
        db.put(c, VERSION + ':week:2026-09-14',
               {'config': {'owned_since': NOW + 10 ** 9}})
        save(db, c, dict(id='gap-brace', week='2026-09-14', ticker='DEMO',
                         status='WIN', historical_stats={}))
        save(db, c, dict(id='gap-zeros', week='2026-09-14', ticker='DEMO',
                         status='LOSS', historical_stats=dict(ZEROS)))
        save(db, c, dict(id='kept', week='2026-09-14', ticker='DEMO',
                         status='WIN', historical_stats=dict(POPULATED)))


def payloads_by_id(db):
    return {p['id']: p for p in saved_payloads(db)}


def test_backfill_fills_only_empty_records_and_is_idempotent(db):
    seed_source_record(db)
    seed_stored_week(db)
    report = backfill_week_historical_stats(db, '2026-09-14', NOW)
    assert report['cohort'] == 3
    assert report['populated_before'] == 1
    assert report['filled'] == 2
    assert report['populated_after'] == 3
    by_id = payloads_by_id(db)
    assert by_id['gap-brace']['historical_stats'] == RECOMPUTED
    assert by_id['gap-zeros']['historical_stats'] == RECOMPUTED
    assert by_id['kept']['historical_stats'] == POPULATED
    second = backfill_week_historical_stats(db, '2026-09-14', NOW)
    assert second['filled'] == 0
    assert second['populated_before'] == 3
    assert second['populated_after'] == 3
    assert payloads_by_id(db)['kept']['historical_stats'] == POPULATED


CRON = 'test-cron-token-' + 'y' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
BACKFILL_PATH = '/api/admin/smoothers-history-backfill'


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('COMPASS_CRON_TOKEN', CRON)
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'backfill-app.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    cfg.role = 'web'  # web-only: no background writers on the test file
    store = Store(cfg.db)
    store.initialize()
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    return client, store


def test_backfill_path_is_cron_token_gated():
    assert BACKFILL_PATH in CRON_TOKEN_PATHS


def test_backfill_endpoint_requires_cron_token(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    r = client.post(BACKFILL_PATH, json={'week': '2026-09-14'})
    assert r.status_code == 401


def test_backfill_endpoint_fills_and_reports(tmp_path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    seed_source_record(store)
    seed_stored_week(store)
    r = client.post(BACKFILL_PATH, headers=AUTH, json={'week': '2026-09-14'})
    assert r.status_code == 200
    body = r.json()
    assert body['ok'] is True
    assert body['cohort'] == 3
    assert body['populated_before'] == 1
    assert body['filled'] == 2
    assert body['populated_after'] == 3
    assert payloads_by_id(store)['gap-brace']['historical_stats'] == RECOMPUTED
    assert payloads_by_id(store)['kept']['historical_stats'] == POPULATED
