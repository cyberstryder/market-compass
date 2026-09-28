"""Shared helpers for the ICT futures concept detectors.

Production futures bars land under resolved keys like
`bar_window:NQ.c.0@123456` (alias@instrument_id), while tests write the
bare alias (`bar_window:NQ.c.0`). These helpers resolve either form so
detectors stay instrument-agnostic and test-friendly.

Evidence only. No alerts, no trades.
"""
from .market import number


def _bars_from_window(window):
    """Normalize bar_window rows ([ts,o,h,l,c,v,...]) to dicts."""
    out = []
    for row in window or []:
        try:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        except (IndexError, TypeError):
            continue
        if None in (ts, o, h, l, c):
            continue
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c})
    return out


def _bars_from_recent(rows):
    """Normalize db.recent('bar') dicts to the same shape."""
    out = []
    for r in rows or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'], 'l': p['l'], 'c': p['c']})
    return sorted(out, key=lambda b: b['ts'])


def resolve_bar_key(db, c, alias):
    """Return the db key suffix holding bars for `alias`.

    Prefers an exact `bar_window:<alias>` hit (tests), else the newest
    `bar_window:<alias>@<iid>` production key. Returns None when neither exists.
    """
    if db.get(c, 'bar_window:' + alias, None):
        return alias
    best, best_ts = None, -1
    for key in db.prefix(c, 'bar_window:' + alias + '@'):
        rows = db.get(c, key, [])
        ts = rows[-1][0] if rows and len(rows[-1]) else -1
        if ts and ts > best_ts:
            best, best_ts = key[11:], ts
    return best


def ict_bars(db, c, alias, limit=1800):
    """Bar dicts for a configured alias, via resolve_bar_key. Empty list when missing."""
    key = resolve_bar_key(db, c, alias)
    if not key:
        return []
    rows = db.get(c, 'bar_window:' + key, [])
    bars = _bars_from_window(rows)
    return bars[-limit:] if limit else bars


def atr(bars, period=14):
    """Wilder ATR over ascending bar dicts. None when insufficient."""
    if len(bars) < period + 1:
        return None
    trs = []
    prev_c = bars[0]['c']
    for b in bars[1:]:
        trs.append(max(b['h'] - b['l'], abs(b['h'] - prev_c), abs(b['l'] - prev_c)))
        prev_c = b['c']
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for t in trs[period:]:
        a = (a * (period - 1) + t) / period
    return a if a and a > 0 else None


def fractal_swings(bars, n=2):
    """Fractal swing highs/lows: extreme vs n bars each side.

    Returns ([(idx, price)], [(idx, price)]) = (highs, lows).
    """
    highs, lows = [], []
    for i in range(n, len(bars) - n):
        h, l = bars[i]['h'], bars[i]['l']
        if all(h >= bars[j]['h'] for j in range(i - n, i + n + 1) if j != i) and \
           any(h > bars[j]['h'] for j in range(i - n, i + n + 1) if j != i):
            highs.append((i, h))
        if all(l <= bars[j]['l'] for j in range(i - n, i + n + 1) if j != i) and \
           any(l < bars[j]['l'] for j in range(i - n, i + n + 1) if j != i):
            lows.append((i, l))
    return highs, lows
