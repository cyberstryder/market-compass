import time

import pytest

from compass.store import Store
from compass.market import day
from compass import pick_check as pc


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'pick.db'))
    store.initialize()
    yield store
    store.engine.dispose()


NOW = time.time()
TODAY = day(NOW)


def seed(db, c):
    db.put(c, 'apex_magnet:latest', {
        'at': NOW, 'radius': 200, 'tolerance': 0.0015, 'excluded': {},
        'rows': {'SPY': {'symbol': 'SPY', 'spot': 600.0, 'magnet': 605.0,
                         'distance_pct': 0.008, 'signal': 'approaching'}}})
    db.put(c, 'gap_cont:SPY:%s' % TODAY,
           {'symbol': 'SPY', 'day': TODAY, 'stage': 'qualified', 'qualified': True,
            'gap_pct': 0.021, 'gap_direction': 'up', 'break_direction': 'up'})
    db.put(c, 'breakout:SPY:%s:range:up' % TODAY,
           {'symbol': 'SPY', 'direction': 'up', 'level': 598.0, 'status': 'fresh',
            'day': TODAY})


def test_direction_variants():
    assert pc.normalize_direction('call') == 'long'
    assert pc.normalize_direction('PUT') == 'short'
    assert pc.normalize_direction('Bearish') == 'short'
    with pytest.raises(ValueError):
        pc.normalize_direction('sideways')


def test_empty_db_all_no_data(db):
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'SPY', 'long', 600.0, 610.0, 'sal')
    assert out['ticker'] == 'SPY'
    assert out['direction'] == 'long'
    assert out['pillars']['tape']['alignment'] == 'no_data'
    assert out['pillars']['gap']['alignment'] == 'no_data'
    assert out['pillars']['breakout']['alignment'] == 'no_data'
    assert out['note'].startswith('Research evidence only')


def test_seeded_supports_long(db):
    with db.tx() as c:
        seed(db, c)
        out = pc.check(db, c, NOW, 'spy', 'long', 600.0, 612.0, 'sal')
    assert out['ticker'] == 'SPY'  # normalized
    assert out['pillars']['gap']['alignment'] == 'supports'
    assert out['pillars']['breakout']['alignment'] == 'supports'
    assert out['pillars']['apex']['detail']['nearest_above']['magnet'] == 605.0
    # Magnet at 605 sits between spot 600 and target 612.
    assert '605.00' in (out['pillars']['apex']['note'] or '')
    assert out['spot'] == 600.0
    assert out['evidence_score'] == 2


def test_contradicts_short(db):
    with db.tx() as c:
        seed(db, c)
        out = pc.check(db, c, NOW, 'SPY', 'short', 600.0, 590.0)
    assert out['pillars']['gap']['alignment'] == 'contradicts'
    assert out['pillars']['breakout']['alignment'] == 'contradicts'


def test_tape_section_logic():
    disp = {'recent_confirmed': [
        {'symbol': 'SPY', 'side': 'call', 'trial_id': 'a'},
        {'symbol': 'SPY', 'side': 'put', 'trial_id': 'b'},
        {'symbol': 'QQQ', 'side': 'call', 'trial_id': 'c'},
    ]}
    sec = pc._tape_section(disp, 'SPY', 'long')
    assert sec['alignment'] == 'neutral'
    assert sec['detail'] == {'confirmed': 2, 'with_pick': 1, 'against_pick': 1}
    sec = pc._tape_section(disp, 'QQQ', 'short')
    assert sec['alignment'] == 'contradicts'


def test_validation(db):
    with db.tx() as c:
        with pytest.raises(ValueError):
            pc.check(db, c, NOW, '', 'long')
        with pytest.raises(ValueError):
            pc.check(db, c, NOW, 'SPY', 'sideways')
        with pytest.raises(ValueError):
            pc.check(db, c, NOW, 'SPY', 'long', entry=-5)
        with pytest.raises(ValueError):
            pc.check(db, c, NOW, 'SPY!!!', 'long')


def test_record_logged_and_retrievable(db):
    with db.tx() as c:
        pc.check(db, c, NOW, 'SPY', 'long', 600.0, 610.0, 'sal')
        rows = pc.recent(db, c, 10)
    assert len(rows) == 1
    rec = rows[0]['payload']
    assert rec['ticker'] == 'SPY' and rec['source'] == 'sal'
    assert set(rec['pillars']) == {'apex', 'tape', 'gap', 'breakout', 'exposure'}
