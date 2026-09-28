"""Trendline liquidity detector (ICT futures, Phase 1): trendline touch/sweep.

Evidence only. This module never alerts, never trades, and never admits
anything to the live scanner. Fractal swings (N=2) on the minute-bar window
(needs >= 60 bars); 2+ swing highs -> falling trendline, 2+ swing lows ->
rising trendline, with a linear fit through the first/last swing. A bar
extreme within 0.05*ATR of the line counts as a touch; the third touch
becomes a watch row. A poke beyond the line by >= sweep_ticks that closes
back within the confirm window is a turtle-soup-style reversal signal
(entry/stop/target mechanics, rr >= 1.5).

Snapshot written: 'ict_trendline_liquidity:latest' with
    {'at': now, 'rows': {symbol: [row, ...]}, 'excluded': {...}, ...}
'rows' is ALWAYS {symbol: [row, ...]} -- a list per symbol, even for a
single row -- so downstream consumers (tier_a_b) can rely on the shape.
"""
from .market import number
from .ict_common import ict_bars

MIN_BARS = 60
FRACTAL_N_DEFAULT = 2
TL_TOL_ATR_FRAC = 0.05   # touch tolerance as a fraction of ATR(14)
SOUP_ATR_FRAC = 0.1      # sweep_ticks = max(instrument_tick, 0.1 * ATR)
SOUP_CONFIRM_BARS = 3    # bars allowed for the close-back confirmation
MIN_RR_DEFAULT = 1.5
SCAN_THROTTLE = 120      # seconds between scans


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


