"""Breaking Out / Triangle: range breaks and coil patterns from daily bars.

Pure price-and-volume patterns, computed entirely from our own daily bars:
  - Range breaks: daily close beyond the prior 20/50/253-session high/low,
    close-only (intraday wick-throughs don't count), volume-confirmed at
    1.5x the 20-day average. Weak-volume breaks are kept as unconfirmed_break.
  - Triangles: 15-session coil (contracting 5-session block ranges + lower
    highs / higher lows structure), forming vs firing, 3-session invalidation.

Evidence only: no alerts, no admission, no trades. Forward marks (5/10/20
sessions + max favorable/adverse excursion) feed the weekly outcome table.
  - Bull/bear traps: a 20-session range break that closes back INSIDE the
    range within 3 sessions. The failed breakout traps momentum chasers;
    the fade direction is flagged with the opposite range edge as target.
    Structure: credit-spread candidate (14-21 DTE). Paper trading (no alerts)
    via trap_paper when TRAP_PAPER_ENABLED is set.
"""
from .market import number, session, day
from . import trap_paper

VOLUME_RATIO = 1.5
VOLUME_LOOKBACK = 20
FRESH_SESSIONS = 3
INVALIDATION_SESSIONS = 3
TRAP_LOOKBACK = 20
TRAP_WINDOW = 3
RANGE_FLAVORS = {'range20': 20, 'range50': 50, 'range52w': 253}
FLAT_PCT = 0.005


def daily_bars(db, c, symbol, limit=300):
    """Ascending daily bars with o/h/l/c/v, deduplicated by session day."""
    seen, out = set(), []
    for r in db.recent(c, 'daily', symbol, limit=limit):
        p = r.get('payload') or {}
        if not all(number(p.get(k)) is not None for k in ('o', 'h', 'l', 'c')):
            continue
        d = day(r['ts'])
        if d in seen:
            continue
        seen.add(d)
        out.append({'ts': r['ts'], 'day': d, 'o': number(p['o']), 'h': number(p['h']),
                    'l': number(p['l']), 'c': number(p['c']),
                    'v': number(p.get('v'))})
    return sorted(out, key=lambda b: b['ts'])


def detect_range_break(bars, lookback):
    """Close-only break of the prior `lookback`-session range on the last bar.

    Returns a break dict or None. Never counts intraday wick-throughs.
    """
    if len(bars) < lookback + 1:
        return None
    window, last = bars[-(lookback + 1):-1], bars[-1]
    range_high = max(b['h'] for b in window)
    range_low = min(b['l'] for b in window)
    if last['c'] > range_high:
        direction, level = 'up', range_high
    elif last['c'] < range_low:
        direction, level = 'down', range_low
    else:
        return None
    vols = [b['v'] for b in window[-VOLUME_LOOKBACK:] if b['v']]
    avg_vol = sum(vols) / len(vols) if vols else None
    ratio = (last['v'] / avg_vol) if (last['v'] and avg_vol) else None
    return {'direction': direction, 'level': level, 'close': last['c'],
            'volume': last['v'], 'avg_volume': avg_vol,
            'volume_ratio': ratio,
            'confirmation': 'confirmed' if ratio is not None and ratio >= VOLUME_RATIO
                            else 'unconfirmed_break'}


def detect_trap(bars, lookback=TRAP_LOOKBACK, window=TRAP_WINDOW):
    """Bull/bear trap: 20-session range break that closes back inside within `window` sessions.

    Scans each of the last `window` bars for a range break, then checks whether
    a subsequent close fell back inside the broken range. Returns a trap dict
    or None. The fade direction is opposite the break; the target is the
    opposite range edge.
    """
    if len(bars) < lookback + 2:
        return None
    # check each candidate break day in the window (oldest first)
    for offset in range(window, 0, -1):
        bi = len(bars) - offset - 1  # break-day index
        win = bars[bi - lookback:bi]
        if len(win) < lookback:
            continue
        range_high = max(b['h'] for b in win)
        range_low = min(b['l'] for b in win)
        bc = bars[bi]['c']
        if bc > range_high:
            break_dir, trap_kind = 'up', 'bull_trap'
        elif bc < range_low:
            break_dir, trap_kind = 'down', 'bear_trap'
        else:
            continue
        # did a later close fall back inside the range?
        for j in range(bi + 1, len(bars)):
            cj = bars[j]['c']
            if range_low <= cj <= range_high:
                fade = 'down' if break_dir == 'up' else 'up'
                target = range_low if fade == 'down' else range_high
                return {'kind': trap_kind, 'break_direction': break_dir,
                        'direction': fade, 'target': target,
                        'range_high': range_high, 'range_low': range_low,
                        'break_day': bars[bi]['day'], 'trap_day': bars[j]['day'],
                        'break_close': bc, 'trap_close': cj}
            # a further break in the same direction invalidates the trap setup
            if (break_dir == 'up' and cj > range_high) or \
               (break_dir == 'down' and cj < range_low):
                break
    return None


