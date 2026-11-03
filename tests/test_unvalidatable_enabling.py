"""F2/F4 enabling-build tests (unvalidatable-families registration §10).

F4: the per-session candidate census exists every scan — including
zero-candidate scans — with both vendor-input families present as
explicit zero/absent counts, never missing keys.
F2: scanner status carries the flow summary's scan-time state (event
age and retrieval age as separate clocks, eligibility flag, row
count), and exposure input states are classified present vs
absent/ineligible without changing any candidate gate.
"""
import asyncio
from datetime import datetime
import pytest
from compass.store import Store
from compass import swing_ideas  # Register focused-position tables before Store.initialize.
from compass.config import Config
from compass.market import NY
from compass.engine import Engine
from compass.providers import Collectors
from compass.research import collect, scheduled_feeds
from compass.scanner import Scanner, exposure_candidates, snapshot
from compass.vendor_freshness import CACHE_POLICY

NOW = datetime(2026, 9, 14, 10, 15, tzinfo=NY).timestamp()
DAY = '2026-09-14'


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'enabling.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def quote(t=NOW, bid=101.10, ask=101.11):
    return {'ts': t, 'bid': bid, 'ask': ask, 'bid_size': 100, 'ask_size': 100,
            'source': 'fixture'}


def fact():
    return {'symbol': 'SPY', 'status': 'ready', 'asof': NOW, 'bar_start': NOW - 60,
            'session_open': NOW - 2700, 'session_close': NOW + 20000, 'day': DAY,
            'bar': {'o': 100, 'h': 101.2, 'l': 99.8, 'c': 101, 'v': 200},
            'previous_bar': {'o': 100, 'h': 100.5, 'l': 99.9, 'c': 100, 'v': 100},
            'atr14': 1, 'ema9': 100.5, 'ema21': 100.3, 'ema21_previous': 100.2,
            'vwap': 100.2, 'htf15_bias': 1, 'rvol20': 2, 'high20': 100.8,
            'low20': 99.5, 'high5': 100.8, 'low5': 99.7, 'or_complete': False,
            'prior_complete': False}


def bars_window(last_close=101.0, prev_close=100.0):
    """40 consecutive completed minute bars ending at NOW-60."""
    rows = []
    for i in range(40):
        ts = NOW - (40 - i) * 60
        close = 100.0
        if i == 38:
            close = prev_close
        if i == 39:
            close = last_close
        rows.append([ts, close, close + 0.2, close - 0.2, close, 100])
    return rows


def apex_row(price, score=90):
    return {'symbol': 'SPY', 'source_ts': NOW - 10, 'received': NOW,
            'cache_policy': CACHE_POLICY, 'vendor_stale': False,
            'levels': [{'kind': 'apex', 'price': price, 'score': score,
                        'known_at': NOW - 3600}]}


def test_census_records_zero_families_on_a_quiet_scan(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), scanner_paper=False)
    with db.tx() as c:
        db.put(c, 'latestbar:SPY', {'ts': NOW})
        db.put(c, 'quote:SPY', quote())
        Scanner(db, cfg).scan(c, NOW, Engine(db, cfg))
        census = db.get(c, 'scanner:census:' + DAY)
        assert census is not None and census['scans'] == 1
        # Both families are present as keys with explicit zero/absent
        # counts — a family generating nothing is visible, not missing.
        assert census['exposure_level_break'] == {
            'symbol_scans': 0, 'candidate_symbols': 0,
            'input_present_no_candidate': 0, 'input_absent_or_ineligible': 0}
        assert census['flow_price_breakout']['scans'] == 1
        assert census['flow_price_breakout']['candidate_scans'] == 0
        assert census['flow_price_breakout']['input_absent_or_ineligible'] == 1
        assert 'exposure_level_break' not in census['candidates']
        assert 'flow_price_breakout' not in census['candidates']
        # The census and the flow surface are served in the snapshot.
        snap = snapshot(db, c, cfg, NOW)
        assert snap['census']['day'] == DAY
        assert snap['status']['flow_input']['summary_present'] is False
        assert snap['status']['flow_input']['eligible_for_live_confirmation'] is False
        assert snap['status']['flow_input']['rows'] == 0


