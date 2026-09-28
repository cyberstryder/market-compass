"""Golden Zone + VWAP: mean-reversion fade of impulsive fractal legs.

An impulsive leg = directional move >= ICT_GZ_LEG_ATR_MULT (1.5) x ATR(14)
from a fractal swing origin to a fractal swing extreme. The Fibonacci golden
zone (61.8%-78.6% retracement of the leg) overlapping session VWAP (within
0.25x ATR) is the POI. Signal fires when price retraces into the zone:
direction is counter to the leg (mean reversion toward the leg origin).

CONVENTION DEVIATION (documented): the brief says "stop beyond 78.6% by
0.25 ATR". For a counter-leg entry the 78.6% level sits on the *favorable*
side of the trade (a deeper retracement helps a fade targeting the origin),
so the stop is placed beyond the adverse (61.8%) zone edge instead, plus
0.25x ATR buffer. The continuation variant (bos_gz_vwap) uses the literal
beyond-78.6% stop, where it is the adverse edge.

Evidence only. No alerts. Paper fills via ict_paper only when both
ICT_FUTURES_PAPER_ENABLED and the detector flag are on.
"""
from .market import number, day, CT  # noqa: F401  (CT kept for session-math convention)
from .ict_common import ict_bars, atr, fractal_swings, session_vwap
from .ict_paper import submit as _paper_submit

SCAN_THROTTLE = 120
ATR_PERIOD = 14
FIB_618 = 0.618
FIB_786 = 0.786
FRACTAL_N_DEFAULT = 2
LEG_ATR_MULT_DEFAULT = 1.5
VWAP_TOL_ATR_FRAC_DEFAULT = 0.25
STOP_ATR_FRAC_DEFAULT = 0.25
MIN_RR_DEFAULT = 1.5
VWAP_MIN_BARS_DEFAULT = 20


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


def detect_legs(bars, n=FRACTAL_N_DEFAULT, leg_atr_mult=LEG_ATR_MULT_DEFAULT,
                atr_period=ATR_PERIOD):
    """Impulsive fractal legs, oldest-first.

    Consecutive opposite swings (low->high = bullish leg, high->low = bearish);
    a leg qualifies when |extreme - origin| >= leg_atr_mult * ATR.
    """
    if len(bars) < 2 * n + 3:
        return []
    a = atr(bars, atr_period)
    if not a or a <= 0:
        return []
    leg_atr_mult = number(leg_atr_mult) or LEG_ATR_MULT_DEFAULT
    highs, lows = fractal_swings(bars, n)
    seq = sorted([(i, p, 'high') for i, p in highs] +
                 [(i, p, 'low') for i, p in lows])
    legs = []
    for (i0, p0, k0), (i1, p1, k1) in zip(seq, seq[1:]):
        if k0 == k1:
            continue
        rng = abs(p1 - p0)
        if rng >= leg_atr_mult * a:
            legs.append({'direction': 'bullish' if k0 == 'low' else 'bearish',
                         'origin': p0, 'extreme': p1,
                         'origin_idx': i0, 'extreme_idx': i1, 'range': rng,
                         'origin_ts': bars[i0]['ts'], 'extreme_ts': bars[i1]['ts'],
                         'atr': a})
    return legs


def golden_zone(leg):
    """(zone_lo, zone_hi): 78.6%..61.8% retracement band of the leg."""
    o, e = number(leg['origin']), number(leg['extreme'])
    r = abs(e - o)
    if leg['direction'] == 'bullish':
        return (e - FIB_786 * r, e - FIB_618 * r)
    return (o + FIB_618 * r, o + FIB_786 * r)


def vwap_confluence_ok(zone, vwap, atr_v,
                       tol_atr_frac=VWAP_TOL_ATR_FRAC_DEFAULT):
    """True when session VWAP overlaps the zone within tol x ATR."""
    vwap = number(vwap)
    if vwap is None or not atr_v or atr_v <= 0:
        return False
    zlo, zhi = zone
    dist = 0.0 if zlo <= vwap <= zhi else min(abs(vwap - zlo), abs(vwap - zhi))
    tol_atr_frac = number(tol_atr_frac)
    if tol_atr_frac is None:
        tol_atr_frac = VWAP_TOL_ATR_FRAC_DEFAULT
    return dist <= tol_atr_frac * atr_v


