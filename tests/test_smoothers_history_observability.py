"""Record-gap observability (2026-11-22): the Nov 16 board stored empty
historical_stats board-wide because two bare except layers swallowed a
generation-time failure without logging. These tests pin the fix:
both layers log, stored values are unchanged on every path, and a board
that generates with zero populated records raises a health flag."""
import logging
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from test_projects import db  # noqa: F401  (fixture)
from compass import smoothers_history
from compass.native_smoothers import VERSION, schedule, weekly
from compass.projects import records

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