def _blocks(bars15):
    """Three 5-session blocks: (high, low, center_ts, range)."""
    blocks = []
    for i in range(3):
        block = bars15[i * 5:(i + 1) * 5]
        hi = max(b['h'] for b in block)
        lo = min(b['l'] for b in block)
        blocks.append({'high': hi, 'low': lo,
                       'ts': sum(b['ts'] for b in block) / 5,
                       'range': hi - lo})
    return blocks


def _decreasing(xs):
    return xs[0] > xs[1] > xs[2]


def _increasing(xs):
    return xs[0] < xs[1] < xs[2]


def _flat(xs):
    return all(abs(b - a) / a <= FLAT_PCT for a, b in zip(xs, xs[1:]))


def detect_triangle(bars15):
    """Mechanical coil detection on 15 sessions.

    Returns (flavor, upper_fn, lower_fn) or None. Trendlines are returned as
    (t0, p0, t1, p1) point pairs; value_at() evaluates them.
    """
    if len(bars15) < 15:
        return None
    blocks = _blocks(bars15[-15:])
    ranges = [b['range'] for b in blocks]
    if not (ranges[0] > ranges[1] > ranges[2]):
        return None  # not contracting
    highs = [b['high'] for b in blocks]
    lows = [b['low'] for b in blocks]
    lower_highs, higher_lows = _decreasing(highs), _increasing(lows)
    flat_highs, flat_lows = _flat(highs), _flat(lows)
    if lower_highs and higher_lows:
        flavor = 'triangle_sym'
    elif higher_lows and flat_highs:
        flavor = 'triangle_asc'
    elif lower_highs and flat_lows:
        flavor = 'triangle_desc'
    else:
        return None
    upper = (blocks[0]['ts'], blocks[0]['high'], blocks[2]['ts'], blocks[2]['high'])
    lower = (blocks[0]['ts'], blocks[0]['low'], blocks[2]['ts'], blocks[2]['low'])
    return flavor, upper, lower


def value_at(line, ts):
    t0, p0, t1, p1 = line
    if t1 == t0:
        return p1
    return p0 + (p1 - p0) * (ts - t0) / (t1 - t0)


def triangle_state(coil, last_bar):
    """Forming vs firing for a detected coil against the latest bar."""
    flavor, upper, lower = coil
    close = last_bar['c']
    if close > value_at(upper, last_bar['ts']):
        return 'firing', 'up'
    if close < value_at(lower, last_bar['ts']):
        return 'firing', 'down'
    return 'forming', None


def forward_marks(bars, idx, direction):
    """5/10/20-session forward returns + max favorable/adverse excursion."""
    base = bars[idx]['c']
    if not base:
        return {}
    sign = 1 if direction == 'up' else -1
    marks = {}
    for name, offset in (('d5', 5), ('d10', 10), ('d20', 20)):
        if idx + offset < len(bars):
            marks[name] = sign * (bars[idx + offset]['c'] - base) / base
    window = bars[idx + 1:idx + 21]
    if window:
        marks['mfe'] = sign * (max(b['h'] for b in window) - base) / base
        marks['mae'] = sign * (min(b['l'] for b in window) - base) / base
    return marks


def _event_id(symbol, day_str, pattern, direction):
    return 'brk:%s:%s:%s:%s' % (symbol, day_str, pattern, direction)


def _update_marks(db, c, event, bars):
    idx = next((i for i, b in enumerate(bars) if b['day'] == event['day']), None)
    if idx is None:
        return False
    marks = forward_marks(bars, idx, event['direction'])
    if marks != event.get('marks'):
        event['marks'] = marks
        db.put(c, 'breakout:' + event['id'], event)
        return True
    return False


