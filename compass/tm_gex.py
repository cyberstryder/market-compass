"""TraderMatrix GEX shadow logging (incorporation plan Build #2, 2026-11-02).

Polls TraderMatrix's per-symbol gamma exposure for the index/gold source
symbols, converts the levels into futures space at write time via the
vendor's proxy block, and stamps the context onto futures entries/exits
and 0DTE shadow rows. LOGGING AND CONTEXT ONLY: no consumer may gate,
rank, block, or alert on this data. The gate before any use is the
plan's placebo-strike information test on >=20 accumulated sessions,
run as a later study — this module only accumulates the evidence.

Vendor framing, adopted verbatim from the plan: the flip is a regime
boundary, not a level price is drawn toward; walls are where hedging
concentrates, not price targets.

State:
- tmgex:<SYM> — latest normalized snapshot for SPX, QQQ, GLD, SPY.
- events kind 'tmgex' — append-only 5-minute buckets per symbol during
  the capture windows; this is the forward history the C2 options
  candidate's >=20-session precondition counts.
- tmgex:coverage — the honest coverage map. USO's proxy is null at the
  vendor (verified live 2026-11-02), so NO CL-mapped GEX levels exist:
  MCL/CL are uncovered and get SPX index-regime context only. Nothing
  here fakes a crude proxy.

Cadence: one poll per symbol per 300s inside its capture window
(SPX/QQQ/SPY 03:00-16:45 CT, GLD 00:00-16:45 CT, Mon-Fri) = 4 symbols
x 12 polls/hour = 0.8 req/min, ~2.7% of the shared 30 req/min
TraderMatrix budget. The engine drives refresh() outside any tick
transaction, mirroring _refresh_intel_if_due (2026-10-23 lesson: no
vendor HTTP inside a tick transaction).
"""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .instruments import future_root
from .market import number
from .vendor import source_time

log = logging.getLogger(__name__)
CT = ZoneInfo("America/Chicago")

VERSION = "tmgex-v1"
SYMBOLS = ("SPX", "QQQ", "GLD", "SPY")
POLL_INTERVAL = 300          # seconds between polls of one symbol
BUCKET_S = 300               # history bucket width (5 minutes)
STALE_S = 900                # stamps refuse snapshots older than this
POCKET_FRESH_S = 2100        # plan: pocket snapshots fresh <= 35 min
DIGEST_FRESH_S = 4 * 3600    # digest line may use same-day snapshots
JOB_KEY = "tmgex:job"
COVERAGE_KEY = "tmgex:coverage"

# Capture windows in minutes after CT midnight, Mon-Fri. GLD starts at
# midnight because the MGC survivors trade the Asia/London windows.
_WINDOWS = {"SPX": (180, 1005), "QQQ": (180, 1005), "SPY": (180, 1005),
            "GLD": (0, 1005)}

# Futures root -> tmgex source symbol. Roots absent here (MCL, CL, YM,
# M2K, the FX micros, silver) have no vendor GEX proxy: uncovered.
ROOT_SOURCE = {"ES": "SPX", "MES": "SPX", "NQ": "QQQ", "MNQ": "QQQ",
               "GC": "GLD", "MGC": "GLD"}

COVERAGE = {
    "covered": {"ES": "SPX", "NQ": "QQQ", "GC": "GLD",
                "MES": "SPX", "MNQ": "QQQ", "MGC": "GLD"},
    "uncovered": {
        "MCL": "USO proxy is null at the vendor (verified 2026-11-02); "
               "no CL-mapped GEX levels exist. SPX index-regime context "
               "only — no levels are fabricated.",
        "CL": "USO proxy is null at the vendor (verified 2026-11-02); "
              "no CL-mapped GEX levels exist.",
    },
    "note": "Gamma pockets are null for index tickers by vendor design; "
            "pockets exist only for single names (via research:extra_gex_*).",
}


def capture_open(symbol, now):
    """True when `symbol` is inside its plan capture window (CT)."""
    window = _WINDOWS.get(symbol)
    if window is None:
        return False
    dt = datetime.fromtimestamp(now, CT)
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return window[0] <= minutes <= window[1]