def detect_signal(bars, leg, vwap, atr_v,
                  vwap_tol_atr_frac=VWAP_TOL_ATR_FRAC_DEFAULT,
                  stop_atr_frac=STOP_ATR_FRAC_DEFAULT,
                  min_rr=MIN_RR_DEFAULT):
    """Most recent retrace-into-zone after the leg extreme, counter-leg direction.

    Returns the signal dict, or None when VWAP confluence fails, no bar
    retraces into the zone, or risk is non-positive. When the leg origin
    yields rr < min_rr the target is extended to exactly min_rr (per the
    "leg origin or 1.5R" brief).
    """
    if not atr_v or atr_v <= 0 or number(vwap) is None:
        return None
    zone = golden_zone(leg)
    if not vwap_confluence_ok(zone, vwap, atr_v, vwap_tol_atr_frac):
        return None
    zlo, zhi = zone
    sig_idx = None
    for j in range(leg['extreme_idx'] + 1, len(bars)):
        cj = bars[j]['c']
        if cj is not None and zlo <= cj <= zhi:
            sig_idx = j  # keep the most recent retrace
    if sig_idx is None:
        return None
    entry = bars[sig_idx]['c']
    stop_atr_frac = number(stop_atr_frac)
    if stop_atr_frac is None:
        stop_atr_frac = STOP_ATR_FRAC_DEFAULT
    buf = stop_atr_frac * atr_v
    min_rr = number(min_rr)
    if min_rr is None:
        min_rr = MIN_RR_DEFAULT
    if leg['direction'] == 'bullish':
        direction = 'short'
        stop = zhi + buf  # adverse (61.8%) edge; see module docstring
        risk = stop - entry
        target, target_kind = leg['origin'], 'leg_origin'
        reward = entry - target
    else:
        direction = 'long'
        stop = zlo - buf
        risk = entry - stop
        target, target_kind = leg['origin'], 'leg_origin'
        reward = target - entry
    if risk <= 0:
        return None
    rr = reward / risk
    if rr < min_rr:
        if direction == 'short':
            target = entry - min_rr * risk
        else:
            target = entry + min_rr * risk
        target_kind = 'min_rr_fallback'
        reward = min_rr * risk
        rr = min_rr
    zmid = (zlo + zhi) / 2.0
    return {'direction': direction, 'signal_ts': bars[sig_idx]['ts'],
            'level_price': zmid, 'level_kind': 'golden_zone',
            'leg_direction': leg['direction'], 'leg_origin': leg['origin'],
            'leg_extreme': leg['extreme'], 'leg_range': leg['range'],
            'leg_origin_ts': leg['origin_ts'], 'leg_extreme_ts': leg['extreme_ts'],
            'zone_lo': zlo, 'zone_hi': zhi, 'vwap': number(vwap),
            'entry': entry, 'stop': stop, 'target': target,
            'target_kind': target_kind,
            'risk_pts': risk, 'reward_pts': reward, 'rr': rr,
            'stop_buf_atr_frac': stop_atr_frac, 'atr': atr_v}


def _exclusion_reason(bars, leg, vwap, atr_v, tol_atr_frac):
    if leg is None:
        return 'no_legs'
    if number(vwap) is None:
        return 'no_vwap'
    if not vwap_confluence_ok(golden_zone(leg), vwap, atr_v, tol_atr_frac):
        return 'no_confluence'
    return 'no_retrace'


