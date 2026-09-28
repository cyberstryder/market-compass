"""SMT divergence: correlated-pair divergence at session extremes.

Evidence only. This module never alerts, never trades, and never auto-admits
to the live scanner. For each configured pair (A, B): if one leg takes out its
prior-session extreme by >= sweep_ticks while the other leg's extreme stays
below its own prior extreme (within an ATR tolerance), and the divergent leg
prints a reversal bar within ICT_SMT_CONFIRM_BARS bars, that is divergence:
bearish at highs, bullish at lows. Detection is symmetric in the legs; the
leg that fails to confirm is the divergent leg.
"""
from .market import number, day, CT  # noqa: F401  (CT kept for session-math convention)
from .ict_common import ict_bars

SCAN_THROTTLE = 120
TICK_FALLBACK_DEFAULT = 0.25
CONFIRM_BARS_DEFAULT = 3
ATR_FRAC_SWEEP = 0.1
ATR_FRAC_TOL = 0.05
TAKEOUT_LOOKBACK = 20
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
    """ATR over the bar window; None when too few bars."""
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


def _check_extreme(conf_bars, div_bars, prior_conf, prior_div,
                   sweep_conf, sweep_div, tol_div, side,
                   conf_label, div_label, confirm_bars, takeout_lookback):
    """One ordered divergence check.

    The confirming leg takes its prior extreme (high -> bearish, low ->
    bullish) while the divergent leg fails to take its own. Requires a
    reversal bar in the divergent leg within `confirm_bars` of the window
    end and at/after the takeout bar. Returns the event dict or None.
    """
    if not conf_bars or not div_bars or side not in ('high', 'low'):
        return None
    prior_conf_px = number((prior_conf or {}).get(side))
    opp = 'low' if side == 'high' else 'high'
    prior_div_px = number((prior_div or {}).get(side))
    prior_div_opp = number((prior_div or {}).get(opp))
    if not prior_conf_px or not prior_div_px:
        return None
    prior_conf, prior_div = prior_conf_px, prior_div_px
    bearish = side == 'high'
    # Most recent takeout bar in the confirming leg.
    take_idx = None
    for i, b in enumerate(conf_bars):
        hit = (b['h'] >= prior_conf + sweep_conf) if bearish \
            else (b['l'] <= prior_conf - sweep_conf)
        if hit:
            take_idx = i
    if take_idx is None:
        return None
    if take_idx < len(conf_bars) - takeout_lookback:
        return None  # stale takeout, not a live divergence
    # The divergent leg fails to take its own extreme.
    if bearish:
        div_extreme = max(b['h'] for b in div_bars)
        failed = div_extreme < prior_div + tol_div
        takeout_price = conf_bars[take_idx]['h']
    else:
        div_extreme = min(b['l'] for b in div_bars)
        failed = div_extreme > prior_div - tol_div
        takeout_price = conf_bars[take_idx]['l']
    if not failed:
        return None
    # Reversal bar in the divergent leg, within the last confirm_bars bars.
    take_ts = conf_bars[take_idx]['ts']
    rev = None
    for b in div_bars[-confirm_bars:]:
        is_rev = (b['c'] < b['o']) if bearish else (b['c'] > b['o'])
        if is_rev and b['ts'] >= take_ts:
            rev = b  # keep the latest qualifying bar
    if rev is None:
        return None
    direction = 'short' if bearish else 'long'
    entry = rev['c']
    if bearish:
        stop = rev['h'] + sweep_div
        risk = stop - entry
        if prior_div_opp and prior_div_opp < entry:
            target, target_kind = prior_div_opp, 'prior_session_low'
        else:
            target, target_kind = entry - 2 * risk, 'two_r'
        reward = entry - target
    else:
        stop = rev['l'] - sweep_div
        risk = entry - stop
        if prior_div_opp and prior_div_opp > entry:
            target, target_kind = prior_div_opp, 'prior_session_high'
        else:
            target, target_kind = entry + 2 * risk, 'two_r'
        reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    return {
        'direction': direction,
        'confirming_leg': conf_label, 'divergent_leg': div_label,
        'takeout_ts': take_ts, 'takeout_price': takeout_price,
        'reversal_ts': rev['ts'], 'signal_ts': rev['ts'],
        'divergent_extreme': div_extreme,
        'level_price': prior_conf,
        'level_kind': 'prior_session_high' if bearish else 'prior_session_low',
        'entry': entry, 'stop': stop, 'target': target,
        'target_kind': target_kind,
        'risk_pts': risk, 'reward_pts': reward, 'rr': reward / risk,
        'sweep_ticks': sweep_conf, 'div_sweep_ticks': sweep_div,
        'tol_pts': tol_div,
        'params': {'confirm_bars': confirm_bars,
                   'takeout_lookback': takeout_lookback},
    }


