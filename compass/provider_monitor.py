"""Provider usage and cost-effectiveness monitor.

Tracks every external API call by provider to answer:
- Which providers are we actually using?
- What's the cost per useful request?
- Which providers can we drop?

Providers tracked:
- alpaca ($99/mo): stocks, options chains, OPRA stream
- databento ($199/mo): CME futures
- massive ($199/mo): options chains, VIX, index reference
- tradermatrix (~$80/mo): flow, GEX, IV rank
- obsidian (Cipher X): Flash Agentic setups
"""
import time
from urllib.parse import urlparse
from datetime import datetime, timezone

# Monthly costs for ROI calculation
COSTS = {
    'alpaca': 99,
    'databento': 199,
    'massive': 199,
    'tradermatrix': 80,
    'obsidian': 0,  # Cipher X tier, cost tracked separately
}

# Map hostname to provider
def provider_from_url(url):
    host = urlparse(url).hostname or ''
    if 'alpaca' in host:
        return 'alpaca'
    if 'massive.com' in host or 'polygon.io' in host:
        return 'massive'
    if 'databento' in host:
        return 'databento'
    if 'traderdaddy.pro' in host or 'tradermatrix' in host:
        return 'tradermatrix'
    if 'obsidian' in host.lower() or 'accessobsidian' in host:
        return 'obsidian'
    return 'other'


def record_call(db, provider, endpoint, success, latency_ms, bytes_out=0):
    """Record a single API call. Called from providers.py get()."""
    now = time.time()
    day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')

    # Increment counters
    key_base = f"provider_usage:{provider}:{day}"
    c = db.conn  # Get raw connection for pipeline

    # Use db.put for simple counters (atomic incr via get+put)
    total = db.get(c, f"{key_base}:total", 0) + 1
    db.put(c, f"{key_base}:total", total)

    if success:
        ok = db.get(c, f"{key_base}:success", 0) + 1
        db.put(c, f"{key_base}:success", ok)
    else:
        fail = db.get(c, f"{key_base}:fail", 0) + 1
        db.put(c, f"{key_base}:fail", fail)

    # Track latency (running average)
    avg_key = f"{key_base}:avg_latency_ms"
    n_key = f"{key_base}:latency_n"
    n = db.get(c, n_key, 0)
    avg = db.get(c, avg_key, 0)
    new_avg = (avg * n + latency_ms) / (n + 1) if n > 0 else latency_ms
    db.put(c, avg_key, new_avg)
    db.put(c, n_key, n + 1)

    # Track endpoint breakdown (top endpoints per provider)
    ep_key = f"{key_base}:endpoint:{endpoint}"
    ep_count = db.get(c, ep_key, 0) + 1
    db.put(c, ep_key, ep_count)

    # Update last-seen
    db.put(c, f"provider_usage:{provider}:last_seen", now)


def get_daily_usage(db, provider, day=None):
    """Get usage stats for a provider on a given day."""
    if day is None:
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    key_base = f"provider_usage:{provider}:{day}"
    c = db.conn

    total = db.get(c, f"{key_base}:total", 0)
    success = db.get(c, f"{key_base}:success", 0)
    fail = db.get(c, f"{key_base}:fail", 0)
    avg_latency = db.get(c, f"{key_base}:avg_latency_ms", 0)

    return {
        'provider': provider,
        'day': day,
        'total_calls': total,
        'success': success,
        'fail': fail,
        'success_rate': (success / total * 100) if total > 0 else 0,
        'avg_latency_ms': round(avg_latency, 1),
        'monthly_cost': COSTS.get(provider, 0),
        'cost_per_1k_calls': round(COSTS.get(provider, 0) / (total / 1000), 2) if total > 0 else 0,
    }


def get_all_providers_usage(db, day=None):
    """Get usage for all providers."""
    providers = ['alpaca', 'databento', 'massive', 'tradermatrix', 'obsidian']
    return [get_daily_usage(db, p, day) for p in providers]


def get_weekly_summary(db):
    """Get 7-day rolling summary per provider."""
    from datetime import timedelta
    today = datetime.now(timezone.utc)

    summary = {}
    for provider in ['alpaca', 'databento', 'massive', 'tradermatrix', 'obsidian']:
        total_calls = 0
        total_success = 0
        total_fail = 0
        days_active = 0

        for i in range(7):
            day = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            usage = get_daily_usage(db, provider, day)
            if usage['total_calls'] > 0:
                days_active += 1
                total_calls += usage['total_calls']
                total_success += usage['success']
                total_fail += usage['fail']

        summary[provider] = {
            'provider': provider,
            'monthly_cost': COSTS.get(provider, 0),
            'weekly_calls': total_calls,
            'days_active': days_active,
            'success_rate': round(total_success / total_calls * 100, 1) if total_calls > 0 else 0,
            'avg_daily_calls': round(total_calls / 7, 0),
            'est_monthly_calls': round(total_calls / 7 * 30, 0),
        }

    return summary
