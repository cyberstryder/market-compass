"""BOS + Golden Zone + VWAP: break of structure supplies the trend direction,
then a retrace into the displacement leg's Golden Zone (61.8%-78.6%)
overlapping session VWAP supplies the continuation entry.

BOS = close beyond the latest confirmed fractal swing with displacement
(bar body >= 50% of its range, or the close clears the swing by >= 0.5x
ATR(14)) -- the same definition as ict_bos_fvg. The displacement leg runs
from the pre-break fractal swing (the impulse origin) to the post-break
extreme. VWAP overlap = session VWAP within 0.25 ATR of the zone.
Signal fires on the first close back inside the zone after the break;
direction = BOS direction. Stop sits beyond the 78.6% edge by 0.25 ATR;
target is opposing structure when it gives >= 1.5R, else exactly 2R.

Evidence only. No alerts. Paper fills via ict_paper only when both
ICT_FUTURES_PAPER_ENABLED and ICT_BOS_GZ_VWAP_ENABLED are on.
"""
from .market import number, day, CT  # noqa: F401  (CT kept for session-math convention)
from .ict_common import (ict_bars, atr, fractal_swings, detect_bos,
                         session_vwap)
from .ict_paper import submit as _paper_submit

SCAN_THROTTLE = 120
ATR_PERIOD = 14
FRACTAL_N_DEFAULT = 2
BODY_FRAC_DEFAULT = 0.5
BEYOND_ATR_FRAC_DEFAULT = 0.5
FIB_618 = 0.618
FIB_786 = 0.786
VWAP_TOL_ATR_FRAC_DEFAULT = 0.25
STOP_ATR_FRAC_DEFAULT = 0.25
MIN_RR_DEFAULT = 1.5
VWAP_MIN_BARS_DEFAULT = 20


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
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c,
                    'v': row[5] if len(row) > 5 else None})
    return out


def _bars_from_recent(rows):
    """Normalize db.recent('bar') dicts to bar dicts."""
    out = []
    for r in rows or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'], 'l': p['l'],
                    'c': p['c'], 'v': p.get('v')})
    return sorted(out, key=lambda b: b['ts'])


def displacement_leg(bars, bos, n=FRACTAL_N_DEFAULT):
    """The BOS displacement leg: pre-break fractal swing -> post-break extreme.

    Long: origin = most recent fractal swing low at/before the BOS bar;
    extreme = highest high from the origin through the latest bar.
    Short mirrors. Returns dict or None when no origin swing exists.
    """
    highs, lows = fractal_swings(bars, n)
    bidx = bos['bos_idx']
    if bos['direction'] == 'long':
        cands = [(i, p) for i, p in lows if i <= bidx]
        if not cands:
            return None
        oidx, origin = max(cands)
        extreme = max(b['h'] for b in bars[oidx:])
        return {'direction': 'long', 'origin': origin, 'extreme': extreme,
                'origin_idx': oidx, 'range': extreme - origin}
    cands = [(i, p) for i, p in highs if i <= bidx]
    if not cands:
        return None
    oidx, origin = max(cands)
    extreme = min(b['l'] for b in bars[oidx:])
    return {'direction': 'short', 'origin': origin, 'extreme': extreme,
            'origin_idx': oidx, 'range': origin - extreme}


def golden_zone(leg):
    """(zone_lo, zone_hi): 78.6%..61.8% retracement band of the leg."""
    o, e, r = leg['origin'], leg['extreme'], leg['range']
    if leg['direction'] == 'long':
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


def _opposing_structure(bars, entry, direction, n=FRACTAL_N_DEFAULT):
    """Nearest confirmed fractal swing beyond entry in the trade direction."""
    highs, lows = fractal_swings(bars, n)
    if direction == 'long':
        cands = [p for _, p in highs if p > entry]
        return min(cands) if cands else None
    cands = [p for _, p in lows if p < entry]
    return max(cands) if cands else None


def detect_signal(bars, bos, vwap, atr_v, n=FRACTAL_N_DEFAULT,
                  vwap_tol_atr_frac=VWAP_TOL_ATR_FRAC_DEFAULT,
                  stop_atr_frac=STOP_ATR_FRAC_DEFAULT,
                  min_rr=MIN_RR_DEFAULT):
    """First close back inside the leg's golden zone after BOS, with VWAP overlap.

    Returns the signal dict, or None when the leg/VWAP/retrace/risk checks fail.
    """
    if not atr_v or atr_v <= 0 or number(vwap) is None:
        return None
    leg = displacement_leg(bars, bos, n)
    if leg is None or leg['range'] <= 0:
        return None
    zone = golden_zone(leg)
    if not vwap_confluence_ok(zone, vwap, atr_v, vwap_tol_atr_frac):
        return None
    zlo, zhi = zone
    sig_idx = None
    for j in range(bos['bos_idx'] + 1, len(bars)):
        cj = bars[j]['c']
        if cj is not None and zlo <= cj <= zhi:
            sig_idx = j
            break  # the first retrace after the break is the entry
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
    direction = bos['direction']
    if direction == 'long':
        stop = zlo - buf  # beyond the 78.6% edge
        risk = entry - stop
    else:
        stop = zhi + buf
        risk = stop - entry
    if risk <= 0:
        return None
    opp = _opposing_structure(bars, entry, direction, n)
    if direction == 'long':
        if opp is not None and opp - entry >= min_rr * risk:
            target, target_kind = opp, 'opposing_structure'
        else:
            target, target_kind = entry + 2 * risk, 'two_r'
        reward = target - entry
    else:
        if opp is not None and entry - opp >= min_rr * risk:
            target, target_kind = opp, 'opposing_structure'
        else:
            target, target_kind = entry - 2 * risk, 'two_r'
        reward = entry - target
    rr = reward / risk
    if rr < min_rr:
        return None
    return {'direction': direction, 'signal_ts': bars[sig_idx]['ts'],
            'bos_ts': bos['bos_ts'], 'bos_price': bos['swing_price'],
            'level_price': (zlo + zhi) / 2.0,
            'level_kind': 'bos_golden_zone_vwap',
            'leg_origin': leg['origin'], 'leg_extreme': leg['extreme'],
            'leg_range': leg['range'],
            'zone_lo': zlo, 'zone_hi': zhi, 'vwap': number(vwap),
            'entry': entry, 'stop': stop, 'target': target,
            'target_kind': target_kind,
            'risk_pts': risk, 'reward_pts': reward, 'rr': rr,
            'stop_buf_atr_frac': stop_atr_frac, 'atr': atr_v}


