"""TraderMatrix GEX shadow logging (incorporation plan Build #2, 2026-11-02).

Pins:
- normalize(): the verified REST /gex/{s} shape -> tmgex row, with the
  futures-space conversion computed at write time (flip_src x ratio),
  walls converted, regime from the signed total, pockets only where
  the vendor returns them, and NO futures mapping when proxy is null
  (USO -> crude stays honestly uncovered);
- refresh(): throttled polls write tmgex:<SYM> state + one append-only
  'tmgex' event per 5-minute bucket; vendor errors are recorded, never
  raised, and never block the other symbols;
- capture windows (plan: SPX/QQQ/SPY 03:00-16:45 CT, GLD 00:00-16:45);
- gex_ctx(): futures-space stamps — covered roots only, stale or
  proxy-less snapshots refuse to stamp;
- shadow_stamps(): the 0DTE measurement fields (SPY flip distance,
  name pocket kind / GEX bias from fresh research snapshots only);
- booking sites carry the stamps: survivors submit (MGC stamped, MCL
  honestly bare), engine paper entry + flatten exit;
- the digest GEX line renders from fresh rows and vanishes without them.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass.config import Config
from compass import tm_gex, survivors, scanner_shadow, day_digest
from compass.engine import Engine

CT = ZoneInfo("America/Chicago")
MON_10AM = datetime(2026, 11, 2, 10, 0, tzinfo=CT).timestamp()  # Mon


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'tmgex.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def _payload(symbol, spot, flip, total, proxy=None, pocket=None, walls=()):
    body = {
        "symbol": symbol, "spotPrice": spot, "gammaFlipLevel": flip,
        "totalGEX": total, "totalCallGEX": 1.0, "totalPutGEX": -2.0,
        "putCallGEXRatio": 3.0, "maxGammaStrike": walls[0]["strike"] if walls else spot,
        "keyLevels": list(walls), "byStrike": [], "gammaPocket": pocket,
        "proxy": proxy,
        "interpretation": {"marketRegime": "Negative Gamma" if total < 0 else "Positive Gamma"},
        "lastUpdated": "2026-11-02T16:00:00.000Z",
        "expirationsUsed": ["2026-11-02"],
    }
    return {"success": True, "cached": False, "data": body}


SPX_PAYLOAD = _payload(
    "SPX", 7541.83, 7695.04, -4.226e10,
    proxy={"futuresLabel": "ES", "ratio": 1.0063, "basis": 0,
           "measuredAt": "2026-11-02T16:00:04.686Z", "source": "live"},
    walls=[{"strike": 7520, "netGEX": -3.148e9, "type": "support"},
           {"strike": 7570, "netGEX": -2.486e9, "type": "resistance"}])
QQQ_PAYLOAD = _payload(
    "QQQ", 681.04, 719.67, -1.9e10,
    proxy={"futuresLabel": "NQ", "ratio": 41.281255, "basis": 0,
           "measuredAt": "2026-11-02T16:01:44.287Z", "source": "live"},
    walls=[{"strike": 680, "netGEX": -6.25e8, "type": "pin"}])
GLD_PAYLOAD = _payload(
    "GLD", 361.56, 362.89, 5.0e8,
    proxy={"futuresLabel": "GC", "ratio": 10.927185, "basis": 0,
           "measuredAt": "2026-11-02T15:55:27.369Z", "source": "live"},
    pocket={"kind": "positive_pocket", "label": "Positive Gamma Pocket"},
    walls=[{"strike": 365, "netGEX": -1.87e7, "type": "resistance"}])
SPY_PAYLOAD = _payload("SPY", 752.15, 745.0, 1.2e10)  # proxy null by design
USO_PAYLOAD = _payload("USO", 157.0, 161.50, -3.0e8)  # proxy null: no CL map

PAYLOADS = {"SPX": SPX_PAYLOAD, "QQQ": QQQ_PAYLOAD,
            "GLD": GLD_PAYLOAD, "SPY": SPY_PAYLOAD}


# ---------------------------------------------------------------- normalize

def test_normalize_converts_to_futures_space():
    row = tm_gex.normalize("SPX", SPX_PAYLOAD, MON_10AM)
    assert row["proxy_sym"] == "ES" and row["ratio"] == 1.0063
    assert row["flip_fut"] == pytest.approx(7695.04 * 1.0063)
    assert row["walls_fut"][0]["px"] == pytest.approx(7520 * 1.0063)
    assert row["walls"][0]["px"] == 7520  # source space retained
    assert row["bias"] == "short_gamma" and row["total_gex"] < 0
    assert row["gamma_pocket"] is None  # indices never carry one
    assert row["flip_dist_pct"] == pytest.approx(
        (7541.83 - 7695.04) / 7695.04 * 100, abs=0.01)
    assert row["source_ts"] is not None and row["received"] == MON_10AM


def test_normalize_gld_pocket_and_positive_regime():
    row = tm_gex.normalize("GLD", GLD_PAYLOAD, MON_10AM)
    assert row["gamma_pocket"] == "positive_pocket"
    assert row["bias"] == "long_gamma"
    assert row["flip_fut"] == pytest.approx(362.89 * 10.927185)


def test_normalize_no_proxy_means_no_futures_levels():
    row = tm_gex.normalize("USO", USO_PAYLOAD, MON_10AM)
    assert row["proxy_sym"] is None and row["ratio"] is None
    assert row["flip_fut"] is None and row["walls_fut"] == []
    assert row["flip_src"] == 161.50  # source-space flip still logged


def test_normalize_rejects_unusable_payloads():
    bad = _payload("SPX", 100.0, 0, -1.0)  # flip missing/zero
    with pytest.raises(ValueError):
        tm_gex.normalize("SPX", bad, MON_10AM)
    with pytest.raises(ValueError):
        tm_gex.normalize("SPX", {"success": False, "data": {}}, MON_10AM)
    with pytest.raises(ValueError):
        tm_gex.normalize("QQQ", SPX_PAYLOAD, MON_10AM)  # symbol mismatch


# ---------------------------------------------------------------- windows

def test_capture_windows():
    mon_2am = datetime(2026, 11, 2, 2, 0, tzinfo=CT).timestamp()
    mon_30am = datetime(2026, 11, 2, 0, 30, tzinfo=CT).timestamp()
    sat_10am = datetime(2026, 11, 7, 10, 0, tzinfo=CT).timestamp()
    mon_17pm = datetime(2026, 11, 2, 17, 0, tzinfo=CT).timestamp()
    assert tm_gex.capture_open("SPX", MON_10AM)
    assert not tm_gex.capture_open("SPX", mon_2am)     # index starts 03:00
    assert tm_gex.capture_open("GLD", mon_30am)       # gold starts 00:00
    assert not tm_gex.capture_open("GLD", mon_17pm)   # all stop 16:45
    assert not tm_gex.capture_open("SPX", sat_10am)   # trading days only
    assert not tm_gex.capture_open("USO", MON_10AM)   # not a polled symbol


# ---------------------------------------------------------------- refresh

def test_refresh_writes_state_buckets_and_throttles(db):
    calls = []

    def fake(sym):
        calls.append(sym)
        return PAYLOADS[sym]

    out = tm_gex.refresh(db, MON_10AM, fetch_fn=fake)
    assert sorted(out["polled"]) == ["GLD", "QQQ", "SPX", "SPY"]
    assert out["errors"] == {} and len(calls) == 4
    with db.tx() as c:
        row = db.get(c, "tmgex:SPX")
        assert row["flip_fut"] == pytest.approx(7695.04 * 1.0063)
        assert len(db.recent(c, "tmgex", "SPX")) == 1
        assert db.get(c, "tmgex:coverage")["uncovered"]["MCL"]
    # Inside the poll interval: nothing is due, nothing refetched.
    out2 = tm_gex.refresh(db, MON_10AM + 60, fetch_fn=fake)
    assert out2["due"] == [] and len(calls) == 4
    # Next interval: due again, and the new 5-minute bucket appends.
    out3 = tm_gex.refresh(db, MON_10AM + 300, fetch_fn=fake)
    assert sorted(out3["polled"]) == ["GLD", "QQQ", "SPX", "SPY"]
    with db.tx() as c:
        assert len(db.recent(c, "tmgex", "SPX")) == 2


def test_refresh_records_errors_and_continues(db):
    def fake(sym):
        if sym == "QQQ":
            raise RuntimeError("vendor down")
        return PAYLOADS[sym]

    out = tm_gex.refresh(db, MON_10AM, fetch_fn=fake)
    assert out["errors"] == {"QQQ": "RuntimeError"}
    assert sorted(out["polled"]) == ["GLD", "SPX", "SPY"]
    with db.tx() as c:
        assert db.get(c, "tmgex:QQQ") is None
        assert db.get(c, "tmgex:SPX") is not None
        job = db.get(c, "tmgex:job")
        assert job["last_errors"] == {"QQQ": "RuntimeError"}


def test_refresh_outside_windows_polls_nothing(db):
    sat = datetime(2026, 11, 7, 10, 0, tzinfo=CT).timestamp()
    out = tm_gex.refresh(db, sat, fetch_fn=lambda s: PAYLOADS[s])
    assert out["due"] == [] and out["polled"] == []


# ---------------------------------------------------------------- gex_ctx

def _seed(db, symbol, payload, received):
    row = tm_gex.normalize(symbol, payload, received)
    with db.tx() as c:
        db.put(c, "tmgex:" + symbol, row)
    return row


def test_gex_ctx_math_and_nearest_wall(db):
    row = _seed(db, "QQQ", QQQ_PAYLOAD, MON_10AM)
    flip_fut = row["flip_fut"]
    with db.tx() as c:
        ctx = tm_gex.gex_ctx(db, c, "MNQ.c.0", flip_fut + 25.0, 0.25,
                             MON_10AM + 30)
    assert ctx["flip_side"] == 1 and ctx["src_symbol"] == "QQQ"
    assert ctx["src"] == "tm" and ctx["regime"] == "short_gamma"
    assert ctx["dist_flip_ticks"] == pytest.approx(100.0)
    # Only wall: 680 src -> futures space; distance is wall - price.
    assert ctx["nearest_wall_px"] == pytest.approx(680 * 41.281255)
    assert ctx["dist_wall_ticks"] == pytest.approx(
        (680 * 41.281255 - (flip_fut + 25.0)) / 0.25)
    with db.tx() as c:
        below = tm_gex.gex_ctx(db, c, "NQ.c.0", flip_fut - 2.5, 0.25,
                               MON_10AM + 30)
    assert below["flip_side"] == -1
    assert below["dist_flip_ticks"] == pytest.approx(-10.0)


def test_gex_ctx_refuses_uncovered_stale_and_missing(db):
    _seed(db, "QQQ", QQQ_PAYLOAD, MON_10AM)
    with db.tx() as c:
        # MCL/CL: no vendor proxy exists — never stamped.
        assert tm_gex.gex_ctx(db, c, "MCL.v.0", 100.0, 0.01, MON_10AM) is None
        # MES maps to SPX, which was not seeded here.
        assert tm_gex.gex_ctx(db, c, "MES.c.0", 7500.0, 0.25, MON_10AM) is None
        # Stale snapshot (> 900s) refuses.
        assert tm_gex.gex_ctx(db, c, "MNQ.c.0", 29000.0, 0.25,
                              MON_10AM + 901) is None
        # No price, no stamp.
        assert tm_gex.gex_ctx(db, c, "MNQ.c.0", None, 0.25, MON_10AM) is None


# ---------------------------------------------------------------- shadow stamps

def test_shadow_stamps_from_fresh_sources(db):
    spy = _seed(db, "SPY", SPY_PAYLOAD, MON_10AM)
    body = dict(GLD_PAYLOAD["data"])
    with db.tx() as c:
        db.put(c, "research:extra_gex_GLD",
               {"received": MON_10AM, "data": {"success": True, "data": body}})
        out = tm_gex.shadow_stamps(db, c, "GLD", MON_10AM + 60)
    assert out["spy_flip_dist_pct"] == spy["flip_dist_pct"]
    assert out["name_pocket_kind"] == "positive_pocket"
    assert out["name_gex_bias"] == "long_gamma"


def test_shadow_stamps_stale_research_is_none(db):
    _seed(db, "SPY", SPY_PAYLOAD, MON_10AM)
    body = dict(GLD_PAYLOAD["data"])
    with db.tx() as c:
        db.put(c, "research:extra_gex_GLD",
               {"received": MON_10AM - 3600,
                "data": {"success": True, "data": body}})
        out = tm_gex.shadow_stamps(db, c, "GLD", MON_10AM)
        empty = tm_gex.shadow_stamps(db, c, "AAPL", MON_10AM)
    assert out["name_pocket_kind"] is None and out["name_gex_bias"] is None
    assert out["spy_flip_dist_pct"] is not None  # SPY leg unaffected
    assert empty == {"spy_flip_dist_pct": out["spy_flip_dist_pct"],
                     "name_pocket_kind": None, "name_gex_bias": None}


def test_shadow_record_carries_the_stamps(db):
    _seed(db, "SPY", SPY_PAYLOAD, MON_10AM)
    payload = {"publication": {"contract": {
        "expiry": "2026-11-06", "strike": 250, "type": "CALL"}}}
    with db.tx() as c:
        rec = scanner_shadow.new_record(
            db, c, "shadow:test", payload, "session_sweep_reclaim", "SPY",
            "SPY261106C00250000", None, MON_10AM, MON_10AM, MON_10AM + 3600)
    assert rec["spy_flip_dist_pct"] is not None
    assert rec["name_pocket_kind"] is None and rec["name_gex_bias"] is None


# ---------------------------------------------------------------- booking sites

SV_NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()


def _sv_sig(strat, **kw):
    base = {"strategy": strat, "dir": survivors.DIRECTION[strat],
            "close": 100.0, "ts": SV_NOW - 60, "tod": 720,
            "sdate": "2026-09-28", "atr_t": 100.0, "bar_index": 0}
    if strat in ("S2", "S4"):
        base.update({"stop_ticks": 25.0, "target_ticks": 50.0})
    if strat in survivors.HORIZON:
        base["hold"] = survivors.HORIZON[strat]
    base.update(kw)
    return base


def _sv_cfg():
    cfg = Config(local=True, risk=100)
    cfg.ict_futures_paper = True
    cfg.traderspost_enabled = False
    return cfg


def test_survivor_submit_stamps_covered_root(db):
    _seed(db, "GLD", GLD_PAYLOAD, SV_NOW)
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, _sv_cfg(), SV_NOW, "S3",
                                      "MGC.v.0", _sv_sig("S3"))
        assert r["submitted"]
        pos = db.get(c, "position:ict:survivor_s3:MGC.v.0:t1")
    ctx = pos["gex_ctx"]
    assert ctx["src_symbol"] == "GLD" and ctx["regime"] == "long_gamma"
    assert ctx["flip_side"] == -1  # entry ~100 vs flip ~3965 futures pts


def test_survivor_submit_mcl_carries_no_stamp(db):
    _seed(db, "GLD", GLD_PAYLOAD, SV_NOW)
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, _sv_cfg(), SV_NOW, "S2",
                                      "MCL.v.0", _sv_sig("S2"))
        assert r["submitted"]
        pos = db.get(c, "position:ict:survivor_s2:MCL.v.0:t1")
    assert "gex_ctx" not in pos  # crude is uncovered — honestly bare


def test_engine_entry_and_flatten_exit_stamps(db):
    sunday = datetime(2026, 9, 13, 17, 16, tzinfo=CT).timestamp()
    later = datetime(2026, 9, 14, 17, 2, tzinfo=CT).timestamp()
    row = tm_gex.normalize("SPX", SPX_PAYLOAD, sunday)
    row["flip_fut"] = 90.0  # entry (~100.50) sits above the flip
    engine = Engine(db, Config(local=True, paper_trading=True))
    sig = {"id": "gexfix", "symbol": "MESZ6@1", "side": "long",
           "signal_time": sunday, "stop_distance": 1,
           "strategy": "fixture", "track": "intraday"}
    quote = {"ts": sunday, "bid": 100, "ask": 100.25,
             "bid_size": 10, "ask_size": 10}
    with db.tx() as c:
        db.put(c, "tmgex:SPX", row)
        db.put(c, "quote:MESZ6@1", quote)
        assert engine.enter(c, sig, sunday)
        pos = db.get(c, "position:MESZ6@1")
    assert pos["gex_ctx"]["flip_side"] == 1
    assert pos["gex_ctx"]["dist_flip_ticks"] == pytest.approx(42.0)
    with db.tx() as c:
        row["received"] = later - 60  # fresh again at the exit read
        db.put(c, "tmgex:SPX", row)
        db.put(c, "quote:MESZ6@1", {**quote, "ts": later})
        engine.exits(c, later)
        closed = db.get(c, "position:MESZ6@1")
    assert closed["status"] == "closed"
    assert closed["exit_reason"] == "session_flatten"
    assert closed["gex_ctx_exit"]["flip_side"] == 1


# ---------------------------------------------------------------- digest

def test_digest_line_renders_and_vanishes(db):
    _seed(db, "SPX", SPX_PAYLOAD, MON_10AM)
    _seed(db, "QQQ", QQQ_PAYLOAD, MON_10AM)
    _seed(db, "GLD", GLD_PAYLOAD, MON_10AM)
    with db.tx() as c:
        line = tm_gex.digest_line(db, c, MON_10AM + 600)
    assert line.startswith("GEX — ")
    for token in ("ES", "NQ", "GC", "SPX", "short gamma", "long gamma"):
        assert token in line
    with db.tx() as c:
        section = day_digest._futures(db, c, MON_10AM - 3600, MON_10AM + 600)
    assert any(l.startswith("GEX — ") for l in section["lines"])


def test_digest_line_none_without_rows(db):
    with db.tx() as c:
        assert tm_gex.digest_line(db, c, MON_10AM) is None
        section = day_digest._futures(db, c, MON_10AM - 3600, MON_10AM)
    assert not any("GEX" in l for l in section["lines"])
