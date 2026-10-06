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


def test_invalidation_first_is_immediate_loss(db):
    # Invalidation touched on day 2 while the horizon is incomplete:
    # the setup failed, so it resolves as a loss right away, not 'open'.
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101],
                  highs=[101, 102], lows=[99.5, 98.0])
        out = ps.measure_outcome(
            db, c, check(entry=100.0, target=105.0, invalidation=99.0), NOW)
    assert out['status'] == 'loss'
    assert out['invalidation_hit_first'] is True
    assert out['target_hit'] is False


def test_target_before_invalidation_is_win(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104],
                  highs=[101, 106, 103, 104, 105],
                  lows=[99.5, 100.0, 101.0, 102.0, 103.0])
        out = ps.measure_outcome(
            db, c, check(entry=100.0, target=105.0, invalidation=99.0), NOW)
    assert out['status'] == 'win'
    assert out['target_hit'] is True
    assert out['invalidation_hit_first'] is False


def test_same_bar_touch_scores_conservative_loss(db):
    # One daily bar touches both target and invalidation: order is
    # unknowable from daily bars, so it scores as invalidation-first.
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104],
                  highs=[106, 102, 103, 104, 105],
                  lows=[98.0, 100.0, 101.0, 102.0, 103.0])
        out = ps.measure_outcome(
            db, c, check(entry=100.0, target=105.0, invalidation=99.0), NOW)
    assert out['status'] == 'loss'
    assert out['invalidation_hit_first'] is True


def test_short_invalidation_first_is_loss(db):
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102],
                  highs=[101, 103, 104], lows=[99, 100, 101])
        out = ps.measure_outcome(
            db, c, check(direction='short', entry=100.0, target=95.0,
                         invalidation=102.0), NOW)
    assert out['status'] == 'loss'
    assert out['invalidation_hit_first'] is True


def test_no_invalidation_keeps_legacy_behavior(db):
    # Without an invalidation level, a target touch is a win even if price
    # later fell hard — the pre-existing rule is unchanged.
    with db.tx() as c:
        seed_bars(db, c, 'SPY', [100, 101, 102, 103, 104],
                  highs=[101, 106, 103, 104, 105],
                  lows=[90.0, 90.0, 90.0, 90.0, 90.0])
        out = ps.measure_outcome(db, c, check(entry=100.0, target=105.0), NOW)
    assert out['status'] == 'win'
    assert out['target_hit'] is True


def test_scorecard_groups_by_pattern(db):
    with db.tx() as c:
        seed_bars(db, c, 'AAPL', [332, 333, 334, 335, 336, 337],
                  highs=[333, 334, 335, 336, 337, 338])
        seed_bars(db, c, 'NVDA', [237, 237, 237, 237, 237],
                  highs=[238, 238, 238, 238, 238])
        _log(db, c, ticker='AAPL', direction='long', entry=332.0, target=335.0,
             invalidation=330.0, source='flash_agentic', pattern='FLOOR BOUNCE #4')
        _log(db, c, ticker='NVDA', direction='long', entry=237.0, target=240.0,
             invalidation=235.0, source='flash_agentic', pattern='FLOOR BOUNCE #4')
        sc = ps.scorecard(db, c, NOW + 10 * DAY)
    bp = {(b['source'], b['pattern']): b for b in sc['by_pattern']}
    fb = bp[('flash_agentic', 'FLOOR BOUNCE #4')]
    assert fb['checks'] == 2
    assert (fb['wins'], fb['losses']) == (1, 1)
    assert fb['hit_rate'] == 0.5


def test_planned_r_long_short_and_invalid():
    assert ps.planned_r({'direction': 'long', 'entry': 100.0, 'target': 106.0,
                         'invalidation': 98.0}) == 3.0
    assert ps.planned_r({'direction': 'short', 'entry': 100.0, 'target': 94.0,
                         'invalidation': 102.0}) == 3.0
    # no edge cases survive
    assert ps.planned_r({'direction': 'long', 'entry': 100.0, 'target': 106.0,
                         'invalidation': 100.0}) is None
    assert ps.planned_r({'direction': 'long', 'entry': 100.0, 'target': 99.0,
                         'invalidation': 98.0}) is None
    assert ps.planned_r({'direction': 'long', 'entry': 100.0, 'target': None,
                         'invalidation': 98.0}) is None
    # effective entry fallback
    assert ps.planned_r({'direction': 'long', 'entry': None, 'target': 106.0,
                         'invalidation': 98.0}, entry_used=100.0) == 3.0


def test_realized_r_win_loss_open():
    assert ps.realized_r({'status': 'win'}, 2.5) == 2.5
    assert ps.realized_r({'status': 'loss'}, 2.5) == -1.0
    assert ps.realized_r({'status': 'open'}, 2.5) is None
    assert ps.realized_r({'status': 'win'}, None) is None


def _seed_check(db, c, ts, **kw):
    db.append(c, 'pick_check', 'test', kw.get('ticker', 'AAA'), ts,
              {'ticker': kw.get('ticker', 'AAA'),
               'direction': kw.get('direction', 'long'),
               'entry': kw.get('entry'), 'target': kw.get('target'),
               'invalidation': kw.get('invalidation'),
               'spot': kw.get('spot'), 'source': kw.get('source', 'flash_agentic'),
               'pattern': kw.get('pattern', 'FLOOR BOUNCE #4'),
               'evidence_score': 80, 'at': ts},
              key='r-%s-%s' % (kw.get('ticker', 'AAA'), ts))


def test_scorecard_expectancy_r(db):
    now = NOW + 10 * DAY
    with db.tx() as c:
        # AAA long 100 -> target 110 hit (planned 2R: risk 5, reward 10)
        seed_bars(db, c, 'AAA', [100, 102, 112, 113, 114, 115],
                  highs=[101, 103, 112, 114, 115, 116],
                  lows=[99, 101, 102, 103, 104, 105])
        _seed_check(db, c, NOW, ticker='AAA', entry=100.0, target=110.0,
                    invalidation=95.0)
        # BBB long 100 -> invalidation 95 hit first (loss = -1R)
        seed_bars(db, c, 'BBB', [100, 98, 94, 96, 97, 98],
                  highs=[101, 99, 96, 97, 98, 99],
                  lows=[99, 97, 94, 95, 96, 97])
        _seed_check(db, c, NOW, ticker='BBB', entry=100.0, target=110.0,
                    invalidation=95.0)
        sc = ps.scorecard(db, c, now, limit=50)
    rows = sc['checks']
    assert len(rows) == 2
    by_r = {r['ticker']: r for r in rows}
    assert by_r['AAA']['planned_r'] == 2.0
    assert by_r['AAA']['realized_r'] == 2.0
    assert by_r['BBB']['realized_r'] == -1.0
    pat = [p for p in sc['by_pattern'] if p['pattern'] == 'FLOOR BOUNCE #4'][0]
    assert pat['expectancy_r'] == 0.5  # (2.0 + -1.0) / 2
    assert pat['total_r'] == 1.0
    assert pat['r_count'] == 2
    src = [s for s in sc['by_source'] if s['source'] == 'flash_agentic'][0]
    assert src['expectancy_r'] == 0.5
