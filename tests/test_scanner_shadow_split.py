"""Seat/shadow split tests (underlying-first validation, 2026-10-25).

Covers:
- a seated cell (an operator-promoted family x name) books exactly
  as before — since the 2026-11-02 unseat no family is promoted by
  default, so the mechanism tests promote exposure_level_break
  explicitly via env;
- the unseat (2026-11-02): session_sweep_reclaim is BARRED — even an
  env that seats AND promotes it cannot book it; it shadow-scores,
  its graduation flag never fires, and the book's default state has
  NO booking family (PROMOTED_FAMILIES empty);
- non-seated cells (sweep x TSLA; dead family volume_breakout x MU)
  create a shadow record and NO booking;
- shadow scoring: measured exit on fresh quotes, data_gap on stale
  marks and on quoteless force-close, data_gap excluded from PF;
- graduation flag math at the >=20 scored / PF >= 1.2 bar, gated on
  family promotion status;
- env override of the seated set; non-scanner alerts unaffected.
"""
import time
import types

import pytest

from compass.store import Store
from compass import scanner_shadow, zero_dte_paper

MU_OPT = "MU251027C00950000"
TSLA_OPT = "TSLA251027C00250000"
NFLX_OPT = "NFLX251027C01100000"


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'shadow.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _seed_alert(db, c, now, key, family, underlying, opt, quote=True,
                source_id=None):
    db.put(c, "zero_dte_paper:scan_ts", now - 3600)
    payload = {
        "status": "option_setup_new",
        "signal_price": 100.0,
        "underlying_invalidation": 98.0,
        "publication": {
            "category": "options_0dte",
            "contract": {"symbol": opt, "underlying": underlying,
                         "strike": 100, "type": "call",
                         "expiry": "2026-10-27"},
            "status": "option_setup_new",
        },
    }
    if family is not None:
        payload["rule"] = family
        payload["strategy"] = "0dte-compass-scanner-v2:" + family
    if source_id:
        payload["source_id"] = source_id
    db.append(c, "alert", "setup_study", opt, now - 5, payload, key=key)
    _seed_daily(db, c, underlying, now)
    if quote:
        # Gate-compliant under the whole-list book (respec 2026-11-01):
        # ask 0.80 <= G1 cap 0.90; spread 0.02 <= max(15% of ask, 0.05);
        # the seeded daily history gives range20 = 3% so G3 passes and
        # ask/spot = 0.008 <= range20/2.
        db.put(c, "quote:" + opt, {"ts": now, "bid": 0.78, "ask": 0.80})


def _seed_daily(db, c, symbol, now):
    # 21 prior sessions of 3% RTH range (25h steps: always distinct
    # calendar days, DST-proof).
    bar = {"o": 100.0, "h": 101.5, "l": 98.5, "c": 100.0, "v": 1000}
    items = [(symbol, now - i * 90000, bar) for i in range(1, 22)]
    db.append_bars(c, "daily", "fixture", items)


def _seated_cfg(*cells, promoted=""):
    seats = ";".join("%s:%s" % (fam, name) for fam, name in cells)
    return types.SimpleNamespace(
        scanner_paper_seats=seats, scanner_promoted_families=promoted,
        odte_book_seated_names="")


def _shadow_key(key):
    return scanner_shadow.SHADOW_PREFIX + "scan-" + key


# ------------------------------------------------------------------ gate


def test_seated_cell_books_exactly_as_before(db):
    # Respec 2026-11-01: nobody is seated by default (no grandfathering)
    # — the seat here is the operator action, passed explicitly.
    # Unseat 2026-11-02: no family is promoted by default either, so
    # the booking mechanism is exercised on exposure_level_break,
    # promoted explicitly via env (it is shadow-only by default but
    # NOT barred, so the mechanism stays reachable for it).
    cfg = _seated_cfg(("exposure_level_break", "MU"),
                      promoted="exposure_level_break")
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "seat1", "exposure_level_break", "MU", MU_OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        trade = db.get(c, "trade:0dte-scan-seat1")
        shadow = db.get(c, _shadow_key("seat1"))
    assert opened == 1
    assert trade is not None and trade["status"] == "open"
    assert trade["family"] == "exposure_level_break"
    assert trade["entry"] == 0.81
    assert shadow is None


def test_sweep_nonseat_name_shadows_without_booking(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "sh1", "session_sweep_reclaim", "TSLA",
                    TSLA_OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200)
        trade = db.get(c, "trade:0dte-scan-sh1")
        rec = db.get(c, _shadow_key("sh1"))
    assert opened == 0
    assert trade is None
    assert rec is not None
    assert rec["family"] == "session_sweep_reclaim"
    assert rec["underlying"] == "TSLA"
    assert rec["status"] == "open"
    assert rec["entry"] == 0.81
    assert rec["trigger"]["signal_price"] == 100.0
    assert rec["trigger"]["invalidation"] == 98.0


