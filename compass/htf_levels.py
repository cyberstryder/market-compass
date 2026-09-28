"""ICT HTF levels: prior-day/prior-week high/low + daily fractal swings.

Evidence only. This module never alerts and never trades. It reads daily bars
via db.recent(c, 'daily', symbol, limit=30) and emits a flat level list that
later detectors (turtle_soup, continuation) join as static support/resistance.
Missing or malformed daily rows degrade to exclusions, never exceptions.
"""
from datetime import datetime

from .market import number, CT

FRACTAL_N_DEFAULT = 2        # bars each side for a fractal swing
SCAN_THROTTLE = 3600         # seconds between scans (levels change daily)
DAILY_LIMIT = 30


def _daily_bars(db, c, symbol, limit=DAILY_LIMIT):
    """Normalize db.recent('daily') rows to ascending [{ts,o,h,l,c}].

    db.recent returns newest-first; we return oldest-first for fractal math.
    """
    out = []
    for r in db.recent(c, 'daily', symbol, limit=limit) or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'],
                    'l': p['l'], 'c': p['c']})
    return sorted(out, key=lambda b: b['ts'])


def _tv_daily_bars(symbol, cache_dir):
    """TradingView cache read for the scan path. Never touches the network."""
    try:
        from .tradingview_daily import daily_bars_for_scan
    except Exception:
        return []
    try:
        return daily_bars_for_scan(symbol, cache_dir=cache_dir) or []
    except Exception:
        return []


def _ct_week_key(ts):
    d = datetime.fromtimestamp(ts, CT).date()
    iso = d.isocalendar()
    return (iso[0], iso[1])


def fractal_swings(closes, n=FRACTAL_N_DEFAULT):
    """Fractal swing indices on an oldest-first close series.

    A swing high at i requires close[i] strictly greater than the closes on
    all N bars each side; mirrored for swing lows. Returns (highs, lows) as
    lists of ascending indices.
    """
    highs, lows = [], []
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = FRACTAL_N_DEFAULT
    if n < 1 or len(closes) < 2 * n + 1:
        return highs, lows
    vals = [number(c) for c in closes]
    for i in range(n, len(vals) - n):
        v = vals[i]
        if v is None:
            continue
        left = vals[i - n:i]
        right = vals[i + 1:i + n + 1]
        if any(x is None for x in left + right):
            continue
        if all(v > x for x in left + right):
            highs.append(i)
        elif all(v < x for x in left + right):
            lows.append(i)
    return highs, lows


def levels_for_symbol(symbol, daily, fractal_n, now):
    """Build HTF level rows for one symbol from ascending daily bars."""
    rows = []
    last = daily[-1]
    last_week = _ct_week_key(last['ts'])
    prior = daily[-2] if len(daily) >= 2 else daily[-1]
    age_prior = 1 if len(daily) >= 2 else 0
    for kind, price in (('pdh', prior['h']), ('pdl', prior['l'])):
        price = number(price)
        if price is None:
            continue
        rows.append({'symbol': symbol, 'price': price, 'kind': kind,
                     'age_sessions': age_prior, 'at': now})
    # Prior complete week: most recent week that is not the latest bar's week.
    weeks = {}
    for b in daily:
        weeks.setdefault(_ct_week_key(b['ts']), []).append(b)
    prior_weeks = sorted(k for k in weeks if k != last_week)
    if prior_weeks:
        wk = weeks[prior_weeks[-1]]
        pwh = max(b['h'] for b in wk)
        pwl = min(b['l'] for b in wk)
        age = sum(1 for b in daily if b['ts'] > max(x['ts'] for x in wk))
        for kind, price in (('pwh', pwh), ('pwl', pwl)):
            price = number(price)
            if price is None:
                continue
            rows.append({'symbol': symbol, 'price': price, 'kind': kind,
                         'age_sessions': age, 'at': now})
    # Fractal swings on daily closes.
    highs, lows = fractal_swings([b['c'] for b in daily], n=fractal_n)
    last_idx = len(daily) - 1
    for i in highs:
        rows.append({'symbol': symbol, 'price': number(daily[i]['c']),
                     'kind': 'swing_high', 'age_sessions': last_idx - i,
                     'at': now})
    for i in lows:
        rows.append({'symbol': symbol, 'price': number(daily[i]['c']),
                     'kind': 'swing_low', 'age_sessions': last_idx - i,
                     'at': now})
    return rows


def scan(db, c, cfg, now):
    """Throttled HTF-level scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_htf_levels', False):
        return {'ran': False, 'reason': 'disabled'}
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    if now - db.get(c, 'ict_htf_levels:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    try:
        fractal_n = int(getattr(cfg, 'ict_htf_fractal_n', FRACTAL_N_DEFAULT))
    except (TypeError, ValueError):
        fractal_n = FRACTAL_N_DEFAULT
    levels, excluded, sources = [], {}, {}
    use_tv = bool(getattr(cfg, 'ict_htf_tradingview', False))
    tv_cache_dir = getattr(cfg, 'ict_tv_cache_dir', '') or None
    for symbol in symbols:
        daily, source = [], None
        if use_tv:
            daily = _tv_daily_bars(symbol, tv_cache_dir)
            if daily:
                source = 'tradingview'
        if not daily:
            daily = _daily_bars(db, c, symbol)
            if daily:
                source = 'db'
        if not daily:
            excluded[symbol] = 'no_daily'
            continue
        sources[symbol] = source
        levels.extend(levels_for_symbol(symbol, daily, fractal_n, now))
    if not levels:
        return {'ran': False, 'reason': 'no_bars', 'excluded': excluded}
    db.put(c, 'ict_htf_levels:latest',
           {'at': now, 'levels': levels, 'excluded': excluded,
            'symbols': symbols, 'fractal_n': fractal_n,
            'sources': sources,
            'status': 'running' if levels else 'idle'})
    db.put(c, 'ict_htf_levels:scanned_at', now)
    return {'ran': True, 'symbols': len(symbols), 'rows': len(levels),
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: flat level list plus a per-symbol map."""
    latest = db.get(c, 'ict_htf_levels:latest', {})
    levels = latest.get('levels') or []
    by_symbol = {}
    for r in levels:
        by_symbol.setdefault(r.get('symbol'), []).append(r)
    return {
        'asof': latest.get('at'),
        'symbols': latest.get('symbols', []),
        'fractal_n': latest.get('fractal_n', FRACTAL_N_DEFAULT),
        'levels': levels[:limit],
        'map': by_symbol,
        'excluded': latest.get('excluded', {}),
        'sources': latest.get('sources', {}),
        'note': 'Research evidence only. No auto-admission, no alerts, no trades.',
    }
