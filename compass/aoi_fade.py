"""AOI 50/20 Fade: mean-reversion fade of wicks into HTF zones.

A port of the "AOI 50/20 Fade" rule specification (friend-shared PDF):
when price probes a higher-timeframe area of interest on the 1-hour chart
and closes back through it, fade the probe with a fixed point target and
a structural stop buffered by 1H ATR(14).

Zones (built from history strictly before the current day, no lookahead):
  1. 4H fractal swing highs/lows (k=2), last 10 each. Single-price zones.
  2. Unmitigated 1H fair value gaps. Range zones [gap_bottom, gap_top].
  3. Fibonacci retracements (0.382/0.5/0.618/0.786) of the most recent 4H
     swing leg. Single-price zones.

Signal (on 1H bars):
  Bullish (fade long): bar low <= bullish zone top AND bar close > zone top.
  Bearish (fade short): bar high >= bearish zone bottom AND bar close < zone bottom.

Levels:
  Long:  entry = zone top, stop = zone bottom - 1.0*ATR(14),
         target = entry + points.
  Short: entry = zone bottom, stop = zone top + 1.0*ATR(14),
         target = entry - points.
  Points: MNQ 50, MES 20, MGC 10 (micro equivalents of the spec's
  NQ 50 / ES 20 / GC 10; dollar values are 1/10th on micros).

Adaptations for prop-firm intraday operation:
  - Entries go through ict_paper.submit(), which enforces the market-hours
    gate (no entries when closed) and the 15:45 CT session flatten.
  - The spec's 30-day trade resolution window is NOT implemented; positions
    are managed by the engine exits() loop (intraday stops/targets + flatten).
  - Micros only. MNQ/MES/MGC use the spec's point targets; SIL/MCL targets
    derived from contract specs (see POINT_TARGETS).
  - Sessions use CT, not PT.

Evidence only. This module never alerts, never trades live.
"""
import time
from datetime import datetime, timedelta

from .market import number, CT
from .ict_common import ict_bars

SCAN_THROTTLE = 120
ATR_LEN = 14

# Micro point targets from the spec (NQ 50 / ES 20 / GC 10).
# SIL/MCL derived: MCL 1.0pt = $100 (~1x 1H ATR, dollar-equivalent).
# SIL 0.40pt = $400 (~1x 1H ATR); a $100-equivalent 0.10pt target would be
# ~0.25x ATR and break the strategy's risk:reward, so SIL is sized by
# volatility, not dollar-equivalence. SIL's contract (1,000 oz, $1,000/pt)
# is 1/5th of SI, not a 1/10th micro.
POINT_TARGETS = {
    'MNQ.c.0': 50.0,
    'MES.c.0': 20.0,
    'MGC.v.0': 10.0,
    'SIL.v.0': 0.40,
    'MCL.v.0': 1.0,
}

FIB_LEVELS = (0.382, 0.5, 0.618, 0.786)

# Sessions for the one-signal-per-(zone, session, day) filter, CT.
SESSION_DEFS = {
    'asia': ('17:00', '01:00'),    # 15:00-00:00 PT
    'europe': ('01:00', '07:30'),  # 00:00-06:30 PT
    'new_york': ('07:30', '14:00'),  # 06:30-13:00 PT
    'midday': ('14:00', '16:00'),  # 13:00-15:00 PT
}


def _bars_from_window(window):
    out = []
    for row in window or []:
        try:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        except (IndexError, TypeError):
            continue
        if None in (ts, o, h, l, c):
            continue
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c})
    return sorted(out, key=lambda b: b['ts'])


