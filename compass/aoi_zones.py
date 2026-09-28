"""ICT AOI zones: premium/discount dealing range + order blocks.

Evidence only. This module never alerts, never trades, and never admits
anything to the live scanner. It records the dealing range (last complete
NY AM session high-low, CT), splits it into premium / discount /
equilibrium, and marks order blocks at displacement bars.
"""
import time
from datetime import datetime, timedelta

from .market import number, CT
from .ict_common import ict_bars

SCAN_THROTTLE = 120
ATR_LEN = 14

SESSION_DEFS = {
    'asia': ('18:00', '01:00'),
    'london': ('01:00', '07:00'),
    'ny_am': ('07:00', '11:30'),
    'ny_pm': ('12:30', '15:00'),
}


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


def _hhmm(s):
    parts = str(s).split(':')
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, TypeError):
        return None


def _session_defs(cfg):
    defs = getattr(cfg, 'ict_session_defs', None)
    return defs if isinstance(defs, dict) and defs else SESSION_DEFS


def _last_complete_window(now, name, defs):
    """Most recent fully-elapsed session window for `name`, CT wall-clock.

    Returns (start_ts, end_ts, date_iso) or None when the name is unknown.
    """
    entry = defs.get(name)
    if not entry or len(entry) != 2:
        return None
    sh, sm = _hhmm(entry[0]) or (None, None)
    eh, em = _hhmm(entry[1]) or (None, None)
    if sh is None or eh is None:
        return None
    local = datetime.fromtimestamp(now, CT)
    for back in range(8):
        d = (local - timedelta(days=back)).date()
        start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=CT).timestamp()
        end = datetime(d.year, d.month, d.day, eh, em, tzinfo=CT).timestamp()
        if end <= start:  # overnight window, e.g. Asia 18:00 -> 01:00
            end += 86400
        if now >= end:
            return start, end, d.isoformat()
    return None


def _bar_duration(bars):
    best, prev = None, None
    for t in sorted(b['ts'] for b in bars):
        if prev is not None and t > prev and (best is None or t - prev < best):
            best = t - prev
        prev = t
    return best or 60.0


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


def find_order_block(bars, atr=None, disp_atr_mult=1.5):
    """Pure: last opposite-direction candle before the most recent displacement bar.

    bars: ascending [{ts,o,h,l,c}]. Displacement = bar range >=
    disp_atr_mult x ATR(14) (ATR from the 14 bars before the candidate, or
    the explicit `atr`). Returns the OB dict or None when there is no
    displacement bar, the displacement bar is a doji, or no opposite candle
    precedes it. Bullish displacement -> bullish OB (bias long); bearish ->
    bearish OB (bias short).
    """
    clean = []
    for b in bars or []:
        vals = {k: number((b.get(k) if isinstance(b, dict) else None)) for k in
                ('ts', 'o', 'h', 'l', 'c')}
        if any(v is None for v in vals.values()):
            continue
        clean.append(vals)
    if len(clean) < ATR_LEN + 2:
        return None
    mult = number(disp_atr_mult) or 1.5
    disp, disp_atr = None, None
    for i in range(len(clean) - 1, ATR_LEN - 1, -1):
        a = number(atr)
        if a is None:
            a = _atr_before(clean, i)
        if not a or a <= 0:
            continue
        if clean[i]['h'] - clean[i]['l'] >= mult * a:
            disp, disp_atr = i, a
            break
    if disp is None:
        return None
    d = clean[disp]
    if d['c'] > d['o']:
        bullish, want_bearish = True, True
    elif d['c'] < d['o']:
        bullish, want_bearish = False, False
    else:
        return None  # doji displacement: no opposite candle to anchor on
    ob = None
    for j in range(disp - 1, -1, -1):
        b = clean[j]
        if want_bearish and b['c'] < b['o']:
            ob = j
            break
        if not want_bearish and b['c'] > b['o']:
            ob = j
            break
    if ob is None:
        return None
    o = clean[ob]
    return {
        'zone_kind': 'bullish_ob' if bullish else 'bearish_ob',
        'top': o['h'], 'bottom': o['l'],
        'bias': 'long' if bullish else 'short',
        'formed_ts': o['ts'], 'disp_ts': d['ts'],
        'disp_range': d['h'] - d['l'], 'atr': disp_atr,
        'disp_atr_mult': mult,
    }


def _ob_trade_levels(ob, entry_close):
    """Paper-trade levels for a newly formed order block.

    Convention (documented choice): the OB is a momentum POI, so the paper
    entry is the close following formation in the bias direction; the stop
    sits beyond the OB far edge by 0.25x ATR; the target is exactly 2R.
    Returns None when the bias is not directional or risk is non-positive.
    """
    bias = (ob or {}).get('bias')
    entry = number(entry_close)
    atr_v = number((ob or {}).get('atr'))
    top, bottom = number(ob.get('top')), number(ob.get('bottom'))
    if bias not in ('long', 'short') or not entry or not atr_v or atr_v <= 0:
        return None
    if top is None or bottom is None:
        return None
    buf = 0.25 * atr_v
    if bias == 'long':
        stop = bottom - buf
        risk = entry - stop
        target = entry + 2 * risk
    else:
        stop = top + buf
        risk = stop - entry
        target = entry - 2 * risk
    if risk <= 0:
        return None
    return {'direction': bias, 'entry': entry, 'stop': stop, 'target': target,
            'target_kind': 'two_r', 'risk_pts': risk,
            'reward_pts': 2 * risk, 'rr': 2.0,
            'level_kind': 'order_block', 'level_price': (top + bottom) / 2.0}


