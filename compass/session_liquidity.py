"""ICT session liquidity map: Asian/London/NY session highs/lows per futures symbol.

Evidence only. This module never alerts and never trades. It computes, for each
configured ICT symbol and each session kind (CT wall-clock), the high/low of
the last *complete* session window and marks swept highs/lows. The snapshot is
the canonical level source consumed by turtle_soup and other detectors via
`db.get(c, 'ict_session_liquidity:latest')` (defensive: `.get('levels') or []`).
"""
from datetime import datetime, timedelta

from .market import number, CT
from .ict_common import ict_bars, resolve_bar_key

# Session windows as ((start_h, start_m), (end_h, end_m)) in CT wall-clock.
# Asia wraps midnight, so its end hour is expressed past 24.
SESSION_DEFS = {
    'asia': ((18, 0), (25, 0)),   # 18:00 -> 01:00 next day
    'london': ((1, 0), (7, 0)),
    'ny_am': ((7, 0), (11, 30)),
    'ny_pm': ((12, 30), (15, 0)),
}

# Per-instrument tick sizes (points per tick) keyed by symbol root.
# Sweep distance = tick * ICT_SWEEP_TICKS_MULT (default 2).
INSTRUMENT_TICKS = {
    'ES': 0.25, 'NQ': 0.25, 'YM': 0.25, 'RTY': 0.1, 'MES': 0.25, 'MNQ': 0.25,
    'GC': 0.1, 'MGC': 0.1, 'SI': 0.005, 'SIL': 0.005, 'CL': 0.01, 'MCL': 0.01,
    'ZB': 0.03125, 'ZN': 0.015625, 'ZC': 0.25, 'ZS': 0.25, 'ZW': 0.25,
    'HE': 0.025, 'LE': 0.025, 'NG': 0.001,
}
DEFAULT_TICK = 0.01

SCAN_THROTTLE = 300      # seconds between scans (sessions move slowly)
BAR_LIMIT = 3000         # recent() depth for ~24h of intraday bars


def _bars_from_window(window):
    """Normalize bar_window rows ([ts,o,h,l,c,v,...]) to ascending dicts."""
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


def _to_secs(hm):
    """(hour, minute) or hour-float -> seconds from the session-start date."""
    if isinstance(hm, (int, float)):
        return float(hm) * 3600.0
    h, m = hm
    return float(h) * 3600.0 + float(m) * 60.0


def _normalize_defs(raw):
    """Session defs -> {kind: (start_secs, end_secs)}, defensive on bad input."""
    defs = {}
    for kind, pair in (raw or {}).items():
        try:
            start, end = pair
            s, e = _to_secs(start), _to_secs(end)
            if e > s:
                defs[str(kind)] = (s, e)
        except (TypeError, ValueError):
            continue
    return defs or {k: (_to_secs(s), _to_secs(e)) for k, (s, e) in SESSION_DEFS.items()}


def _midnight_ts(d):
    return datetime(d.year, d.month, d.day, tzinfo=CT).timestamp()


def last_complete_window(kind, now, defs=None):
    """Return (start_ts, end_ts) of the last completed window for `kind`.

    A session is complete once `now` passes its end. Returns None when no
    completed window is found in the lookback.
    """
    defs = _normalize_defs(defs)
    if kind not in defs:
        return None
    start_secs, end_secs = defs[kind]
    duration = end_secs - start_secs
    now_ct = datetime.fromtimestamp(now, CT)
    for back in range(0, 6):
        d0 = (now_ct - timedelta(days=back)).date()
        start_ts = _midnight_ts(d0) + start_secs
        end_ts = start_ts + duration
        if end_ts <= now:
            return start_ts, end_ts
    return None


def sweep_ticks_for(symbol, cfg=None):
    """Points a bar must wick beyond an extreme to count as a sweep."""
    override = number(getattr(cfg, 'ict_sweep_ticks', None)) if cfg is not None else None
    if override and override > 0:
        return override
    mult = number(getattr(cfg, 'ict_sweep_ticks_mult', 2.0)) if cfg is not None else 2.0
    if not mult or mult <= 0:
        mult = 2.0
    root = str(symbol).split('.')[0].upper()
    tick = INSTRUMENT_TICKS.get(root, DEFAULT_TICK)
    return tick * mult


def _merge_bars(window_rows, recent_rows):
    """Union of window and recent bars, deduped by ts, ascending."""
    seen = {}
    for b in _bars_from_window(window_rows) + _bars_from_recent(recent_rows):
        ts = b['ts']
        if ts not in seen:
            seen[ts] = b
    return sorted(seen.values(), key=lambda b: b['ts'])


