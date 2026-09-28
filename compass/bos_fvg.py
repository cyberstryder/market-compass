"""BOS + FVG: break of structure with displacement, then retrace into the first
unmitigated fair value gap in the BOS direction (the point of interest).

BOS = close beyond the latest confirmed fractal swing with displacement
(bar body >= 50% of its range, or the close clears the swing by >= 0.5x
ATR(14)). FVG = classic 3-candle imbalance (low[i] > high[i-2] bullish;
high[i] < low[i-2] bearish). A wick trading into the zone marks it mitigated.
Signal fires on the first retrace bar whose wick enters the newest
unmitigated in-direction FVG while the close still holds the far edge;
direction = BOS direction. The freshest such setup wins.

Evidence only. No alerts. Paper fills via ict_paper only when both
ICT_FUTURES_PAPER_ENABLED and the detector flag are on.
"""
from .market import number, day, CT  # noqa: F401  (CT kept for session-math convention)
from .ict_common import (ict_bars, atr, fractal_swings, detect_bos,
                         detect_fvgs, fvg_mitigated)
from .ict_paper import submit as _paper_submit

SCAN_THROTTLE = 120
ATR_PERIOD = 14
FRACTAL_N_DEFAULT = 2
BODY_FRAC_DEFAULT = 0.5
BEYOND_ATR_FRAC_DEFAULT = 0.5
FVG_LOOKBACK_DEFAULT = 30
STOP_ATR_FRAC_DEFAULT = 0.25
MIN_RR_DEFAULT = 1.5


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


def _touches(bar, fvg, direction):
    """Wick entry into the FVG zone."""
    if direction == 'long':
        return bar['l'] <= fvg['top']
    return bar['h'] >= fvg['bottom']


def _holds(bar, fvg, direction):
    """Close still respects the FVG far edge (zone not failed through)."""
    if direction == 'long':
        return bar['c'] >= fvg['bottom']
    return bar['c'] <= fvg['top']


def _opposing_structure(bars, entry, direction, n=2):
    """Nearest confirmed fractal swing beyond entry in the trade direction."""
    highs, lows = fractal_swings(bars, n)
    if direction == 'long':
        cands = [p for _, p in highs if p > entry]
        return min(cands) if cands else None
    cands = [p for _, p in lows if p < entry]
    return max(cands) if cands else None


def detect_signal(bars, bos, atr_v, n=FRACTAL_N_DEFAULT,
                  fvg_lookback=FVG_LOOKBACK_DEFAULT,
                  stop_atr_frac=STOP_ATR_FRAC_DEFAULT,
                  min_rr=MIN_RR_DEFAULT):
    """Freshest first-touch retrace into an unmitigated in-direction FVG.

    Considers FVGs formed within `fvg_lookback` bars at/before the BOS bar
    and still unmitigated at the BOS bar; each candidate fires on its first
    touching bar after BOS. Returns the latest-firing signal, or None.
    """
    if not atr_v or atr_v <= 0:
        return None
    try:
        fvg_lookback = int(number(fvg_lookback) or FVG_LOOKBACK_DEFAULT)
    except (TypeError, ValueError):
        fvg_lookback = FVG_LOOKBACK_DEFAULT
    stop_atr_frac = number(stop_atr_frac)
    if stop_atr_frac is None:
        stop_atr_frac = STOP_ATR_FRAC_DEFAULT
    min_rr = number(min_rr)
    if min_rr is None:
        min_rr = MIN_RR_DEFAULT
    direction = bos['direction']
    bidx = bos['bos_idx']
    fvgs = [f for f in detect_fvgs(bars)
            if f['direction'] == direction
            and f['formed_idx'] <= bidx
            and f['formed_idx'] >= bidx - fvg_lookback
            and not fvg_mitigated(bars, f, up_to_idx=bidx)]
    fvgs.sort(key=lambda f: f['formed_idx'], reverse=True)
    buf = stop_atr_frac * atr_v
    best = None
    for fvg in fvgs:
        for j in range(bidx + 1, len(bars)):
            b = bars[j]
            if not _touches(b, fvg, direction) or not _holds(b, fvg, direction):
                continue
            if any(_touches(bars[k], fvg, direction)
                   for k in range(fvg['formed_idx'] + 1, j)):
                continue  # not the first touch; an earlier bar claimed it
            entry = b['c']
            if direction == 'long':
                stop = fvg['bottom'] - buf
                risk = entry - stop
            else:
                stop = fvg['top'] + buf
                risk = stop - entry
            if risk <= 0:
                break
            struct = _opposing_structure(bars[:j + 1], entry, direction, n=n)
            if direction == 'long':
                reward_s = (struct - entry) if struct else None
            else:
                reward_s = (entry - struct) if struct else None
            if reward_s and reward_s / risk >= min_rr:
                target, target_kind = struct, 'opposing_structure'
                reward = reward_s
            else:
                target = entry + 2 * risk if direction == 'long' \
                    else entry - 2 * risk
                target_kind = 'two_r'
                reward = 2 * risk
            rr = reward / risk
            if rr < min_rr:
                break
            sig = {'direction': direction, 'signal_ts': b['ts'],
                   'level_price': (fvg['bottom'] + fvg['top']) / 2.0,
                   'level_kind': 'fvg',
                   'bos_ts': bos['bos_ts'], 'bos_idx': bidx,
                   'bos_swing_price': bos['swing_price'],
                   'fvg_bottom': fvg['bottom'], 'fvg_top': fvg['top'],
                   'fvg_formed_ts': fvg['formed_ts'],
                   'entry': entry, 'stop': stop, 'target': target,
                   'target_kind': target_kind,
                   'risk_pts': risk, 'reward_pts': reward, 'rr': rr,
                   'stop_buf_atr_frac': stop_atr_frac, 'atr': atr_v}
            if best is None or sig['signal_ts'] > best['signal_ts']:
                best = sig
            break  # first touch only per FVG
    return best


