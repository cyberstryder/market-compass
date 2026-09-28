"""ICT displacement continuation: break of a level + displacement, retest entry.

Evidence only. This module never alerts, never trades, and never admits
anything to the live scanner. Levels come from the session-liquidity and
HTF-level snapshots (defensive: absent -> excluded). A signal needs a close
beyond a level on a displacement bar; entry is the first retest touch of the
broken level within a few bars, else the displacement bar's close.
"""
import time

from .market import number, day
from .ict_common import ict_bars

SCAN_THROTTLE = 120
ATR_LEN = 14


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


def _true_ranges(bars):
    trs, prev_c = [], None
    for b in bars:
        h, l, c = b['h'], b['l'], b['c']
        if prev_c is None:
            trs.append(h - l)
        else:
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    return trs


def _atr_before(bars, i, length=ATR_LEN):
    """ATR(length) from the `length` bars strictly before index i."""
    if i < length:
        return None
    seg = _true_ranges(bars)[i - length:i]
    vals = [number(v) for v in seg]
    if len(vals) < length or any(v is None or v < 0 for v in vals):
        return None
    return sum(vals) / length


def _levels_from_snapshots(db, c):
    """Flatten session-liquidity highs/lows and HTF levels into level dicts.

    Defensive: either snapshot may be missing or malformed -> contributes
    nothing.
    """
    out = []
    sess = db.get(c, 'ict_session_liquidity:latest', {}) or {}
    for r in (sess.get('levels') or []):
        if not isinstance(r, dict):
            continue
        sym = r.get('symbol')
        hi, lo = number(r.get('high')), number(r.get('low'))
        if hi:
            out.append({'symbol': sym, 'price': hi, 'kind': 'session_high',
                        'session': r.get('session'), 'source': 'session_liquidity'})
        if lo:
            out.append({'symbol': sym, 'price': lo, 'kind': 'session_low',
                        'session': r.get('session'), 'source': 'session_liquidity'})
    htf = db.get(c, 'ict_htf_levels:latest', {}) or {}
    for r in (htf.get('levels') or []):
        if not isinstance(r, dict):
            continue
        p = number(r.get('price'))
        if p:
            out.append({'symbol': r.get('symbol'), 'price': p,
                        'kind': r.get('kind') or 'htf', 'source': 'htf_levels'})
    return out


def _opposing(sym_levels, broken, direction, ref):
    """Nearest level beyond `ref` in the trade direction, excluding the break."""
    best = None
    for l in sym_levels:
        p = number(l.get('price'))
        if p is None:
            continue
        if abs(p - broken) <= 1e-9 * max(1.0, abs(broken)):
            continue
        if direction == 'long' and p > ref and (best is None or p < best):
            best = p
        if direction == 'short' and p < ref and (best is None or p > best):
            best = p
    return best


def detect_break(bars, level, direction, atr=None, retest_bars=5, min_rr=1.5,
                 buffer_atr_frac=0.1, opposing=None, level_kind='level',
                 symbol=None, now=None, disp_atr_mult=1.5, fresh_atr_mult=1.0):
    """Pure: find the most recent close-beyond-level on a displacement bar.

    bars: ascending [{ts,o,h,l,c}]. direction: 'long' (close above level) or
    'short' (close below). The breaking bar must have range >=
    disp_atr_mult x ATR(14) measured on the 14 bars before it. Entry is the
    first bar touching the broken level within `retest_bars` bars after the
    break (entry at the level), else the displacement bar's close. Stop is
    the displacement origin extreme minus/plus buffer_atr_frac x ATR.
    Target is `opposing` (next opposing liquidity) when beyond entry, else
    2R. Returns the event row or None when there is no qualifying break or
    rr < min_rr.
    """
    level = number(level)
    if level is None or level <= 0 or direction not in ('long', 'short'):
        return None
    clean = []
    for b in bars or []:
        vals = {k: number((b.get(k) if isinstance(b, dict) else None)) for k in
                ('ts', 'o', 'h', 'l', 'c')}
        if any(v is None for v in vals.values()):
            continue
        clean.append(vals)
    if len(clean) < ATR_LEN + 2:
        return None
    long = direction == 'long'
    mult = number(disp_atr_mult) or 1.5
    rb = int(number(retest_bars) if number(retest_bars) is not None else 5)
    minrr = number(min_rr)
    minrr = 1.5 if minrr is None else minrr
    fam = number(fresh_atr_mult)
    fam = 1.0 if fam is None else fam
    bfrac = number(buffer_atr_frac)
    bfrac = 0.1 if bfrac is None else bfrac
    for i in range(len(clean) - 1, ATR_LEN - 1, -1):
        b = clean[i]
        if long and not b['c'] > level:
            continue
        if not long and not b['c'] < level:
            continue
        a = number(atr)
        if a is None:
            a = _atr_before(clean, i)
        if not a or a <= 0:
            continue
        if clean[i]['h'] - clean[i]['l'] < mult * a:
            continue
        return _build_signal(clean, i, a, level, long, rb, minrr, bfrac,
                             opposing, level_kind, symbol, now, mult, fam)
    return None


