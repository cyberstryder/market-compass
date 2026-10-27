"""Tests for the Simple page "What we're tracking" payload
(compass/simple.py tracking()): the live futures book, the 0DTE scanner
seat/shadow split, and the shadow iceberg watch. Style mirrors
tests/test_simple.py (Store fixture, db.put seeding)."""
import sys
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from compass.store import Store
from compass import simple

CT = ZoneInfo("America/Chicago")
NOW = time.time()
TODAY = datetime.fromtimestamp(NOW, CT).date().isoformat()


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'simple_tracking.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _ict_trade(tid, detector, symbol, pnl, exited_ago=3600, **kw):
    t = {'id': tid, 'detector': detector, 'symbol': symbol,
         'source': 'ict_paper', 'status': 'closed', 'pnl': pnl,
         'entered_at': NOW - exited_ago - 1800,
         'exited_at': NOW - exited_ago}
    t.update(kw)
    return t


def test_tracking_empty_db(db):
    with db.tx() as c:
        out = simple.summary(db, c, NOW)
    tr = out['tracking']
    assert tr is not None
    # Futures book: the six post-prune combos, live vs paper-only split.
    # Since the 2026-10-26 evening pause (AOI Fade carved out), NO
    # combo is live — every detector is a paper-only carve-out.
    fb = tr['futures_book']
    assert fb['window_days'] == 28
    assert len(fb['combos']) == 6
    live = {(x['detector'], x['symbol']) for x in fb['combos'] if x['live']}
    assert live == set()
    paper = {(x['detector'], x['symbol'])
             for x in fb['combos'] if not x['live']}
    assert paper == {('golden_zone', 'MCL'), ('golden_zone', 'MNQ'),
                     ('rumers_box', 'MYM'), ('turtle_soup', 'MCL'),
                     ('aoi_fade', 'MCL'), ('aoi_fade', 'MES')}
    assert all(x['trades'] == 0 and x['pf'] is None and not x['kill_watch']
               for x in fb['combos'])
    assert fb['kill_watch'] == []
    # Scanner: the validated seats render even with no data; the shadow
    # side is either not-yet-landed (None) or a zeroed scorecard.
    sc = tr['scanner']
    assert sc['paper_seats'] == {'session_sweep_reclaim': ['AMD', 'MU', 'NFLX']}
    assert len(sc['seated']) == 3
    assert all(x['trades'] == 0 and x['open'] == 0 for x in sc['seated'])
    assert sc['shadow'] is None or sc['shadow']['totals']['alerts'] == 0
    # Iceberg: clean zero state.
    ib = tr['iceberg']
    assert ib['alerts_window'] == 0
    assert ib['latest'] == []
    assert ib['promotion']['scored_total'] == 0
    assert ib['diag_present'] is False


def test_tracking_futures_book_stats_and_kill_watch(db):
    with db.tx() as c:
        db.put(c, 'trade:ict-a1', _ict_trade('a1', 'aoi_fade', 'MCL.c.0', 100.0))
        db.put(c, 'trade:ict-a2', _ict_trade('a2', 'aoi_fade', 'MCL.c.0', 50.0))
        db.put(c, 'trade:ict-a3', _ict_trade('a3', 'aoi_fade', 'MCL.c.0', -30.0))
        db.put(c, 'trade:ict-t1', _ict_trade('t1', 'turtle_soup', 'MCL.c.0', 20.0))
        db.put(c, 'trade:ict-t2', _ict_trade('t2', 'turtle_soup', 'MCL.c.0', -80.0))
        # data_gap trades never count (house rule).
        db.put(c, 'trade:ict-g1', _ict_trade('g1', 'golden_zone', 'MCL.c.0',
                                             999.0, data_gap=True))
        # Outside the 28-day window: excluded.
        db.put(c, 'trade:ict-o1', _ict_trade('o1', 'aoi_fade', 'MES.c.0',
                                             500.0, exited_ago=40 * 86400))
        out = simple.summary(db, c, NOW)
    fb = out['tracking']['futures_book']
    by = {(x['detector'], x['symbol']): x for x in fb['combos']}
    aoi = by[('aoi_fade', 'MCL')]
    assert aoi['trades'] == 3 and aoi['wins'] == 2 and aoi['losses'] == 1
    assert aoi['net'] == 120.0 and aoi['pf'] == 5.0
    assert aoi['win_rate'] == round(100.0 * 2 / 3, 1)
    assert aoi['kill_watch'] is False
    assert aoi['live'] is False  # paper-only since the 2026-10-26 pause
    ts = by[('turtle_soup', 'MCL')]
    assert ts['trades'] == 2 and ts['pf'] == 0.25 and ts['kill_watch'] is True
    assert ts['live'] is False  # paper-only since 2026-10-26; still tracked
    assert fb['kill_watch'] == ['turtle_soup|MCL']
    gz = by[('golden_zone', 'MCL')]
    assert gz['trades'] == 0 and gz['live'] is False
    mes = by[('aoi_fade', 'MES')]
    assert mes['trades'] == 0  # only the stale trade existed


