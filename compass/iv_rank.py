"""IV rank via TraderMatrix MCP server.

For an options-only trader, IV rank is fundamental context: high rank favors
selling premium, low rank favors buying it. The REST surface has no confirmed
IV endpoint; the MCP server documents get_iv_rank.

Evidence: stored per symbol, shown on dashboard and in 0DTE context. IV rank
moves slowly (percentile over trailing year), so this is a slow poll, not an
alert trigger.
"""
import time

VERSION = "ivrank-v1"
SCAN_THROTTLE = 3600  # 1 hour; IV rank barely moves intraday
TOOL_NAME = "get_iv_rank"


def _headers(api_key):
    return {"X-API-Key": api_key, "Content-Type": "application/json",
            "Accept": "application/json"}


def fetch_iv_rank(api_key, symbol, timeout=20):
    """Call get_iv_rank for one symbol. Returns normalized dict or None."""
    import httpx
    from .darkpool import MCP_URL, _rpc, _extract_prints
    params = {"name": TOOL_NAME, "arguments": {"symbol": symbol}}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(MCP_URL, headers=_headers(api_key),
                           json=_rpc("tools/call", params))
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError("MCP get_iv_rank error: %s" % (data["error"],))
    prints = _extract_prints(data.get("result"))
    if not prints:
        return None
    raw = prints[0] if isinstance(prints, list) else prints
    if not isinstance(raw, dict):
        return None

    def num(*keys):
        for k in keys:
            v = raw.get(k)
            try:
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                continue
        return None
    return {
        "symbol": symbol,
        "iv_rank": num("iv_rank", "ivRank", "rank"),
        "iv": num("iv", "implied_volatility", "current_iv"),
        "iv_high_52w": num("iv_high_52w", "high_52w", "year_high"),
        "iv_low_52w": num("iv_low_52w", "low_52w", "year_low"),
        "at": time.time(),
    }


def scan(db, c, cfg, now):
    """Throttled pass: fetch IV rank for watch symbols, store per symbol."""
    if not getattr(cfg, "iv_rank", True):
        return {"ran": False, "reason": "disabled"}
    api_key = getattr(cfg, "matrix", "") or ""
    if not api_key:
        return {"ran": False, "reason": "no_api_key"}
    if now - db.get(c, "iv_rank:scanned_at", 0) < SCAN_THROTTLE:
        return {"ran": False, "reason": "throttled"}
    symbols = list(getattr(cfg, "watch_symbols", ()) or [])[:20]
    fetched, errors = 0, 0
    for symbol in symbols:
        try:
            row = fetch_iv_rank(api_key, symbol)
            if row and row.get("iv_rank") is not None:
                db.put(c, "iv_rank:" + symbol, {**row, "status": "ok"})
                fetched += 1
            else:
                errors += 1
        except Exception:
            errors += 1
    db.put(c, "iv_rank:scanned_at", now)
    db.health("iv_rank", "available" if fetched else "error",
              "%d symbols, %d errors" % (fetched, errors), poll_ts=now)
    return {"ran": True, "fetched": fetched, "errors": errors}


def get(db, c, symbol):
    """Stored IV rank for one symbol, {} if unavailable."""
    return db.get(c, "iv_rank:" + str(symbol or "").upper(), {}) or {}


def describe(row):
    """One-line summary: 'IV rank 78 (high — favor selling premium)'."""
    rank = (row or {}).get("iv_rank")
    if rank is None:
        return "IV rank unavailable"
    bias = ("high — favor selling premium" if rank >= 70 else
            "low — favor buying premium" if rank <= 30 else
            "mid — no premium bias")
    return "IV rank %.0f (%s)" % (rank, bias)


def display(db, c, now):
    """All stored IV ranks for the dashboard panel."""
    symbols = list(getattr(db, "_cfg_symbols", None) or [])
    out = {}
    # Read every iv_rank:* key via prefix scan.
    try:
        for key, val in db.prefix(c, "iv_rank:").items():
            if key == "iv_rank:scanned_at":
                continue
            sym = key.split("iv_rank:", 1)[1]
            if isinstance(val, dict) and val.get("iv_rank") is not None:
                out[sym] = {"iv_rank": val["iv_rank"], "iv": val.get("iv"),
                            "at": val.get("at"), "bias": describe(val)}
    except Exception:
        pass
    return {"asof": now, "version": VERSION, "ranks": out,
            "last_scan": db.get(c, "iv_rank:scanned_at", 0),
            "note": "Evidence: premium bias context, not a trigger."}
