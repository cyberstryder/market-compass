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
    assert fu['drill'][0]['name'] == 'AOI Zones'
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
    assert set(out['types']) == {'smoothers', 'futures', 'flow_pulse', 'flash', '0dte'}
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