def scan(db, c, cfg, now):
    """Throttled BOS+FVG scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_bos_fvg', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_bos_fvg:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = getattr(cfg, 'ict_symbols', ()) or ()
    try:
        n = int(number(getattr(cfg, 'ict_bf_fractal_n', FRACTAL_N_DEFAULT))
                or FRACTAL_N_DEFAULT)
    except (TypeError, ValueError):
        n = FRACTAL_N_DEFAULT
    body_frac = number(getattr(cfg, 'ict_bf_body_frac', BODY_FRAC_DEFAULT))
    if body_frac is None:
        body_frac = BODY_FRAC_DEFAULT
    beyond_frac = number(getattr(cfg, 'ict_bf_beyond_atr_frac',
                                 BEYOND_ATR_FRAC_DEFAULT))
    if beyond_frac is None:
        beyond_frac = BEYOND_ATR_FRAC_DEFAULT
    try:
        lookback = int(number(getattr(cfg, 'ict_bf_fvg_lookback',
                                      FVG_LOOKBACK_DEFAULT))
                       or FVG_LOOKBACK_DEFAULT)
    except (TypeError, ValueError):
        lookback = FVG_LOOKBACK_DEFAULT
    stop_frac = number(getattr(cfg, 'ict_bf_stop_atr_frac', STOP_ATR_FRAC_DEFAULT))
    if stop_frac is None:
        stop_frac = STOP_ATR_FRAC_DEFAULT
    min_rr = number(getattr(cfg, 'ict_bf_min_rr', MIN_RR_DEFAULT))
    if min_rr is None:
        min_rr = MIN_RR_DEFAULT
    params = {'fractal_n': n, 'body_frac': body_frac,
              'beyond_atr_frac': beyond_frac, 'fvg_lookback': lookback,
              'stop_atr_frac': stop_frac, 'min_rr': min_rr}
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
        if not events:
            excluded['no_bos'] = excluded.get('no_bos', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_bos', 'at': now}
            continue
        bos = events[-1]
        sig = detect_signal(bars, bos, atr_v, n=n, fvg_lookback=lookback,
                            stop_atr_frac=stop_frac, min_rr=min_rr)
        if sig is None:
            excluded['no_signal'] = excluded.get('no_signal', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_signal', 'at': now,
                            'bos_direction': bos['direction'],
                            'bos_ts': bos['bos_ts']}
            continue
        row = {'symbol': symbol, 'concept': 'bos_fvg', 'at': now,
               'params': dict(params), **sig}
        rows[symbol] = row
        state = db.get(c, 'ict_bos_fvg:signal:' + symbol, {}) or {}
        if state.get('date') != today or state.get('signal_ts') != sig['signal_ts']:
            payload = {k: v for k, v in row.items() if k != 'at'}
            db.append(c, 'ict_bos_fvg_signal', 'ict_futures', symbol, now,
                      payload, key='ictbf:%s:%s' % (symbol, today))
            db.put(c, 'ict_bos_fvg:signal:' + symbol,
                   {'signal_ts': sig['signal_ts'], 'date': today,
                    'direction': sig['direction'], 'at': now})
            n_signals += 1
        _paper_submit(db, c, cfg, now, 'bos_fvg', symbol, sig)
    db.put(c, 'ict_bos_fvg:latest',
           {'at': now, 'rows': rows, 'excluded': excluded, 'params': params,
            'status': 'running' if rows else 'idle'})
    db.put(c, 'ict_bos_fvg:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent BOS+FVG signals."""
    latest = db.get(c, 'ict_bos_fvg:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'ict_bos_fvg_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts; '
                    'simulated paper fills only when ICT_FUTURES_PAPER_ENABLED '
                    'is on. No live orders.'}
