"""
TraderMatrix Intelligence Integration.

Fetches Earnings Calendar, Economic Calendar, and Market Health from
TraderMatrix API and makes them available to the engine as filters.

- Earnings Calendar: Flags OPRA tickers reporting earnings this week.
  Engine can avoid (volatility risk) or target (earnings plays) these tickers.
- Economic Calendar: Flags high-impact macro events (CPI, NFP, FOMC).
  Engine pauses futures trading during release windows to avoid chop.
- Market Health: Composite score (1-5) + signals.
  Engine uses as regime filter: HIGH = cautious on longs, LOW = cautious on shorts.

Web-only tools (Sector Flow, Reversal Finder, Earnings Flow) are scraped
via scheduled browser tasks and stored in the same DB keys.
See cron job: tradermatrix-web-scrape
"""
import json
import logging
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
CT = ZoneInfo("America/Chicago")

# OPRA tickers (16) - from summary
OPRA_TICKERS = {
    "AAPL", "AMD", "AMZN", "AVGO", "CRWV", "GOOGL", "META", "MSFT",
    "MU", "NFLX", "NVDA", "PLTR", "QQQ", "SPY", "TSLA", "USAR"
}

# High-impact economic events that should pause trading
HIGH_IMPACT_KEYWORDS = {
    "CPI", "Consumer Price Index",
    "NFP", "Non-Farm Payrolls", "Nonfarm Payrolls",
    "FOMC", "Federal Open Market",
    "GDP", "Gross Domestic Product",
    "PPI", "Producer Price Index",
    "Retail Sales",
    "Jobless Claims",
    "Unemployment Rate",
    "Fed Chair", "Powell",
    "Interest Rate Decision",
}

# API base (same as tradermatrix_api.py)
API_BASE = "https://api.traderdaddy.pro/api/v1"

