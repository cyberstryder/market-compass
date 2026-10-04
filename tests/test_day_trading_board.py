import pytest
from compass.store import Store
from compass.market import session, day
from compass import day_trading_board as dtb

DAY = '2026-09-25'
SESS_OPEN, SESS_CLOSE = session(DAY)
T_BUILD, T_REFRESH = SESS_OPEN - 3600, SESS_OPEN - 1800


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'board.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class Cfg:
    day_trading_board = True
    stocks = ['SPY', 'QQQ']


@pytest.fixture
def morning(db):
    with db.tx() as c:
        now = T_BUILD + 1200
        db.append(c, 'daily', 'test', 'SPY', SESS_OPEN - 86400,
                  {'o': 99, 'h': 101, 'l': 98, 'c': 100.0})
        db.put(c, 'quote:SPY', {'bid': 102.0, 'ask': 102.1, 'ts': now})
        db.put(c, 'research:earnings',
               {'items': [{'symbol': 'SPY', 'date': DAY, 'timing': 'before_open'}]})
        db.put(c, 'research:economic_calendar',
               {'items': [{'name': 'CPI', 'date': DAY, 'impact': 'high'}]})
        db.put(c, 'research:extra_sector_map', {'SPY': 'Technology'})
        db.put(c, 'research:sector_dashboard',
               {'sectors': {'Technology': {'flow_direction': 'bullish'}}})
        db.put(c, 'matrix:unusual_activity',
               {'rows': [{'symbol': 'SPY', 'source_ts': now - 600,
                           'option_type': 'call', 'premium': 300000}]})
        db.append(c, 'alert', 'test', 'SPY', now,
                  {'status': 'setup_triggered', 'id': 'sig1'}, key='s1')
    return db, now


def test_component_scores(morning):
    db, now = morning
    with db.tx() as c:
        ctx = {'flow_rows': db.get(c, 'matrix:unusual_activity')['rows'],
               'sector_dashboard': db.get(c, 'research:sector_dashboard'),
               'sector_map': db.get(c, 'research:extra_sector_map'),
               'earnings': db.get(c, 'research:earnings'),
               'economic_calendar': db.get(c, 'research:economic_calendar')}
        row = dtb.score_symbol(db, c, 'SPY', now, ctx)
    comps = row['components']
    assert comps['earnings']['score'] == 1
    assert comps['economic']['score'] == 1
    assert comps['mover']['score'] == 1  # +2.05%
    assert comps['flow']['score'] == 1
    assert comps['sector']['score'] == 1
    assert comps['radar']['score'] == 1
    assert row['total'] == 6


def test_earnings_during_market_hours_scores_two(morning):
    db, now = morning
    with db.tx() as c:
        db.put(c, 'research:earnings',
               {'items': [{'symbol': 'SPY', 'date': DAY, 'timing': 'during_market_hours'}]})
        row = dtb.score_symbol(db, c, 'SPY', now, {'earnings': db.get(c, 'research:earnings')})
    assert row['components']['earnings']['score'] == 2


def test_quiet_symbol_scores_zero(morning):
    db, now = morning
    with db.tx() as c:
        row = dtb.score_symbol(db, c, 'QQQ', now, {})
    assert row['total'] == 0


def test_cadence_build_refresh_freeze(morning):
    db, now = morning
    with db.tx() as c:
        assert dtb.scan(db, c, Cfg(), T_BUILD - 60)['action'] is None
        assert db.get(c, 'board:' + DAY) is None
        r = dtb.scan(db, c, Cfg(), T_BUILD + 60)
        assert r['action'] == 'built' and r['symbols'] == 3  # SPY, QQQ + MU
        board = db.get(c, 'board:' + DAY)
        assert board['rows'][0]['symbol'] == 'SPY'  # ranked first
        assert board['frozen'] is False
        db.put(c, 'day_board:scanned_at', 0)
        r = dtb.scan(db, c, Cfg(), T_REFRESH + 60)
        assert r['action'] == 'refreshed'
        db.put(c, 'day_board:scanned_at', 0)
        r = dtb.scan(db, c, Cfg(), SESS_OPEN + 60)
        assert r['action'] == 'frozen'
        assert db.get(c, 'board:' + DAY)['frozen'] is True
        db.put(c, 'day_board:scanned_at', 0)
        assert dtb.scan(db, c, Cfg(), SESS_OPEN + 120)['action'] is None


def test_evening_review(morning):
    db, now = morning
    with db.tx() as c:
        dtb.scan(db, c, Cfg(), T_BUILD + 60)
        db.put(c, 'day_board:scanned_at', 0)
        dtb.scan(db, c, Cfg(), SESS_OPEN + 60)  # freeze
        # Off-board name produces an alert; board name has intraday range.
        db.append(c, 'alert', 'test', 'XYZ', SESS_OPEN + 3600,
                  {'status': 'something_else'}, key='x1')
        for i, px in enumerate((102, 103, 101.5)):
            db.append(c, 'bar', 'test', 'SPY', SESS_OPEN + 3600 + 60 * i,
                      {'o': px, 'h': px + 0.2, 'l': px - 0.2, 'c': px})
        db.put(c, 'day_board:scanned_at', 0)
        r = dtb.scan(db, c, Cfg(), SESS_CLOSE + 60)
        assert r['review'] == 'reviewed'
        review = db.get(c, 'board_review:' + DAY)
        spy = next(n for n in review['names'] if n['symbol'] == 'SPY')
        assert spy['had_candidate'] is True and spy['day_range_pct'] is not None
        assert review['board_hit_rate'] == pytest.approx(1/3)  # SPY hit of 3 (SPY, QQQ, MU)
        # 1 off-board alert (XYZ) of 2 total alerts (SPY setup_triggered + XYZ).
        assert review['off_board_alert_fraction'] == pytest.approx(0.5)


def test_display(morning):
    db, now = morning
    with db.tx() as c:
        dtb.scan(db, c, Cfg(), T_BUILD + 60)
        out = dtb.display(db, c, now)
    # New feeds (2026-10-01 revival): the dead vendor research keys the
    # fixture seeds are ignored. SPY scores mover + flow + radar = 3;
    # earnings (FMP stub), economic (no 2026-09-25 event), and sector
    # (index ETFs have no sector mapping) are 0.
    assert out['board'][0]['total'] == 3 and out['frozen'] is False
    assert 'push to #morning-brief' in out['note']