def test_census_exposure_input_present_but_no_cross(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), scanner_paper=False)
    with db.tx() as c:
        db.put(c, 'bar_window:SPY', bars_window())
        db.put(c, 'latestbar:SPY', {'ts': NOW})
        db.put(c, 'quote:SPY', quote())
        db.put(c, 'apex:SPY', apex_row(150.0))  # far away: no cross possible
        Scanner(db, cfg).scan(c, NOW, Engine(db, cfg))
        census = db.get(c, 'scanner:census:' + DAY)
        exposure = census['exposure_level_break']
        assert exposure['symbol_scans'] == 1
        assert exposure['input_present_no_candidate'] == 1
        assert exposure['candidate_symbols'] == 0
        assert 'exposure_level_break' not in census['candidates']


def test_census_exposure_candidate_counted(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), scanner_paper=False)
    with db.tx() as c:
        db.put(c, 'bar_window:SPY', bars_window())
        db.put(c, 'latestbar:SPY', {'ts': NOW})
        db.put(c, 'quote:SPY', quote())
        db.put(c, 'apex:SPY', apex_row(100.5))  # prev close 100 <= 100.5 < last close 101
        Scanner(db, cfg).scan(c, NOW, Engine(db, cfg))
        census = db.get(c, 'scanner:census:' + DAY)
        exposure = census['exposure_level_break']
        assert exposure['candidate_symbols'] == 1
        assert census['candidates']['exposure_level_break'] == 1


def test_flow_input_surface_reports_fresh_summary_and_no_candidate(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), scanner_paper=False)
    with db.tx() as c:
        db.put(c, 'scanner_features:SPY', fact())
        db.put(c, 'quote:SPY', quote())
        # Fresh and eligible, but premium below the $100k trigger floor:
        # the conjunction does not complete — surface must say so.
        db.put(c, 'matrix:unusual_activity', {
            'source_ts': NOW - 5, 'received': NOW, 'day': DAY,
            'status': 'available', 'total': 1,
            'rows': [{'symbol': 'SPY', 'source_ts': NOW - 5, 'premium': 50000,
                      'score': 90, 'sentiment': 'Bullish', 'vendor_id': 'one'}]})
        Scanner(db, cfg).scan(c, NOW, Engine(db, cfg))
        status = db.get(c, 'scanner:status')
        flow = status['flow_input']
        assert flow['summary_present'] is True
        assert flow['rows'] == 1
        assert flow['latest_event_age_seconds'] == 5
        assert flow['retrieval_age_seconds'] == 0
        assert flow['freshness_status'] == 'current'
        assert flow['eligible_for_live_confirmation'] is True
        assert flow['candidates_this_scan'] == 0
        census = db.get(c, 'scanner:census:' + DAY)
        assert census['flow_price_breakout']['input_present_no_candidate'] == 1
        assert census['flow_price_breakout']['candidate_scans'] == 0


def test_flow_input_surface_reports_stale_summary_as_ineligible(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), scanner_paper=False)
    with db.tx() as c:
        db.put(c, 'scanner_features:SPY', fact())
        db.put(c, 'quote:SPY', quote())
        db.put(c, 'matrix:unusual_activity', {
            'source_ts': NOW - 3600, 'received': NOW, 'day': DAY,
            'status': 'available', 'total': 3,
            'rows': [{'symbol': 'SPY', 'source_ts': NOW - 3600,
                      'premium': 200000, 'score': 90, 'sentiment': 'Bullish',
                      'vendor_id': 'old'}]})
        Scanner(db, cfg).scan(c, NOW, Engine(db, cfg))
        flow = db.get(c, 'scanner:status')['flow_input']
        assert flow['summary_present'] is True
        assert flow['rows'] == 1
        assert flow['latest_event_age_seconds'] == 3600
        assert flow['freshness_status'] == 'event_stale'
        assert flow['eligible_for_live_confirmation'] is False
        census = db.get(c, 'scanner:census:' + DAY)
        assert census['flow_price_breakout']['input_absent_or_ineligible'] == 1


