"""Tests for the Simple page "What we're tracking" payload
(compass/simple.py tracking()): the Futures Sweep and Stock Sweeps
forward shadows (which replaced the survivors-era live futures book
2026-11-16), the 0DTE scanner seat/shadow split, and the shadow
iceberg watch. Style mirrors tests/test_simple.py (Store fixture,
db.put seeding)."""
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


def test_tracking_empty_db(db):
    with db.tx() as c:
        out = simple.summary(db, c, NOW)
    tr = out['tracking']
    assert tr is not None
    # Futures Sweep shadow: clean empty state — the module deploys
    # separately, so with no state row every root renders zeroed for
    # both configurations and the first eligible session is the
    # registered Tue 2026-11-17.
    fs = tr['futures_sweep']
    assert fs['available'] is False
    assert fs['first_session'] is None
    assert fs['expected_first_session'] == '2026-11-17'
    assert [x['root'] for x in fs['roots']] == [
        'MES', 'MNQ', 'MYM', 'MGC', 'MCL', 'M2K', 'M6E', 'M6A']
    assert all(x[cfg]['n'] == 0 and x[cfg]['net'] == 0.0
               and x[cfg]['pf'] is None
               for x in fs['roots'] for cfg in ('as_written', 'unlimited'))
    assert fs['verdict'] == {'min_weeks': 12, 'min_trades_per_root': 50}
    # Stock Sweeps shadow: clean empty state over the frozen 23-name
    # book from the full-list screen.
    ss = tr['stock_sweeps']
    assert ss['available'] is False
    assert ss['book_n'] == 23 and 'AMD' in ss['book']
    assert ss['underlying'] == {'n': 0, 'net': 0.0, 'pf': None,
                                'win_rate': None}
    assert ss['per_symbol'] == []
    # MYM Turn-of-Month shadow: clean empty state — no state row,
    # zeroed stats, the frozen verdict clock (first read at 12
    # trades; verdict at 50 trades AND 12 weeks).
    mt = tr['mym_tom']
    assert mt['available'] is False
    assert mt['first_session'] is None and mt['weeks'] is None
    assert mt['n'] == 0 and mt['net'] == 0.0 and mt['pf'] is None
    assert mt['stress_net'] == 0.0
    assert mt['counts'] == {'created': 0, 'open': 0, 'closed': 0}
    assert mt['verdict'] == {'min_trades': 50, 'min_weeks': 12,
                             'first_read_trades': 12}
    # Scanner: the book renders honestly with no data — no seats by
    # default under the whole-list book; the shadow side is either
    # not-yet-landed (None) or a zeroed scorecard.
    sc = tr['scanner']
    # Whole-list book (respec 2026-11-01): NO default seats — nobody is
    # grandfathered in; the seated set is empty until an operator seats
    # a graduated name.
    assert sc['paper_seats'] == {}
    assert sc['seated'] == []
    assert sc['seats_source'] == 'none'
    assert sc['shadow'] is None or sc['shadow']['totals']['alerts'] == 0
    # Iceberg: clean zero state.
    ib = tr['iceberg']
    assert ib['alerts_window'] == 0
    assert ib['latest'] == []
    assert ib['promotion']['scored_total'] == 0
    assert ib['diag_present'] is False