def classify_session(symbol, kind, bars, start_ts, end_ts, sweep_ticks, now):
    """Build one session liquidity row, or {'excluded': reason}.

    swept_high is True when a later bar (ts > the high's bar) wicked above
    high + sweep_ticks and closed back at/below the high; mirrored for lows.
    """
    wb = [b for b in bars if start_ts <= b['ts'] <= end_ts]
    if not wb:
        return {'symbol': symbol, 'session': kind, 'excluded': 'no_bars'}
    hi_bar = max(wb, key=lambda b: (b['h'], -b['ts']))
    lo_bar = min(wb, key=lambda b: (b['l'], b['ts']))
    high, low = hi_bar['h'], lo_bar['l']
    after_hi = [b for b in bars if b['ts'] > hi_bar['ts']]
    after_lo = [b for b in bars if b['ts'] > lo_bar['ts']]
    swept_high = any(b['h'] >= high + sweep_ticks and b['c'] <= high
                     for b in after_hi)
    swept_low = any(b['l'] <= low - sweep_ticks and b['c'] >= low
                    for b in after_lo)
    sweep_high_ts = next((b['ts'] for b in after_hi
                          if b['h'] >= high + sweep_ticks and b['c'] <= high), None)
    sweep_low_ts = next((b['ts'] for b in after_lo
                         if b['l'] <= low - sweep_ticks and b['c'] >= low), None)
    return {
        'symbol': symbol, 'session': kind,
        'session_start': start_ts, 'session_end': end_ts,
        'session_date': datetime.fromtimestamp(start_ts, CT).date().isoformat(),
        'high': number(high), 'low': number(low),
        'high_ts': hi_bar['ts'], 'low_ts': lo_bar['ts'],
        'swept_high': bool(swept_high), 'swept_low': bool(swept_low),
        'sweep_high_ts': sweep_high_ts, 'sweep_low_ts': sweep_low_ts,
        'sweep_ticks': number(sweep_ticks),
        'complete': True,
        'at': now,
    }


def scan(db, c, cfg, now):
    """Throttled session-liquidity scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_session_liquidity', False):
        return {'ran': False, 'reason': 'disabled'}
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    if now - db.get(c, 'ict_session_liquidity:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    raw_defs = getattr(cfg, 'ict_session_defs', None)
    defs = _normalize_defs(raw_defs)
    levels, excluded, sweep_signals = [], {}, 0
    for symbol in symbols:
        _key = resolve_bar_key(db, c, symbol)
        bars = _merge_bars(db.get(c, 'bar_window:' + _key, []) if _key else [],
                           db.recent(c, 'bar', symbol, limit=BAR_LIMIT))
        if not bars:
            excluded[symbol] = 'no_bars'
            continue
        ticks = sweep_ticks_for(symbol, cfg)
        for kind in defs:
            window = last_complete_window(kind, now, raw_defs)
            if not window:
                excluded['%s:%s' % (symbol, kind)] = 'no_complete_window'
                continue
            row = classify_session(symbol, kind, bars, window[0], window[1],
                                   ticks, now)
            if 'excluded' in row:
                excluded['%s:%s' % (symbol, kind)] = row['excluded']
                continue
            levels.append(row)
            # Newly detected sweeps are idempotent signal events (evidence trail).
            for side in ('high', 'low'):
                if not row['swept_' + side]:
                    continue
                key = 'icts:%s:%s:%s:%s' % (symbol, kind, side, row['session_date'])
                if db.get(c, 'ict_session_liquidity:swept:' + key):
                    continue
                if db.append(c, 'ict_session_liquidity_signal', 'ict_futures',
                             symbol, row['sweep_' + side + '_ts'] or now,
                             {'session': kind, 'session_date': row['session_date'],
                              'side': side, 'level': row[side],
                              'sweep_ticks': row['sweep_ticks']}, key=key):
                    db.put(c, 'ict_session_liquidity:swept:' + key, now)
                    sweep_signals += 1
    if not levels:
        return {'ran': False, 'reason': 'no_bars', 'excluded': excluded}
    db.put(c, 'ict_session_liquidity:latest',
           {'at': now, 'levels': levels, 'excluded': excluded,
            'symbols': symbols, 'sweep_signals': sweep_signals,
            'status': 'running' if levels else 'idle'})
    db.put(c, 'ict_session_liquidity:scanned_at', now)
    return {'ran': True, 'symbols': len(symbols), 'rows': len(levels),
            'excluded': excluded, 'sweep_signals': sweep_signals}


def display(db, c, now, limit=200):
    """Dashboard payload: flat level list plus a per-symbol map."""
    latest = db.get(c, 'ict_session_liquidity:latest', {})
    levels = latest.get('levels') or []
    by_symbol = {}
    for r in levels:
        by_symbol.setdefault(r.get('symbol'), []).append(r)
    signals = db.recent(c, 'ict_session_liquidity_signal', limit=50)
    return {
        'asof': latest.get('at'),
        'symbols': latest.get('symbols', []),
        'levels': levels[:limit],
        'map': by_symbol,
        'excluded': latest.get('excluded', {}),
        'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']}
                    for s in signals],
        'note': 'Research evidence only. No auto-admission, no alerts, no trades.',
    }