def detect_divergence(bars_a, bars_b, prior_a, prior_b, label_a='A', label_b='B',
                      confirm_bars=CONFIRM_BARS_DEFAULT,
                      tick_fallback=TICK_FALLBACK_DEFAULT,
                      atr_frac_sweep=ATR_FRAC_SWEEP, atr_frac_tol=ATR_FRAC_TOL,
                      takeout_lookback=TAKEOUT_LOOKBACK):
    """Detect SMT divergence between two legs.

    prior_a / prior_b are {'high': .., 'low': ..} prior-session extremes.
    Checks both extremes (high -> bearish, low -> bullish) and both leg
    orders; the leg that fails to confirm is the divergent leg. Returns a
    list of event dicts (possibly empty), most naturally one.
    """
    events = []
    if not bars_a or not bars_b:
        return events
    try:
        confirm_bars = int(number(confirm_bars) or CONFIRM_BARS_DEFAULT)
    except (TypeError, ValueError):
        confirm_bars = CONFIRM_BARS_DEFAULT
    tick_fallback = number(tick_fallback) or TICK_FALLBACK_DEFAULT
    atr_a, atr_b = _atr(bars_a), _atr(bars_b)
    sweep_a = max(tick_fallback, atr_frac_sweep * atr_a) if atr_a else tick_fallback
    sweep_b = max(tick_fallback, atr_frac_sweep * atr_b) if atr_b else tick_fallback
    tol_a = atr_frac_tol * atr_a if atr_a else 0.0
    tol_b = atr_frac_tol * atr_b if atr_b else 0.0
    for side in ('high', 'low'):
        for (cb, db_, pc, pd, sc, sd, td, cl, dl) in (
                (bars_a, bars_b, prior_a, prior_b, sweep_a, sweep_b, tol_b,
                 label_a, label_b),
                (bars_b, bars_a, prior_b, prior_a, sweep_b, sweep_a, tol_a,
                 label_b, label_a)):
            ev = _check_extreme(cb, db_, pc, pd, sc, sd, td, side, cl, dl,
                                confirm_bars, takeout_lookback)
            if ev:
                events.append(ev)
    return events


def _prior_extremes(levels):
    """Latest complete {symbol: {'high','low'}} from session-liquidity rows.

    Row shape per spec: {symbol, session, high, low, high_ts, low_ts,
    complete, ...}. Defensive: skips rows without numeric extremes.
    """
    prior = {}
    for row in levels or []:
        if not isinstance(row, dict):
            continue
        sym = row.get('symbol')
        hi, lo = number(row.get('high')), number(row.get('low'))
        if not sym or not hi or not lo:
            continue
        if not row.get('complete'):
            continue
        ts = number(row.get('high_ts')) or number(row.get('low_ts')) or 0
        cur = prior.get(sym)
        if cur is None or ts >= cur.get('ts', 0):
            prior[sym] = {'high': hi, 'low': lo, 'ts': ts,
                          'session': row.get('session')}
    return prior


