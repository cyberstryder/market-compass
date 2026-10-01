"""Mechanical ticker read: a structured directional read from Compass's own data.

Evidence only. No entries, no targets, no stops, no trade structures, no
sizing. Given a ticker symbol, this assembles what Compass already collects:

- session stats + spot (minute bars and the latest quote)
- intraday structure: swing highs/lows, session VWAP, spot vs VWAP,
  the largest-volume bar of the session, trend classification
- apex magnets: nearest support/resistance levels with scores and signals
- gamma flip: level, regime, and spot distance (TraderMatrix GEX matrices)
- tape: confirmed-trial directional flow summary (long vs short)
- gap: today's gap-continuation board status

...into mechanical factors and a net directional lean. Every factor is a
deterministic rule over stored data; the lean is a pure count of bullish
minus bearish factors. Missing data degrades to neutral/no_data factors,
never to invented values.
"""

import time

from .market import number, day
from . import apex_magnet, tape_confirmed, gap_continuation

SWING_LOOKBACK = 2   # bars each side for a fractal swing
VWAP_TOLERANCE = 0.001  # 0.10% band counts as "at VWAP"


def _bars(window):
    """Normalize bar_window rows ([ts,o,h,l,c,v,vw?]) to dicts, ascending."""
    out = []
    for row in window or []:
        try:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        except (IndexError, TypeError):
            continue
        if None in (ts, o, h, l, c):
            continue
        v = None
        try:
            v = row[5]
        except (IndexError, TypeError):
            v = None
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c,
                    'v': number(v)})
    return sorted(out, key=lambda b: b['ts'])


def _session_bars(bars, now):
    """Bars stamped on today's exchange date."""
    today = day(now)
    return [b for b in bars if day(b['ts']) == today]


def _vwap(bars):
    """Session VWAP from typical price * volume. None without volume."""
    num = den = 0.0
    for b in bars:
        v = b.get('v')
        if v is None or v <= 0:
            continue
        tp = (b['h'] + b['l'] + b['c']) / 3.0
        num += tp * v
        den += v
    return num / den if den > 0 else None


def _swings(bars, n=SWING_LOOKBACK):
    """Fractal swing highs/lows: extreme vs n bars on each side."""
    highs, lows = [], []
    for i in range(n, len(bars) - n):
        h, l = bars[i]['h'], bars[i]['l']
        if all(h >= bars[j]['h'] for j in range(i - n, i + n + 1) if j != i):
            highs.append({'ts': bars[i]['ts'], 'price': h})
        if all(l <= bars[j]['l'] for j in range(i - n, i + n + 1) if j != i):
            lows.append({'ts': bars[i]['ts'], 'price': l})
    return highs, lows


def _trend(highs, lows):
    """Classify from the last three swings each side."""
    if len(highs) < 3 or len(lows) < 3:
        return 'insufficient_data'
    hh = all(highs[-i]['price'] > highs[-i - 1]['price'] for i in (1, 2))
    hl = all(lows[-i]['price'] > lows[-i - 1]['price'] for i in (1, 2))
    lh = all(highs[-i]['price'] < highs[-i - 1]['price'] for i in (1, 2))
    ll = all(lows[-i]['price'] < lows[-i - 1]['price'] for i in (1, 2))
    if hh and hl:
        return 'uptrend'
    if lh and ll:
        return 'downtrend'
    return 'mixed'


def _largest_volume_bar(bars):
    """The session's highest-volume bar with range and vs-average multiple."""
    vols = [b for b in bars if b.get('v')]
    if not vols:
        return None
    avg = sum(b['v'] for b in vols) / len(vols)
    big = max(vols, key=lambda b: b['v'])
    return {'ts': big['ts'], 'volume': big['v'],
            'vs_avg': round(big['v'] / avg, 1) if avg else None,
            'range': round(big['h'] - big['l'], 2),
            'direction': 'up' if big['c'] >= big['o'] else 'down'}


def _spot(db, c, symbol, bars):
    """Latest quote mid; falls back to the last bar close."""
    q = db.get(c, 'quote:' + symbol, {}) or {}
    bid, ask = number(q.get('bid')), number(q.get('ask'))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0, q.get('ts')
    if bars:
        return bars[-1]['c'], bars[-1]['ts']
    return None, None