def _atr(bars, period=14):
    """Classic ATR over the ascending bar dicts; None when too short."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b['h'] - b['l'], abs(b['h'] - p['c']), abs(b['l'] - p['c'])))
    tail = trs[-period:]
    return sum(tail) / len(tail) if tail else None


def find_swings(bars, n=2):
    """Fractal swings on ascending bar dicts [{ts,o,h,l,c}].

    Bar i is a swing high iff h[i] is strictly greater than the highs of
    the n bars on each side (mirror for swing lows).

    Returns {'highs': [(idx, ts, price)], 'lows': [(idx, ts, price)]} with
    idx the position in the bars list.
    """
    highs, lows = [], []
    for i in range(n, len(bars) - n):
        h = bars[i]['h']
        if all(h > bars[j]['h'] for j in range(i - n, i + n + 1) if j != i):
            highs.append((i, bars[i]['ts'], h))
        l = bars[i]['l']
        if all(l < bars[j]['l'] for j in range(i - n, i + n + 1) if j != i):
            lows.append((i, bars[i]['ts'], l))
    return {'highs': highs, 'lows': lows}


def fit_line(swings):
    """Linear fit through the first and last swing.

    swings: [(idx, ts, price), ...]. Returns
    {'slope': s, 'idx0': i0, 'price0': p0} or None when < 2 swings.
    """
    if len(swings) < 2:
        return None
    (i0, _t0, p0), (i1, _t1, p1) = swings[0], swings[-1]
    if i1 == i0:
        return None
    return {'slope': (p1 - p0) / (i1 - i0), 'idx0': i0, 'price0': p0}


def _line_at(line, i):
    """Trendline price at bar index i."""
    return line['price0'] + line['slope'] * (i - line['idx0'])


def _opposing_target(db, c, symbol, direction, entry, risk):
    """Turtle-soup-style target: opposing session extreme from the liquidity
    map when one exists past the entry, else entry +/- 2R."""
    extreme = None
    try:
        if db is not None and c is not None:
            snap = db.get(c, 'ict_session_liquidity:latest', {}) or {}
            for row in snap.get('levels') or []:
                if not isinstance(row, dict) or row.get('symbol') != symbol:
                    continue
                px = number(row.get('high' if direction == 'long' else 'low'))
                if px is None:
                    continue
                if direction == 'long' and px > entry:
                    extreme = px if extreme is None else max(extreme, px)
                elif direction == 'short' and px < entry:
                    extreme = px if extreme is None else min(extreme, px)
    except Exception:
        extreme = None
    if extreme is not None:
        return extreme
    return entry + 2 * risk if direction == 'long' else entry - 2 * risk


def _find_sweep(bars, line, side, anchor_idx, sweep_ticks, confirm,
                min_rr, now, row_base, symbol, db, c):
    """Most recent completed sweep+reclaim against the line, or None.

    Rising line (long): a bar's low pokes below line - sweep_ticks and a
    close back at/above the line prints within `confirm` bars (the poke bar
    itself may be the confirming bar). Falling line mirrors.
    """
    direction = 'long' if side == 'rising' else 'short'
    best = None
    for i in range(anchor_idx, len(bars)):
        lv = _line_at(line, i)
        extreme = bars[i]['l'] if side == 'rising' else bars[i]['h']
        beyond = (lv - extreme) if side == 'rising' else (extreme - lv)
        if beyond < sweep_ticks:
            continue
        conf = None
        for j in range(i, min(len(bars), i + 1 + confirm)):
            lj = _line_at(line, j)
            cl = bars[j]['c']
            if (side == 'rising' and cl >= lj) or (side == 'falling' and cl <= lj):
                conf = j
                break
        if conf is None:
            continue
        entry = bars[conf]['c']
        if side == 'rising':
            sweep_extreme = min(b['l'] for b in bars[i:conf + 1])
            stop = sweep_extreme - sweep_ticks
            risk = entry - stop
        else:
            sweep_extreme = max(b['h'] for b in bars[i:conf + 1])
            stop = sweep_extreme + sweep_ticks
            risk = stop - entry
        if risk is None or risk <= 0:
            continue
        target = _opposing_target(db, c, symbol, direction, entry, risk)
        if target is None:
            continue
        reward = (target - entry) if direction == 'long' else (entry - target)
        rr = reward / risk if risk > 0 else None
        if rr is None or rr < min_rr:
            continue
        best = dict(row_base,
                    state='signal', signal_ts=bars[conf]['ts'],
                    entry=entry, stop=stop, target=target,
                    risk_pts=risk, reward_pts=reward, rr=rr,
                    level_price=lv, at=now)
    return best


def analyze_symbol(symbol, bars, now, params, db=None, c=None):
    """Pure-ish detection over one ascending bar window.

    Returns (watch_rows, signal_rows). db/c are optional and only used to
    look up an opposing session extreme for the target (turtle-soup
    mechanics); without them the target defaults to 2R. Never raises on
    bad data.
    """
    watch, signals = [], []
    try:
        n = int(params.get('fractal_n', FRACTAL_N_DEFAULT) or FRACTAL_N_DEFAULT)
    except (TypeError, ValueError):
        n = FRACTAL_N_DEFAULT
    a = _atr(bars, 14)
    if not a or a <= 0:
        return watch, signals
    tol_frac = number(params.get('tol_atr_frac')) or TL_TOL_ATR_FRAC
    tol = tol_frac * a
    sweep_ticks = max(number(params.get('instrument_tick')) or 0.0,
                      SOUP_ATR_FRAC * a)
    try:
        confirm = int(params.get('confirm_bars', SOUP_CONFIRM_BARS)
                      or SOUP_CONFIRM_BARS)
    except (TypeError, ValueError):
        confirm = SOUP_CONFIRM_BARS
    min_rr = number(params.get('min_rr')) or MIN_RR_DEFAULT
    swings = find_swings(bars, n)
    last = len(bars) - 1
    for side, swing_list, ext in (('rising', swings['lows'], 'l'),
                                  ('falling', swings['highs'], 'h')):
        if len(swing_list) < 2:
            continue
        line = fit_line(swing_list)
        if line is None:
            continue
        anchor_idx = swing_list[-1][0]
        touches = []
        for i in range(swing_list[0][0], len(bars)):
            lv = _line_at(line, i)
            extreme = bars[i]['h'] if ext == 'h' else bars[i]['l']
            if abs(extreme - lv) <= tol:
                touches.append((i, bars[i]['ts'], extreme))
        direction = 'long' if side == 'rising' else 'short'
        row_base = {
            'symbol': symbol, 'concept': 'trendline', 'direction': direction,
            'level_kind': 'trendline', 'level_price': _line_at(line, last),
            'touches': len(touches), 'line_slope': line['slope'],
            'params': {'fractal_n': n, 'tol_atr_frac': tol_frac,
                       'sweep_ticks': sweep_ticks, 'confirm_bars': confirm,
                       'min_rr': min_rr, 'line_side': side},
            'at': now,
        }
        sig = _find_sweep(bars, line, side, anchor_idx, sweep_ticks,
                          confirm, min_rr, now, row_base, symbol, db, c)
        if sig is not None:
            signals.append(sig)
        elif len(touches) >= 3:
            watch.append(dict(row_base, state='watch',
                              signal_ts=touches[-1][1],
                              entry=None, stop=None, target=None,
                              risk_pts=None, reward_pts=None, rr=None))
    return watch, signals


def scan(db, c, cfg, now):
    """Throttled trendline scan over configured symbols."""
    if not getattr(cfg, 'ict_trendline_liquidity', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_trendline_liquidity:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = list(getattr(cfg, 'ict_symbols', ()) or ())
    try:
        n = int(getattr(cfg, 'ict_tl_fractal_n', FRACTAL_N_DEFAULT)
                or FRACTAL_N_DEFAULT)
    except (TypeError, ValueError):
        n = FRACTAL_N_DEFAULT
    tol_frac = number(getattr(cfg, 'ict_tl_tol_atr_frac', TL_TOL_ATR_FRAC)) or TL_TOL_ATR_FRAC
    try:
        confirm = int(getattr(cfg, 'ict_soup_confirm_bars', SOUP_CONFIRM_BARS)
                      or SOUP_CONFIRM_BARS)
    except (TypeError, ValueError):
        confirm = SOUP_CONFIRM_BARS
    min_rr = number(getattr(cfg, 'ict_soup_min_rr', MIN_RR_DEFAULT)) or MIN_RR_DEFAULT
    tick_map = getattr(cfg, 'ict_instrument_ticks', {}) or {}
    rows, excluded = {}, {}
    n_signals = 0
    for symbol in symbols:
        bars = ict_bars(db, c, symbol)
        if not bars:
            bars = _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))
        if len(bars) < MIN_BARS:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            continue
        tick = number(tick_map.get(symbol)) if isinstance(tick_map, dict) else None
        params = {'fractal_n': n, 'tol_atr_frac': tol_frac,
                  'confirm_bars': confirm, 'min_rr': min_rr,
                  'instrument_tick': tick}
        wrows, srows = analyze_symbol(symbol, bars, now, params, db, c)
        sym_rows = wrows + srows
        if sym_rows:
            rows[symbol] = sym_rows
        # Signal appends are idempotent per symbol/completed sweep.
        prev = db.get(c, 'ict_trendline_liquidity:signal:' + symbol, {}) or {}
        for s in srows:
            if s.get('signal_ts') == prev.get('signal_ts'):
                continue
            db.append(c, 'ict_trendline_liquidity_signal', 'ict_futures',
                      symbol, now, s,
                      key='icttl:%s:%d' % (symbol, int(s['signal_ts'])))
            db.put(c, 'ict_trendline_liquidity:signal:' + symbol,
                   {'signal_ts': s['signal_ts'], 'at': now,
                    'direction': s['direction']})
            n_signals += 1
            prev = {'signal_ts': s['signal_ts']}
    db.put(c, 'ict_trendline_liquidity:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'status': 'running' if rows or not excluded else 'idle'})
    db.put(c, 'ict_trendline_liquidity:scanned_at', now)
    if not rows:
        return {'ran': False, 'reason': 'no_bars'}
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent sweep signals."""
    latest = db.get(c, 'ict_trendline_liquidity:latest', {}) or {}
    all_rows = []
    for sym_rows in (latest.get('rows') or {}).values():
        if isinstance(sym_rows, list):
            all_rows.extend(r for r in sym_rows if isinstance(r, dict))
        elif isinstance(sym_rows, dict):
            all_rows.append(sym_rows)
    rows = sorted(all_rows,
                  key=lambda r: 0 if r.get('state') == 'signal' else 1)[:limit]
    signals = db.recent(c, 'ict_trendline_liquidity_signal', limit=50)
    return {'asof': latest.get('at'), 'excluded': latest.get('excluded', {}),
            'rows': rows,
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']}
                        for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