def scan(db, c, cfg, now):
    """Once per day after the close: detect breaks, coils, invalidations."""
    if not getattr(cfg, 'breakouts', True):
        return {'ran': False, 'reason': 'disabled'}
    today = day(now)
    hours = session(today)
    if not hours or now < hours[1]:
        return {'ran': False, 'reason': 'not_after_close'}
    if db.get(c, 'breakouts:scanned_day') == today:
        return {'ran': False, 'reason': 'done_today'}
    new_events, forming = 0, 0
    # Scan the full watchlist (144 TradingView combined list), not just the
    # 15-stock 0DTE focus. Data is already collected for all of them.
    universe = getattr(cfg, 'watch_symbols', cfg.stocks)
    for symbol in universe:
        bars = daily_bars(db, c, symbol)
        if len(bars) < 16:
            continue
        # --- range breaks on today's close ---
        for pattern, lookback in RANGE_FLAVORS.items():
            brk = detect_range_break(bars, lookback)
            if not brk:
                continue
            eid = _event_id(symbol, today, pattern, brk['direction'])
            if db.get(c, 'breakout:' + eid) is None:
                event = {'id': eid, 'symbol': symbol, 'day': today, 'pattern': pattern,
                         'kind': 'range_break', 'direction': brk['direction'],
                         'level': brk['level'], 'close': brk['close'],
                         'volume_ratio': brk['volume_ratio'],
                         'confirmation': brk['confirmation'], 'status': 'fresh',
                         'sessions_since_break': 0, 'marks': {},
                         'evaluated_at': now}
                _update_marks(db, c, event, bars)
                db.put(c, 'breakout:' + eid, event)
                db.append(c, 'breakout_event', 'breakouts', symbol, now,
                          {'event_id': eid, 'pattern': pattern,
                           'direction': brk['direction'],
                           'confirmation': brk['confirmation']}, key=eid)
                new_events += 1
        # --- bull/bear traps: 20d break that closed back inside within 3 sessions ---
        trap = detect_trap(bars)
        if trap:
            eid = 'trap:%s:%s:%s' % (symbol, trap['trap_day'], trap['kind'])
            if db.get(c, 'breakout:' + eid) is None:
                event = {'id': eid, 'symbol': symbol, 'day': trap['trap_day'],
                         'pattern': trap['kind'], 'kind': trap['kind'],
                         'direction': trap['direction'],
                         'break_direction': trap['break_direction'],
                         'break_day': trap['break_day'],
                         'level': trap['trap_close'], 'target': trap['target'],
                         'range_high': trap['range_high'],
                         'range_low': trap['range_low'],
                         'close': trap['trap_close'],
                         'structure': 'credit spread (14-21 DTE, hold to expiration)',
                         'confirmation': 'n/a', 'status': 'fresh',
                         'sessions_since_break': 0, 'marks': {},
                         'evaluated_at': now}
                _update_marks(db, c, event, bars)
                db.put(c, 'breakout:' + eid, event)
                db.append(c, 'breakout_event', 'breakouts', symbol, now,
                          {'event_id': eid, 'pattern': trap['kind'],
                           'direction': trap['direction'],
                           'confirmation': 'n/a'}, key=eid)
                new_events += 1
        # --- triangles: firing check on prior-15 coil, forming check on last 15 ---
        fired_today = False
        if len(bars) >= 16:
            coil = detect_triangle(bars[-16:-1])
            if coil:
                state, direction = triangle_state(coil, bars[-1])
                if state == 'firing':
                    vols = [b['v'] for b in bars[-21:-1] if b['v']]
                    avg = sum(vols) / len(vols) if vols else None
                    ratio = (bars[-1]['v'] / avg) if (bars[-1]['v'] and avg) else None
                    eid = _event_id(symbol, today, coil[0], direction)
                    if db.get(c, 'breakout:' + eid) is None:
                        event = {'id': eid, 'symbol': symbol, 'day': today,
                                 'pattern': coil[0], 'kind': 'triangle_fire',
                                 'direction': direction,
                                 'level': value_at(coil[1] if direction == 'up' else coil[2],
                                                   bars[-1]['ts']),
                                 'close': bars[-1]['c'], 'volume_ratio': ratio,
                                 'confirmation': 'confirmed' if ratio is not None and ratio >= VOLUME_RATIO
                                                 else 'unconfirmed_break',
                                 'status': 'firing',
                                 'upper': coil[1], 'lower': coil[2],
                                 'sessions_since_fire': 0, 'marks': {},
                                 'evaluated_at': now}
                        _update_marks(db, c, event, bars)
                        db.put(c, 'breakout:' + eid, event)
                        db.append(c, 'breakout_event', 'breakouts', symbol, now,
                                  {'event_id': eid, 'pattern': coil[0],
                                   'direction': direction,
                                   'confirmation': event['confirmation']}, key=eid)
                        new_events += 1
                        db.put(c, 'triangle_state:' + symbol, None)
                    fired_today = True
            if not fired_today:
                coil_now = detect_triangle(bars[-15:])
                if coil_now:
                    db.put(c, 'triangle_state:' + symbol,
                           {'symbol': symbol, 'day': today, 'flavor': coil_now[0],
                            'upper': coil_now[1], 'lower': coil_now[2],
                            'state': triangle_state(coil_now, bars[-1])[0],
                            'evaluated_at': now})
                    forming += 1
                elif db.get(c, 'triangle_state:' + symbol):
                    db.put(c, 'triangle_state:' + symbol, None)
        # --- invalidation + mark updates for open events ---
        for key, event in db.prefix(c, 'breakout:').items():
            if not isinstance(event, dict) or event.get('symbol') != symbol:
                continue
            if event.get('kind') == 'triangle_fire' and event.get('status') == 'firing':
                event['sessions_since_fire'] = event.get('sessions_since_fire', 0) + 1
                lo, hi = value_at(event['lower'], bars[-1]['ts']), value_at(event['upper'], bars[-1]['ts'])
                if lo <= bars[-1]['c'] <= hi:
                    event['status'] = 'failed_triangle'
                elif event['sessions_since_fire'] >= INVALIDATION_SESSIONS:
                    event['status'] = 'done'
                event['evaluated_at'] = now
                db.put(c, 'breakout:' + event['id'], event)
            elif event.get('kind') in ('range_break', 'bull_trap', 'bear_trap') and event.get('status') == 'fresh':
                # A break/trap is actionable for FRESH_SESSIONS sessions after the close.
                event['sessions_since_break'] = event.get('sessions_since_break', 0) + 1
                if event['sessions_since_break'] >= FRESH_SESSIONS:
                    event['status'] = 'done'
                event['evaluated_at'] = now
                db.put(c, 'breakout:' + event['id'], event)
            _update_marks(db, c, event, bars)
        # --- trap paper spreads: enter on T+1 close for fresh traps ---
        if trap_paper.paper_enabled(cfg):
            for key, event in db.prefix(c, 'breakout:').items():
                if not isinstance(event, dict) or event.get('symbol') != symbol:
                    continue
                if event.get('kind') not in ('bull_trap', 'bear_trap'):
                    continue
                if event.get('status') != 'fresh':
                    continue
                if event.get('sessions_since_break', 0) < 1:
                    continue
                if event.get('paper_entered'):
                    continue
                entry_price = number(bars[-1]['c']) if bars else None
                if not entry_price:
                    continue
                res = trap_paper.submit(
                    db, c, cfg, now,
                    {**event, 'entry_day': today},
                    entry_price, bars)
                if res.get('submitted'):
                    event['paper_entered'] = res['trade_id']
                    event['evaluated_at'] = now
                    db.put(c, 'breakout:' + event['id'], event)
    # --- expire-settle open trap spreads ---
    if trap_paper.paper_enabled(cfg):
        by_day = {}
        def close_for(sym, day_str):
            if sym not in by_day:
                by_day[sym] = {b['day']: b for b in daily_bars(db, c, sym, limit=400)}
            b = by_day.get(sym, {}).get(day_str)
            return number(b['c']) if b else None
        trap_paper.settle(db, c, cfg, now, today, close_for)
    db.put(c, 'breakouts:scanned_day', today)
    return {'ran': True, 'new_events': new_events, 'forming': forming}


