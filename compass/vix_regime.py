"""VIX Regime Filter: adjust strategy selection based on VIX level.

This is a meta-layer, not a detector. It classifies the current VIX regime
and provides guidance on which strategies should run.

Regimes:
- LOW (VIX < 15): Complacent, tight ranges. Favor breakouts, momentum.
- NORMAL (15 <= VIX <= 25): All strategies run normally.
- HIGH (VIX > 25): Fear, wide ranges. Favor fades, mean reversion.

Research shows 10-20 percentage point win rate swings for the same strategy
across regimes. This filter removes negative alpha by disabling strategies
in regimes where they underperform.
"""
import time

from .market import number

SCAN_THROTTLE = 300  # 5 minutes; VIX moves intraday, gap fade needs a morning read
SNAPSHOT_URL = "https://api.massive.com/v3/snapshot"
VIX_TICKER = "I:VIX"


def fetch_vix(api_key, timeout=20):
    """Fetch current VIX from the Massive unified indices snapshot.

    Massive is already the primary options-data vendor (MASSIVE_API_KEY);
    the unified snapshot endpoint serves I:VIX with no new keys needed.
    Returns {"value", "market_status", "last_updated", ...} or None.
    """
    import httpx
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(SNAPSHOT_URL,
                          params={"ticker": VIX_TICKER, "apiKey": api_key})
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    for row in results:
        if str(row.get("ticker", "")).upper() != VIX_TICKER:
            continue
        try:
            v = float(row.get("value"))
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return {
            "value": v,
            "market_status": row.get("market_status"),
            "last_updated": row.get("last_updated"),
            "timeframe": row.get("timeframe"),
            "source": "massive",
            "at": time.time(),
        }
    return None


def scan(db, c, cfg, now):
    """Throttled pass: fetch VIX from Massive, store under the keys
    get_vix() probes ('vix:latest' first). Runs whenever the Massive key
    is present so data is already flowing before VIX_REGIME_ENABLED flips."""
    if not getattr(cfg, "massive", ""):
        return {"ran": False, "reason": "no_api_key"}
    if now - db.get(c, "vix:scanned_at", 0) < SCAN_THROTTLE:
        return {"ran": False, "reason": "throttled"}
    db.put(c, "vix:scanned_at", now)
    try:
        row = fetch_vix(cfg.massive)
    except Exception as e:
        db.health("vix_regime", "error", "fetch failed: %s" % (e,),
                  poll_ts=now)
        return {"ran": True, "fetched": False, "error": str(e)}
    if not row or not row.get("value"):
        db.health("vix_regime", "error", "no value in snapshot", poll_ts=now)
        return {"ran": True, "fetched": False, "error": "no_value"}
    db.put(c, "vix:latest", row)
    db.health("vix_regime", "available",
              "VIX %.2f (%s)" % (row["value"], row.get("market_status") or "?"),
              poll_ts=now)
    return {"ran": True, "fetched": True, "vix": row["value"]}


def get_vix(db, c):
    """Get current VIX level from database. Returns float or None."""
    # Try multiple sources
    for key in ('vix:latest', 'market:vix', 'vix:current'):
        data = db.get(c, key, {})
        if isinstance(data, dict):
            v = number(data.get('value') or data.get('vix') or data.get('close'))
            if v and v > 0:
                return v
        elif isinstance(data, (int, float)) and data > 0:
            return float(data)
    return None


def classify_regime(vix, low_threshold=15.0, high_threshold=25.0):
    """Classify VIX into regime. Returns 'low', 'normal', or 'high'."""
    if vix is None:
        return 'normal'  # Default to normal if no data
    if vix < low_threshold:
        return 'low'
    if vix > high_threshold:
        return 'high'
    return 'normal'


def should_run_detector(detector_name, regime):
    """Determine if a detector should run in the given regime.
    
    Returns True if the detector should run, False if it should be skipped.
    
    Logic:
    - Breakout strategies (bos, continuation) struggle in high VIX (false breakouts)
    - Fade strategies (turtle_soup, aoi_fade) struggle in low VIX (no snap-back)
    - In normal regime, everything runs
    """
    # Normalize detector name
    name = detector_name.lower().replace('-', '_').replace('ict_', '')
    
    # Breakout strategies: disable in high VIX
    breakout_detectors = {'bos_fvg', 'bos_gz_vwap', 'continuation', 'morning_drive', 'icc', 'rumers_box'}
    # Fade strategies: disable in low VIX  
    fade_detectors = {'turtle_soup', 'aoi_fade', 'aoi_zones', 'gap_fade'}
    
    if regime == 'high':
        # High VIX: breakouts fail, fades work
        if name in breakout_detectors:
            return False
        return True
    elif regime == 'low':
        # Low VIX: fades don't work (no volatility for snap-back), breakouts work
        if name in fade_detectors:
            return False
        return True
    else:  # normal
        return True


def get_regime_info(db, c, cfg):
    """Get current regime info. Returns dict with regime, vix, and timestamp."""
    if not getattr(cfg, 'vix_regime_enabled', False):
        return {'enabled': False, 'regime': 'normal', 'vix': None}
    
    vix = get_vix(db, c)
    low = number(getattr(cfg, 'vix_low_threshold', 15.0)) or 15.0
    high = number(getattr(cfg, 'vix_high_threshold', 25.0)) or 25.0
    regime = classify_regime(vix, low, high)
    
    return {
        'enabled': True,
        'regime': regime,
        'vix': vix,
        'low_threshold': low,
        'high_threshold': high,
        'at': time.time(),
    }
