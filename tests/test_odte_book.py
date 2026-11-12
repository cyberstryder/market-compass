"""Whole-list 0DTE book (respec 2026-11-01): the unified G1-G4 gate
set, the Stage-1 fill stamps, the single booking path, and the Stage-3
swing-surface retirement flag.

Gate expectations are the frozen respec's: G2 spread <= max(15% of
ask, $0.05), G3 trailing-20 median range >= 1.50% + ask/spot <=
range20/2, G4 caps (1 open/name, 2 bookings/name/day, 6 open
book-wide, $250/day planned loss). G1 (the $0.90 premium cap) was
removed 2026-11-12 at Josh's decision; planned max loss remains
50 x ask + $2.80 and still feeds G4."""
import time
import types

import pytest

from compass import odte_book, scanner_shadow, zero_dte_paper
from compass.config import Config
from compass.market import day
from compass.scanner import Scanner
from compass.engine import Engine
from compass.store import Store

OPT = "MU251031C00100000"


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'odte_book.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def _seed_daily(db, c, symbol, now, range_pct=0.03):
    half = range_pct * 100.0 / 2.0
    bar = {"o": 100.0, "h": 100.0 + half, "l": 100.0 - half,
           "c": 100.0, "v": 1000}
    items = [(symbol, now - i * 90000, bar) for i in range(1, 22)]
    db.append_bars(c, "daily", "fixture", items)


def _seed_alert(db, c, now, key, family, underlying, opt, bid=0.78,
                ask=0.80):
    db.put(c, "zero_dte_paper:scan_ts", now - 3600)
    payload = {
        "status": "option_setup_new",
        "signal_price": 100.0,
        "underlying_invalidation": 98.0,
        "publication": {
            "category": "options_0dte",
            "contract": {"symbol": opt, "underlying": underlying,
                         "strike": 100, "type": "call",
                         "expiry": day(now)},
            "status": "option_setup_new",
        },
    }
    if family is not None:
        payload["rule"] = family
        payload["strategy"] = "0dte-compass-scanner-v2:" + family
    db.append(c, "alert", "setup_study", opt, now - 5, payload, key=key)
    _seed_daily(db, c, underlying, now)
    db.put(c, "quote:" + opt, {"ts": now, "bid": bid, "ask": ask,
                               "source": "fixture"})


def _seated_cfg(*cells, promoted=""):
    seats = ";".join("%s:%s" % (fam, name) for fam, name in cells)
    return types.SimpleNamespace(
        scanner_paper_seats=seats, scanner_promoted_families=promoted,
        odte_book_seated_names="")


def _rejections(db, c):
    return [r["payload"] for r in db.recent(c, "alert", limit=50)
            if r["payload"].get("status") == "booking_rejected"]


# ------------------------------------------------------------------ G1-G3


def test_planned_max_loss_math():
    assert odte_book.planned_max_loss(0.90) == 47.80
    assert odte_book.planned_max_loss(0.50) == 27.80
    assert odte_book.planned_max_loss(None) is None


def test_gate_no_premium_cap(db):
    # G1 removed 2026-11-12: a $1.20 ask (above the retired $0.90
    # cap) is no longer an eligibility rejection. The premium is
    # recorded in details for Josh's manual call on the published
    # setup, and planned max loss still prices it.
    now = time.time()
    with db.tx() as c:
        _seed_daily(db, c, "XYZ", now)
        ok, why, det = odte_book.evaluate_candidate(
            db, c, "XYZ", {"bid": 1.16, "ask": 1.20}, 100.0, now)
    assert ok and why is None
    assert det["ask"] == 1.20
    assert det["spread"] == 0.04
    assert det["ask_spot_ratio"] == 0.012
    assert det["planned_max_loss"] == 62.80


