"""Evidence enrichment + stats reset tests (audit #8, 2026-10-21).

Covers:
- entry_evidence.build snapshots gamma regime / walls / IV rank / VWAP /
  squeeze / calendar flags from the existing stores, degrading to None;
- zero_dte_paper._open_scanner stores the snapshot on the trade record;
- alert_ownership.format_message appends the evidence block to the
  verbose 0DTE Discord alert;
- simple._0dte counts only evidence-carrying scanner trades in the
  scorecard while pre-enrichment trades stay visible in detail views.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass import entry_evidence, simple, zero_dte_paper
from compass.alert_ownership import format_message

CT = ZoneInfo("America/Chicago")
OPT = "TSLA251021C00250000"
UNDERLYING = "TSLA"


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'evidence.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _seed_sources(db, c, now):
    day_str = datetime.fromtimestamp(now, CT).strftime("%Y-%m-%d")
    hhmm = datetime.fromtimestamp(now, CT).strftime("%H:%M")
    db.put(c, "quote:" + UNDERLYING, {"ts": now, "bid": 249.9, "ask": 250.1})
    db.put(c, "gamma_flip:" + UNDERLYING,
           {"symbol": UNDERLYING, "status": "ok", "flip": 255.0,
            "spot": 249.0, "regime": "below_flip", "distance_pct": -2.35})
    db.put(c, "gamma_levels:" + UNDERLYING,
           {"status": "ok", "spot": 249.0,
            "levels": {"call_wall": 260.0, "put_wall": 240.0,
                       "neutral_gamma": 255.0, "concentration": 260.0}})
    db.put(c, "apex:" + UNDERLYING,
           {"symbol": UNDERLYING, "levels": [
               {"kind": "apex", "price": 255.0, "score": 88},
               {"kind": "apex", "price": 245.0, "score": 81}]})
    db.put(c, "iv_rank:" + UNDERLYING,
           {"symbol": UNDERLYING, "iv_rank": 63.0, "iv": 0.42,
            "at": now, "status": "ok"})
    db.put(c, "scanner_features:" + UNDERLYING,
           {"symbol": UNDERLYING, "status": "ready", "vwap": 248.15,
            "atr14": 3.0, "asof": now})
    db.put(c, "squeeze:latest",
           {"at": now, "states": {UNDERLYING: {
               "state": "fired", "direction": "long", "squeeze_bars": 6}}})
    db.put(c, "intel:earnings:" + day_str,
           {"date": day_str, "opra_earnings": [
               {"symbol": UNDERLYING, "date": day_str, "time": "AMC"}]})
    db.put(c, "intel:economic:" + day_str,
           {"date": day_str, "high_impact": [
               {"title": "CPI", "date": day_str, "time": hhmm,
                "impact": "high"}]})


def _seed_alert(db, c, now, key="evt1"):
    db.put(c, "zero_dte_paper:scan_ts", now - 3600)
    db.append(c, "alert", "setup_study", OPT, now - 5, {
        "status": "option_setup_new",
        "publication": {
            "category": "options_0dte",
            "contract": {"symbol": OPT, "underlying": UNDERLYING,
                         "strike": 250, "type": "call",
                         "expiry": "2025-10-21"},
            "status": "option_setup_new",
        },
    }, key=key)
    db.put(c, "quote:" + OPT, {"ts": now, "bid": 2.0, "ask": 2.1})


# ---------------------------------------------------------------- builder


def test_build_snapshots_every_section(db):
    now = time.time()
    with db.tx() as c:
        _seed_sources(db, c, now)
        snap = entry_evidence.build(db, c, UNDERLYING, now)
    assert snap["version"] == entry_evidence.VERSION
    assert snap["price"] == 250.0  # underlying quote mid
    assert snap["gamma"] == {"flip": 255.0, "regime": "below_flip",
                             "distance_pct": -1.961}
    # Nearest wall wins across sources: Apex 255/245 beat the GEX walls.
    assert snap["walls"]["above"] == {"price": 255.0, "kind": "apex",
                                      "distance": 5.0}
    assert snap["walls"]["below"] == {"price": 245.0, "kind": "apex",
                                      "distance": 5.0}
    assert snap["iv_rank"] == {"rank": 63.0, "iv": 0.42}
    assert snap["vwap"] == {"vwap": 248.15, "position": "above",
                            "distance": 1.85}
    assert snap["squeeze"] == {"state": "fired", "direction": "long",
                               "bars": 6}
    assert snap["calendar"]["earnings_today"] is True
    assert snap["calendar"]["earnings_time"] == "AMC"
    assert snap["calendar"]["high_impact_news_today"] is True
    assert snap["calendar"]["news_window_now"] is True


def test_build_walls_fall_back_to_gex_walls(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, "gamma_levels:" + UNDERLYING,
               {"status": "ok", "levels": {"call_wall": 260.0,
                                           "put_wall": 240.0,
                                           "concentration": 270.0}})
        snap = entry_evidence.build(db, c, UNDERLYING, now, price=250.0)
    assert snap["walls"]["above"] == {"price": 260.0,
                                       "kind": "gex_call_wall",
                                       "distance": 10.0}
    assert snap["walls"]["below"] == {"price": 240.0,
                                      "kind": "gex_put_wall",
                                      "distance": 10.0}


def test_build_missing_sources_degrade_to_nulls(db):
    now = time.time()
    with db.tx() as c:
        snap = entry_evidence.build(db, c, UNDERLYING, now)
    assert isinstance(snap, dict)
    assert snap["price"] is None
    for field in ("gamma", "walls", "iv_rank", "vwap", "squeeze", "calendar"):
        assert snap[field] is None, field


# ---------------------------------------------------------------- booking


def test_scanner_booking_stores_evidence_snapshot(db):
    now = time.time()
    with db.tx() as c:
        _seed_sources(db, c, now)
        _seed_alert(db, c, now)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 3600)
        rec = db.get(c, "trade:0dte-scan-evt1")
    assert opened == 1
    assert isinstance(rec.get("evidence"), dict)
    assert rec["evidence"]["gamma"]["regime"] == "below_flip"
    assert rec["evidence"]["iv_rank"]["rank"] == 63.0
    assert rec["evidence"]["walls"]["above"]["price"] == 255.0
    assert rec["evidence"]["calendar"]["earnings_today"] is True


def test_scanner_booking_without_sources_still_books_with_nulls(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 3600)
        rec = db.get(c, "trade:0dte-scan-evt1")
    assert opened == 1
    assert isinstance(rec.get("evidence"), dict)
    assert rec["evidence"]["gamma"] is None
    assert rec["evidence"]["walls"] is None
    assert rec["evidence"]["calendar"] is None


# ---------------------------------------------------------------- alert text


def _alert_row(now, snap_marker):
    payload = {
        "status": "option_setup_new",
        "strategy": "0dte-compass-scanner-v2:volume_breakout",
        "entry": 2.05,
        "last_quote": {"bid": 2.0, "ask": 2.1, "ts": now,
                       "source": "alpaca"},
        "publication": {
            "trade_id": "t-1", "category": "options_0dte",
            "event": "ENTRY", "horizon": "intraday",
            "contract": {"symbol": "O:" + OPT, "underlying": UNDERLYING,
                         "expiration": "2025-10-21", "type": "CALL",
                         "strike": 250},
        },
    }
    if snap_marker is not None:
        payload["evidence_snapshot"] = snap_marker
    return {"id": 7, "ts": now, "symbol": OPT, "source": "setup_study",
            "payload": payload}


def test_alert_text_appends_evidence_block(db):
    now = time.time()
    with db.tx() as c:
        _seed_sources(db, c, now)
        snap = entry_evidence.build(db, c, UNDERLYING, now)
    text = format_message(_alert_row(now, snap))
    # Verbose canary lines are unchanged and come first.
    assert "Observed bid / ask: $2.00 / $2.10" in text
    assert "Quote source: alpaca" in text
    assert "Evidence: Gamma below flip (amplified) · flip 255" in text
    assert "IV rank 63" in text
    assert "Price above VWAP (+1.85)" in text
    assert "Walls: above apex 255 (5.00 away)" in text
    assert "below apex 245 (5.00 away)" in text
    assert "Earnings today (AMC)" in text
    assert "High-impact news window now" in text


def test_alert_text_without_snapshot_has_no_evidence_block():
    text = format_message(_alert_row(time.time(), None))
    assert "Evidence:" not in text


# ---------------------------------------------------------------- stats reset


def _trade(key, **kw):
    base = {"id": key, "kind": "scanner", "symbol": OPT,
            "underlying": UNDERLYING, "side": "long", "qty": 1,
            "entry": 2.0, "status": "closed", "pnl": 0.0,
            "exited_at": time.time()}
    base.update(kw)
    return base


def test_simple_stats_count_only_evidence_scanner_trades(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, "trade:0dte-scan-old",
               _trade("trade:0dte-scan-old", pnl=-50.25))
        db.put(c, "trade:0dte-scan-new",
               _trade("trade:0dte-scan-new", pnl=30.0,
                      evidence={"version": entry_evidence.VERSION}))
        db.put(c, "trade:0dte-scan-open-old",
               _trade("trade:0dte-scan-open-old", status="open",
                      pnl=None, exited_at=None))
        # SPY plan is a separate program: no snapshot, handling unchanged.
        db.put(c, "trade:0dte-spy-2025-10-21-call-wall",
               _trade("trade:0dte-spy-2025-10-21-call-wall",
                      kind="spy_plan", underlying="SPY", pnl=10.0,
                      target_rule="wall"))
        block, _by_day = simple._0dte(db, c, now)
        detail = simple._detail_0dte(db, c, now, "0DTE " + UNDERLYING)
    stats = block["stats"]
    # Headline total: evidence scanner win + SPY plan win; old book is out.
    assert stats["tracked"] == 2
    assert stats["wins"] == 2
    assert stats["pnl"] == 40.0
    assert stats["open"] == 0  # pre-evidence open trade is not in the book
    assert stats["pre_evidence"] == 2
    assert stats["pre_evidence_pnl"] == -50.25
    scanner = [d for d in block["drill"] if d["name"] == "0DTE scanner"][0]
    assert scanner["stats"]["tracked"] == 1
    assert scanner["stats"]["pnl"] == 30.0
    tsla = [d for d in block["drill"] if d["name"] == "0DTE " + UNDERLYING][0]
    assert tsla["stats"]["tracked"] == 1
    spy = [d for d in block["drill"]
           if d["name"] == "SPY morning plan (wall target)"][0]
    assert spy["stats"]["tracked"] == 1
    # History is preserved in the trade-by-trade detail view.
    assert len(detail["items"]) == 2