def scan(db, c, cfg, now):
    """Throttled SMT scan over configured correlated pairs."""
    if not getattr(cfg, 'ict_smt_divergence', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_smt_divergence:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    pairs = getattr(cfg, 'ict_smt_pairs', ()) or ()
    try:
        confirm_bars = int(number(getattr(cfg, 'ict_smt_confirm_bars',
                                          CONFIRM_BARS_DEFAULT)) or CONFIRM_BARS_DEFAULT)
    except (TypeError, ValueError):
        confirm_bars = CONFIRM_BARS_DEFAULT
    tick_fb = number(getattr(cfg, 'ict_tick_fallback',
                             TICK_FALLBACK_DEFAULT)) or TICK_FALLBACK_DEFAULT
    # Defensive: the session-liquidity snapshot may be absent.
    liq = db.get(c, 'ict_session_liquidity:latest', {}) or {}
    prior = _prior_extremes(liq.get('levels') or [])
    rows, excluded = {}, {}
    today = day(now)
    n_signals = 0
    for pair in pairs:
        parts = str(pair).split(':')
        if len(parts) != 2 or not all(p.strip() for p in parts):
            excluded['bad_pair'] = excluded.get('bad_pair', 0) + 1
            rows[str(pair)] = {'pair': str(pair), 'excluded': 'bad_pair',
                               'at': now}
            continue
        a, b = parts[0].strip(), parts[1].strip()
        bars_a = (ict_bars(db, c, a)
                  or _bars_from_recent(db.recent(c, 'bar', a, limit=500)))
        bars_b = (ict_bars(db, c, b)
                  or _bars_from_recent(db.recent(c, 'bar', b, limit=500)))
        if not bars_a or not bars_b:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            rows[pair] = {'pair': pair, 'excluded': 'no_bars', 'at': now,
                          'legs': {'a': a, 'b': b,
                                   'a_bars': len(bars_a), 'b_bars': len(bars_b)}}
            continue
        if a not in prior or b not in prior:
            excluded['no_levels'] = excluded.get('no_levels', 0) + 1
            rows[pair] = {'pair': pair, 'excluded': 'no_levels', 'at': now,
                          'legs': {'a': a, 'b': b}}
            continue
        events = detect_divergence(bars_a, bars_b, prior[a], prior[b],
                                   label_a=a, label_b=b,
                                   confirm_bars=confirm_bars,
                                   tick_fallback=tick_fb)
        if not events:
            excluded['no_divergence'] = excluded.get('no_divergence', 0) + 1
            rows[pair] = {'pair': pair, 'excluded': 'no_divergence', 'at': now,
                          'legs': {'a': a, 'b': b}}
            continue
        best = max(events, key=lambda e: e['signal_ts'])
        row = {'pair': pair, 'symbol': pair, 'concept': 'smt_divergence',
               'at': now, 'params': {'confirm_bars': confirm_bars,
                                     'tick_fallback': tick_fb},
               **best}
        rows[pair] = row
        # Idempotent per pair/day/signal_ts (db key is per pair+date).
        state = db.get(c, 'ict_smt_divergence:signal:' + pair, {}) or {}
        if state.get('date') != today or state.get('signal_ts') != best['signal_ts']:
            payload = {k: v for k, v in row.items() if k != 'at'}
            db.append(c, 'ict_smt_signal', 'ict_futures', pair, now,
                      payload, key='ictsmt:%s:%s' % (pair, today))
            db.put(c, 'ict_smt_divergence:signal:' + pair,
                   {'signal_ts': best['signal_ts'], 'date': today,
                    'direction': best['direction'],
                    'divergent_leg': best['divergent_leg'], 'at': now})
            n_signals += 1
    db.put(c, 'ict_smt_divergence:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'params': {'confirm_bars': confirm_bars, 'tick_fallback': tick_fb},
            'status': 'running' if rows else 'idle'})
    db.put(c, 'ict_smt_divergence:scanned_at', now)
    return {'ran': True, 'pairs': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent divergence signals."""
    latest = db.get(c, 'ict_smt_divergence:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'ict_smt_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
