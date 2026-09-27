"""Apex Magnet scanner: vendor magnet levels joined with our price action.

Evidence only. This module never auto-admits to the live scanner, never
alerts, and never trades. Rows are research candidates; signal outcomes are
measured so a future promotion rule can be decided from data, not marketing.
"""
import time

from .market import number, day, session
from .vendor_freshness import confirmation, context_check

RADIUS_DEFAULT = 0.02      # 2% default scan radius
TOLERANCE_DEFAULT = 0.001  # 0.10% touch/break tolerance band
APPROACH_BARS = 5          # bars for the "approaching" momentum check
SCAN_THROTTLE = 120        # seconds between scans
APEX_SOURCE_LIMIT = 600
APEX_POLL_LIMIT = 600


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


def select_magnet(apex):
    """Pick the dominant magnet (highest vendor score) and the gamma flip.

    Returns (magnet, flip) where magnet is the level dict or None, or
    (None, reason-string) when unusable.
    """
    levels = (apex or {}).get('levels') or []
    apex_levels = [r for r in levels if r.get('kind') == 'apex'
                   and number(r.get('price')) and number(r.get('score')) is not None]
    if not apex_levels:
        return None, 'no_apex_levels'
    magnet = max(apex_levels, key=lambda r: r['score'])
    flip = None
    for r in levels:
        if r.get('kind') == 'gamma_flip' and number(r.get('price')):
            flip = number(r['price'])
            break
    return {'price': number(magnet['price']), 'score': number(magnet['score']),
            'net_gex': number(magnet.get('net_gex')), 'oi': number(magnet.get('oi'))}, flip


def detect_signal(bars, magnet, sess_open, tolerance=TOLERANCE_DEFAULT):
    """Classify price action at the magnet for one session.

    bars: ascending [{ts,o,h,l,c}]. Returns (signal, tests) where signal is
    'broke_through' | 'tested_holding' | 'approaching' | None.
    """
    sbars = [b for b in bars if b['ts'] >= sess_open]
    if not sbars:
        return None, 0
    lo, hi = magnet * (1 - tolerance), magnet * (1 + tolerance)
    first = sbars[0]['c']
    origin = 'above' if first > hi else 'below' if first < lo else 'at'
    tests = 0
    broke = False
    for b in sbars:
        if b['l'] <= hi and b['h'] >= lo:
            tests += 1
        c = b['c']
        if origin == 'below' and c > hi:
            broke = True
        elif origin == 'above' and c < lo:
            broke = True
    if broke:
        return 'broke_through', tests
    if tests:
        return 'tested_holding', tests
    if len(sbars) > APPROACH_BARS:
        before = abs(sbars[-1 - APPROACH_BARS]['c'] - magnet)
        after = abs(sbars[-1]['c'] - magnet)
        if after < before:
            return 'approaching', 0
    return None, 0


def _sector_for(symbol, sector_map):
    """Defensive lookup; the sector map payload shape is vendor-defined."""
    if not isinstance(sector_map, dict):
        return None
    direct = sector_map.get(symbol)
    if isinstance(direct, str):
        return direct
    if isinstance(direct, dict) and isinstance(direct.get('sector'), str):
        return direct['sector']
    items = sector_map.get('items')
    if isinstance(items, list):
        for row in items:
            if not isinstance(row, dict):
                continue
            data = row.get('data') if isinstance(row.get('data'), dict) else row
            if data.get('symbol') == symbol and isinstance(data.get('sector'), str):
                return data['sector']
    return None


def classify_row(symbol, apex, bars, daily_closes, sector, now,
                 radius=RADIUS_DEFAULT, tolerance=TOLERANCE_DEFAULT):
    """Build one magnet row, or {'excluded': reason}."""
    eligible = (context_check(apex, now)['eligible_for_context']
                if apex.get('cache_policy')
                else confirmation(apex, now, APEX_SOURCE_LIMIT, APEX_POLL_LIMIT)['eligible_for_live_confirmation'])
    if not eligible:
        return {'excluded': 'apex_not_fresh'}
    magnet, flip = select_magnet(apex)
    if magnet is None:
        return {'excluded': flip}  # flip carries the reason string here
    bounds = session(day(now))
    if not bounds:
        return {'excluded': 'no_session'}
    sess_open = bounds[0]
    spot = number(apex.get('spot')) or (bars[-1]['c'] if bars else None)
    if not spot or spot <= 0:
        return {'excluded': 'no_spot'}
    distance = abs(spot - magnet['price']) / spot
    if distance > radius:
        return {'excluded': 'outside_radius'}
    signal, tests = detect_signal(bars, magnet['price'], sess_open, tolerance)
    sma20 = sum(daily_closes[-20:]) / 20 if len(daily_closes) >= 20 else None
    day_pct = ((daily_closes[-1] / daily_closes[-2]) - 1
               if len(daily_closes) >= 2 and daily_closes[-2] else None)
    return {
        'symbol': symbol, 'spot': spot, 'spot_ts': apex.get('source_ts'),
        'day_pct': day_pct, 'sma20': sma20,
        'sma20_side': ('above' if spot > sma20 else 'below') if sma20 else None,
        'magnet': magnet['price'], 'magnet_score': magnet['score'],
        'magnet_basis': 'current',
        'distance_pct': distance, 'role': 'resistance' if magnet['price'] > spot else 'support',
        'vs_flip': ('above' if spot > flip else 'below') if flip else None,
        'gamma_flip': flip, 'signal': signal, 'tests': tests,
        'sector': sector, 'apex_source_ts': apex.get('source_ts'),
        'apex_age_s': round(now - apex['source_ts'], 1) if apex.get('source_ts') else None,
        'at': now,
    }