def scan(db, c, cfg, now):
    """Throttled golden-zone scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_golden_zone', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_golden_zone:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = getattr(cfg, 'ict_symbols', ()) or ()
    try:
        n = int(number(getattr(cfg, 'ict_gz_fractal_n', FRACTAL_N_DEFAULT))
                or FRACTAL_N_DEFAULT)
    except (TypeError, ValueError):
        n = FRACTAL_N_DEFAULT
    leg_mult = number(getattr(cfg, 'ict_gz_leg_atr_mult', LEG_ATR_MULT_DEFAULT))
    if leg_mult is None:
        leg_mult = LEG_ATR_MULT_DEFAULT
    tol_frac = number(getattr(cfg, 'ict_gz_vwap_tol_atr_frac',
                              VWAP_TOL_ATR_FRAC_DEFAULT))
    if tol_frac is None:
        tol_frac = VWAP_TOL_ATR_FRAC_DEFAULT
    stop_frac = number(getattr(cfg, 'ict_gz_stop_atr_frac', STOP_ATR_FRAC_DEFAULT))
    if stop_frac is None:
        stop_frac = STOP_ATR_FRAC_DEFAULT
    min_rr = number(getattr(cfg, 'ict_gz_min_rr', MIN_RR_DEFAULT))
    if min_rr is None:
        min_rr = MIN_RR_DEFAULT
    try:
        vwap_min = int(number(getattr(cfg, 'ict_gz_vwap_min_bars',
                                      VWAP_MIN_BARS_DEFAULT))
                       or VWAP_MIN_BARS_DEFAULT)
    except (TypeError, ValueError):
        vwap_min = VWAP_MIN_BARS_DEFAULT
    params = {'fractal_n': n, 'leg_atr_mult': leg_mult,
              'vwap_tol_atr_frac': tol_frac, 'stop_atr_frac': stop_frac,
              'min_rr': min_rr, 'vwap_min_bars': vwap_min}
    rows, excluded = {}, {}
    today = day(now)
    n_signals = 0
    for symbol in symbols:
        window = ict_bars(db, c, symbol)
        bars = window or _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))
        if not bars:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_bars', 'at': now}
            continue
        atr_v = atr(bars, ATR_PERIOD)
        legs = detect_legs(bars, n=n, leg_atr_mult=leg_mult)
        leg = legs[-1] if legs else None
        vwap = session_vwap(bars, now, min_bars=vwap_min)
        sig = (detect_signal(bars, leg, vwap, atr_v,
                             vwap_tol_atr_frac=tol_frac,
                             stop_atr_frac=stop_frac, min_rr=min_rr)
               if leg else None)
        if sig is None:
            reason = _exclusion_reason(bars, leg, vwap, atr_v, tol_frac)
            excluded[reason] = excluded.get(reason, 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': reason, 'at': now,
                            'n_legs': len(legs),
                            'vwap': number(vwap)}
            continue
        row = {'symbol': symbol, 'concept': 'golden_zone', 'at': now,
               'params': dict(params), **sig}
        rows[symbol] = row
        state = db.get(c, 'ict_golden_zone:signal:' + symbol, {}) or {}
        if state.get('date') != today or state.get('signal_ts') != sig['signal_ts']:
            payload = {k: v for k, v in row.items() if k != 'at'}
            db.append(c, 'ict_golden_zone_signal', 'ict_futures', symbol, now,
                      payload, key='ictgz:%s:%s' % (symbol, today))
            db.put(c, 'ict_golden_zone:signal:' + symbol,
                   {'signal_ts': sig['signal_ts'], 'date': today,
                    'direction': sig['direction'], 'at': now})
            n_signals += 1
        _paper_submit(db, c, cfg, now, 'golden_zone', symbol, sig)
    db.put(c, 'ict_golden_zone:latest',
           {'at': now, 'rows': rows, 'excluded': excluded, 'params': params,
            'status': 'running' if rows else 'idle'})
    db.put(c, 'ict_golden_zone:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent golden-zone signals."""
    latest = db.get(c, 'ict_golden_zone:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'ict_golden_zone_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts; '
                    'simulated paper fills only when ICT_FUTURES_PAPER_ENABLED '
                    'is on. No live orders.'}