def _resample(bars, seconds):
    """Resample ascending 1m bars to `seconds`-second bars."""
    if not bars:
        return []
    out, cur = [], None
    for b in bars:
        bucket = int(b['ts'] // seconds) * seconds
        if cur is None or cur['ts'] != bucket:
            if cur is not None:
                out.append(cur)
            cur = {'ts': bucket, 'o': b['o'], 'h': b['h'],
                   'l': b['l'], 'c': b['c']}
        else:
            cur['h'] = max(cur['h'], b['h'])
            cur['l'] = min(cur['l'], b['l'])
            cur['c'] = b['c']
    if cur is not None:
        out.append(cur)
    return out


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


def _atr(bars, length=ATR_LEN):
    """ATR(length) over the last `length` bars."""
    if len(bars) < length + 1:
        return None
    vals = _true_ranges(bars[-length - 1:])
    nums = [number(v) for v in vals]
    if any(v is None or v < 0 for v in nums):
        return None
    return sum(nums) / len(nums)


def find_fractal_swings(bars, k=2, keep=10):
    """4H fractal swings: bar i is a swing low if low[i] is the lowest of
    low[i-k..i+k]; swing high is the mirror. Returns (swing_lows, swing_highs),
    each a list of prices, most recent last, capped at `keep`."""
    lows, highs = [], []
    for i in range(k, len(bars) - k):
        window_l = [bars[j]['l'] for j in range(i - k, i + k + 1)]
        window_h = [bars[j]['h'] for j in range(i - k, i + k + 1)]
        if bars[i]['l'] == min(window_l):
            lows.append(bars[i]['l'])
        if bars[i]['h'] == max(window_h):
            highs.append(bars[i]['h'])
    return lows[-keep:], highs[-keep:]


def find_fvgs(bars):
    """Unmitigated 1H fair value gaps.

    Bullish FVG at bar i: low[i-1] > high[i+1], bar i+1 closed bullish.
    Zone = [high[i+1], low[i-1]] (bottom, top). Unmitigated: no later bar's
    low traded at or below the gap top (gap not filled).
    Bearish is the mirror.
    Returns list of {'bias': 'long'/'short', 'top', 'bottom', 'formed_ts'}.
    """
    zones = []
    n = len(bars)
    for i in range(1, n - 1):
        # Bullish FVG
        if bars[i - 1]['l'] > bars[i + 1]['h'] and bars[i + 1]['c'] > bars[i + 1]['o']:
            top = bars[i - 1]['l']
            bottom = bars[i + 1]['h']
            mitigated = any(b['l'] <= top for b in bars[i + 2:])
            if not mitigated:
                zones.append({'bias': 'long', 'top': top, 'bottom': bottom,
                              'zone_kind': 'bullish_fvg',
                              'formed_ts': bars[i]['ts']})
        # Bearish FVG
        if bars[i - 1]['h'] < bars[i + 1]['l'] and bars[i + 1]['c'] < bars[i + 1]['o']:
            top = bars[i - 1]['h']
            bottom = bars[i + 1]['l']
            mitigated = any(b['h'] >= bottom for b in bars[i + 2:])
            if not mitigated:
                zones.append({'bias': 'short', 'top': top, 'bottom': bottom,
                              'zone_kind': 'bearish_fvg',
                              'formed_ts': bars[i]['ts']})
    return zones


def fib_zones(bars_4h):
    """Fib retracement zones from the most recent 4H swing leg.

    From the most recent 4H swing low, find the highest 4H high since; if
    higher, add support zones at 0.382/0.5/0.618/0.786 (fade long).
    From the most recent 4H swing high, find the lowest 4H low since; if
    lower, add resistance zones (fade short). Single-price zones.
    """
    zones = []
    if len(bars_4h) < 5:
        return zones
    swing_lows, swing_highs = find_fractal_swings(bars_4h, keep=1)
    if swing_lows:
        sl = swing_lows[-1]
        # Highest high since the swing low
        sl_idx = next((i for i in range(len(bars_4h) - 1, -1, -1)
                       if bars_4h[i]['l'] == sl), None)
        if sl_idx is not None:
            hh = max(b['h'] for b in bars_4h[sl_idx:])
            if hh > sl:
                rng = hh - sl
                for f in FIB_LEVELS:
                    px = hh - f * rng
                    zones.append({'bias': 'long', 'top': px, 'bottom': px,
                                  'zone_kind': 'fib_support',
                                  'formed_ts': bars_4h[-1]['ts']})
    if swing_highs:
        sh = swing_highs[-1]
        sh_idx = next((i for i in range(len(bars_4h) - 1, -1, -1)
                       if bars_4h[i]['h'] == sh), None)
        if sh_idx is not None:
            ll = min(b['l'] for b in bars_4h[sh_idx:])
            if ll < sh:
                rng = sh - ll
                for f in FIB_LEVELS:
                    px = ll + f * rng
                    zones.append({'bias': 'short', 'top': px, 'bottom': px,
                                  'zone_kind': 'fib_resistance',
                                  'formed_ts': bars_4h[-1]['ts']})
    return zones


def build_zones(bars_1m, now):
    """Build all AOI zones from history strictly before today (CT), no lookahead."""
    day_start = datetime.fromtimestamp(now, CT).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    hist = [b for b in bars_1m if b['ts'] < day_start]
    if len(hist) < 60:
        return []
    bars_1h = _resample(hist, 3600)
    bars_4h = _resample(hist, 14400)
    zones = []
    # 1. Fractal swings (single-price zones)
    swing_lows, swing_highs = find_fractal_swings(bars_4h)
    for px in swing_lows:
        zones.append({'bias': 'long', 'top': px, 'bottom': px,
                      'zone_kind': 'swing_low', 'formed_ts': day_start})
    for px in swing_highs:
        zones.append({'bias': 'short', 'top': px, 'bottom': px,
                      'zone_kind': 'swing_high', 'formed_ts': day_start})
    # 2. Unmitigated FVGs
    zones.extend(find_fvgs(bars_1h))
    # 3. Fib retracements
    zones.extend(fib_zones(bars_4h))
    return zones


def _session_for(ts):
    """Session name for a CT timestamp."""
    dt = datetime.fromtimestamp(ts, CT)
    hm = dt.strftime('%H:%M')
    for name, (s, e) in SESSION_DEFS.items():
        if s <= e:
            if s <= hm < e:
                return name
        else:  # overnight
            if hm >= s or hm < e:
                return name
    return 'unknown'


def check_signal(bar, zones, atr):
    """Check one 1H bar against zones. Returns signal dict or None.

    Bullish: bar low <= zone top AND bar close > zone top -> long.
    Bearish: bar high >= zone bottom AND bar close < zone bottom -> short.
    """
    if atr is None or atr <= 0:
        return None
    for z in zones:
        top, bottom = number(z['top']), number(z['bottom'])
        if top is None or bottom is None:
            continue
        if z['bias'] == 'long':
            if bar['l'] <= top and bar['c'] > top:
                return {'direction': 'long', 'zone': z,
                        'entry': top,
                        'stop': bottom - 1.0 * atr,
                        'atr': atr}
        elif z['bias'] == 'short':
            if bar['h'] >= bottom and bar['c'] < bottom:
                return {'direction': 'short', 'zone': z,
                        'entry': bottom,
                        'stop': top + 1.0 * atr,
                        'atr': atr}
    return None


def _trade_levels(sig, points):
    """Add fixed point target to a signal. Returns None if invalid."""
    direction = sig['direction']
    entry = number(sig['entry'])
    stop = number(sig['stop'])
    if not entry or not stop:
        return None
    if direction == 'long':
        if stop >= entry:
            return None
        target = entry + points
    else:
        if stop <= entry:
            return None
        target = entry - points
    return {'direction': direction, 'entry': entry, 'stop': stop,
            'target': target, 'target_kind': 'fixed_points',
            'risk_pts': abs(entry - stop), 'reward_pts': points,
            'level_kind': sig['zone']['zone_kind'],
            'level_price': (sig['zone']['top'] + sig['zone']['bottom']) / 2.0}


def scan(db, c, cfg, now):
    """Throttled AOI fade scan over configured symbols."""
    if not getattr(cfg, 'ict_aoi_fade', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'ict_aoi_fade:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ())
               if s in POINT_TARGETS]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    day_iso = datetime.fromtimestamp(now, CT).date().isoformat()
    fired = 0
    for symbol in symbols:
        window = ict_bars(db, c, symbol)
        bars = _bars_from_window(window)
        if len(bars) < 120:
            continue
        zones = build_zones(bars, now)
        if not zones:
            continue
        bars_1h = _resample(bars, 3600)
        if len(bars_1h) < 15:
            continue
        atr = _atr(bars_1h)
        # Check today's 1H bars (completed bars only, skip the forming bar)
        day_start = datetime.fromtimestamp(now, CT).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_bars = [b for b in bars_1h
                      if b['ts'] >= day_start and b['ts'] + 3600 <= now]
        for bar in today_bars:
            sig = check_signal(bar, zones, atr)
            if not sig:
                continue
            z = sig['zone']
            sess = _session_for(bar['ts'])
            # One signal per (zone, session, day)
            zkey = '%s:%.2f:%s' % (z['zone_kind'], z['top'], z['bias'])
            seen_key = 'ict_aoi_fade:fired:%s:%s:%s:%s' % (
                symbol, day_iso, sess, zkey)
            if db.get(c, seen_key):
                continue
            levels = _trade_levels(sig, POINT_TARGETS[symbol])
            if not levels:
                continue
            from .ict_paper import submit as _paper_submit
            res = _paper_submit(db, c, cfg, now, 'aoi_fade', symbol, levels)
            if res.get('submitted'):
                db.put(c, seen_key, bar['ts'])
                db.append(c, 'ict_aoi_fade_signal', 'ict_futures', symbol, now,
                          {k: v for k, v in levels.items()},
                          key='ictaof:%s:%d' % (symbol, int(bar['ts'])))
                fired += 1
    db.put(c, 'ict_aoi_fade:scanned_at', now)
    db.put(c, 'ict_aoi_fade:latest', {'at': now, 'fired': fired})
    return {'ran': True, 'fired': fired}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent signals."""
    latest = db.get(c, 'ict_aoi_fade:latest', {})
    signals = db.recent(c, 'ict_aoi_fade_signal', limit=50)
    return {'asof': latest.get('at'), 'fired': latest.get('fired', 0),
            'symbols': sorted(POINT_TARGETS),
            'point_targets': POINT_TARGETS,
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']}
                        for s in signals],
            'note': 'AOI 50/20 Fade (intraday variant). Research evidence only.'}
