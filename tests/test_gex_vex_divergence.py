"""GEX/VEX divergence unblock (2026-11-02).

Covers:
- the read-only divergence split in the /api/simple 0DTE block
  (compass/simple.py _gex_vex_split): divergent vs non-divergent
  {tracked, wins, win_rate, pnl, avg_r} under the frozen definitions —
  closed rows only, data_gap rows excluded from both arms, rows
  without the entry stamp in neither arm, avgR = mean of
  pnl / (entry x 100 x qty), and NO post-2026-10-21 evidence-epoch
  filter (the banked sample is the pre-evidence cohort);
- the booking-stamp gap fix: scanner_shadow.new_record (the whole-list
  book's shadow / data-gap record builder) now carries the same
  gex/vex/gex_vex_divergent spread as the seated booking path.
"""
import time

import pytest

from compass.store import Store
from compass import scanner_shadow, simple, zero_dte_paper

NOW = time.time()
TSLA_OPT = "TSLA251027C00250000"


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'gexvex.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _trade(**kw):
    base = {"kind": "scanner", "status": "closed", "exited_at": NOW,
            "qty": 1, "entry": 1.0, "pnl": 0.0}
    base.update(kw)
    return base


def _split(db, c, now=NOW):
    out = simple.summary(db, c, now)
    return out["types"]["0dte"]["stats"]["gex_vex_divergence"]


def test_split_math_and_exclusions(db):
    with db.tx() as c:
        # Divergent arm: +50 on $1.00 premium (R +0.50), -50 on $2.00
        # (R -0.25). The win is a pre-evidence scanner row (no evidence
        # snapshot) — it must still count: the banked sample is exactly
        # that cohort.
        db.put(c, "trade:0dte-scan-d1", _trade(
            pnl=50.0, entry=1.0, gex=-120.0, vex=8000.0,
            gex_vex_divergent=True))
        db.put(c, "trade:0dte-scan-d2", _trade(
            pnl=-50.0, entry=2.0, evidence={"x": 1},
            gex=300.0, vex=-9000.0, gex_vex_divergent=True))
        # Non-divergent arm: +25 on $1.00 (R +0.25), -25 on $0.50
        # (R -0.50). Second row is a SPY-plan trade (wall rule).
        db.put(c, "trade:0dte-scan-n1", _trade(
            pnl=25.0, entry=1.0, evidence={"x": 1},
            gex=100.0, vex=7000.0, gex_vex_divergent=False))
        db.put(c, "trade:0dte-spy-2026-10-19-call-wall", _trade(
            kind="spy_plan", target_rule="wall",
            pnl=-25.0, entry=0.5,
            gex=None, vex=None, gex_vex_divergent=False))
        # Excluded: data_gap divergent row (flew blind).
        db.put(c, "trade:0dte-scan-g1", _trade(
            pnl=-999.0, data_gap=True, gex_vex_divergent=True))
        # Excluded: still-open divergent row.
        db.put(c, "trade:0dte-scan-o1", _trade(
            status="open", exited_at=None, pnl=None,
            gex_vex_divergent=True))
        # Excluded: closed row with no stamp at all (no divergence data).
        db.put(c, "trade:0dte-scan-u1", _trade(pnl=999.0))
        # Excluded: MU scalp row (merged into the block, never stamped).
        db.put(c, "trade:mu-x1", {"kind": "mu_scalp", "status": "closed",
                                  "exited_at": NOW, "pnl": 500.0})
        split = _split(db, c)
    div, non = split["divergent"], split["non_divergent"]
    assert div == {"tracked": 2, "wins": 1, "win_rate": 50.0, "pnl": 0.0,
                   "avg_r": 0.125, "r_trades": 2}
    assert non == {"tracked": 2, "wins": 1, "win_rate": 50.0, "pnl": 0.0,
                   "avg_r": -0.125, "r_trades": 2}