def _apex_section(display, symbol, spot):
    rows = [r for r in (display.get('rows') or [])
            if str(r.get('symbol') or '').upper() == symbol]
    if not rows or spot is None:
        return {'nearest_resistance': None, 'nearest_support': None,
                'signal': None, 'has_data': bool(rows)}
    above = sorted((r for r in rows if number(r.get('magnet')) is not None
                    and r['magnet'] > spot), key=lambda r: r['magnet'])
    below = sorted((r for r in rows if number(r.get('magnet')) is not None
                    and r['magnet'] < spot), key=lambda r: r['magnet'],
                   reverse=True)

    def slim(r):
        return {'magnet': r['magnet'], 'score': r.get('score'),
                'signal': r.get('signal'), 'role': r.get('role'),
                'distance_pct': r.get('distance_pct')}

    return {'nearest_resistance': slim(above[0]) if above else None,
            'nearest_support': slim(below[0]) if below else None,
            'signal': rows[0].get('signal'), 'has_data': True}


def _gamma_section(db, c, symbol, spot):
    g = db.get(c, 'gamma_flip:' + symbol, {}) or {}
    flip = number(g.get('flip'))
    if flip is None or spot is None:
        return {'flip': flip, 'regime': None,
                'distance_pct': None, 'has_data': flip is not None}
    regime = 'positive' if spot > flip else 'negative' if spot < flip else 'at_flip'
    return {'flip': flip, 'regime': regime,
            'distance_pct': round((spot - flip) / flip, 4),
            'has_data': True}


def _tape_section(display, symbol):
    recent = [r for r in (display.get('recent_confirmed') or [])
              if str(r.get('symbol') or '').upper() == symbol]
    long_t = [r for r in recent if str(r.get('side') or '').lower() == 'long']
    short_t = [r for r in recent if str(r.get('side') or '').lower() == 'short']
    call_p = sum(number(r.get('call_premium')) or 0 for r in recent)
    put_p = sum(number(r.get('put_premium')) or 0 for r in recent)
    scores = [number(r.get('max_score')) for r in recent]
    scores = [s for s in scores if s is not None]
    return {'long_trials': len(long_t), 'short_trials': len(short_t),
            'call_premium': round(call_p, 2), 'put_premium': round(put_p, 2),
            'max_score': max(scores) if scores else None,
            'has_data': bool(recent)}


def _gap_section(display, symbol):
    states = [s for s in (display.get('board') or [])
              if str(s.get('symbol') or '').upper() == symbol]
    st = states[0] if states else None
    if not st:
        return {'on_board': False, 'gap_pct': None, 'direction': None,
                'qualified': False}
    return {'on_board': True,
            'gap_pct': st.get('gap_pct'),
            'direction': st.get('gap_direction') or st.get('direction'),
            'qualified': bool(st.get('qualified')),
            'break_direction': st.get('break_direction')}