def test_dead_family_on_seated_name_shadows(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "sh2", "volume_breakout", "MU", MU_OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200)
        trade = db.get(c, "trade:0dte-scan-sh2")
        rec = db.get(c, _shadow_key("sh2"))
    assert opened == 0
    assert trade is None
    assert rec is not None and rec["family"] == "volume_breakout"


def test_non_scanner_alert_still_books(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "earn1", None, "MU", MU_OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200)
        trade = db.get(c, "trade:0dte-scan-earn1")
    assert opened == 1
    assert trade is not None and trade["family"] is None


def test_env_override_changes_the_seats(db):
    cfg = types.SimpleNamespace(
        scanner_paper_seats="exposure_level_break:TSLA",
        scanner_promoted_families="exposure_level_break",
        odte_book_seated_names="")
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "ov1", "exposure_level_break", "TSLA",
                    TSLA_OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        trade = db.get(c, "trade:0dte-scan-ov1")
        # MU lost its seat under the override: it shadows now.
        _seed_alert(db, c, now, "ov2", "exposure_level_break", "MU", MU_OPT)
        opened2 = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        shadow = db.get(c, _shadow_key("ov2"))
    assert opened == 1 and trade is not None
    assert opened2 == 0 and shadow is not None


def test_seat_parsing_fails_closed_on_garbage():
    # Respec 2026-11-01: the default seated set is EMPTY (no
    # grandfathering) and an unparseable override seats nobody — fail
    # closed, the opposite of the old validated-default fallback.
    assert scanner_shadow.paper_seats(None) == {}
    assert dict(scanner_shadow.PAPER_SEATS) == {}
    bad = types.SimpleNamespace(scanner_paper_seats="nonsense",
                                scanner_promoted_families="")
    assert scanner_shadow.paper_seats(bad) == {}
    assert scanner_shadow.seats_source(bad) == "none"
    assert scanner_shadow.is_seated("session_sweep_reclaim", "MU") is False
    assert scanner_shadow.is_seated("session_sweep_reclaim", "AMD") is False
    assert scanner_shadow.is_seated("session_sweep_reclaim", "NFLX") is False


# ------------------------------------------------------------------ unseat


def test_no_booking_family_by_default(db):
    # Unseat 2026-11-02: PROMOTED_FAMILIES is empty — the book has no
    # booking family — and session_sweep_reclaim sits in the barred
    # set alongside the three dead families. The scorecard surface
    # (what /api/simple serves) shows exactly that.
    assert scanner_shadow.PROMOTED_FAMILIES == frozenset()
    assert scanner_shadow.promoted_families(None) == frozenset()
    assert "session_sweep_reclaim" in scanner_shadow.BARRED_FAMILIES
    with db.tx() as c:
        card = scanner_shadow.scorecard(db, c, None)
    assert card["promoted_families"] == []
    assert card["barred_families"] == ["level_breakdown",
                                       "session_sweep_reclaim",
                                       "trend_pullback",
                                       "volume_breakout"]
    assert card["paper_seats"] == {}


def test_unseated_sweep_cannot_book_even_when_env_seats_and_promotes(db):
    # The barred check wins over BOTH operator envs: seat sweep x MU
    # via ODTE_BOOK_SEATED_NAMES and promote the family via
    # SCANNER_PROMOTED_FAMILIES — the alert still shadow-scores and
    # NO booking is created.
    cfg = types.SimpleNamespace(
        scanner_paper_seats="",
        scanner_promoted_families="session_sweep_reclaim",
        odte_book_seated_names="session_sweep_reclaim:MU")
    assert scanner_shadow.is_seated(
        "session_sweep_reclaim", "MU", cfg) is False
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "uns1", "session_sweep_reclaim", "MU",
                    MU_OPT)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        trade = db.get(c, "trade:0dte-scan-uns1")
        rec = db.get(c, _shadow_key("uns1"))
    assert opened == 0
    assert trade is None
    assert rec is not None
    assert rec["family"] == "session_sweep_reclaim"
    assert rec["status"] == "open" and rec["entry"] == 0.81


def test_alert_family_derivation():
    assert scanner_shadow.alert_family({"rule": "level_breakdown"}) == \
        "level_breakdown"
    assert scanner_shadow.alert_family(
        {"strategy": "0dte-compass-scanner-v2:flow_price_breakout"}) == \
        "flow_price_breakout"
    assert scanner_shadow.alert_family({"strategy": "earnings-play-v1"}) \
        is None
    assert scanner_shadow.alert_family({}) is None
    assert scanner_shadow.alert_family(None) is None