def apex_payload():
    return {'success': True, 'freshness': {}, 'data': {
        'symbol': 'SPY', 'snapshotTime': '2026-09-14T14:15:00Z',
        'spotPrice': 101.0, 'gammaFlip': 100.5, 'mode': 'fixture',
        'expirationsUsed': [],
        'levels': [{'strike': 102, 'score': 80, 'netGEX': 1.5, 'totalOI': 10}]}}


def test_research_collect_writes_event_and_refreshes_apex(db):
    """F1 regression: one due apex feed -> research event appended,
    research:<feed> snapshot stored, and apex:SPY refreshed with the
    fetch's received stamp (the exposure family's input row)."""
    async def run():
        cfg = Config(local=True, stocks=('SPY',), matrix='fixture')
        worker = Collectors(db, cfg)
        with db.tx() as c:
            for feed in scheduled_feeds(db, c, cfg, NOW):
                if feed.key != 'apex_SPY':
                    db.put(c, 'research_job:' + feed.key, {'attempted_at': NOW})

        async def request(path, key):
            assert key == 'apex_SPY'
            return apex_payload(), NOW
        worker.matrix_request = request
        result = await collect(worker, NOW)
        assert result is not None and result['key'] == 'apex_SPY'
        with db.tx() as c:
            events = [r for r in db.recent(c, 'research')
                      if r['payload'].get('key') == 'apex_SPY']
            assert len(events) == 1
            apex = db.get(c, 'apex:SPY')
            assert apex is not None and apex['received'] == NOW
            assert apex['levels'][0]['price'] == 102
            assert db.get(c, 'research:apex_SPY')['received'] == NOW
            job = db.get(c, 'research_job:apex_SPY')
            assert job['succeeded_at'] == NOW and job['failures'] == 0
        await worker.close()
    asyncio.run(run())


def test_research_collect_rotates_to_other_feeds_on_later_turns(db):
    """The due-job rotation must keep serving foreground feeds across
    turns (a single stuck pool must not silence every feed)."""
    async def run():
        cfg = Config(local=True, stocks=('SPY',), matrix='fixture')
        worker = Collectors(db, cfg)
        calls = []

        async def request(path, key):
            calls.append(key)
            if key.startswith('apex_'):
                return apex_payload(), NOW
            return {'results': [{'symbol': 'SPY',
                                 'detectedAt': '2026-09-14T14:15:00Z'}]}, NOW
        worker.matrix_request = request
        # Turn 1 serves the background pool (30s expansion budget); the
        # foreground then rotates by rank — apex_SPY (priority 8) sits
        # behind the priority-9/10 feeds, so allow a full rotation.
        for _ in range(12):
            await collect(worker, NOW)
        with db.tx() as c:
            keys = {r['payload'].get('key') for r in db.recent(c, 'research')}
        assert 'apex_SPY' in keys
        assert len(keys) >= 5  # rotation reached several distinct feeds
        await worker.close()
    asyncio.run(run())


def test_exposure_diag_states_do_not_change_gates():
    f = fact()
    diag = {}
    assert exposure_candidates(f, None, NOW, 0.01, diag) == []
    assert diag['state'] == 'absent'
    stale = apex_row(100.5) | {'vendor_stale': True}
    diag = {}
    assert exposure_candidates(f, stale, NOW, 0.01, diag) == []
    assert diag['state'] == 'ineligible'
    diag = {}
    assert exposure_candidates(f, apex_row(150.0), NOW, 0.01, diag) == []
    assert diag['state'] == 'present'
    diag = {}
    items = exposure_candidates(f, apex_row(100.5), NOW, 0.01, diag)
    assert len(items) == 1 and items[0]['rule'] == 'exposure_level_break'
    assert diag['state'] == 'present'
    # Without diag the behavior is byte-identical.
    assert len(exposure_candidates(f, apex_row(100.5), NOW, 0.01)) == 1
    assert exposure_candidates(f, apex_row(150.0), NOW, 0.01) == []