def _scan_trade(tid, underlying, pnl, family='session_sweep_reclaim', **kw):
    t = {'id': tid, 'kind': 'scanner', 'family': family,
         'underlying': underlying, 'symbol': 'X', 'status': 'closed',
         'pnl': pnl, 'entered_at': NOW - 7200, 'exited_at': NOW - 3600,
         'evidence': {'snapshot': True}}
    t.update(kw)
    return t


def test_tracking_seated_scanner_stats(db):
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-m1',
               _scan_trade('m1', 'MU', 60.0))
        db.put(c, 'trade:0dte-scan-m2',
               _scan_trade('m2', 'MU', -40.0))
        db.put(c, 'trade:0dte-scan-m3',
               _scan_trade('m3', 'MU', None, status='open', exited_at=None))
        # data_gap and pre-evidence trades are excluded by _0dte's rules.
        db.put(c, 'trade:0dte-scan-m4',
               _scan_trade('m4', 'MU', 500.0, data_gap=True))
        db.put(c, 'trade:0dte-scan-m5',
               _scan_trade('m5', 'MU', 500.0, evidence=None))
        # Not seated: wrong name for the family, wrong family for the name.
        db.put(c, 'trade:0dte-scan-t1',
               _scan_trade('t1', 'TSLA', 500.0))
        db.put(c, 'trade:0dte-scan-v1',
               _scan_trade('v1', 'MU', 500.0, family='volume_breakout'))
        out = simple.summary(db, c, NOW)
    seated = {(x['family'], x['underlying']): x
              for x in out['tracking']['scanner']['seated']}
    mu = seated[('session_sweep_reclaim', 'MU')]
    assert mu['trades'] == 2 and mu['wins'] == 1 and mu['losses'] == 1
    assert mu['net'] == 20.0 and mu['pf'] == 1.5 and mu['open'] == 1
    assert seated[('session_sweep_reclaim', 'AMD')]['trades'] == 0
    assert seated[('session_sweep_reclaim', 'NFLX')]['trades'] == 0


def _iceberg_alert(aid, ts_ago, scores=None, **kw):
    a = {'id': aid, 'kind': 'iceberg_alert', 'symbol': 'MU261031C00320000',
         'underlying': 'MU', 'expiry': '2026-10-31', 'strike': 320.0,
         'type': 'call', 'side': 'bullish', 'alert_ts': NOW - ts_ago,
         'day': TODAY, 'est_parent_contracts': 850,
         'est_parent_premium': 42300.0, 'score': 0.81,
         'scores': scores or {}, 'status': 'complete'}
    a.update(kw)
    return a


