import pytest
from datetime import datetime, timezone
from compass.store import Store
from compass import calendar_pnl as cal

# 2026-10-01 20:00 UTC = 2026-10-01 15:00 CDT
TS_OCT1 = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc).timestamp()
# 2026-09-30 20:00 UTC = 2026-09-30 15:00 CDT
TS_SEP30 = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc).timestamp()
# 2026-09-29 20:00 UTC = 2026-09-29 15:00 CDT (before cutoff)
TS_SEP29 = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'cal.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def trade(tid, strategy, asset, pnl, exited_at, status='closed'):
    return {'id': tid, 'strategy': strategy, 'asset': asset, 'symbol': 'X',
            'status': status, 'pnl': pnl, 'exited_at': exited_at,
            'entered_at': exited_at - 3600, 'qty': 1}


def test_bucket_for():
    assert cal.bucket_for({'asset': 'future', 'strategy': 'ict-turtle-soup'}) == 'futures'
    assert cal.bucket_for({'asset': 'option', 'strategy': '0dte-underlying-orb-v2'}) == '0dte'
    assert cal.bucket_for({'asset': 'option', 'strategy': 'trap-spread'}) == 'trap-spread'
    assert cal.bucket_for({'asset': 'option', 'strategy': 'smoother-weekly'}) == 'smoothers'
    assert cal.bucket_for({'asset': 'option', 'strategy': 'swing20-v1'}) == 'swings'
    assert cal.bucket_for({'asset': 'option', 'strategy': 'mystery'}) == 'other'


def test_exit_day_is_chicago():
    # 2026-10-01 20:00 UTC is 15:00 CDT on Oct 1
    assert cal.exit_day({'exited_at': TS_OCT1}) == '2026-10-01'
    # 2026-10-01 04:00 UTC is 23:00 CDT on Sep 30
    late = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc).timestamp()
    assert cal.exit_day({'exited_at': late}) == '2026-09-30'
    assert cal.exit_day({}) is None


def test_month_calendar_groups_and_respects_cutoff(db):
    with db.tx() as c:
        db.put(c, 'trade:a', trade('a', '0dte-orb', 'option', 900.0, TS_OCT1))
        db.put(c, 'trade:b', trade('b', '0dte-orb', 'option', -100.0, TS_OCT1))
        db.put(c, 'trade:c', trade('c', 'ict-turtle-soup', 'future', 250.0, TS_SEP30))
        db.put(c, 'trade:d', trade('d', '0dte-orb', 'option', 9999.0, TS_SEP29))
        db.put(c, 'trade:e', trade('e', '0dte-orb', 'option', 50.0, TS_OCT1, status='open'))
        db.put(c, 'trade:f', trade('f', 'trap-spread', 'option', 120.0, TS_OCT1))
        out = cal.month_calendar(db, c, '2026-10', '2026-09-30')
    assert out['month'] == '2026-10'
    assert out['cutoff_day'] == '2026-09-30'
    # Sep 29 trade excluded by cutoff; open trade excluded; Sep 30 out of month
    assert '2026-09-29' not in out['days']
    assert '2026-09-30' not in out['days']
    day = out['days']['2026-10-01']
    assert day['buckets']['0dte'] == {'pnl': 800.0, 'trades': 2, 'wins': 1}
    assert day['buckets']['trap-spread'] == {'pnl': 120.0, 'trades': 1, 'wins': 1}
    assert day['total'] == {'pnl': 920.0, 'trades': 3, 'wins': 2}
    mt = out['month_total']['total']
    assert mt == {'pnl': 920.0, 'trades': 3, 'wins': 2}


def test_month_calendar_september(db):
    with db.tx() as c:
        db.put(c, 'trade:c', trade('c', 'ict-turtle-soup', 'future', 250.0, TS_SEP30))
        out = cal.month_calendar(db, c, '2026-09', '2026-09-30')
    assert out['days']['2026-09-30']['buckets']['futures']['pnl'] == 250.0


def test_month_calendar_empty(db):
    with db.tx() as c:
        out = cal.month_calendar(db, c, '2026-10', '2026-09-30')
    assert out['days'] == {}
    assert out['month_total']['total'] == {'pnl': 0.0, 'trades': 0, 'wins': 0}