def test_split_avg_r_skips_rows_without_premium(db):
    with db.tx() as c:
        # Stamped and closed but no entry premium: counts in tracked /
        # wins / pnl, contributes no R.
        db.put(c, "trade:0dte-scan-d1", _trade(
            pnl=10.0, entry=None, gex_vex_divergent=True))
        db.put(c, "trade:0dte-scan-d2", _trade(
            pnl=-20.0, entry=1.0, gex_vex_divergent=True))
        split = _split(db, c)
    div = split["divergent"]
    assert div["tracked"] == 2 and div["wins"] == 1
    assert div["pnl"] == -10.0
    assert div["r_trades"] == 1 and div["avg_r"] == -0.2


def test_split_present_and_empty_on_empty_book(db):
    with db.tx() as c:
        split = _split(db, c)
    for arm in ("divergent", "non_divergent"):
        assert split[arm] == {"tracked": 0, "wins": 0, "win_rate": None,
                              "pnl": 0.0, "avg_r": None, "r_trades": 0}


# ------------------------------------------------------------------ stamps

def _payload(underlying="TSLA", symbol=TSLA_OPT):
    return {"status": "option_setup_new",
            "publication": {
                "category": "options_0dte",
                "contract": {"symbol": symbol, "underlying": underlying,
                             "strike": 100, "type": "call",
                             "expiry": "2026-10-27"},
                "status": "option_setup_new"}}


def test_new_record_stamps_divergence(db):
    with db.tx() as c:
        # Opposite signs, |VEX| >= 5000 -> divergent.
        db.put(c, "exposure:TSLA", {"gex": -120.0, "vex": 8000.0})
        rec = scanner_shadow.new_record(
            db, c, "shadow:0dte-scan-x", _payload(), "session_sweep_reclaim",
            "TSLA", TSLA_OPT, 0.81, NOW, NOW - 5, NOW + 7200)
    assert rec["gex"] == -120.0 and rec["vex"] == 8000.0
    assert rec["gex_vex_divergent"] is True


def test_new_record_stamps_non_divergent_and_fallback(db):
    with db.tx() as c:
        # Same signs -> not divergent.
        db.put(c, "exposure:TSLA", {"gex": 100.0, "vex": 8000.0})
        rec = scanner_shadow.new_record(
            db, c, "shadow:0dte-scan-x", _payload(), "session_sweep_reclaim",
            "TSLA", TSLA_OPT, 0.81, NOW, NOW - 5, NOW + 7200)
        assert rec["gex_vex_divergent"] is False
        # No exposure row at all -> the zero_dte_paper fallback shape.
        rec2 = scanner_shadow.new_record(
            db, c, "shadow:0dte-scan-y", _payload("AMD", "AMD251027C00100000"),
            "session_sweep_reclaim", "AMD", "AMD251027C00100000", 0.81,
            NOW, NOW - 5, NOW + 7200)
    assert rec2["gex"] is None and rec2["vex"] is None
    assert rec2["gex_vex_divergent"] is False


def test_shadow_booking_path_carries_stamp(db):
    """End to end through zero_dte_paper._open_scanner: a non-seated
    scanner alert's shadow record carries the divergence stamp."""
    now = NOW
    with db.tx() as c:
        db.put(c, "zero_dte_paper:scan_ts", now - 3600)
        payload = _payload()
        payload["rule"] = "session_sweep_reclaim"
        payload["strategy"] = "0dte-compass-scanner-v2:session_sweep_reclaim"
        payload["signal_price"] = 100.0
        db.append(c, "alert", "setup_study", TSLA_OPT, now - 5, payload,
                  key="gvx1")
        bar = {"o": 100.0, "h": 101.5, "l": 98.5, "c": 100.0, "v": 1000}
        db.append_bars(c, "daily", "fixture",
                       [("TSLA", now - i * 90000, bar) for i in range(1, 22)])
        db.put(c, "quote:" + TSLA_OPT, {"ts": now, "bid": 0.78, "ask": 0.80})
        db.put(c, "exposure:TSLA", {"gex": -120.0, "vex": 8000.0})
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200)
        rec = db.get(c, scanner_shadow.SHADOW_PREFIX + "scan-gvx1")
    assert opened == 0  # shadow path books nothing in the paper book
    assert rec is not None
    assert rec["gex"] == -120.0 and rec["vex"] == 8000.0
    assert rec["gex_vex_divergent"] is True
