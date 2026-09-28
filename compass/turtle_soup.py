"""Turtle soup: liquidity sweep + reversal (ICT concept detector).

Evidence only. This module never alerts, never trades, and never auto-admits
to the live scanner. It logs sweep+reclaim events as research rows so a future
promotion rule can be decided from data, not narrative.

Long setup: a bar's low trades below a liquidity level by >= sweep_ticks,
then price closes back above the level within ICT_SOUP_CONFIRM_BARS bars.
Mirror for shorts.
"""
from .market import number, day, CT  # noqa: F401  (CT kept for session-math convention)
from .ict_common import ict_bars

SCAN_THROTTLE = 120
TICK_FALLBACK_DEFAULT = 0.25
CONFIRM_BARS_DEFAULT = 3
ATR_FRAC_DEFAULT = 0.1
MIN_RR_DEFAULT = 1.5
ATR_PERIOD = 14


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


def _atr(bars, period=ATR_PERIOD):
    """Wilder-style ATR over the bar window; None when too few bars."""
    if not bars or len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]['h'], bars[i]['l'], bars[i - 1]['c']
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _sweep_ticks(bars, tick_fallback=TICK_FALLBACK_DEFAULT, atr_frac=ATR_FRAC_DEFAULT):
    """sweep_ticks = max(instrument tick, atr_frac * ATR(14)) when ATR exists."""
    atr = _atr(bars)
    if atr and atr > 0:
        return max(tick_fallback, atr_frac * atr)
    return tick_fallback


def detect_sweep(bars, level, direction, sweep_ticks=TICK_FALLBACK_DEFAULT,
                 confirm_bars=CONFIRM_BARS_DEFAULT, level_kind='level',
                 opposing=(), min_rr=MIN_RR_DEFAULT, buffer_ticks=None):
    """Find the most recent sweep+reclaim of `level` in ascending `bars`.

    direction 'long': a bar's low < level - sweep_ticks, then a close back
    above `level` within `confirm_bars` (the sweep bar itself may confirm).
    Mirror for 'short'. Entry = confirming close, stop = sweep extreme +/-
    buffer, target = nearest opposing session extreme beyond entry, else
    entry +/- 2R. Returns the signal dict, or None when there is no sweep,
    no reclaim in time, or rr < min_rr.
    """
    level = number(level)
    if not bars or not level or direction not in ('long', 'short'):
        return None
    sweep_ticks = number(sweep_ticks) or TICK_FALLBACK_DEFAULT
    try:
        confirm_bars = int(number(confirm_bars) or CONFIRM_BARS_DEFAULT)
    except (TypeError, ValueError):
        confirm_bars = CONFIRM_BARS_DEFAULT
    buffer_ticks = number(buffer_ticks)
    if buffer_ticks is None:
        buffer_ticks = sweep_ticks
    min_rr = number(min_rr)
    if min_rr is None:
        min_rr = 0.0
    best = None
    n = len(bars)
    for i in range(n):
        b = bars[i]
        if direction == 'long':
            swept = b['l'] < level - sweep_ticks
        else:
            swept = b['h'] > level + sweep_ticks
        if not swept:
            continue
        extreme = b['l'] if direction == 'long' else b['h']
        for j in range(i, min(n, i + confirm_bars + 1)):
            cj = bars[j]['c']
            confirmed = (cj > level) if direction == 'long' else (cj < level)
            if not confirmed:
                continue
            entry = cj
            if direction == 'long':
                stop = extreme - buffer_ticks
                risk = entry - stop
                cands = [number(p) for p in (opposing or [])
                         if number(p) and number(p) > entry]
                if cands:
                    target, target_kind = min(cands), 'session_extreme'
                else:
                    target, target_kind = entry + 2 * risk, 'two_r'
                reward = target - entry
            else:
                stop = extreme + buffer_ticks
                risk = stop - entry
                cands = [number(p) for p in (opposing or [])
                         if number(p) and number(p) < entry]
                if cands:
                    target, target_kind = max(cands), 'session_extreme'
                else:
                    target, target_kind = entry - 2 * risk, 'two_r'
                reward = entry - target
            if risk <= 0:
                continue
            rr = reward / risk
            if rr < min_rr:
                continue
            best = {
                'direction': direction,
                'level_price': level, 'level_kind': level_kind,
                'sweep_ts': b['ts'], 'signal_ts': bars[j]['ts'],
                'sweep_extreme': extreme, 'confirm_close': entry,
                'entry': entry, 'stop': stop, 'target': target,
                'target_kind': target_kind,
                'risk_pts': risk, 'reward_pts': reward, 'rr': rr,
                'sweep_ticks': sweep_ticks, 'buffer_ticks': buffer_ticks,
                'confirm_lag_bars': j - i,
            }
            break  # first reclaim for this sweep; keep scanning for later sweeps
    return best