def test_tracking_iceberg_alerts_and_promotion(db):
    scored = {'close': {'status': 'scored', 'contract_ret_pct': 123.4,
                        'underlying_ret_pct': 1.5}}
    with db.tx() as c:
        db.put(c, 'iceberg_alert:a1', _iceberg_alert('a1', 7200, scores=scored))
        db.put(c, 'iceberg_alert:a2', _iceberg_alert('a2', 3600))
        db.put(c, 'iceberg:diag', {'enabled': True, 'prints_seen': 12345,
                                   'quotes_seen': 99999,
                                   'contracts_tracked': 42,
                                   'alerts_fired': 2, 'errors': 0,
                                   'last_alert_at': NOW - 3600})
        out = simple.summary(db, c, NOW)
    ib = out['tracking']['iceberg']
    assert ib['diag_present'] is True
    assert ib['prints_seen'] == 12345 and ib['contracts_tracked'] == 42
    assert ib['alerts_window'] == 2 and ib['alerts_total'] == 2
    assert ib['alerts_open'] == 0
    # Latest first; the scored alert carries its close return.
    assert [a['underlying'] for a in ib['latest']] == ['MU', 'MU']
    assert ib['latest'][0]['alert_ts'] == NOW - 3600
    assert ib['latest'][0]['close_ret_pct'] is None
    assert ib['latest'][1]['close_ret_pct'] == 123.4
    assert ib['close']['scored'] == 1
    assert ib['close']['mean_contract_ret_pct'] == 123.4
    assert ib['promotion']['scored_total'] == 1
    assert ib['promotion']['meets_bar'] is False


def test_tracking_shadow_shaping(monkeypatch, db):
    """When scanner_shadow is present, its scorecard is the source of
    truth and the page payload is the compact shaping of it: top cells
    by scored count, graduation progress, ready list."""
    canned = {
        'paper_seats': {'session_sweep_reclaim': ['AMD', 'MU', 'NFLX']},
        'seats_source': 'env',
        'promoted_families': ['session_sweep_reclaim'],
        'shadow_cells': [
            {'family': 'session_sweep_reclaim', 'underlying': 'TSLA',
             'alerts': 30, 'open': 2, 'data_gaps': 1, 'scored': 25,
             'wins': 15, 'losses': 10, 'win_rate': 0.6, 'pf': 1.5,
             'net': 210.0,
             'graduation': {'bar': {'min_scored': 20, 'min_pf': 1.2},
                            'family_promoted': True, 'scored_needed': 0,
                            'pf_met': True, 'ready_for_josh': True}},
            {'family': 'volume_breakout', 'underlying': 'MU',
             'alerts': 9, 'open': 1, 'data_gaps': 0, 'scored': 7,
             'wins': 3, 'losses': 4, 'win_rate': 0.4286, 'pf': 0.8,
             'net': -35.0,
             'graduation': {'bar': {'min_scored': 20, 'min_pf': 1.2},
                            'family_promoted': False, 'scored_needed': 13,
                            'pf_met': False, 'ready_for_josh': False}},
        ],
        'totals': {'alerts': 39, 'open': 3, 'scored': 32, 'data_gaps': 1},
        'graduation_bar': {'min_scored': 20, 'min_pf': 1.2},
    }
    stub = types.ModuleType('compass.scanner_shadow')
    stub.scorecard = lambda db_, c_, cfg=None: canned
    # `from . import scanner_shadow` resolves via the parent package
    # attribute first when the real module was imported earlier in the
    # session, so patch both routes.
    import compass
    monkeypatch.setattr(compass, 'scanner_shadow', stub, raising=False)
    monkeypatch.setitem(sys.modules, 'compass.scanner_shadow', stub)
    with db.tx() as c:
        tr = simple.tracking(db, c, NOW)
    sc = tr['scanner']
    assert sc['available'] is True and sc['seats_source'] == 'env'
    sh = sc['shadow']
    assert sh['totals'] == canned['totals']
    assert sh['cells_tracking'] == 2
    # Top cell = most scored; win-rate fraction -> percent.
    assert sh['cells'][0]['underlying'] == 'TSLA'
    assert sh['cells'][0]['win_rate'] == 60.0
    assert sh['cells'][0]['ready'] is True
    assert sh['cells'][1]['scored_needed'] == 13
    assert sh['ready'] == ['Session sweep reclaim × TSLA']
    assert sh['bar'] == {'min_scored': 20, 'min_pf': 1.2}


def test_public_page_tracking_wiring():
    from pathlib import Path
    root = Path(__file__).parent.parent / 'compass' / 'static'
    html = (root / 'simple-public.html').read_text()
    js = (root / 'simple-public.js').read_text()
    assert 'id="trackingBody"' in html
    assert 'What we\'re tracking' in html
    assert 'renderTracking' in js
    assert 'tracking' in js
