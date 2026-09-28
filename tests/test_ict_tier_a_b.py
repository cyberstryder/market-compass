from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import tier_a_b as tb

NOW = 1700000000.0
SYM = 'NQ.c.0'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'tiers.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def sig_row(concept, direction='long', ts=NOW, entry=100.0, stop=99.0,
            target=102.0, symbol=SYM, **kw):
    row = {'symbol': symbol, 'concept': concept, 'direction': direction,
           'signal_ts': ts, 'entry': entry, 'stop': stop, 'target': target,
           'risk_pts': abs(entry - stop), 'reward_pts': abs(target - entry),
           'rr': 2.0, 'level_price': 101.0, 'level_kind': 'session_high',
           'at': ts}
    row.update(kw)
    return row


def seed(db, c, key, symbol, rows):
    db.put(c, key, {'at': NOW, 'rows': {symbol: rows}})


def flat_bars(n=60, low=99.0, rng=1.0):
    rows = []
    for i in range(n):
        ts = i * 60
        rows.append([ts, 99.5, low + rng, low, 99.5, 1000])
    return rows


class Cfg:
    ict_tier_a_b = True


# --- scan tiering ---

def test_three_concepts_form_tier_a(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM,
             [sig_row('turtle_soup', ts=NOW)])
        seed(db, c, 'ict_continuation:latest', SYM,
             [sig_row('continuation', ts=NOW - 100, shift='fresh')])
        seed(db, c, 'ict_trendline_liquidity:latest', SYM,
             [sig_row('trendline', ts=NOW - 200)])
        out = tb.scan(db, c, Cfg(), NOW)
    assert out == {'ran': True, 'tier_a': 1, 'tier_b': 0, 'unranked': 0,
                   'n_groups': 1}
    with db.tx() as c:
        latest = db.get(c, 'ict_tier_a_b:latest')
        tier = latest['tiers']['A'][0]
        assert tier['tier'] == 'A' and tier['n_concepts'] == 3
        assert tier['concepts'] == ['continuation', 'trendline', 'turtle_soup']
        assert tier['shift'] == 'fresh'  # copied from the continuation row
        assert tier['entry'] == pytest.approx(100.0)
        assert tier['stop'] == pytest.approx(99.0)   # conservative extreme
        assert tier['target'] == pytest.approx(102.0)
        assert len(tier['component_rows']) == 3
        disp = tb.display(db, c, NOW)
        assert len(disp['tier_a']) == 1
        assert disp['note'].startswith('Research evidence only')


def test_two_concepts_form_tier_b(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM, [sig_row('turtle_soup', ts=NOW)])
        seed(db, c, 'ict_trendline_liquidity:latest', SYM,
             [sig_row('trendline', ts=NOW - 300)])
        out = tb.scan(db, c, Cfg(), NOW)
    assert out['tier_b'] == 1 and out['tier_a'] == 0
    with db.tx() as c:
        tier = db.get(c, 'ict_tier_a_b:latest')['tiers']['B'][0]
        assert tier['tier'] == 'B' and tier['n_concepts'] == 2


def test_single_concept_goes_unranked(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM, [sig_row('turtle_soup', ts=NOW)])
        out = tb.scan(db, c, Cfg(), NOW)
    assert out['unranked'] == 1 and out['tier_a'] == 0 and out['tier_b'] == 0
    with db.tx() as c:
        latest = db.get(c, 'ict_tier_a_b:latest')
        assert latest['tiers'] == {'A': [], 'B': []}
        assert len(latest['unranked']) == 1
        assert latest['unranked'][0]['tier'] is None


def test_window_expiry_splits_groups(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM,
             [sig_row('turtle_soup', ts=NOW), sig_row('turtle_soup', ts=NOW - 2000)])
        out = tb.scan(db, c, Cfg(), NOW)
    # 2000s apart > 1800s window -> two separate one-concept groups
    assert out['unranked'] == 2 and out['n_groups'] == 2


def test_chain_link_keeps_nearby_rows_together(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM,
             [sig_row('turtle_soup', ts=NOW), sig_row('turtle_soup', ts=NOW - 1700)])
        out = tb.scan(db, c, Cfg(), NOW)
    assert out['n_groups'] == 1 and out['unranked'] == 1


def test_missing_snapshots_yield_empty_tiers(db):
    with db.tx() as c:
        out = tb.scan(db, c, Cfg(), NOW)
    assert out == {'ran': True, 'tier_a': 0, 'tier_b': 0, 'unranked': 0,
                   'n_groups': 0}