def test_gate_g2_spread(db):
    now = time.time()
    with db.tx() as c:
        _seed_daily(db, c, "XYZ", now)
        ok, why, _ = odte_book.evaluate_candidate(
            db, c, "XYZ", {"bid": 0.60, "ask": 0.80}, 100.0, now)
        assert not ok and why == "spread_too_wide"
        # The nickel floor: a 5c spread on a 30c ask passes.
        ok2, _, _ = odte_book.evaluate_candidate(
            db, c, "XYZ", {"bid": 0.25, "ask": 0.30}, 100.0, now)
    assert ok2


def test_gate_g3_no_history_and_thin_range(db):
    now = time.time()
    with db.tx() as c:
        ok, why, _ = odte_book.evaluate_candidate(
            db, c, "NOHIST", {"bid": 0.78, "ask": 0.80}, 100.0, now)
        assert not ok and why == "movement_ineligible"
        _seed_daily(db, c, "THIN", now, range_pct=0.01)
        ok2, why2, _ = odte_book.evaluate_candidate(
            db, c, "THIN", {"bid": 0.78, "ask": 0.80}, 100.0, now)
    assert not ok2 and why2 == "movement_ineligible"


def test_gate_g3_premium_to_movement_ratio(db):
    now = time.time()
    with db.tx() as c:
        _seed_daily(db, c, "CALM", now, range_pct=0.016)
        # range20/2 = 0.008; ask/spot = 0.0085 > 0.008.
        ok, why, _ = odte_book.evaluate_candidate(
            db, c, "CALM", {"bid": 0.83, "ask": 0.85}, 100.0, now)
    assert not ok and why == "movement_ineligible"


def test_gate_pass_stamps_details(db):
    now = time.time()
    with db.tx() as c:
        _seed_daily(db, c, "XYZ", now)
        ok, why, det = odte_book.evaluate_candidate(
            db, c, "XYZ", {"bid": 0.78, "ask": 0.80}, 100.0, now)
    assert ok and why is None
    assert det["range20"] == 0.03
    assert det["planned_max_loss"] == 42.80


# ------------------------------------------------------------------ G4 caps


def _booked(db, c, key, underlying, planned=42.80, status="closed",
            entered_at=None):
    db.put(c, "trade:0dte-scan-" + key, {
        "id": "trade:0dte-scan-" + key, "kind": "scanner",
        "family": "session_sweep_reclaim", "underlying": underlying,
        "status": status, "entry": 0.81, "entered_at": entered_at,
        "planned_max_loss": planned, "data_gap": False})


def test_caps_name_position_open(db):
    now = time.time()
    with db.tx() as c:
        _booked(db, c, "a", "MU", status="open", entered_at=now)
        ok, why = odte_book.check_caps(db, c, "MU", 42.80, now)
    assert not ok and why == "name_position_open"


def test_caps_name_daily_cap(db):
    now = time.time()
    with db.tx() as c:
        _booked(db, c, "a", "MU", entered_at=now)
        _booked(db, c, "b", "MU", entered_at=now)
        ok, why = odte_book.check_caps(db, c, "MU", 42.80, now)
    assert not ok and why == "name_daily_cap"


def test_caps_book_open_cap(db):
    now = time.time()
    with db.tx() as c:
        for i, name in enumerate(["A", "B", "C", "D", "E", "F"]):
            _booked(db, c, "o%d" % i, name, status="open",
                    entered_at=now)
        ok, why = odte_book.check_caps(db, c, "MU", 42.80, now)
    assert not ok and why == "book_open_cap"


def test_caps_daily_risk_budget(db):
    now = time.time()
    with db.tx() as c:
        _booked(db, c, "a", "AAA", planned=120.0, entered_at=now)
        _booked(db, c, "b", "BBB", planned=120.0, entered_at=now)
        ok, why = odte_book.check_caps(db, c, "MU", 42.80, now)
        assert not ok and why == "daily_risk_budget"
        ok2, _ = odte_book.check_caps(db, c, "MU", 5.0, now)
    assert ok2


# ------------------------------------------- unified booking path (consumer)