def display(db, c, now, days=7):
    """Board: forming coils + fresh breaks/fires + bull/bear traps. Weekly outcome table."""
    today = day(now)
    forming, fresh, traps = [], [], []
    for key, state in db.prefix(c, 'triangle_state:').items():
        if isinstance(state, dict) and state.get('state') == 'forming':
            forming.append(state)
    events = [e for k, e in db.prefix(c, 'breakout:').items() if isinstance(e, dict)]
    for e in events:
        if e.get('status') in ('fresh', 'firing'):
            if e.get('kind') in ('bull_trap', 'bear_trap'):
                traps.append(e)
            else:
                fresh.append(e)
    fresh.sort(key=lambda e: e.get('day', ''), reverse=True)
    traps.sort(key=lambda e: e.get('day', ''), reverse=True)

    table = {}
    for e in events:
        marks = e.get('marks') or {}
        if marks.get('d20') is None:
            continue
        cell = (e.get('pattern'), e.get('direction'), e.get('confirmation'))
        stats = table.setdefault(cell, {'n': 0, 'wins': 0, 'sum_ret': 0.0})
        stats['n'] += 1
        stats['wins'] += 1 if marks['d20'] > 0 else 0
        stats['sum_ret'] += marks['d20']
    rows = [{'pattern': k[0], 'direction': k[1], 'confirmation': k[2],
             'n': v['n'], 'win_rate': v['wins'] / v['n'],
             'mean_d20': v['sum_ret'] / v['n']} for k, v in sorted(table.items())]
    return {'asof': now, 'forming': forming, 'fresh': fresh[:50],
            'traps': traps[:25],
            'weekly_outcomes': rows,
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
