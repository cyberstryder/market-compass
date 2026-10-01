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
    # gap + breakout support (+1 each), apex 'info' (rows present) adds +0.25.
    assert out['evidence_score'] == 2.25
    assert out['pillars']['apex']['alignment'] == 'info'


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


def seed_ict(db, c, det_key, rows, at=NOW):
    db.put(c, det_key + ':latest', {'at': at, 'rows': rows})


def test_ict_pillar_absent_for_equity(db):
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'SPY', 'long', 600.0, 610.0, 'sal')
    assert 'ict' not in out['pillars']


def test_ict_supports_when_detector_fired_with_pick(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_bos_fvg', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'direction': 'short',
                       'signal_ts': NOW - 1800, 'at': NOW}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    ict = out['pillars']['ict']
    assert ict['alignment'] == 'supports'
    assert ict['detail']['bos_fvg']['verdict'] == 'supports'
    assert ict['detail']['golden_zone']['verdict'] == 'no_data'


def test_ict_contradicts_on_opposite_signal(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_bos_fvg', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'direction': 'long',
                       'signal_ts': NOW - 1800, 'at': NOW}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    assert out['pillars']['ict']['alignment'] == 'contradicts'


def test_ict_neutral_on_excluded_no_setup(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_bos_fvg', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'excluded': 'no_bos',
                       'at': NOW}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    ict = out['pillars']['ict']
    assert ict['alignment'] == 'neutral'
    assert ict['detail']['bos_fvg']['verdict'] == 'neutral'


def test_ict_no_data_on_no_bars_and_stale(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_bos_fvg', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'excluded': 'no_bars',
                       'at': NOW}})
        seed_ict(db, c, 'ict_golden_zone', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'direction': 'short',
                       'signal_ts': NOW - 1800, 'at': NOW}},
                 at=NOW - 100 * 3600)
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    ict = out['pillars']['ict']
    assert ict['detail']['bos_fvg']['verdict'] == 'no_data'
    assert ict['detail']['golden_zone']['verdict'] == 'no_data'
    assert ict['alignment'] == 'no_data'


def test_ict_smt_pair_matching(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_smt_divergence', {
            'MNQ.c.0:MES.c.0': {'pair': 'MNQ.c.0:MES.c.0',
                               'direction': 'short',
                               'signal_ts': NOW - 900, 'at': NOW}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    ict = out['pillars']['ict']
    assert ict['detail']['smt_divergence']['verdict'] == 'supports'
    assert ict['alignment'] == 'supports'


def test_ict_split_detectors_neutral(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_bos_fvg', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'direction': 'short',
                       'signal_ts': NOW - 1800, 'at': NOW}})
        seed_ict(db, c, 'ict_turtle_soup', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'direction': 'long',
                       'signal_ts': NOW - 1800, 'at': NOW}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    assert out['pillars']['ict']['alignment'] == 'neutral'


def test_ict_pillar_counts_in_score(db):
    with db.tx() as c:
        seed_ict(db, c, 'ict_bos_fvg', {
            'MNQ.c.0': {'symbol': 'MNQ.c.0', 'direction': 'short',
                       'signal_ts': NOW - 1800, 'at': NOW}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    # 5 equity pillars no_data (not counted) + ict supports (+1, counted)
    assert out['evidence_score'] == 1
    assert out['pillars_counted'] == 1


def test_info_scores_quarter_not_zero(db):
    """'info' pillars contribute +0.25, separating real-but-unconfirmed
    evidence from true no-data (0)."""
    with db.tx() as c:
        seed(db, c)  # apex rows present -> 'info'; gap/breakout -> 'supports'
        out = pc.check(db, c, NOW, 'spy', 'long', 600.0, 612.0, 'sal')
    assert out['pillars']['apex']['alignment'] == 'info'
    # 2 supports (+2) + 1 info (+0.25); no_data pillars add nothing.
    assert out['evidence_score'] == 2.25
    # 'info' is not a voting pillar: pillars_counted excludes it.
    assert out['pillars_counted'] == 2


def test_info_vs_no_data_separation(db):
    """Two checks differing only by info-vs-no_data must score differently."""
    with db.tx() as c:
        # No seeds at all: every pillar no_data -> score 0.
        out_empty = pc.check(db, c, NOW, 'SPY', 'long', 600.0, 610.0, 'sal')
    assert out_empty['evidence_score'] == 0
    with db.tx() as c:
        # Only apex rows (info), nothing else.
        db.put(c, 'apex_magnet:latest', {
            'at': NOW, 'radius': 200, 'tolerance': 0.0015, 'excluded': {},
            'rows': {'SPY': {'symbol': 'SPY', 'spot': 600.0, 'magnet': 605.0,
                             'distance_pct': 0.008, 'signal': 'approaching'}}})
        out_info = pc.check(db, c, NOW, 'SPY', 'long', 600.0, 610.0, 'sal')
    assert out_info['pillars']['apex']['alignment'] == 'info'
    assert out_info['evidence_score'] == 0.25
    assert out_info['evidence_score'] > out_empty['evidence_score']


def test_scoring_weights_unchanged(db):
    """supports=+1, contradicts=-1, neutral=0, no_data=0 are unchanged."""
    with db.tx() as c:
        seed(db, c)
        out_long = pc.check(db, c, NOW, 'spy', 'long', 600.0, 612.0, 'sal')
        out_short = pc.check(db, c, NOW, 'SPY', 'short', 600.0, 590.0, 'x')
    # long: 2 supports + 1 info = 2.25
    assert out_long['evidence_score'] == 2.25
    # short: 2 contradicts (-2) + 1 info (+0.25) = -1.75
    assert out_short['pillars']['gap']['alignment'] == 'contradicts'
    assert out_short['pillars']['breakout']['alignment'] == 'contradicts'
    assert out_short['evidence_score'] == -1.75