def test_tracking_shadow_states(db):
    """Seeded shadow state rows (the shapes compass/futures_sweep.py
    and compass/stock_sweeps.py write) flow through to the page
    payload; unseeded roots zero out."""
    with db.tx() as c:
        db.put(c, 'futures_sweep:state', {
            'counts': {'created': 5, 'open': 1, 'closed': 4},
            'first_session': '2026-11-17',
            'weeks_since_first_session': 0.1,
            'per_root': {'MNQ': {
                'as_written': {'n': 3, 'net': 120.5, 'pf': 1.8,
                               'stress_net': 90.0},
                'unlimited': {'n': 4, 'net': -20.0, 'pf': 0.9,
                              'stress_net': -45.0}}},
            'pooled': {
                'as_written': {'n': 3, 'net': 120.5, 'pf': 1.8,
                               'stress_net': 90.0},
                'unlimited': {'n': 4, 'net': -20.0, 'pf': 0.9,
                              'stress_net': -45.0}}})
        db.put(c, 'stock_sweeps:state', {
            'counts': {'created': 3, 'open': 1, 'closed': 2,
                       'data_gap': 0},
            'first_session': '2026-11-17',
            'underlying': {'n': 2, 'net': 55.0, 'pf': 2.1,
                           'win_rate': 50.0},
            'premium': {'n': 2, 'net': 30.0, 'pf': 1.5,
                        'win_rate': 50.0},
            'per_symbol': {'AMD': {'n': 2, 'net': 55.0}},
            'book': {'AMD': 0.35, 'QQQ': 0.5}, 'windows': []})
        out = simple.summary(db, c, NOW)
    fs = out['tracking']['futures_sweep']
    assert fs['available'] is True and fs['counts']['closed'] == 4
    mnq = [x for x in fs['roots'] if x['root'] == 'MNQ'][0]
    assert mnq['as_written'] == {'n': 3, 'net': 120.5, 'pf': 1.8,
                                 'stress_net': 90.0}
    assert mnq['unlimited']['net'] == -20.0
    mes = [x for x in fs['roots'] if x['root'] == 'MES'][0]
    assert mes['as_written']['n'] == 0  # unseeded roots zero out
    # The mgc_extended section (2026-11-18): present and zeroed when
    # the module state predates it; per-root buckets ride along.
    assert mnq['mgc_extended']['n'] == 0
    ext = fs['mgc_extended']
    assert ext['first_session'] is None and ext['weeks'] is None
    assert ext['stats'] == {'n': 0, 'net': 0.0, 'pf': None,
                            'stress_net': 0.0}
    assert ext['verdict'] == {'min_weeks': 12, 'min_trades': 50}
    ss = out['tracking']['stock_sweeps']
    assert ss['available'] is True and ss['book_n'] == 2
    assert ss['underlying']['net'] == 55.0
    assert ss['per_symbol'] == [{'symbol': 'AMD', 'n': 2, 'net': 55.0}]


def test_tracking_mym_tom_and_mgc_extended_states(db):
    """Seeded MYM TOM state and the futures sweep's mgc_extended
    section flow through to the payload with the modules' exact
    figures — the page renders these fields verbatim, so a shaping
    slip here becomes wrong numbers on the page."""
    with db.tx() as c:
        db.put(c, 'mym_tom:state', {
            'counts': {'created': 2, 'open': 0, 'closed': 2},
            'first_session': '2026-11-30',
            'weeks_since_first_session': 0.4,
            'stats': {'n': 2, 'net': -35.5, 'pf': 0.4,
                      'stress_net': -41.0}})
        db.put(c, 'futures_sweep:state', {
            'counts': {'created': 3, 'open': 0, 'closed': 3},
            'first_session': '2026-11-17',
            'weeks_since_first_session': 0.6,
            'mgc_extended_first_session': '2026-11-19',
            'mgc_extended_weeks_since_first_session': 0.3,
            'per_root': {'MGC': {
                'as_written': {'n': 1, 'net': -30.0, 'pf': 0.5,
                               'stress_net': -35.0},
                'unlimited': {'n': 1, 'net': -30.0, 'pf': 0.5,
                              'stress_net': -35.0},
                'mgc_extended': {'n': 2, 'net': -91.48, 'pf': 0.0,
                                 'stress_net': -99.48}}},
            'pooled': {
                'as_written': {'n': 1, 'net': -30.0, 'pf': 0.5,
                               'stress_net': -35.0},
                'unlimited': {'n': 1, 'net': -30.0, 'pf': 0.5,
                              'stress_net': -35.0},
                'mgc_extended': {'n': 2, 'net': -91.48, 'pf': 0.0,
                                 'stress_net': -99.48}}})
        out = simple.summary(db, c, NOW)
    mt = out['tracking']['mym_tom']
    assert mt['available'] is True
    assert mt['first_session'] == '2026-11-30' and mt['weeks'] == 0.4
    assert mt['counts'] == {'created': 2, 'open': 0, 'closed': 2}
    assert mt['n'] == 2 and mt['net'] == -35.5 and mt['pf'] == 0.4
    assert mt['stress_net'] == -41.0
    fs = out['tracking']['futures_sweep']
    mgc = [x for x in fs['roots'] if x['root'] == 'MGC'][0]
    assert mgc['mgc_extended'] == {'n': 2, 'net': -91.48, 'pf': 0.0,
                                   'stress_net': -99.48}
    ext = fs['mgc_extended']
    assert ext['first_session'] == '2026-11-19' and ext['weeks'] == 0.3
    assert ext['stats'] == {'n': 2, 'net': -91.48, 'pf': 0.0,
                            'stress_net': -99.48}
    assert ext['verdict'] == {'min_weeks': 12, 'min_trades': 50}
    # Non-MGC roots carry a zeroed mgc_extended bucket (the book is
    # MGC only) — the page must not invent extended rows elsewhere.
    mes = [x for x in fs['roots'] if x['root'] == 'MES'][0]
    assert mes['mgc_extended']['n'] == 0