def test_trigger_inputs_recorded_for_native_families(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, "pending_research_options:sig-flow", {"signal": {
            "evidence": [{"source": "tradermatrix", "kind": "unusual_flow",
                          "premium": 250000, "score": 91,
                          "sentiment": "bullish",
                          "classification": "sweep", "vendor_id": "v1"}]}})
        _seed_alert(db, c, now, "fl1", "flow_price_breakout", "MU", MU_OPT,
                    source_id="sig-flow")
        zero_dte_paper._open_scanner(db, c, now, now + 7200)
        rec = db.get(c, _shadow_key("fl1"))
    assert rec is not None
    assert rec["trigger"]["flow"]["premium"] == 250000
    assert rec["trigger"]["flow"]["score"] == 91


# --------------------------------------------------------------- scoring


def _open_shadow(db, c, now, key="sc1", flatten_in=7200):
    _seed_alert(db, c, now, key, "session_sweep_reclaim", "TSLA", TSLA_OPT)
    zero_dte_paper._open_scanner(db, c, now, now + flatten_in)
    return db.get(c, _shadow_key(key))


def test_shadow_measured_target_exit_scores(db):
    now = time.time()
    with db.tx() as c:
        rec = _open_shadow(db, c, now)
        assert rec["status"] == "open" and rec["entry"] == 0.81
        db.put(c, "quote:" + TSLA_OPT,
               {"ts": now + 595, "bid": 3.30, "ask": 3.40})
        closed = scanner_shadow.update(db, c, now + 600)
        rec = db.get(c, _shadow_key("sc1"))
        card = scanner_shadow.scorecard(db, c, None)
    assert closed == 1
    assert rec["status"] == "closed"
    assert rec["exit_reason"] == "target hit (+50%)"
    assert rec["data_gap"] is False
    assert rec["pnl"] == 246.70
    cell = card["shadow_cells"][0]
    assert cell["scored"] == 1 and cell["wins"] == 1
    assert cell["data_gaps"] == 0


def test_shadow_stale_quote_close_is_data_gap(db):
    now = time.time()
    with db.tx() as c:
        _open_shadow(db, c, now)
        # Quote 200s old at evaluation: usable mark, but stale (>120s),
        # so the close is a data gap and never a measured result.
        db.put(c, "quote:" + TSLA_OPT,
               {"ts": now + 200, "bid": 3.30, "ask": 3.40})
        scanner_shadow.update(db, c, now + 400)
        rec = db.get(c, _shadow_key("sc1"))
        card = scanner_shadow.scorecard(db, c, None)
    assert rec["status"] == "closed"
    assert rec["data_gap"] is True
    cell = card["shadow_cells"][0]
    assert cell["scored"] == 0 and cell["data_gaps"] == 1


def test_shadow_quoteless_force_close_is_data_gap(db):
    now = time.time()
    with db.tx() as c:
        _open_shadow(db, c, now, flatten_in=3600)
        scanner_shadow.update(db, c, now + 3600 + 901)
        rec = db.get(c, _shadow_key("sc1"))
    assert rec["status"] == "closed"
    assert rec["exit_reason"] == "session flatten (data gap)"
    assert rec["data_gap"] is True
    assert rec["pnl"] is None


def test_shadow_entry_without_quote_is_data_gap_from_birth(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "nq1", "trend_pullback", "MU", MU_OPT,
                    quote=False)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200)
        rec = db.get(c, _shadow_key("nq1"))
        card = scanner_shadow.scorecard(db, c, None)
    assert opened == 0
    assert rec["status"] == "closed" and rec["data_gap"] is True
    assert rec["entry"] is None
    cell = card["shadow_cells"][0]
    assert cell["data_gaps"] == 1 and cell["scored"] == 0


def test_seated_entry_without_quote_records_seat_data_gap(db):
    # Task 003 item 8: a SEATED alert dropped for no fresh quote must
    # leave the same data_gap accounting the shadow path records —
    # under the seat book's own trade key (disjoint from the shadow
    # keyspace), so the seat scorecard's denominator stops silently
    # shrinking. Exactly one record, no booking, no shadow row.
    cfg = _seated_cfg(("exposure_level_break", "MU"),
                      promoted="exposure_level_break")
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "seatgap1", "exposure_level_break",
                    "MU", MU_OPT, quote=False)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        rec = db.get(c, "trade:0dte-scan-seatgap1")
        shadow = db.get(c, _shadow_key("seatgap1"))
        seat_rows = db.prefix(c, "trade:0dte-scan-")
        card = scanner_shadow.scorecard(db, c, None)
    assert opened == 0
    assert shadow is None
    assert list(seat_rows) == ["trade:0dte-scan-seatgap1"]
    assert rec["kind"] == "scanner"
    assert rec["family"] == "exposure_level_break"
    assert rec["status"] == "closed" and rec["data_gap"] is True
    assert rec["entry"] is None and rec["pnl"] is None
    assert rec["exit_reason"] == "entry quote unavailable (data gap)"
    # Not double-counted into the shadow book.
    assert card["totals"]["alerts"] == 0
    assert card["totals"]["data_gaps"] == 0


