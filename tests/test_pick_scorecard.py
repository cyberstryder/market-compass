import time

import pytest

from compass.store import Store
from compass import pick_scorecard as ps


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'sc.db'))
    store.initialize()
    yield store
    store.engine.dispose()


NOW = 1_700_000_000.0
DAY = 86400


def bars(symbol, closes, highs=None, lows=None, start=NOW + DAY, step=DAY):
    # Bars start the session after the check: a bar stamped exactly at the
    # check time is (correctly) excluded by the anti-lookahead filter.
    highs = highs or closes
    lows = lows or closes
    return [(symbol, start + i * step,
             {'o': closes[i], 'h': highs[i], 'l': lows[i], 'c': closes[i],
              'v': 1000})
            for i in range(len(closes))]


def seed_bars(db, c, symbol, closes, **kw):
    db.append_bars(c, 'daily', 'test', bars(symbol, closes, **kw))


def check(**kw):
    base = {'ticker': 'SPY', 'direction': 'long', 'entry': None,
            'target': None, 'spot': None, 'source': 'sal',
            'evidence_score': 1, 'at': NOW}
    base.update(kw)
    return base


def test_target_hit_long_is_win(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104, 105],
                  highs=[101, 106, 103, 104, 105, 106])
        out = ps.measure_outcome(db, c, check(entry=100.0, target=105.0), NOW)
    assert out['status'] == 'win'
    assert out['target_hit'] is True
    assert out['mfe_pct'] == 6.0
    assert out['entry_source'] == 'given'


def test_target_missed_full_horizon_is_loss(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 100, 100, 100, 100],
                  highs=[101, 101, 101, 101, 101])
        out = ps.measure_outcome(db, c, check(entry=100.0, target=120.0), NOW)
    assert out['status'] == 'loss'
    assert out['target_hit'] is False
    assert out['sessions_measured'] == 5


def test_incomplete_horizon_is_open(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101], highs=[101, 102])
        out = ps.measure_outcome(db, c, check(entry=100.0, target=120.0), NOW)
    assert out['status'] == 'open'
    assert out['sessions_measured'] == 2


def test_short_target_hit(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 99, 98, 97, 96],
                  lows=[99, 98, 94, 96, 95])
        out = ps.measure_outcome(db, c, check(direction='short', entry=100.0,
                                             target=95.0), NOW)
    assert out['status'] == 'win'
    assert out['target_hit'] is True
    assert out['mfe_pct'] == 6.0  # direction-adjusted


def test_no_target_resolves_on_horizon_return(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104])
        win = ps.measure_outcome(db, c, check(entry=100.0), NOW)
    assert win['status'] == 'win'
    assert win['horizon_return_pct'] == 4.0
    with db.tx() as c:
        seed_bars(db, c, 'QQQ', [100, 99, 98, 97, 96])
        loss = ps.measure_outcome(db, c, check(ticker='QQQ', direction='short',
                                              entry=100.0), NOW)
    assert loss['status'] == 'win'  # short profits as price falls


def test_no_bars_is_unknown(db):
    with db.tx() as c:
        out = ps.measure_outcome(db, c, check(entry=100.0, target=110.0), NOW)
    assert out['status'] == 'unknown'


def test_entry_falls_back_to_spot(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104],
                  highs=[101, 102, 103, 104, 110])
        out = ps.measure_outcome(db, c, check(spot=100.0, target=108.0), NOW)
    assert out['status'] == 'win'
    assert out['entry_source'] == 'spot'


def test_first_close_entry_excludes_entry_bar(db):
    # No entry/spot: entry becomes day-1 close; day-1's high must not count.
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104, 105],
                  highs=[110, 101, 102, 103, 104, 105])
        out = ps.measure_outcome(db, c, check(target=108.0), NOW)
    assert out['entry_source'] == 'first_close'
    assert out['entry_used'] == 100.0
    assert out['target_hit'] is False
    assert out['status'] == 'loss'


def test_no_lookahead(db):
    # Bars at/before the check time are ignored.
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [90, 91], highs=[150, 150],
                  start=NOW - 2 * DAY)
        out = ps.measure_outcome(db, c, check(entry=100.0, target=105.0), NOW)
    assert out['status'] == 'unknown'


def _log(db, c, **kw):
    rec = check(**kw)
    db.append(c, 'pick_check', 'dashboard', rec['ticker'], rec['at'], rec)


def test_scorecard_aggregates_by_source(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104, 105],
                  highs=[101, 106, 103, 104, 105, 106])
        seed_bars(db, c, 'QQQ', [100, 100, 100, 100, 100],
                  highs=[101, 101, 101, 101, 101])
        _log(db, c, ticker='SPY', entry=100.0, target=105.0, source='sal',
             evidence_score=2)
        _log(db, c, ticker='QQQ', entry=100.0, target=120.0, source='sal',
             evidence_score=-1)
        _log(db, c, ticker='SPY', entry=100.0, target=105.0, source='wex',
             evidence_score=3)
        sc = ps.scorecard(db, c, NOW + 10 * DAY)
    by = {b['source']: b for b in sc['by_source']}
    assert by['sal']['checks'] == 2
    assert (by['sal']['wins'], by['sal']['losses']) == (1, 1)
    assert by['sal']['hit_rate'] == 0.5
    assert by['wex']['hit_rate'] == 1.0
    assert by['sal']['avg_evidence_score_win'] == 2.0
    assert by['sal']['avg_evidence_score_loss'] == -1.0
    assert len(sc['checks']) == 3
    assert sc['horizon_sessions'] == 5