def test_funnel_publications(db):
    """The 0DTE block surfaces the setup funnel's published trades:
    entries (option_setup_new) matched to exits (setup_result) by
    contract symbol."""
    with db.tx() as c:
        db.append(c, 'alert', 'setup_study', 'QQQ261116C00667000',
                  NOW - 7200,
                  {'status': 'option_setup_new',
                   'symbol': 'QQQ261116C00667000',
                   'publication': {'category': 'options_0dte',
                                   'contract': {
                                       'symbol': 'QQQ261116C00667000',
                                       'underlying': 'QQQ',
                                       'strike': 667, 'type': 'call',
                                       'expiry': '2026-11-16'}},
                   'entry': 2.93},
                  'option-setup-entry:t1')
        db.append(c, 'alert', 'setup_study', 'QQQ261116C00667000',
                  NOW - 3600,
                  {'status': 'setup_result',
                   'symbol': 'QQQ261116C00667000',
                   'exit': 2.01, 'exit_reason': 'stop', 'pnl': -93.3,
                   'outcome': 'loss'},
                  'setup-result:t1')
        pubs = simple._funnel_publications(db, c, NOW)
        block, _ = simple._0dte(db, c, NOW)
    assert pubs['published'] == 1 and pubs['closed'] == 1
    assert pubs['open'] == 0 and pubs['wins'] == 0
    assert pubs['net'] == -93.3
    r = pubs['recent'][0]
    assert r['underlying'] == 'QQQ' and r['status'] == 'closed'
    assert r['exit_reason'] == 'stop' and r['entry'] == 2.93
    # The block carries the publications and leads its drills with them.
    assert block['stats']['publications']['published'] == 1
    assert block['drill'][0]['name'] == 'Published trades (setup funnel)'
    assert not any('SPY morning plan' in d['name']
                   for d in block['drill'])
    assert 'ask cap' not in block['fires']



def _scan_trade(tid, underlying, pnl, family='session_sweep_reclaim', **kw):
    t = {'id': tid, 'kind': 'scanner', 'family': family,
         'underlying': underlying, 'symbol': 'X', 'status': 'closed',
         'pnl': pnl, 'entered_at': NOW - 7200, 'exited_at': NOW - 3600,
         'evidence': {'snapshot': True}}
    t.update(kw)
    return t


def test_tracking_seated_scanner_stats(monkeypatch, db):
    # Seating is an operator action under the respec; this test seats
    # the three names explicitly, as production would after graduation.
    monkeypatch.setenv('ODTE_BOOK_SEATED_NAMES',
                       'session_sweep_reclaim:MU,AMD,NFLX')
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
    # data_gap rows are counted first-class now, not silently dropped.
    assert mu['data_gaps'] == 1
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
    # MYM Turn-of-Month block + the Futures Sweep MGC-extended rows
    # render from the tracking payload (2026-11-21).
    assert 'mym_tom' in js
    assert 'MYM Turn-of-Month tracking' in js
    assert 'mgc_extended' in js
    assert 'Extended window' in js
    assert '15:45 ET' in js