def test_seated_wide_spread_rejected_not_booked(db):
    # Seated-path coverage runs on exposure_level_break, promoted
    # explicitly via env: since the 2026-11-02 unseat no family is
    # promoted by default (session_sweep_reclaim is barred).
    cfg = _seated_cfg(("exposure_level_break", "MU"),
                      promoted="exposure_level_break")
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "g2x", "exposure_level_break", "MU",
                    OPT, bid=0.60, ask=0.80)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        trade = db.get(c, "trade:0dte-scan-g2x")
        rejections = _rejections(db, c)
    assert opened == 0 and trade is None
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "spread_too_wide"
    assert rejections[0]["quote_evidence"]["ask"] == 0.80


def test_shadow_above_cap_books_record(db):
    # G1 removed 2026-11-12: the same $1.20-ask alert the cap used to
    # reject at booking now books a shadow record (opened counts only
    # seated bookings, so it stays 0), with the premium carried on
    # the record's entry (ask + slippage) and no booking_rejected.
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "g1x", "session_sweep_reclaim", "TSLA",
                    "TSLA251031C00250000", bid=1.16, ask=1.20)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200)
        rec = db.get(c, scanner_shadow.SHADOW_PREFIX + "scan-g1x")
        rejections = _rejections(db, c)
    assert opened == 0 and rec is not None
    assert rejections == []
    assert rec["entry"] == 1.21
    # The premium is recorded on the record (consulted quote's ask)
    # and priced into the planned max loss.
    assert rec["entry_quote"]["ask"] == 1.20
    assert rec["planned_max_loss"] == 62.80


def test_seated_booking_carries_fill_stamps(db):
    cfg = _seated_cfg(("exposure_level_break", "MU"),
                      promoted="exposure_level_break")
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "ok1", "exposure_level_break", "MU", OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        trade = db.get(c, "trade:0dte-scan-ok1")
    assert opened == 1 and trade is not None
    ev = trade["entry_quote"]
    assert ev["bid"] == 0.78 and ev["ask"] == 0.80
    assert ev["source"] == "fixture" and ev["age_s"] < 5
    assert trade["planned_max_loss"] == 42.80
    assert trade["dte_bucket"] == "0"
    assert trade["trigger"]["signal_price"] == 100.0
    assert trade["trigger"]["invalidation"] == 98.0


def test_retry_options_no_longer_books_scanner_signals(db):
    # One booking path: the non-research pending row for a scanner
    # signal is routed to the alert-consumer path, never engine.enter.
    cfg = Config(local=True, stocks=("MU",), futures=(),
                 paper_trading=True)
    now = time.time()
    signal = {"id": "sig-route-1", "symbol": "MU", "side": "long",
              "strategy": "compass-scanner-v2:session_sweep_reclaim",
              "signal_time": now - 60, "signal_price": 100.0,
              "invalidation": 98.0}
    with db.tx() as c:
        db.put(c, "pending_options:sig-route-1",
               {"signal": signal, "status": "waiting",
                "created_at": now, "expires_at": now + 120})
        Scanner(db, cfg).retry_options(c, {}, now, Engine(db, cfg))
        pending = db.get(c, "pending_options:sig-route-1")
        positions = db.prefix(c, "position:")
    assert pending["status"] == "routed_to_book_path"
    assert positions == {}


# ------------------------------------------------------- Stage 3 retirement


def test_swing_ideas_default_off_flag_layer(monkeypatch):
    monkeypatch.delenv("SWING_IDEAS_ENABLED", raising=False)
    assert Config(local=True).swing_ideas is False
    monkeypatch.setenv("SWING_IDEAS_ENABLED", "true")
    assert Config(local=True).swing_ideas is True


def test_trigger_bound_default_is_decision_time_60(monkeypatch):
    monkeypatch.delenv("SCANNER_TRIGGER_QUOTE_MAX_AGE", raising=False)
    assert Config(local=True).scanner_trigger_quote_max_age == 60