def _exclusion_reason(bars, bos, vwap, atr_v, tol_atr_frac, n):
    if bos is None:
        return 'no_bos'
    if number(vwap) is None:
        return 'no_vwap'
    leg = displacement_leg(bars, bos, n)
    if leg is None or leg['range'] <= 0:
        return 'no_leg'
    if not vwap_confluence_ok(golden_zone(leg), vwap, atr_v, tol_atr_frac):
        return 'no_confluence'
    return 'no_retrace'


def scan(db, c, cfg, now):
    """Throttled BOS+GZ/VWAP scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_bos_gz_vwap', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_bos_gz_vwap:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = getattr(cfg, 'ict_symbols', ()) or ()
    try:
        n = int(number(getattr(cfg, 'ict_bgv_fractal_n', FRACTAL_N_DEFAULT))
                or FRACTAL_N_DEFAULT)
    except (TypeError, ValueError):
        n = FRACTAL_N_DEFAULT
    body_frac = number(getattr(cfg, 'ict_bgv_body_frac', BODY_FRAC_DEFAULT))
    if body_frac is None:
        body_frac = BODY_FRAC_DEFAULT
    beyond_frac = number(getattr(cfg, 'ict_bgv_beyond_atr_frac',
                                 BEYOND_ATR_FRAC_DEFAULT))
    if beyond_frac is None:
        beyond_frac = BEYOND_ATR_FRAC_DEFAULT
    tol_frac = number(getattr(cfg, 'ict_bgv_vwap_tol_atr_frac',
                              VWAP_TOL_ATR_FRAC_DEFAULT))
    if tol_frac is None:
        tol_frac = VWAP_TOL_ATR_FRAC_DEFAULT
    stop_frac = number(getattr(cfg, 'ict_bgv_stop_atr_frac',
                               STOP_ATR_FRAC_DEFAULT))
    if stop_frac is None:
        stop_frac = STOP_ATR_FRAC_DEFAULT
    min_rr = number(getattr(cfg, 'ict_bgv_min_rr', MIN_RR_DEFAULT))
    if min_rr is None:
        min_rr = MIN_RR_DEFAULT
    try:
        vwap_min = int(number(getattr(cfg, 'ict_bgv_vwap_min_bars',
                                      VWAP_MIN_BARS_DEFAULT))
                       or VWAP_MIN_BARS_DEFAULT)
    except (TypeError, ValueError):
        vwap_min = VWAP_MIN_BARS_DEFAULT
    params = {'fractal_n': n, 'body_frac': body_frac,
              'beyond_atr_frac': beyond_frac,
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
        events = detect_bos(bars, n=n, atr_period=ATR_PERIOD,
                            body_frac=body_frac, beyond_atr_frac=beyond_frac)
        bos = events[-1] if events else None
        vwap = session_vwap(bars, now, min_bars=vwap_min)
        sig = (detect_signal(bars, bos, vwap, atr_v, n=n,
                             vwap_tol_atr_frac=tol_frac,
                             stop_atr_frac=stop_frac, min_rr=min_rr)
               if bos else None)
        if sig is None:
            reason = _exclusion_reason(bars, bos, vwap, atr_v, tol_frac, n)
            excluded[reason] = excluded.get(reason, 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': reason, 'at': now,
                            'bos_direction': bos['direction'] if bos else None,
                            'bos_ts': bos['bos_ts'] if bos else None}
            continue
        row = {'symbol': symbol, 'concept': 'bos_gz_vwap', 'at': now,
               'params': dict(params), **sig}
        rows[symbol] = row
        state = db.get(c, 'ict_bos_gz_vwap:signal:' + symbol, {}) or {}
        if state.get('date') != today or state.get('signal_ts') != sig['signal_ts']:
            payload = {k: v for k, v in row.items() if k != 'at'}
            db.append(c, 'ict_bos_gz_vwap_signal', 'ict_futures', symbol, now,
                      payload, key='ictbgv:%s:%s' % (symbol, today))
            db.put(c, 'ict_bos_gz_vwap:signal:' + symbol,
                   {'signal_ts': sig['signal_ts'], 'date': today,
                    'direction': sig['direction'], 'at': now})
            n_signals += 1
        _paper_submit(db, c, cfg, now, 'bos_gz_vwap', symbol, sig)
    db.put(c, 'ict_bos_gz_vwap:latest',
           {'at': now, 'rows': rows, 'excluded': excluded, 'params': params,
            'status': 'running' if rows else 'idle'})
    db.put(c, 'ict_bos_gz_vwap:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent BOS+GZ/VWAP signals."""
    latest = db.get(c, 'ict_bos_gz_vwap:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'ict_bos_gz_vwap_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts; '
                    'simulated paper fills only when ICT_FUTURES_PAPER_ENABLED '
                    'is on. No live orders.'}