def _outcome(prior_bars, magnet, tolerance):
    """Session outcome for a prior-session signal: pin / break / reject / no_test."""
    lo, hi = magnet * (1 - tolerance), magnet * (1 + tolerance)
    if not prior_bars:
        return {'outcome': 'unknown', 'reason': 'no_bars'}
    tests = sum(1 for b in prior_bars if b['l'] <= hi and b['h'] >= lo)
    first = prior_bars[0]['c']
    origin = 'above' if first > hi else 'below' if first < lo else 'at'
    broke = any((origin == 'below' and b['c'] > hi) or (origin == 'above' and b['c'] < lo)
                for b in prior_bars)
    last = prior_bars[-1]['c']
    if broke:
        outcome = 'break'
    elif lo <= last <= hi:
        outcome = 'pin'
    elif tests:
        outcome = 'reject'
    else:
        outcome = 'no_test'
    return {'outcome': outcome, 'tests': tests, 'session_close': last,
            'origin': origin}


def _forward_marks(prior_bars, signal_ts, spot):
    """15/30/60-minute marks after the signal, where bars exist."""
    marks = {}
    for label, offset in (('m15', 900), ('m30', 1800), ('m60', 3600)):
        target = signal_ts + offset
        nxt = next((b for b in prior_bars if b['ts'] >= target), None)
        marks[label] = ({'price': nxt['c'],
                         'pct': (nxt['c'] / spot) - 1 if spot else None}
                        if nxt else None)
    return marks


def scan(db, c, cfg, now):
    """Throttled magnet scan over symbols with fresh apex data."""
    if not getattr(cfg, 'apex_magnet', True):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'apex_magnet:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    radius = number(getattr(cfg, 'apex_magnet_radius', RADIUS_DEFAULT)) or RADIUS_DEFAULT
    tolerance = number(getattr(cfg, 'apex_magnet_tolerance', TOLERANCE_DEFAULT)) or TOLERANCE_DEFAULT
    sector_map = db.get(c, 'research:extra_sector_map', {})
    rows, excluded = {}, {}
    today = day(now)
    for key, apex in db.prefix(c, 'apex:').items():
        symbol = key[5:]
        if '@' in symbol:
            continue
        received = apex.get('received', 0) or 0
        if received <= db.get(c, 'apex_magnet:cursor:' + symbol, 0):
            continue
        window = _bars_from_window(db.get(c, 'bar_window:' + symbol, []))
        bars = window or _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))
        daily_rows = db.recent(c, 'daily', symbol, limit=25)
        daily_closes = [r['payload']['c'] for r in reversed(daily_rows)
                        if (r.get('payload') or {}).get('c')]
        row = classify_row(symbol, apex, bars, daily_closes,
                           _sector_for(symbol, sector_map), now, radius, tolerance)
        if 'excluded' in row:
            excluded[row['excluded']] = excluded.get(row['excluded'], 0) + 1
            continue
        rows[symbol] = row
        db.put(c, 'apex_magnet:cursor:' + symbol, received)
        # Signal transitions are idempotent per symbol/session/signal.
        prev = db.get(c, 'apex_magnet:signal:' + symbol, {})
        if row['signal'] and row['signal'] != prev.get('signal'):
            db.append(c, 'apex_magnet_signal', 'apex_magnet', symbol, now,
                      {'session': today, 'signal': row['signal'], 'magnet': row['magnet'],
                       'spot': row['spot'], 'distance_pct': row['distance_pct'],
                       'role': row['role'], 'vs_flip': row['vs_flip'],
                       'previous_signal': prev.get('signal')},
                      key='apexmag:%s:%s:%s' % (symbol, today, row['signal']))
            db.put(c, 'apex_magnet:signal:' + symbol,
                   {'signal': row['signal'], 'at': now, 'magnet': row['magnet'],
                    'spot': row['spot'], 'session': today})
        # Session rollover: close out the prior session's measurement.
        if prev.get('session') and prev['session'] != today and prev.get('signal'):
            prior_bounds = session(prev['session'])
            if prior_bounds:
                prior_bars = _bars_from_recent(
                    db.recent(c, 'bar', symbol, limit=2000, since=prior_bounds[0]))
                result = _outcome(prior_bars, prev['magnet'], tolerance)
                result['marks'] = _forward_marks(prior_bars, prev.get('at', 0),
                                                 prev.get('spot'))
                db.append(c, 'apex_magnet_outcome', 'apex_magnet', symbol, now,
                          {'session': prev['session'], 'signal': prev['signal'],
                           'magnet': prev['magnet'], **result},
                          key='apexmag:%s:%s:outcome' % (symbol, prev['session']))
    db.put(c, 'apex_magnet:latest', {'at': now, 'rows': rows, 'excluded': excluded,
                                    'radius': radius, 'tolerance': tolerance,
                                    'status': 'running' if rows or not excluded else 'idle'})
    db.put(c, 'apex_magnet:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent signals and outcomes."""
    latest = db.get(c, 'apex_magnet:latest', {})
    rows = sorted((latest.get('rows') or {}).values(),
                  key=lambda r: r['distance_pct'])[:limit]
    signals = db.recent(c, 'apex_magnet_signal', limit=50)
    outcomes = db.recent(c, 'apex_magnet_outcome', limit=50)
    return {'asof': latest.get('at'), 'radius': latest.get('radius'),
            'tolerance': latest.get('tolerance'), 'excluded': latest.get('excluded', {}),
            'rows': rows,
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']} for s in signals],
            'outcomes': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']} for s in outcomes],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