def _classify_symbol(symbol, bars, now, eq_frac, disp_mult, range_session, defs):
    """Build one AOI row, or {'excluded': reason}."""
    win = _last_complete_window(now, range_session, defs)
    if not win:
        return {'excluded': 'no_session_window'}
    start, end, sess_date = win
    dur = _bar_duration(bars)
    wbars = [b for b in bars if b['ts'] >= start and b['ts'] + dur <= end]
    if not wbars:
        return {'excluded': 'no_range_bars'}
    hi = max(b['h'] for b in wbars)
    lo = min(b['l'] for b in wbars)
    if not hi > lo:
        return {'excluded': 'flat_range'}
    mid = (hi + lo) / 2.0
    half = eq_frac * (hi - lo)
    price = bars[-1]['c']
    bias = 'long' if price < mid - half else 'short' if price > mid + half else 'neutral'
    zones = [
        {'symbol': symbol, 'concept': 'aoi_zones', 'zone_kind': 'premium',
         'top': hi, 'bottom': mid, 'bias': bias, 'formed_ts': end, 'at': now},
        {'symbol': symbol, 'concept': 'aoi_zones', 'zone_kind': 'discount',
         'top': mid, 'bottom': lo, 'bias': bias, 'formed_ts': end, 'at': now},
        {'symbol': symbol, 'concept': 'aoi_zones', 'zone_kind': 'equilibrium',
         'top': mid + half, 'bottom': mid - half, 'bias': bias,
         'formed_ts': end, 'at': now},
    ]
    ob = find_order_block(bars[-400:], disp_atr_mult=disp_mult)
    ob_row = None
    if ob:
        ob_row = {'symbol': symbol, 'concept': 'aoi_zones', **ob, 'at': now}
        levels = _ob_trade_levels(ob, bars[-1]['c'])
        if levels:
            ob_row.update(levels)
    return {
        'symbol': symbol, 'range_session': range_session, 'session_date': sess_date,
        'range_high': hi, 'range_low': lo, 'range_mid': mid,
        'eq_frac': eq_frac, 'price': price, 'bias': bias,
        'zones': zones, 'order_block': ob_row, 'at': now,
    }


def scan(db, c, cfg, now):
    """Throttled AOI scan over configured symbols."""
    if not getattr(cfg, 'ict_aoi_zones', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'ict_aoi_zones:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    eq_frac = number(getattr(cfg, 'ict_aoi_eq_frac', 0.05))
    eq_frac = 0.05 if eq_frac is None else eq_frac
    disp_mult = number(getattr(cfg, 'ict_aoi_disp_atr_mult', 1.5)) or 1.5
    range_session = getattr(cfg, 'ict_aoi_range_session', 'ny_am') or 'ny_am'
    defs = _session_defs(cfg)
    rows, excluded = {}, {}
    for symbol in symbols:
        window = ict_bars(db, c, symbol)
        bars = window or _bars_from_recent(db.recent(c, 'bar', symbol, limit=2000))
        if not bars:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            continue
        row = _classify_symbol(symbol, bars, now, eq_frac, disp_mult,
                               range_session, defs)
        if 'excluded' in row:
            excluded[row['excluded']] = excluded.get(row['excluded'], 0) + 1
            continue
        rows[symbol] = row
        # Log a signal only when a *new* order block forms (idempotent).
        ob = row.get('order_block')
        if ob:
            seen_key = 'ict_aoi_zones:ob_seen:' + symbol
            if db.get(c, seen_key) != ob['formed_ts']:
                db.append(c, 'ict_aoi_signal', 'ict_futures', symbol, now,
                          {k: v for k, v in ob.items() if k != 'at'},
                          key='ictaoi:%s:ob:%d' % (symbol, int(ob['formed_ts'])))
                db.put(c, seen_key, ob['formed_ts'])
                from .ict_paper import submit as _paper_submit
                _paper_submit(db, c, cfg, now, 'aoi_zones', symbol, ob)
    db.put(c, 'ict_aoi_zones:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'range_session': range_session,
            'status': 'running' if rows or not excluded else 'idle'})
    db.put(c, 'ict_aoi_zones:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent order-block signals."""
    latest = db.get(c, 'ict_aoi_zones:latest', {})
    rows = sorted((latest.get('rows') or {}).values(),
                  key=lambda r: r.get('symbol', ''))[:limit]
    signals = db.recent(c, 'ict_aoi_signal', limit=50)
    return {'asof': latest.get('at'),
            'range_session': latest.get('range_session'),
            'excluded': latest.get('excluded', {}), 'rows': rows,
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']}
                        for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