def _levels_for_symbol(liq_levels, htf_levels, symbol):
    """Normalize the two level snapshots to [(price, kind)] for one symbol.

    Session-liquidity rows look like {symbol, session, high, low, ...};
    HTF rows look like {symbol, price, kind}. Anything else is skipped.
    """
    out = []
    for row in liq_levels or []:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            continue
        sess = row.get('session')
        tag = (':' + str(sess)) if sess else ''
        hi, lo = number(row.get('high')), number(row.get('low'))
        if hi:
            out.append((hi, 'session_high' + tag))
        if lo:
            out.append((lo, 'session_low' + tag))
    for row in htf_levels or []:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            continue
        price = number(row.get('price'))
        if price:
            out.append((price, str(row.get('kind') or 'htf')))
    return out


def _directions_for_kind(kind):
    """Sweep of lows -> long, sweep of highs -> short; unknown -> both."""
    k = (kind or '').lower()
    if 'low' in k:
        return ('long',)
    if 'high' in k:
        return ('short',)
    return ('long', 'short')


def scan(db, c, cfg, now):
    """Throttled turtle-soup scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_turtle_soup', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_turtle_soup:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = getattr(cfg, 'ict_symbols', ()) or ()
    try:
        confirm_bars = int(number(getattr(cfg, 'ict_soup_confirm_bars',
                                          CONFIRM_BARS_DEFAULT)) or CONFIRM_BARS_DEFAULT)
    except (TypeError, ValueError):
        confirm_bars = CONFIRM_BARS_DEFAULT
    atr_frac = number(getattr(cfg, 'ict_soup_atr_frac', ATR_FRAC_DEFAULT)) or ATR_FRAC_DEFAULT
    tick_fb = number(getattr(cfg, 'ict_tick_fallback',
                             TICK_FALLBACK_DEFAULT)) or TICK_FALLBACK_DEFAULT
    min_rr = number(getattr(cfg, 'ict_soup_min_rr', MIN_RR_DEFAULT))
    if min_rr is None:
        min_rr = MIN_RR_DEFAULT
    # Defensive reads: either snapshot may be absent (modules not built yet).
    liq = db.get(c, 'ict_session_liquidity:latest', {}) or {}
    htf = db.get(c, 'ict_htf_levels:latest', {}) or {}
    liq_levels = liq.get('levels') or []
    htf_levels = htf.get('levels') or []
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
        levels = _levels_for_symbol(liq_levels, htf_levels, symbol)
        if not levels:
            excluded['no_levels'] = excluded.get('no_levels', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_levels', 'at': now}
            continue
        sweep_ticks = _sweep_ticks(bars, tick_fb, atr_frac)
        opp_highs = [p for p, k in levels if k.startswith('session_high')]
        opp_lows = [p for p, k in levels if k.startswith('session_low')]
        best = None
        for price, kind in levels:
            for direction in _directions_for_kind(kind):
                sig = detect_sweep(
                    bars, price, direction, sweep_ticks=sweep_ticks,
                    confirm_bars=confirm_bars, level_kind=kind,
                    opposing=opp_highs if direction == 'long' else opp_lows,
                    min_rr=min_rr)
                if sig and (best is None or sig['signal_ts'] > best['signal_ts']):
                    best = sig
        if best is None:
            excluded['no_signal'] = excluded.get('no_signal', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_signal', 'at': now,
                            'n_levels': len(levels), 'sweep_ticks': sweep_ticks}
            continue
        row = {'symbol': symbol, 'concept': 'turtle_soup', 'at': now,
               'params': {'sweep_ticks': sweep_ticks, 'confirm_bars': confirm_bars,
                          'min_rr': min_rr, 'n_levels': len(levels)},
               **best}
        rows[symbol] = row
        # Idempotent per symbol/day/signal_ts (db key is per symbol+date).
        state = db.get(c, 'ict_turtle_soup:signal:' + symbol, {}) or {}
        if state.get('date') != today or state.get('signal_ts') != best['signal_ts']:
            payload = {k: v for k, v in row.items() if k != 'at'}
            db.append(c, 'ict_turtle_soup_signal', 'ict_futures', symbol, now,
                      payload, key='ictsoup:%s:%s' % (symbol, today))
            db.put(c, 'ict_turtle_soup:signal:' + symbol,
                   {'signal_ts': best['signal_ts'], 'date': today,
                    'direction': best['direction'], 'at': now})
            n_signals += 1
        from .ict_paper import submit as _paper_submit
        _paper_submit(db, c, cfg, now, 'turtle_soup', symbol, best)
    db.put(c, 'ict_turtle_soup:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'params': {'confirm_bars': confirm_bars, 'atr_frac': atr_frac,
                       'tick_fallback': tick_fb, 'min_rr': min_rr},
            'status': 'running' if rows else 'idle'})
    db.put(c, 'ict_turtle_soup:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent sweep signals."""
    latest = db.get(c, 'ict_turtle_soup:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'ict_turtle_soup_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