def _factors(session, structure, apex, gamma, tape, gap, spot):
    """Mechanical factor rules. Notes describe what IS, never what to DO."""
    f = []

    def add(name, direction, note):
        f.append({'factor': name, 'direction': direction, 'note': note})

    # --- session position vs VWAP ---
    vwap = structure.get('vwap')
    if vwap is None or spot is None:
        add('price_vs_vwap', 'neutral', 'No VWAP or spot available.')
    else:
        dev = (spot - vwap) / vwap
        if abs(dev) <= VWAP_TOLERANCE:
            add('price_vs_vwap', 'neutral',
                'Spot $%.2f is at session VWAP $%.2f.' % (spot, vwap))
        elif dev > 0:
            add('price_vs_vwap', 'bullish',
                'Spot $%.2f is above session VWAP $%.2f (+%.2f%%).' %
                (spot, vwap, dev * 100))
        else:
            add('price_vs_vwap', 'bearish',
                'Spot $%.2f is below session VWAP $%.2f (%.2f%%).' %
                (spot, vwap, dev * 100))

    # --- trend structure ---
    trend = structure.get('trend')
    if trend == 'uptrend':
        add('trend_structure', 'bullish',
            'Higher highs and higher lows across recent swings.')
    elif trend == 'downtrend':
        add('trend_structure', 'bearish',
            'Lower highs and lower lows across recent swings.')
    elif trend == 'mixed':
        add('trend_structure', 'neutral', 'Swing structure is mixed.')
    else:
        add('trend_structure', 'neutral',
            'Insufficient swing data for trend classification.')

    # --- largest volume bar ---
    lvb = structure.get('largest_volume_bar')
    if not lvb:
        add('volume_bar', 'neutral', 'No volume data this session.')
    else:
        d = 'bearish' if lvb['direction'] == 'down' else 'bullish'
        add('volume_bar', d,
            'Largest-volume bar is %s: %.1f× average volume, $%.2f range.' %
            (lvb['direction'], lvb['vs_avg'] or 0, lvb['range']))

    # --- apex magnets ---
    if not apex.get('has_data'):
        add('apex_magnets', 'neutral', 'No magnet levels for this symbol.')
    else:
        res, sup = apex.get('nearest_resistance'), apex.get('nearest_support')
        sig = apex.get('signal')
        if sig == 'broke_through' and res:
            add('apex_magnets', 'bearish',
                'Price broke through $%.2f resistance (score %s).' %
                (res['magnet'], res.get('score')))
        elif sig == 'broke_through' and sup:
            add('apex_magnets', 'bullish',
                'Price broke through $%.2f support (score %s).' %
                (sup['magnet'], sup.get('score')))
        elif res and sup:
            add('apex_magnets', 'neutral',
                'Between $%.2f support and $%.2f resistance.' %
                (sup['magnet'], res['magnet']))
        elif res:
            add('apex_magnets', 'bearish',
                'Nearest magnet $%.2f is resistance (score %s).' %
                (res['magnet'], res.get('score')))
        elif sup:
            add('apex_magnets', 'bullish',
                'Nearest magnet $%.2f is support (score %s).' %
                (sup['magnet'], sup.get('score')))
        else:
            add('apex_magnets', 'neutral', 'Magnets present but none bracket spot.')

    # --- gamma regime ---
    if not gamma.get('has_data') or gamma.get('regime') is None:
        add('gamma_regime', 'neutral', 'No gamma flip level for this symbol.')
    elif gamma['regime'] == 'positive':
        add('gamma_regime', 'bullish',
            'Spot is above the $%.2f gamma flip: positive-gamma regime, '
            'dealer hedging dampens moves.' % gamma['flip'])
    elif gamma['regime'] == 'negative':
        add('gamma_regime', 'bearish',
            'Spot is below the $%.2f gamma flip: negative-gamma regime, '
            'dealer hedging amplifies moves.' % gamma['flip'])
    else:
        add('gamma_regime', 'neutral',
            'Spot is at the $%.2f gamma flip.' % gamma['flip'])

    # --- tape ---
    if not tape.get('has_data'):
        add('tape_flow', 'neutral', 'No tape-confirmed trials for this symbol.')
    else:
        lp, sp = tape['long_trials'], tape['short_trials']
        if lp > sp:
            add('tape_flow', 'bullish',
                '%d long vs %d short confirmed trial(s); $%s call / $%s put premium.' %
                (lp, sp, _m(tape['call_premium']), _m(tape['put_premium'])))
        elif sp > lp:
            add('tape_flow', 'bearish',
                '%d short vs %d long confirmed trial(s); $%s put / $%s call premium.' %
                (sp, lp, _m(tape['put_premium']), _m(tape['call_premium'])))
        else:
            add('tape_flow', 'neutral',
                'Balanced confirmed flow: %d long, %d short trial(s).' % (lp, sp))

    # --- gap ---
    if not gap.get('on_board'):
        add('gap', 'neutral', 'Not on today\'s gap board.')
    else:
        gp = gap.get('gap_pct')
        pct = ('%.2f%%' % (gp * 100)) if gp is not None else 'n/a'
        if gap.get('qualified'):
            d = 'bullish' if (gp or 0) > 0 else 'bearish'
            add('gap', d, 'Qualified %s gap %s with opening-range break and hold.' %
                (gap.get('direction') or '', pct))
        else:
            add('gap', 'neutral', '%s gap %s, not qualified.' %
                (gap.get('direction') or 'Unclassified', pct))

    return f