def test_aoi_zone_bias_becomes_directional_row(db):
    zone = {'symbol': SYM, 'zone_kind': 'discount', 'top': 101.0,
            'bottom': 99.0, 'bias': 'long', 'formed_ts': NOW - 50, 'at': NOW - 50}
    with db.tx() as c:
        db.put(c, 'ict_aoi_zones:latest', {'at': NOW, 'rows': {SYM: [zone]}})
        seed(db, c, 'ict_turtle_soup:latest', SYM, [sig_row('turtle_soup', ts=NOW)])
        seed(db, c, 'ict_continuation:latest', SYM,
             [sig_row('continuation', ts=NOW - 100)])
        out = tb.scan(db, c, Cfg(), NOW)
    assert out['tier_a'] == 1
    with db.tx() as c:
        tier = db.get(c, 'ict_tier_a_b:latest')['tiers']['A'][0]
        assert set(tier['concepts']) == {'aoi', 'turtle_soup', 'continuation'}
        assert tier['direction'] == 'long'


def test_dict_row_shape_is_tolerated(db):
    # Older/foreign snapshots may store {symbol: row} instead of {symbol: [row]}
    with db.tx() as c:
        db.put(c, 'ict_turtle_soup:latest',
               {'at': NOW, 'rows': {SYM: sig_row('turtle_soup', ts=NOW)}})
        out = tb.scan(db, c, Cfg(), NOW)
    assert out['unranked'] == 1 and out['n_groups'] == 1


# --- shift computation ---

def test_shift_computed_fresh_from_nearby_level(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM,
             [sig_row('turtle_soup', ts=NOW, entry=100.0)])
        db.put(c, 'ict_session_liquidity:latest',
               {'at': NOW, 'levels': [{'symbol': SYM, 'session': 'ny_am',
                                       'high': 100.2, 'low': 99.0}]})
        db.put(c, 'bar_window:' + SYM, flat_bars())  # ATR = 1.0
        tb.scan(db, c, Cfg(), NOW)
        latest = db.get(c, 'ict_tier_a_b:latest')
    assert latest['unranked'][0]['shift'] == 'fresh'  # 0.2 pts < 1.0*ATR


def test_shift_computed_extended_from_distant_level(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM,
             [sig_row('turtle_soup', ts=NOW, entry=100.0)])
        db.put(c, 'ict_session_liquidity:latest',
               {'at': NOW, 'levels': [{'symbol': SYM, 'session': 'ny_am',
                                       'high': 110.0, 'low': 95.0}]})
        db.put(c, 'bar_window:' + SYM, flat_bars())  # ATR = 1.0
        tb.scan(db, c, Cfg(), NOW)
        latest = db.get(c, 'ict_tier_a_b:latest')
    assert latest['unranked'][0]['shift'] == 'extended'


def test_shift_unknown_without_levels_or_bars(db):
    with db.tx() as c:
        seed(db, c, 'ict_turtle_soup:latest', SYM,
             [sig_row('turtle_soup', ts=NOW, shift=None)])
        tb.scan(db, c, Cfg(), NOW)
        latest = db.get(c, 'ict_tier_a_b:latest')
    assert latest['unranked'][0]['shift'] == 'unknown'


# --- misc ---

def test_scan_disabled_flag(db):
    with db.tx() as c:
        cfg = SimpleNamespace(ict_tier_a_b=False)
        assert tb.scan(db, c, cfg, NOW) == {'ran': False, 'reason': 'disabled'}


def test_scan_throttles(db):
    with db.tx() as c:
        assert tb.scan(db, c, Cfg(), NOW)['ran'] is True
        assert tb.scan(db, c, Cfg(), NOW + 10) == {'ran': False, 'reason': 'throttled'}


def test_group_confluence_pure():
    rows = [
        {'symbol': 'A', 'direction': 'long', 'concept': 'x', 'signal_ts': 1000},
        {'symbol': 'A', 'direction': 'long', 'concept': 'y', 'signal_ts': 2000},
        {'symbol': 'A', 'direction': 'long', 'concept': 'z', 'signal_ts': 5000},
        {'symbol': 'A', 'direction': 'short', 'concept': 'x', 'signal_ts': 1000},
        {'symbol': 'B', 'direction': 'long', 'concept': 'x', 'signal_ts': 1000},
    ]
    groups = tb.group_confluence(rows, 1800)
    assert len(groups) == 4
    longs = [g for g in groups if g['symbol'] == 'A' and g['direction'] == 'long']
    assert len(longs) == 2
    first = min(longs, key=lambda g: g['ts_min'])
    assert first['concepts'] == ['x', 'y'] and len(first['rows']) == 2
    assert first['ts_max'] - first['ts_min'] == 1000
