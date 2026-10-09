"""Focused tests for the Simple view aggregations (compass/simple.py)."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from compass.store import Store
from compass import simple
from compass.native_smoothers import weekly as smoothers_weekly

CT = ZoneInfo("America/Chicago")
NOW = time.time()


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'simple.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _smoothers_payload(net_pnl, resolved_at):
    # entry ask 2.0, exit bid chosen so net = (bid-2.0)*100 - 1.32
    exit_bid = round((net_pnl + 1.32) / 100 + 2.0, 2)
    contract = {'symbol': 'DEMO260925C00100000', 'multiplier': 100}
    def q(bid, ask, at_ms):
        return dict(status='available', bid=bid, ask=ask, quote_at_ms=at_ms,
                    quote_age_seconds=1, contract=contract)
    return dict(ticker='DEMO', status='WIN', contract=contract,
                resolution_time=resolved_at,
                quote=q(1.9, 2.0, int(resolved_at * 1000) - 60000),
                exit_quote=q(exit_bid, exit_bid + 0.1, int(resolved_at * 1000)))


def test_smoothers_and_futures_aggregation(db):
    now = NOW
    with db.tx() as c:
        c.execute(db.insert(smoothers_weekly).values(
            id='s1', week='2026-10-05', ticker='DEMO', status='WIN',
            payload=_smoothers_payload(50.0, now)))
        db.put(c, 'trade:ict-1', {'id': 'ict-1', 'symbol': 'SIL.v.0', 'direction': 'long',
                                  'qty': 1, 'strategy': 'ict-aoi-zones', 'entry': 61.3,
                                  'exit': 61.24, 'pnl': -63.0, 'exit_reason': 'stop',
                                  'entered_at': now - 3600, 'exited_at': now})
        db.put(c, 'flow_pulse_track:k1', {'symbol': 'MRK', 'direction': 'bearish',
                                          'status': 'closed', 'entry': 2.0, 'exit': 2.5,
                                          'exited_at': now, 'pnl': 48.7,
                                          'peak_return_pct': 60.0, 'fired_at': now - 86400})
        db.append(c, 'pick_check', 'dashboard', 'AAPL', now,
                   {'ticker': 'AAPL', 'direction': 'long', 'source': 'flash_agentic',
                    'pattern': 'FLOOR BOUNCE #4', 'at': now}, key='pc1')
        db.append(c, 'alert', 'spy_brief', 'SPY', now, {'decision': 'CALL'}, key='sb1')
        out = simple.summary(db, c, now)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 1 and sm['stats']['wins'] == 1
    assert sm['stats']['pnl'] == 49.68  # (2.51-2.00)*100 - 1.32 fee
    assert sm['drill'][0]['name'] == 'DEMO'
    fu = out['types']['futures']
    assert fu['stats']['pnl'] == -63.0 and fu['stats']['tracked'] == 1
    # Full paper-trading roster is always listed, even with zero trades
    # 12 detectors: 11 original + trend_rider (added 2026-10-08)
    assert len(fu['drill']) == 12
    aoi = [d for d in fu['drill'] if d['name'] == 'AOI Zones'][0]
    assert aoi['stats']['tracked'] == 1 and aoi['stats']['pnl'] == -63.0
    gz = [d for d in fu['drill'] if d['name'] == 'Golden Zone + VWAP'][0]
    assert gz['stats']['tracked'] == 0
    fp = out['types']['flow_pulse']
    assert fp['stats']['pnl'] == 48.7 and fp['stats']['peak_50_plus'] == 1
    fl = out['types']['flash']
    assert fl['stats']['tracked'] == 1 and fl['stats']['pnl'] is None
    zd = out['types']['0dte']
    assert zd['stats']['plans'] == 1


def test_calendar_groups_by_chicago_day(db):
    now = NOW
    d = datetime.fromtimestamp(now, CT).date()
    with db.tx() as c:
        db.put(c, 'trade:ict-9', {'id': 'ict-9', 'symbol': 'MCL.v.0', 'direction': 'short',
                                  'qty': 1, 'strategy': 'ict-turtle-soup', 'pnl': 25.0,
                                  'entered_at': now - 7200, 'exited_at': now})
        out = simple.calendar(db, c, now, d.year, d.month)
    assert out['days'][d.isoformat()]['futures']['pnl'] == 25.0


def test_empty_db_returns_empty_blocks(db):
    with db.tx() as c:
        out = simple.summary(db, c, 1_000_000_000.0)
    assert set(out['types']) == {'smoothers', 'futures', 'flow_pulse', 'flash', 'tape', '0dte', 'swings'}
    assert all(t['stats']['tracked'] == 0 for t in out['types'].values())


def test_public_page_has_no_inline_script_or_style():
    # The app's Content-Security-Policy blocks inline scripts and styles;
    # the public page must load its JS/CSS from /static instead, or it
    # renders stuck on "Loading…".
    import re
    from pathlib import Path
    html = (Path(__file__).parent.parent / "compass" / "static" / "simple-public.html").read_text()
    assert "<style>" not in html
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert 'style="' not in html


def test_public_landing_and_detailed_dashboard_routing(tmp_path):
    from compass.app import create_app
    from compass.config import Config
    from fastapi.testclient import TestClient
    cfg = Config(local=True, role="web", db="sqlite:///" + str(tmp_path / "web.db"),
                 password="fixture-password-16")
    app = create_app(cfg)
    with TestClient(app) as client:
        # Simple results page is the public default landing page
        r = client.get("/")
        assert r.status_code == 200
        assert "Market Compass — Trading, in plain English" in r.text
        assert "What each tracker actually does" in r.text
        assert client.get("/simple").status_code == 200
        # Simple APIs are public
        assert client.get("/api/simple").status_code == 200
        assert client.get("/api/simple/calendar?month=2026-10").status_code == 200
        # The detailed workspace still requires auth
        r = client.get("/detailed", follow_redirects=False)
        assert r.status_code in (303, 307)
        client.post("/login", json={"password": "fixture-password-16"})
        assert client.get("/detailed").status_code == 200


def _detail_fixtures(db, now):
    with db.tx() as c:
        c.execute(db.insert(smoothers_weekly).values(
            id='d1', week='2026-09-28', ticker='DEMO', status='LOSS',
            payload=_smoothers_payload(-20.0, now - 7 * 86400)))
        c.execute(db.insert(smoothers_weekly).values(
            id='d2', week='2026-10-05', ticker='DEMO', status='WIN',
            payload=_smoothers_payload(50.0, now)))
        db.put(c, 'trade:ict-d1', {'id': 'ict-d1', 'symbol': 'MNQ.c.0', 'side': 'long',
                                   'qty': 2, 'strategy': 'ict-golden-zone', 'entry': 25000.0,
                                   'exit': 25010.0, 'pnl': 40.0, 'exit_reason': 'target',
                                   'status': 'closed', 'stop': 24990.0, 'target': 25010.0,
                                   'entered_at': now - 7200, 'exited_at': now - 3600})
        db.put(c, 'flow_pulse_track:kd', {'symbol': 'TSLA', 'direction': 'bullish',
                                          'status': 'closed', 'entry': 3.0, 'exit': 2.0,
                                          'exited_at': now, 'pnl': -101.3, 'strike': 500,
                                          'type': 'call', 'expiry': '2026-10-16',
                                          'peak_return_pct': 55.0, 'fired_at': now - 86400})
        db.append(c, 'pick_check', 'dashboard', 'MU', now,
                  {'ticker': 'MU', 'direction': 'long', 'source': 'flash_agentic',
                   'pattern': 'FLOOR BOUNCE #4', 'at': now, 'entry': 100.0,
                   'target': 105.0, 'invalidation': 98.0, 'setup_score': 92},
                  key='pcd1')
        db.append(c, 'alert', 'spy_brief', 'SPY', now,
                  {'decision': 'CALL SETUP CONFIRMED',
                   'plans': {'call': {'stop': 769.21, 'target': 773.51},
                             'put': {'stop': 771.0, 'target': 767.0}}}, key='sbd1')
        db.append(c, 'alert', 'scanner', 'NVDA', now,
                  {'publication': {'category': 'options_0dte', 'event': 'breakout',
                                   'contract': {'underlying': 'NVDA', 'strike': 285,
                                                'type': 'call'}}}, key='od1')


def test_detail_smoothers_weeks(db):
    now = NOW
    _detail_fixtures(db, now)
    with db.tx() as c:
        d = simple.detail(db, c, now, 'smoothers', 'DEMO')
    assert d['title'] == 'DEMO — week by week'
    assert len(d['items']) == 2
    assert d['items'][0]['title'].startswith('Week of Oct')
    assert d['items'][0]['result_cls'] == 'pos'
    assert d['items'][1]['result_cls'] == 'neg'


def test_futures_data_gap_excluded(db):
    # Trades that flew blind (no fresh quotes) are not counted as wins/losses.
    now = NOW
    with db.tx() as c:
        db.put(c, 'trade:ict-g1', {'id': 'ict-g1', 'symbol': 'MES.c.0', 'side': 'long',
                                   'qty': 1, 'strategy': 'ict-aoi-zones', 'entry': 7800.0,
                                   'exit': 7810.0, 'pnl': 50.0, 'exit_reason': 'session_flatten',
                                   'status': 'closed', 'data_gap': True,
                                   'entered_at': now - 7200, 'exited_at': now - 3600})
        db.put(c, 'trade:ict-g2', {'id': 'ict-g2', 'symbol': 'MES.c.0', 'side': 'long',
                                   'qty': 1, 'strategy': 'ict-aoi-zones', 'entry': 7800.0,
                                   'exit': 7810.0, 'pnl': 50.0, 'exit_reason': 'target',
                                   'status': 'closed',
                                   'entered_at': now - 7200, 'exited_at': now - 3600})
        out = simple.summary(db, c, now)
    fu = out['types']['futures']
    assert fu['stats']['tracked'] == 1
    assert fu['stats']['wins'] == 1
    assert fu['stats']['data_gaps'] == 1
    assert fu['stats']['data_gap_pnl'] == 50.0
    with db.tx() as c:
        d = simple.detail(db, c, now, 'futures', 'AOI Zones')
    assert len(d['items']) == 2
    gapped = [i for i in d['items'] if '⚠️' in i['result']]
    assert len(gapped) == 1
    assert any(r[0] == 'Data' for r in gapped[0]['rows'])


def test_detail_futures_trades(db):
    now = NOW
    _detail_fixtures(db, now)
    with db.tx() as c:
        d = simple.detail(db, c, now, 'futures', 'Golden Zone + VWAP')
    assert len(d['items']) == 1
    it = d['items'][0]
    assert it['title'] == 'MNQ long'
    assert it['result'] == '+$40' and it['result_cls'] == 'pos'
    assert any(r[0] == 'Exit reason' and r[1] == 'Target hit' for r in it['rows'])


def test_detail_flash_setups(db):
    now = NOW
    _detail_fixtures(db, now)
    with db.tx() as c:
        d = simple.detail(db, c, now, 'flash', 'FLOOR BOUNCE #4')
    assert len(d['items']) == 1
    it = d['items'][0]
    assert it['title'] == 'MU long'
    assert any(r[0] == 'Invalidation' for r in it['rows'])


def test_detail_0dte_plans_and_alerts(db):
    now = NOW
    _detail_fixtures(db, now)
    with db.tx() as c:
        plans = simple.detail(db, c, now, '0dte', 'SPY morning plan')
        alerts = simple.detail(db, c, now, '0dte', '0DTE scanner')
    assert len(plans['items']) == 1
    assert 'CALL SETUP CONFIRMED' in plans['items'][0]['title']
    assert any(r[0] == 'Call stop/target' for r in plans['items'][0]['rows'])
    assert len(alerts['items']) == 1
    assert 'NVDA' in alerts['items'][0]['title']


def test_detail_unknown_type_returns_none(db):
    with db.tx() as c:
        assert simple.detail(db, c, NOW, 'nope', 'x') is None
        assert simple.detail(db, c, NOW, 'futures', '') is None


def test_detail_endpoint_is_public(tmp_path):
    from compass.app import create_app
    from compass.config import Config
    from fastapi.testclient import TestClient
    cfg = Config(local=True, role="web", db="sqlite:///" + str(tmp_path / "web2.db"),
                 password="fixture-password-16")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/api/simple/detail", params={"type": "futures", "name": "ICC"})
        assert r.status_code == 200
        assert r.json()["name"] == "ICC"
        r = client.get("/api/simple/detail", params={"type": "nope", "name": "x"})
        assert r.status_code == 400


def _zdtp_now():
    # Fixed Monday 10:00am CT — inside the equity session, before any flatten.
    from datetime import datetime
    return datetime(2026, 10, 5, 10, 0, tzinfo=CT).timestamp()


def _zdtp_fixtures(db, now):
    """One confirmed SPY plan + fresh quotes + SPY minute bars."""
    with db.tx() as c:
        db.append(c, 'alert', 'spy_brief', 'SPY', now - 3600,
                  {'decision': 'CALL SETUP CONFIRMED',
                   'plans': {'call': {'stop': 769.0, 'target': 773.0}},
                   'options': {'call': {'symbol': 'SPY261005C00770000', 'strike': 770}}},
                  key='zsb1')
        db.put(c, 'quote:SPY261005C00770000',
               {'bid': 1.00, 'ask': 1.05, 'ts': now - 30})
        # SPY minute bars AFTER the plan: touch the target
        db.put(c, 'bar_window:SPY', [
            [now - 1800, 0, 771.0, 770.0, 770.5, 100],
            [now - 900, 0, 773.5, 771.0, 773.0, 100],
        ])


def test_zero_dte_paper_opens_spy_plan_trade(db):
    from compass import zero_dte_paper as zdp
    from compass.config import Config
    now = _zdtp_now()
    _zdtp_fixtures(db, now)
    cfg = Config()
    with db.tx() as c:
        db.put(c, 'zero_dte_paper:spy_ts', now - 7200)  # running system
        out = zdp.scan(db, c, cfg, now)
    assert out['ran'] is True and out['opened'] == 1
    with db.tx() as c:
        t = db.get(c, 'trade:0dte-spy-2026-10-05-call')
    assert t['kind'] == 'spy_plan' and t['qty'] == 1 and t['status'] == 'open'
    assert t['entry'] == 1.06  # ask + 1c slippage


def test_zero_dte_paper_closes_on_spy_target(db):
    from compass import zero_dte_paper as zdp
    from compass.config import Config
    now = _zdtp_now()
    _zdtp_fixtures(db, now)
    cfg = Config()
    with db.tx() as c:
        db.put(c, 'zero_dte_paper:spy_ts', now - 7200)
        out1 = zdp.scan(db, c, cfg, now)
        assert out1['opened'] == 1 and out1['closed'] == 0
        # a post-entry bar touches the SPY target
        bars = db.get(c, 'bar_window:SPY', [])
        bars.append([now + 30, 0, 774.0, 772.0, 773.5, 100])
        db.put(c, 'bar_window:SPY', bars)
        out = zdp.scan(db, c, cfg, now + 61)
    assert out['closed'] == 1
    with db.tx() as c:
        t = db.get(c, 'trade:0dte-spy-2026-10-05-call')
    assert t['status'] == 'closed' and t['exit_reason'] == 'target hit'
    assert t['pnl'] < 0  # exited at the 1.00 bid below the 1.06 entry


def test_zero_dte_paper_no_backfill(db):
    from compass import zero_dte_paper as zdp
    from compass.config import Config
    now = _zdtp_now()
    _zdtp_fixtures(db, now)
    cfg = Config()
    with db.tx() as c:
        # fresh DB: watermark starts at now, the hour-old plan is ignored
        out = zdp.scan(db, c, cfg, now)
    assert out['opened'] == 0
    with db.tx() as c:
        assert len(db.prefix(c, 'trade:0dte-')) == 0


def test_zero_dte_paper_no_duplicates(db):
    from compass import zero_dte_paper as zdp
    from compass.config import Config
    now = _zdtp_now()
    _zdtp_fixtures(db, now)
    cfg = Config()
    with db.tx() as c:
        db.put(c, 'zero_dte_paper:spy_ts', now - 7200)
        zdp.scan(db, c, cfg, now)
        zdp.scan(db, c, cfg, now + 61)
        n = len(db.prefix(c, 'trade:0dte-'))
    assert n == 1  # opened once, never duplicated


def test_zero_dte_paper_opens_scanner_alert_trade(db):
    from compass import zero_dte_paper as zdp
    from compass.config import Config
    now = _zdtp_now()
    cfg = Config()
    with db.tx() as c:
        db.append(c, 'alert', 'engine', 'NVDA261005C00285000', now - 600,
                  {'publication': {'category': 'options_0dte', 'event': 'ENTRY',
                                   'contract': {'symbol': 'NVDA261005C00285000',
                                                'underlying': 'NVDA', 'strike': 285,
                                                'type': 'call', 'expiry': '2026-10-05'}}},
                  key='zod1')
        db.put(c, 'quote:NVDA261005C00285000', {'bid': 2.00, 'ask': 2.10, 'ts': now - 20})
        db.put(c, 'zero_dte_paper:scan_ts', now - 7200)
        out = zdp.scan(db, c, cfg, now)
    assert out['opened'] == 1
    with db.tx() as c:
        t = db.get(c, 'trade:0dte-scan-zod1')
    assert t['kind'] == 'scanner' and t['qty'] == 1 and t['status'] == 'open'
    assert t['entry'] == 2.11


def test_zero_dte_paper_scanner_exits_on_half(db):
    from compass import zero_dte_paper as zdp
    from compass.config import Config
    now = _zdtp_now()
    cfg = Config()
    with db.tx() as c:
        db.append(c, 'alert', 'engine', 'NVDA261005C00285000', now - 600,
                  {'publication': {'category': 'options_0dte', 'event': 'ENTRY',
                                   'contract': {'symbol': 'NVDA261005C00285000',
                                                'underlying': 'NVDA', 'strike': 285,
                                                'type': 'call', 'expiry': '2026-10-05'}}},
                  key='zod2')
        db.put(c, 'quote:NVDA261005C00285000', {'bid': 2.00, 'ask': 2.10, 'ts': now - 20})
        db.put(c, 'zero_dte_paper:scan_ts', now - 7200)
        zdp.scan(db, c, cfg, now)
        # premium doubles -> +50% target
        db.put(c, 'quote:NVDA261005C00285000', {'bid': 3.30, 'ask': 3.40, 'ts': now + 30})
        out = zdp.scan(db, c, cfg, now + 61)
    assert out['closed'] == 1
    with db.tx() as c:
        t = db.get(c, 'trade:0dte-scan-zod2')
    assert t['status'] == 'closed' and t['exit_reason'] == 'target hit (+50%)'
    assert t['pnl'] > 0


def test_simple_0dte_aggregates_paper_trades(db):
    now = NOW
    with db.tx() as c:
        db.put(c, 'trade:0dte-spy-2026-10-05-call',
               {'id': 'x', 'kind': 'spy_plan', 'status': 'closed', 'pnl': 25.0,
                'exited_at': now})
        db.put(c, 'trade:0dte-spy-2026-10-04-put',
               {'id': 'y', 'kind': 'spy_plan', 'status': 'closed', 'pnl': -10.0,
                'exited_at': now - 86400})
        out = simple.summary(db, c, now)
    zd = out['types']['0dte']
    assert zd['stats']['tracked'] == 2
    assert zd['stats']['wins'] == 1
    assert zd['stats']['pnl'] == 15.0
    spy = [d for d in zd['drill'] if d['name'] == 'SPY morning plan (wall target)'][0]
    assert spy['stats']['tracked'] == 2 and spy['stats']['pnl'] == 15.0
    spy2r = [d for d in zd['drill'] if d['name'] == 'SPY morning plan (2R target)'][0]
    assert spy2r['stats']['tracked'] == 0


def test_detail_flow_pulses(db):
    now = NOW
    with db.tx() as c:
        db.append(c, 'flow_pulse', 'flow_pulse', 'MRK', now - 3600,
                  {'symbol': 'MRK', 'direction': 'bearish',
                   'directional_premium': 1735771, 'opposing_premium': 200000,
                   'print_count': 12, 'max_score': 94,
                   'suggested_contract': {'strike': 95, 'type': 'put', 'expiry': '2026-11-21'},
                   'first_seen': now - 3600},
                  key='flowpulse:2026-10-05:MRK:bearish')
        db.append(c, 'flow_pulse', 'flow_pulse', 'TSLA', now - 1800,
                  {'symbol': 'TSLA', 'direction': 'bullish',
                   'directional_premium': 5300000, 'opposing_premium': 100000,
                   'print_count': 20, 'max_score': 97,
                   'first_seen': now - 1800},
                  key='flowpulse:2026-10-05:TSLA:bullish')
        db.put(c, 'flow_pulse_track:flowpulse:2026-10-05:MRK:bearish',
               {'key': 'flowpulse:2026-10-05:MRK:bearish', 'symbol': 'MRK',
                'direction': 'bearish', 'status': 'open', 'fired_at': now - 3600})
        d = simple.detail(db, c, now, 'flow_pulse', 'Bearish flow')
    assert d['title'] == 'Bearish flow — pulse by pulse'
    assert len(d['items']) == 1
    it = d['items'][0]
    assert it['title'] == 'MRK bearish'
    assert '$1.7M' in it['sub']
    assert any(r[0] == 'Suggested contract' and '95P' in r[1] for r in it['rows'])
    assert any(r[0] == 'Paper track' and r[1] == 'Track open' for r in it['rows'])
    with db.tx() as c:
        d2 = simple.detail(db, c, now, 'flow_pulse', 'Bullish flow')
    assert len(d2['items']) == 1
    assert any(r[0] == 'Paper track' and r[1] == 'No track opened' for r in d2['items'][0]['rows'])


def test_flow_pulse_drill_includes_pulse_counts(db):
    now = NOW
    with db.tx() as c:
        db.append(c, 'flow_pulse', 'flow_pulse', 'MRK', now,
                  {'symbol': 'MRK', 'direction': 'bearish',
                   'directional_premium': 1735771, 'first_seen': now},
                  key='flowpulse:2026-10-05:MRK:bearish')
        out = simple.summary(db, c, now)
    fp = out['types']['flow_pulse']
    assert fp['stats']['pulses'] == 1
    bear = [d for d in fp['drill'] if d['name'] == 'Bearish flow'][0]
    assert bear['stats']['pulses'] == 1


def test_smoother_filters_unmeasurable(db):
    # Public page shows only weeks with real, measured option P&L.
    now = NOW
    measurable = _smoothers_payload(50.0, now)  # WIN with good quotes
    no_quotes = dict(_smoothers_payload(50.0, now), exit_quote=None)
    open_week = dict(_smoothers_payload(50.0, now), status='OPEN')
    with db.tx() as c:
        c.execute(db.insert(smoothers_weekly).values(
            id='s1', week='2026-10-05', ticker='DEMO', status='WIN',
            payload=measurable))
        c.execute(db.insert(smoothers_weekly).values(
            id='s2', week='2026-09-28', ticker='DEMO', status='WIN',
            payload=no_quotes))
        c.execute(db.insert(smoothers_weekly).values(
            id='s3', week='2026-10-12', ticker='DEMO', status='OPEN',
            payload=open_week))
        out = simple.summary(db, c, now)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 1
    assert sm['stats']['measured'] == 1
    assert sm['stats']['pnl'] == 49.68
    with db.tx() as c:
        d = simple.detail(db, c, now, 'smoothers', 'DEMO')
    assert len(d['items']) == 1
    assert d['items'][0]['result'] == '+$50'