def _build_signal(bars, i, atr, level, long, retest_bars, min_rr, buffer_atr_frac,
                  opposing, level_kind, symbol, now, disp_atr_mult,
                  fresh_atr_mult):
    disp = bars[i]
    buf = buffer_atr_frac * atr
    entry, entry_ts, retested = disp['c'], disp['ts'], False
    for j in range(i + 1, min(len(bars), i + 1 + retest_bars)):
        bj = bars[j]
        if (long and bj['l'] <= level) or (not long and bj['h'] >= level):
            entry, entry_ts, retested = level, bj['ts'], True
            break
    origin = disp['l'] if long else disp['h']
    stop = origin - buf if long else origin + buf
    risk = (entry - stop) if long else (stop - entry)
    if risk is None or risk <= 0:
        return None
    opp = number(opposing)
    use_opp = opp is not None and ((opp > entry) if long else (opp < entry))
    target = opp if use_opp else (entry + 2 * risk if long else entry - 2 * risk)
    reward = (target - entry) if long else (entry - target)
    if reward is None or reward <= 0:
        return None
    rr = reward / risk
    if rr < min_rr:
        return None
    shift = 'fresh' if abs(disp['c'] - origin) <= fresh_atr_mult * atr else 'extended'
    return {
        'symbol': symbol, 'concept': 'continuation',
        'direction': 'long' if long else 'short',
        'signal_ts': entry_ts, 'disp_ts': disp['ts'],
        'entry': entry, 'stop': stop, 'target': target,
        'risk_pts': risk, 'reward_pts': reward, 'rr': rr,
        'level_price': level, 'level_kind': level_kind,
        'retested': retested, 'shift': shift,
        'params': {'disp_atr_mult': disp_atr_mult, 'retest_bars': retest_bars,
                   'buffer_atr_frac': buffer_atr_frac, 'buffer': buf,
                   'fresh_atr_mult': fresh_atr_mult, 'min_rr': min_rr,
                   'no_retest_entry': 'displacement_close',
                   'target_source': 'opposing_liquidity' if use_opp else '2R'},
        'at': now if now is not None else entry_ts,
    }


def scan(db, c, cfg, now):
    """Throttled continuation scan over configured symbols."""
    if not getattr(cfg, 'ict_continuation', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'ict_continuation:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    levels = _levels_from_snapshots(db, c)
    rb = number(getattr(cfg, 'ict_cont_retest_bars', 5))
    retest_bars = int(rb) if rb is not None else 5
    min_rr = number(getattr(cfg, 'ict_cont_min_rr', 1.5))
    min_rr = 1.5 if min_rr is None else min_rr
    disp_mult = number(getattr(cfg, 'ict_aoi_disp_atr_mult', 1.5)) or 1.5
    fresh_mult = number(getattr(cfg, 'ict_fresh_atr_mult', 1.0))
    fresh_mult = 1.0 if fresh_mult is None else fresh_mult
    buf_frac = number(getattr(cfg, 'ict_cont_buffer_atr_frac', 0.1))
    buf_frac = 0.1 if buf_frac is None else buf_frac
    rows, excluded = {}, {}
    for symbol in symbols:
        sym_levels = [l for l in levels
                      if l.get('symbol') == symbol and number(l.get('price'))]
        if not sym_levels:
            excluded['no_levels'] = excluded.get('no_levels', 0) + 1
            continue
        window = ict_bars(db, c, symbol)
        bars = window or _bars_from_recent(db.recent(c, 'bar', symbol, limit=2000))
        if not bars:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            continue
        by_dir = {}
        for lv in sym_levels:
            for direction in ('long', 'short'):
                opp = _opposing(sym_levels, lv['price'], direction, lv['price'])
                sig = detect_break(bars, lv['price'], direction,
                                   retest_bars=retest_bars, min_rr=min_rr,
                                   buffer_atr_frac=buf_frac, opposing=opp,
                                   level_kind=lv.get('kind') or 'level',
                                   symbol=symbol, now=now,
                                   disp_atr_mult=disp_mult,
                                   fresh_atr_mult=fresh_mult)
                if sig and (direction not in by_dir or
                            sig['signal_ts'] > by_dir[direction]['signal_ts']):
                    by_dir[direction] = sig
        sigs = sorted(by_dir.values(), key=lambda s: s['signal_ts'],
                      reverse=True)[:5]
        if not sigs:
            excluded['no_break'] = excluded.get('no_break', 0) + 1
            continue
        rows[symbol] = {'symbol': symbol, 'signals': sigs,
                        'n_levels': len(sym_levels), 'at': now}
        for s in sigs:
            # Deterministic key: re-scans of the same break dedup silently.
            db.append(c, 'ict_continuation_signal', 'ict_futures', symbol, now,
                      s, key='ictcont:%s:%s:%s:%s' % (
                          symbol, day(s['disp_ts']), s['direction'],
                          round(s['level_price'], 4)))
        from .ict_paper import submit as _paper_submit
        for s in sigs:
            _paper_submit(db, c, cfg, now, 'continuation', symbol, s)
    db.put(c, 'ict_continuation:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'status': 'running' if rows or not excluded else 'idle'})
    db.put(c, 'ict_continuation:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent continuation signals."""
    latest = db.get(c, 'ict_continuation:latest', {})
    rows = sorted((latest.get('rows') or {}).values(),
                  key=lambda r: r.get('symbol', ''))[:limit]
    signals = db.recent(c, 'ict_continuation_signal', limit=50)
    return {'asof': latest.get('at'), 'excluded': latest.get('excluded', {}),
            'rows': rows,
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']}
                        for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