def test_seated_stale_quote_also_records_seat_data_gap(db):
    # Same drop, stale-quote flavor: the only quote is older than the
    # fresh-quote window, so _fresh_quote returns None.
    cfg = _seated_cfg(("exposure_level_break", "NFLX"),
                      promoted="exposure_level_break")
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, "seatgap2", "exposure_level_break",
                    "NFLX", NFLX_OPT, quote=False)
        db.put(c, "quote:" + NFLX_OPT,
               {"ts": now - 3600, "bid": 2.0, "ask": 2.1})
        opened = zero_dte_paper._open_scanner(db, c, now, now + 7200, cfg)
        rec = db.get(c, "trade:0dte-scan-seatgap2")
    assert opened == 0
    assert rec is not None and rec["data_gap"] is True
    assert rec["status"] == "closed"


def test_active_contracts_include_open_shadow(db):
    now = time.time()
    with db.tx() as c:
        _open_shadow(db, c, now)
        symbols = zero_dte_paper.active_contracts(db, c, now)
    assert TSLA_OPT in symbols


# ------------------------------------------------------------- graduation


def _closed(db, c, key, family, underlying, pnl):
    db.put(c, key, {"id": key, "kind": "scanner_shadow", "family": family,
                    "underlying": underlying, "status": "closed",
                    "data_gap": False, "pnl": pnl})


def _fill_cell(db, c, family, underlying, wins, losses):
    for i in range(wins):
        _closed(db, c, "shadow:0dte-g:%s:%s:w%d" % (family, underlying, i),
                family, underlying, 100.0)
    for i in range(losses):
        _closed(db, c, "shadow:0dte-g:%s:%s:l%d" % (family, underlying, i),
                family, underlying, -50.0)


def test_graduation_flag_math(db):
    promo_cfg = types.SimpleNamespace(
        scanner_paper_seats="",
        scanner_promoted_families="exposure_level_break",
        odte_book_seated_names="")
    with db.tx() as c:
        # 20 scored, PF 3.0 — clears the numeric bar in every cell below.
        _fill_cell(db, c, "session_sweep_reclaim", "MSFT", 12, 8)
        _fill_cell(db, c, "volume_breakout", "NFLX", 12, 8)
        _fill_cell(db, c, "exposure_level_break", "MSFT", 12, 8)
        _fill_cell(db, c, "exposure_level_break", "AAPL", 12, 7)  # 19 scored
        card = scanner_shadow.scorecard(db, c, None)
        promo = scanner_shadow.scorecard(db, c, promo_cfg)
    cells = {(x["family"], x["underlying"]): x for x in card["shadow_cells"]}
    # Unseat 2026-11-02: sweep clears the numbers and STILL never
    # flags — the family is barred, not promoted.
    msft = cells[("session_sweep_reclaim", "MSFT")]
    assert msft["scored"] == 20 and msft["pf"] == 3.0
    assert msft["graduation"]["family_promoted"] is False
    assert msft["graduation"]["family_barred"] is True
    assert msft["graduation"]["pf_met"] is True
    assert msft["graduation"]["ready_for_josh"] is False
    nflx = cells[("volume_breakout", "NFLX")]
    assert nflx["graduation"]["family_promoted"] is False
    assert nflx["graduation"]["family_barred"] is True
    assert nflx["graduation"]["ready_for_josh"] is False
    # The mechanism itself is intact: a family an operator promotes
    # via env flags ready at the bar, and only there.
    pcells = {(x["family"], x["underlying"]): x
              for x in promo["shadow_cells"]}
    elb = pcells[("exposure_level_break", "MSFT")]
    assert elb["graduation"]["family_promoted"] is True
    assert elb["graduation"]["family_barred"] is False
    assert elb["graduation"]["ready_for_josh"] is True
    aapl = pcells[("exposure_level_break", "AAPL")]
    assert aapl["graduation"]["scored_needed"] == 1
    assert aapl["graduation"]["ready_for_josh"] is False
    assert cells[("exposure_level_break", "MSFT")] \
        ["graduation"]["ready_for_josh"] is False
    # No grandfathering (respec 2026-11-01): the scorecard's seated set
    # is empty until an operator seats a graduated name. And since the
    # unseat, the default scorecard has NO promoted family at all.
    assert card["paper_seats"] == {}
    assert card["promoted_families"] == []
    assert promo["promoted_families"] == ["exposure_level_break"]
    assert card["barred_families"] == ["level_breakdown",
                                       "session_sweep_reclaim",
                                       "trend_pullback",
                                       "volume_breakout"]
