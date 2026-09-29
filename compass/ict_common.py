"""Shared helpers for the ICT futures concept detectors.

Production futures bars land under dated-contract keys like
`bar_window:MNQZ25@123456` (raw symbol @ instrument id), as written by the
Databento collectors, while tests write the bare alias
(`bar_window:MNQ.c.0`). These helpers resolve either form so detectors
stay instrument-agnostic and test-friendly: an alias such as `MNQ.c.0`
or `MGC.v.0` resolves to the newest dated-contract window for its root.

Evidence only. No alerts, no trades.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .instruments import future_root
from .market import number

CT = ZoneInfo("America/Chicago")

# bar_window:<ROOT><month><yy>@<iid>, e.g. bar_window:MNQZ25@123456
_DATED_WINDOW = re.compile(r'^bar_window:([A-Z]+)([FGHJKMNQUVXZ]\d{1,4})@(.+)$')


def _bars_from_window(window):
    """Normalize bar_window rows ([ts,o,h,l,c,v,...]) to dicts (volume kept)."""
    out = []
    for row in window or []:
        try:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        except (IndexError, TypeError):
            continue
        if None in (ts, o, h, l, c):
            continue
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c,
                    'v': row[5] if len(row) > 5 else None})
    return out


def _bars_from_recent(rows):
    """Normalize db.recent('bar') dicts to the same shape."""
    out = []
    for r in rows or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'], 'l': p['l'],
                    'c': p['c'], 'v': p.get('v')})
    return sorted(out, key=lambda b: b['ts'])


def _newest(rows_by_key):
    best, best_ts = None, -1
    for key, rows in rows_by_key:
        ts = rows[-1][0] if rows and len(rows[-1]) else -1
        if ts and ts > best_ts:
            best, best_ts = key[11:], ts
    return best


def resolve_bar_key(db, c, alias):
    """Return the db key suffix holding bars for `alias`.

    Prefers an exact `bar_window:<alias>` hit (tests), else the newest
    `bar_window:<alias>@<iid>` key. Futures aliases (`MNQ.c.0`,
    `MGC.v.0`) additionally resolve to the newest dated-contract window
    for their root (`bar_window:MNQZ25@<iid>`), which is the form the
    Databento collectors actually write. Returns None when neither exists.
    """
    if db.get(c, 'bar_window:' + alias, None):
        return alias
    hit = _newest((key, db.get(c, key, []))
                  for key in db.prefix(c, 'bar_window:' + alias + '@'))
    if hit is not None:
        return hit
    root = future_root(alias)
    if root:
        dated = [(key, db.get(c, key, []))
                 for key in db.prefix(c, 'bar_window:' + root)
                 if (m := _DATED_WINDOW.match(key)) and m.group(1) == root]
        return _newest(dated)
    return None


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


def session_vwap(bars, now, min_bars=20):
    """CME Globex-day session VWAP over ascending bar dicts.

    Session = bars with ts >= the most recent 17:00 America/Chicago boundary.
    typical = (h+l+c)/3, weighted by volume when any volume is present,
    else equal-weighted (the "minute-close approximation" convention).
    Returns None when fewer than `min_bars` session bars are available.
    """
    if not bars:
        return None
    try:
        now_f = float(now)
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(now_f, CT)
    boundary = dt.replace(hour=17, minute=0, second=0, microsecond=0)
    if dt < boundary:
        boundary = boundary - timedelta(days=1)
    cutoff = boundary.timestamp()
    sess = [b for b in bars if b.get('ts') is not None and b['ts'] >= cutoff]
    try:
        min_bars = int(min_bars)
    except (TypeError, ValueError):
        min_bars = 20
    if len(sess) < min_bars:
        return None
    vols = [float(b.get('v') or 0) for b in sess]
    weights = vols if sum(vols) > 0 else [1.0] * len(sess)
    num = sum(((b['h'] + b['l'] + b['c']) / 3.0) * w
              for b, w in zip(sess, weights))
    den = sum(weights)
    return num / den if den > 0 else None


def detect_bos(bars, n=2, atr_period=14, body_frac=0.5, beyond_atr_frac=0.5):
    """Break-of-structure closes beyond confirmed fractal swings.

    Bullish BOS: close[i] > latest confirmed swing high (confirmed = swing
    idx <= i - n) with displacement = bar body >= body_frac * range OR the
    close clears the swing by >= beyond_atr_frac * ATR. Bearish mirror.
    Returns oldest-first event dicts.
    """
    if len(bars) < 2 * n + 3:
        return []
    a = atr(bars, atr_period)
    highs, lows = fractal_swings(bars, n)
    events = []
    for i in range(2 * n, len(bars)):
        b = bars[i]
        rng = b['h'] - b['l']
        body = abs(b['c'] - b['o'])
        sh = [(idx, p) for idx, p in highs if idx <= i - n]
        if sh:
            sidx, sp = max(sh)
            if b['c'] > sp:
                disp = (rng > 0 and body >= body_frac * rng) or \
                       (a is not None and (b['c'] - sp) >= beyond_atr_frac * a)
                if disp:
                    events.append({'direction': 'long', 'bos_idx': i,
                                   'bos_ts': b['ts'], 'swing_price': sp,
                                   'swing_idx': sidx, 'close': b['c'], 'atr': a})
                    continue
        sl = [(idx, p) for idx, p in lows if idx <= i - n]
        if sl:
            sidx, sp = max(sl)
            if b['c'] < sp:
                disp = (rng > 0 and body >= body_frac * rng) or \
                       (a is not None and (sp - b['c']) >= beyond_atr_frac * a)
                if disp:
                    events.append({'direction': 'short', 'bos_idx': i,
                                   'bos_ts': b['ts'], 'swing_price': sp,
                                   'swing_idx': sidx, 'close': b['c'], 'atr': a})
    return events


def detect_fvgs(bars):
    """Classic 3-candle fair value gaps over ascending bar dicts.

    Bullish FVG: low[i] > high[i-2], zone = (high[i-2], low[i]).
    Bearish FVG: high[i] < low[i-2], zone = (low[i], high[i-2]).
    Returns oldest-first dicts with direction/bottom/top/formed_idx/formed_ts.
    """
    out = []
    for i in range(2, len(bars)):
        first, last = bars[i - 2], bars[i]
        if last['l'] > first['h']:
            out.append({'direction': 'long', 'bottom': first['h'],
                        'top': last['l'], 'formed_idx': i,
                        'formed_ts': last['ts']})
        elif last['h'] < first['l']:
            out.append({'direction': 'short', 'bottom': last['h'],
                        'top': first['l'], 'formed_idx': i,
                        'formed_ts': last['ts']})
    return out


def fvg_mitigated(bars, fvg, up_to_idx=None):
    """True when a bar after formation wicked into the FVG zone.

    Bullish FVG mitigated when a later bar's low < zone top; bearish when a
    later bar's high > zone bottom. `up_to_idx` caps the scan (inclusive).
    """
    end = len(bars) if up_to_idx is None else min(up_to_idx + 1, len(bars))
    for k in range(fvg['formed_idx'] + 1, end):
        b = bars[k]
        if fvg['direction'] == 'long':
            if b['l'] < fvg['top']:
                return True
        else:
            if b['h'] > fvg['bottom']:
                return True
    return False