def _get_api_key():
    """Get TraderMatrix API key from env or file."""
    key = os.environ.get("TRADERMATRIX_API_KEY")
    if key:
        return key
    key_file = os.path.expanduser("~/.config/tradermatrix/api_key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    return None

def _api_get(endpoint, params=None):
    """Fetch from TraderMatrix API."""
    key = _get_api_key()
    if not key:
        raise ValueError("TRADERMATRIX_API_KEY not set")
    url = f"{API_BASE}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("X-API-Key", key)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

def fetch_earnings_calendar(date_from=None, date_to=None):
    """
    Fetch earnings calendar. Returns list of events.
    If no dates provided, defaults to current week (Mon-Fri).
    """
    params = {}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    try:
        data = _api_get("earnings", params)
        return data.get("events", [])
    except Exception as e:
        log.warning(f"Failed to fetch earnings calendar: {e}")
        return []

def fetch_economic_calendar(date_from=None, date_to=None):
    """
    Fetch economic calendar. Returns list of macro events.
    If no dates provided, defaults to current week.
    """
    params = {}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    try:
        data = _api_get("economic-calendar", params)
        return data.get("events", [])
    except Exception as e:
        log.warning(f"Failed to fetch economic calendar: {e}")
        return []

def fetch_market_health():
    """
    Fetch market health. Returns dict with compositeScore, signals, etc.
    """
    try:
        return _api_get("market-health")
    except Exception as e:
        log.warning(f"Failed to fetch market health: {e}")
        return None

def filter_opra_earnings(events):
    """
    Filter earnings events to only OPRA tickers.
    Returns list of {symbol, date, time, eps_estimate, revenue_estimate, importance}
    """
    opra = []
    for ev in events:
        symbol = ev.get("symbol", "")
        # Handle preferred shares (e.g., FITB/PA) - extract base symbol
        base = symbol.split("/")[0]
        if base in OPRA_TICKERS:
            opra.append({
                "symbol": base,
                "full_symbol": symbol,
                "company": ev.get("companyName"),
                "date": ev.get("date"),
                "time": ev.get("time"),  # BMO (before market open) or AMC (after market close)
                "eps_estimate": ev.get("epsEstimate"),
                "revenue_estimate": ev.get("revenueEstimate"),
                "sector": ev.get("sector"),
                "importance": ev.get("importance"),
                "market_cap": ev.get("marketCapUsd"),
            })
    return opra

def filter_high_impact_economic(events):
    """
    Filter economic events to high-impact only.
    Returns list of {title, date, time, impact, description}
    """
    high = []
    for ev in events:
        title = ev.get("title", "") or ev.get("event", "") or ""
        # Check if title contains high-impact keywords
        is_high = any(kw.lower() in title.lower() for kw in HIGH_IMPACT_KEYWORDS)
        # Also check impact field if present
        impact = ev.get("impact", "") or ev.get("importance", "")
        if isinstance(impact, str) and impact.lower() in ("high", "3", "critical"):
            is_high = True
        if is_high:
            high.append({
                "title": title,
                "date": ev.get("date"),
                "time": ev.get("time"),
                "impact": impact,
                "description": ev.get("description", ""),
                "country": ev.get("country", ""),
            })
    return high

def get_todays_economic_events(events, today_str=None):
    """
    Filter economic events to today only.
    today_str: YYYY-MM-DD (defaults to today in CT)
    """
    if today_str is None:
        today_str = datetime.now(CT).strftime("%Y-%m-%d")
    today_events = []
    for ev in events:
        if ev.get("date") == today_str:
            today_events.append(ev)
    return today_events

def get_market_health_summary(health_data):
    """
    Extract key fields from market health data.
    Returns {score, label, alert_count, watch_count, signals}
    """
    if not health_data:
        return None
    composite = health_data.get("compositeScore", {})
    return {
        "score": composite.get("value"),
        "max_score": composite.get("max", 5),
        "label": composite.get("label"),
        "alert_count": health_data.get("alertCount", 0),
        "watch_count": health_data.get("watchCount", 0),
        "signals": health_data.get("signals", []),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

def is_news_window_now(economic_events, buffer_minutes=15):
    """
    Check if we're currently in a high-impact news window.
    Returns (is_in_window, event) tuple.
    
    News window = event time +/- buffer_minutes.
    Economic events are typically at 7:30 AM CT (8:30 ET) or 9:00 AM CT (10:00 ET).
    """
    now = datetime.now(CT)
    today_str = now.strftime("%Y-%m-%d")
    today_events = get_todays_economic_events(economic_events, today_str)
    high_impact = filter_high_impact_economic(today_events)
    
    for ev in high_impact:
        event_time = ev.get("time")
        if not event_time:
            continue
        # Parse time (format: "HH:MM" or "HH:MM AM/PM" or ISO)
        try:
            # Try HH:MM format
            if ":" in event_time and ("AM" not in event_time and "PM" not in event_time):
                hour, minute = map(int, event_time.split(":")[:2])
                event_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            else:
                # Try parsing as ISO or with AM/PM
                # Skip if can't parse
                continue
            # Check if now is within buffer of event time
            delta = abs((now - event_dt).total_seconds() / 60)
            if delta <= buffer_minutes:
                return True, ev
        except Exception:
            continue
    return False, None

def refresh_intel(db, c):
    """
    Fetch all three intel sources and store in DB.
    Called by engine on startup and periodically (e.g., hourly).
    
    Stores:
    - intel:earnings:{YYYY-MM-DD} - OPRA earnings for the week
    - intel:economic:{YYYY-MM-DD} - High-impact economic events for the week
    - intel:market_health - Latest market health snapshot
    - intel:last_refresh - Timestamp of last refresh
    """
    now = datetime.now(CT)
    today_str = now.strftime("%Y-%m-%d")
    
    # Fetch earnings (current week)
    earnings = fetch_earnings_calendar()
    opra_earnings = filter_opra_earnings(earnings)
    db.put(c, f"intel:earnings:{today_str}", {
        "date": today_str,
        "opra_earnings": opra_earnings,
        "total_events": len(earnings),
        "fetched_at": now.isoformat(),
    })
    log.info(f"Intel: {len(opra_earnings)} OPRA earnings this week (of {len(earnings)} total)")
    
    # Fetch economic calendar (current week)
    economic = fetch_economic_calendar()
    high_impact = filter_high_impact_economic(economic)
    db.put(c, f"intel:economic:{today_str}", {
        "date": today_str,
        "high_impact": high_impact,
        "total_events": len(economic),
        "fetched_at": now.isoformat(),
    })
    log.info(f"Intel: {len(high_impact)} high-impact economic events this week (of {len(economic)} total)")
    
    # Fetch market health
    health = fetch_market_health()
    health_summary = get_market_health_summary(health)
    if health_summary:
        db.put(c, "intel:market_health", health_summary)
        log.info(f"Intel: Market Health score {health_summary['score']}/{health_summary['max_score']} ({health_summary['label']})")
    
    # Record refresh time
    db.put(c, "intel:last_refresh", {
        "at": now.isoformat(),
        "earnings_count": len(opra_earnings),
        "economic_high_impact": len(high_impact),
        "market_health_score": health_summary["score"] if health_summary else None,
    })
    
    return {
        "earnings": opra_earnings,
        "economic_high_impact": high_impact,
        "market_health": health_summary,
    }

def get_earnings_for_symbol(db, c, symbol):
    """
    Check if a symbol has earnings this week.
    Returns earnings event dict or None.
    """
    today_str = datetime.now(CT).strftime("%Y-%m-%d")
    # Try today's cache first
    data = db.get(c, f"intel:earnings:{today_str}")
    if data:
        for ev in data.get("opra_earnings", []):
            if ev.get("symbol") == symbol:
                return ev
    return None

def should_pause_for_news(db, c):
    """
    Check if engine should pause trading due to high-impact news.
    Returns (should_pause, event) tuple.
    """
    today_str = datetime.now(CT).strftime("%Y-%m-%d")
    data = db.get(c, f"intel:economic:{today_str}")
    if not data:
        return False, None
    high_impact = data.get("high_impact", [])
    # Reconstruct events list for is_news_window_now
    # (it expects full event dicts, but we stored filtered ones)
    # For now, check if any high-impact event is today and within window
    now = datetime.now(CT)
    for ev in high_impact:
        if ev.get("date") == today_str:
            event_time = ev.get("time")
            if event_time:
                try:
                    if ":" in event_time and "AM" not in event_time and "PM" not in event_time:
                        hour, minute = map(int, event_time.split(":")[:2])
                        event_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        delta = abs((now - event_dt).total_seconds() / 60)
                        if delta <= 15:  # 15-min buffer
                            return True, ev
                except Exception:
                    continue
    return False, None

def get_market_health_regime(db, c):
    """
    Get market health regime for filtering.
    Returns: "risk_on" (score 1-2), "neutral" (score 3), "risk_off" (score 4-5)
    Or None if no data.
    """
    data = db.get(c, "intel:market_health")
    if not data:
        return None
    score = data.get("score")
    if score is None:
        return None
    if score <= 2:
        return "risk_on"
    elif score == 3:
        return "neutral"
    else:
        return "risk_off"