def _m(x):
    """Compact money: 2604093 -> 2.6M."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return '0'
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if abs(x) >= div:
            return '%.1f%s' % (x / div, suf)
    return '%.0f' % x


def _lean(factors):
    score = sum(1 for x in factors if x['direction'] == 'bullish') - \
        sum(1 for x in factors if x['direction'] == 'bearish')
    if score >= 3:
        label = 'strongly_bullish'
    elif score >= 1:
        label = 'leaning_bullish'
    elif score <= -3:
        label = 'strongly_bearish'
    elif score <= -1:
        label = 'leaning_bearish'
    else:
        label = 'mixed'
    return {'score': score, 'label': label}


def read(db, symbol, now=None):
    """Assemble the mechanical ticker read. Never raises on missing data."""
    now = now if now is not None else time.time()
    symbol = str(symbol or '').strip().upper()
    out = {'symbol': symbol, 'asof': now,
           'note': 'Research evidence only. Mechanical factor assembly; '
                   'no entries, no targets, no trade signal.'}
    if not symbol:
        out['error'] = 'symbol required'
        return out
    try:
        with db.tx() as c:
            return _read_inner(db, c, symbol, now, out)
    except Exception as exc:  # never crash the endpoint on a bad row
        out['error'] = 'read failed: %s' % type(exc).__name__
        out['factors'] = []
        out['lean'] = {'score': 0, 'label': 'mixed'}
        return out


def _read_inner(db, c, symbol, now, out):
    bars = _bars(db.get(c, 'bar_window:' + symbol, []))
    sbars = _session_bars(bars, now)

    spot, spot_ts = _spot(db, c, symbol, sbars or bars)

    # --- session stats ---
    if sbars:
        o, h = sbars[0]['o'], max(b['h'] for b in sbars)
        l, cl = min(b['l'] for b in sbars), sbars[-1]['c']
        session = {'date': day(now), 'open': o, 'high': h, 'low': l,
                   'bars': len(sbars),
                   'change_pct': round((cl - o) / o * 100, 2) if o else None}
    else:
        session = {'date': day(now), 'open': None, 'high': None,
                   'low': None, 'bars': 0, 'change_pct': None}

    # --- structure ---
    vwap = _vwap(sbars) if sbars else None
    highs, lows = _swings(sbars) if len(sbars) >= 2 * SWING_LOOKBACK + 1 else ([], [])
    structure = {
        'vwap': round(vwap, 2) if vwap is not None else None,
        'vs_vwap': ('above' if spot is not None and vwap is not None and
                    spot > vwap * (1 + VWAP_TOLERANCE)
                    else 'below' if spot is not None and vwap is not None and
                    spot < vwap * (1 - VWAP_TOLERANCE)
                    else 'at' if vwap is not None else None),
        'swing_highs': [{'ts': s['ts'], 'price': round(s['price'], 2)}
                        for s in highs[-5:]],
        'swing_lows': [{'ts': s['ts'], 'price': round(s['price'], 2)}
                       for s in lows[-5:]],
        'trend': _trend(highs, lows),
        'largest_volume_bar': _largest_volume_bar(sbars),
    }

    # --- evidence sections (each degrades gracefully) ---
    try:
        apex_d = apex_magnet.display(db, c, now)
    except Exception:
        apex_d = {}
    try:
        tape_d = tape_confirmed.display(db, c, now, days=2)
    except Exception:
        tape_d = {}
    try:
        gap_d = gap_continuation.display(db, c, now)
    except Exception:
        gap_d = {}

    apex = _apex_section(apex_d, symbol, spot)
    gamma = _gamma_section(db, c, symbol, spot)
    tape = _tape_section(tape_d, symbol)
    gap = _gap_section(gap_d, symbol)

    factors = _factors(session, structure, apex, gamma, tape, gap, spot)

    out.update({
        'spot': round(spot, 2) if spot is not None else None,
        'spot_ts': spot_ts,
        'session': session,
        'structure': structure,
        'apex': apex,
        'gamma': gamma,
        'tape': tape,
        'gap': gap,
        'factors': factors,
        'lean': _lean(factors),
    })
    return out