def normalize(symbol, payload, received):
    """One REST /gex/{s} payload -> the tmgex state row (plan §1 shape).

    Futures-space levels are computed here, at write time, so consumers
    never redo the proxy conversion. Raises ValueError on a payload
    that cannot support the row — a bad snapshot must fail the poll,
    not write a half-row."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ValueError("Invalid GEX envelope")
    body = payload.get("data", payload)
    if not isinstance(body, dict):
        raise ValueError("Invalid GEX body")
    if body.get("symbol") and body["symbol"] != symbol:
        raise ValueError("GEX symbol mismatch")
    spot = number(body.get("spotPrice"))
    flip = number(body.get("gammaFlipLevel"))
    if spot is None or spot <= 0 or flip is None or flip <= 0:
        raise ValueError("GEX payload missing spot or flip")
    total = number(body.get("totalGEX"))
    proxy = body.get("proxy") if isinstance(body.get("proxy"), dict) else None
    proxy_sym = proxy.get("futuresLabel") if proxy else None
    ratio = number(proxy.get("ratio")) if proxy else None
    if ratio is not None and ratio <= 0:
        ratio = None
    flip_fut = flip * ratio if ratio else None
    walls = []
    for w in body.get("keyLevels") or []:
        if not isinstance(w, dict):
            continue
        px = number(w.get("strike"))
        if px is None or px <= 0:
            continue
        walls.append({"px": px, "net_gex": number(w.get("netGEX")),
                      "type": w.get("type")})
    walls.sort(key=lambda w: w["px"])
    walls_fut = [{"px": w["px"] * ratio, "net_gex": w["net_gex"],
                  "type": w["type"]} for w in walls] if ratio else []
    pocket = body.get("gammaPocket")
    interp = body.get("interpretation")
    if not isinstance(interp, dict):
        interp = {}
    return {
        "symbol": symbol, "version": VERSION,
        "spot": spot, "flip_src": flip, "flip_fut": flip_fut,
        "proxy_sym": proxy_sym, "ratio": ratio,
        "walls": walls, "walls_fut": walls_fut,
        "total_gex": total,
        "bias": ("short_gamma" if total < 0 else "long_gamma")
                if total is not None else None,
        "vendor_regime": interp.get("marketRegime"),
        "gamma_pocket": pocket.get("kind") if isinstance(pocket, dict) else None,
        "max_gamma_strike": number(body.get("maxGammaStrike")),
        "flip_dist_pct": round((spot - flip) / flip * 100, 3),
        "source_ts": source_time(body.get("lastUpdated"))
                     or (source_time(proxy.get("measuredAt")) if proxy else None),
        "received": received,
    }


def fetch(symbol):
    from .tradermatrix_intel import _api_get
    return _api_get("gex/" + symbol)


def refresh(db, now, fetch_fn=None):
    """One throttled poll pass: decide (short tx) -> HTTP (no connection
    held) -> write (short tx). Mirrors tradermatrix_intel.refresh_intel's
    2026-10-23 structure. Returns a small summary dict; never raises for
    a vendor failure (errors are recorded on the job row and health)."""
    fetch_fn = fetch_fn or fetch
    with db.tx() as c:
        job = db.get(c, JOB_KEY, {}) or {}
        attempts = dict(job.get("attempts") or {})
        rows = {s: db.get(c, "tmgex:" + s, {}) or {} for s in SYMBOLS}
        due = []
        for sym in SYMBOLS:
            if not capture_open(sym, now):
                continue
            last = max(attempts.get(sym, 0), rows[sym].get("received", 0))
            if now - last >= POLL_INTERVAL:
                due.append(sym)
        for sym in due:
            attempts[sym] = now
        job["attempts"] = attempts
        db.put(c, JOB_KEY, job)
    results, errors = {}, {}
    for sym in due:
        try:
            results[sym] = normalize(sym, fetch_fn(sym), now)
        except Exception as e:  # vendor/transport/shape: record, continue
            errors[sym] = (str(e) if isinstance(e, ValueError)
                           else type(e).__name__)[:160]
    with db.tx() as c:
        job = db.get(c, JOB_KEY, {}) or {}
        buckets = dict(job.get("buckets") or {})
        for sym, row in results.items():
            db.put(c, "tmgex:" + sym, row)
            bucket = int(now // BUCKET_S)
            if buckets.get(sym) != bucket:
                db.append(c, "tmgex", "tradermatrix", sym,
                          bucket * BUCKET_S, row)
                buckets[sym] = bucket
        job["buckets"] = buckets
        job["last_errors"] = errors
        job["updated"] = now
        db.put(c, JOB_KEY, job)
        db.put(c, COVERAGE_KEY, COVERAGE)
    if results and not errors:
        db.health("tmgex", "receiving",
                  "TraderMatrix GEX snapshots landing on schedule",
                  last_symbols=",".join(sorted(results)))
    elif results:
        db.health("tmgex", "partial",
                  "Some GEX symbols failed this pass; others continue",
                  last_symbols=",".join(sorted(results)),
                  last_errors=errors)
    elif due:
        db.health("tmgex", "error",
                  "All due GEX polls failed this pass", last_errors=errors)
    if errors:
        log.warning("tmgex poll errors: %s", errors)
    return {"due": due, "polled": sorted(results), "errors": errors}


def gex_ctx(db, c, symbol, price, tick, now):
    """Entry/exit context stamp for one futures price, or None.

    Reads the mapped source symbol's tmgex row and expresses the flip
    and nearest wall in the future's own points. None when the root is
    uncovered (MCL/CL/...), the row is missing, the snapshot is stale
    (> STALE_S), or the source has no futures proxy. Stamps are
    evidence only — dist_flip_ticks is signed (price - flip);
    dist_wall_ticks is signed (wall - price)."""
    if price is None:
        return None
    root = future_root(symbol)
    source = ROOT_SOURCE.get(root) if root else None
    if not source:
        return None
    row = db.get(c, "tmgex:" + source, {}) or {}
    received = row.get("received") or 0
    if not row or now - received > STALE_S:
        return None
    flip_fut = row.get("flip_fut")
    if flip_fut is None:
        return None
    ctx = {"flip_side": 1 if price >= flip_fut else -1,
           "dist_flip_ticks": round((price - flip_fut) / tick, 2) if tick else None,
           "nearest_wall_px": None, "dist_wall_ticks": None,
           "regime": row.get("bias"), "src": "tm", "src_symbol": source,
           "flip_fut": flip_fut, "gex_source_ts": row.get("source_ts"),
           "gex_age_s": round(now - received, 1)}
    walls = row.get("walls_fut") or []
    if walls:
        wall = min(walls, key=lambda w: abs(w["px"] - price))
        ctx["nearest_wall_px"] = wall["px"]
        if tick:
            ctx["dist_wall_ticks"] = round((wall["px"] - price) / tick, 2)
    return ctx


def _research_body(item):
    """Navigate a research:extra_gex_<SYM> snapshot to its GEX body.

    The research store keeps the whole vendor envelope under 'data'
    (body nested one level down) and may also carry per-item rows.
    Returns (body, freshest_ts) or (None, 0)."""
    if not isinstance(item, dict) or not item:
        return None, 0
    stamps = [item.get("received") or 0]
    data = item.get("data")
    candidates = []
    if isinstance(data, dict):
        candidates.append(data)
        nested = data.get("data")
        if isinstance(nested, dict):
            candidates.append(nested)
    for row in item.get("items") or []:
        if isinstance(row, dict):
            stamps.append(row.get("source_ts") or 0)
            if isinstance(row.get("data"), dict):
                candidates.append(row["data"])
    for body in candidates:
        if "gammaPocket" in body or "totalGEX" in body:
            return body, max(stamps)
    return None, max(stamps)


def shadow_stamps(db, c, underlying, now):
    """The plan §1 measurement stamps for one 0DTE shadow row:
    {spy_flip_dist_pct, name_pocket_kind, name_gex_bias}. Every field
    is None when its source is missing or stale — never raises."""
    out = {"spy_flip_dist_pct": None, "name_pocket_kind": None,
           "name_gex_bias": None}
    try:
        row = db.get(c, "tmgex:SPY", {}) or {}
        if row and now - (row.get("received") or 0) <= STALE_S:
            out["spy_flip_dist_pct"] = row.get("flip_dist_pct")
    except Exception:
        pass
    if underlying:
        try:
            body, stamp = _research_body(
                db.get(c, "research:extra_gex_" + underlying, {}))
            if body is not None and stamp and now - stamp <= POCKET_FRESH_S:
                pocket = body.get("gammaPocket")
                if isinstance(pocket, dict):
                    out["name_pocket_kind"] = pocket.get("kind")
                total = number(body.get("totalGEX"))
                if total is not None:
                    out["name_gex_bias"] = ("short_gamma" if total < 0
                                            else "long_gamma")
        except Exception:
            pass
    return out


def digest_line(db, c, now=None):
    """One GEX regime context line for the day digest, or None.

    Display only (levels tools may be displayed immediately per the
    plan); distances are spot-vs-flip percentages, which are identical
    in source and futures space (the proxy ratio cancels)."""
    now = now or time.time()
    segments = []
    for label, source, futures_space in (("ES", "SPX", True),
                                         ("NQ", "QQQ", True),
                                         ("GC", "GLD", True),
                                         ("SPX", "SPX", False)):
        row = db.get(c, "tmgex:" + source, {}) or {}
        if not row or now - (row.get("received") or 0) > DIGEST_FRESH_S:
            continue
        pct = row.get("flip_dist_pct")
        if pct is None:
            continue
        seg = "%s %s flip %.1f%%" % (label, "above" if pct >= 0 else "below",
                                     abs(pct))
        if row.get("bias"):
            seg += " · " + row["bias"].replace("_", " ")
        if futures_space and row.get("walls_fut") and row.get("ratio"):
            spot_fut = row["spot"] * row["ratio"]
            wall = min(row["walls_fut"], key=lambda w: abs(w["px"] - spot_fut))
            seg += " · wall %g" % wall["px"]
        segments.append(seg)
    if not segments:
        return None
    return "GEX — " + " | ".join(segments)
