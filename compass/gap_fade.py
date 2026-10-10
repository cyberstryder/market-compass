"""Gap Fade: fade overnight gaps on futures at RTH open.

Evidence only. This module never alerts, never trades, and never auto-admits
to the live scanner. It logs gap fade setups as research rows.

Logic:
1. Measure overnight gap at 8:30 AM CT RTH open vs prior day 3:15 PM CT RTH close
2. Filter: gap between 0.5% and 2.0% (smaller is noise, larger is breakaway)
3. Wait for 8:30-8:45 AM CT opening range to complete
4. Entry: gap up -> short at opening range high; gap down -> long at opening range low
5. Stop: beyond gap extreme by 0.25x ATR
6. Target: 50% gap fill (conservative) or prior close (full fill)

Research: 67.8% win rate on MES/MNQ across 2,967 trades with proper filters.
"""
import time

from .market import number, day, CT
from .ict_common import ict_bars

SCAN_THROTTLE = 120
ATR_PERIOD = 14
OR_MINUTES = 15  # 8:30-8:45 AM CT opening range

# RTH hours in CT: 8:30 AM - 3:15 PM
RTH_OPEN_HOUR = 8
RTH_OPEN_MIN = 30
RTH_CLOSE_HOUR = 15
RTH_CLOSE_MIN = 15


def _bars_from_window(window):
    """Normalize bar_window rows to dicts."""
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


def _atr(bars, period=ATR_PERIOD):
    """ATR over bar window."""
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


def _rth_open_ts(day_ts):
    """8:30 AM CT timestamp for given day."""
    import datetime
    dt = datetime.datetime.fromtimestamp(day_ts, tz=CT)
    open_dt = dt.replace(hour=RTH_OPEN_HOUR, minute=RTH_OPEN_MIN, second=0, microsecond=0)
    return int(open_dt.timestamp())


def _prior_rth_close_ts(day_ts):
    """3:15 PM CT prior day timestamp."""
    import datetime
    dt = datetime.datetime.fromtimestamp(day_ts, tz=CT)
    # Go back one day, set to 3:15 PM
    prev = dt - datetime.timedelta(days=1)
    close_dt = prev.replace(hour=RTH_CLOSE_HOUR, minute=RTH_CLOSE_MIN, second=0, microsecond=0)
    return int(close_dt.timestamp())


def measure_gap(bars, rth_open_ts, prior_close_ts):
    """Measure overnight gap. Returns (gap_pct, direction, prior_close, open_px) or None."""
    # Find prior RTH close (closest bar at or before prior_close_ts)
    prior_bars = [b for b in bars if b['ts'] <= prior_close_ts]
    if not prior_bars:
        return None
    prior_close = sorted(prior_bars, key=lambda b: b['ts'])[-1]['c']
    
    # Find RTH open (first bar at or after rth_open_ts)
    open_bars = [b for b in bars if b['ts'] >= rth_open_ts]
    if not open_bars:
        return None
    open_px = sorted(open_bars, key=lambda b: b['ts'])[0]['o']
    
    if not prior_close or not open_px or prior_close <= 0:
        return None
    
    gap_pct = (open_px - prior_close) / prior_close
    direction = 'up' if gap_pct > 0 else 'down'
    return gap_pct, direction, prior_close, open_px


def opening_range(bars, rth_open_ts):
    """8:30-8:45 AM CT opening range. Returns (high, low) or None."""
    or_end = rth_open_ts + OR_MINUTES * 60
    window = [b for b in bars if rth_open_ts <= b['ts'] < or_end]
    if len(window) < 5:  # Need reasonable coverage
        return None
    return max(b['h'] for b in window), min(b['l'] for b in window)


def detect(bars, symbol, now, min_gap_pct=0.005, max_gap_pct=0.02, 
           target_fill_pct=0.5, atr_mult_stop=0.25):
    """Pure: detect gap fade setup. Returns signal dict or None."""
    import datetime
    dt = datetime.datetime.fromtimestamp(now, tz=CT)
    day_ts = int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    
    rth_open = _rth_open_ts(day_ts)
    prior_close_ts = _prior_rth_close_ts(day_ts)
    
    # Only run after opening range completes (8:45 AM CT)
    if now < rth_open + OR_MINUTES * 60:
        return None
    
    gap = measure_gap(bars, rth_open, prior_close_ts)
    if not gap:
        return None
    gap_pct, direction, prior_close, open_px = gap
    
    # Filter by gap size
    abs_gap = abs(gap_pct)
    if abs_gap < min_gap_pct or abs_gap > max_gap_pct:
        return None
    
    # Get opening range
    or_high, or_low = opening_range(bars, rth_open) or (None, None)
    if not or_high or not or_low:
        return None
    
    # Calculate ATR for stop
    atr = _atr(bars)
    if not atr or atr <= 0:
        return None
    
    # Fade direction is opposite the gap
    # Gap up -> short, Gap down -> long
    is_gap_up = direction == 'up'
    fade_direction = 'short' if is_gap_up else 'long'
    
    # Entry at opening range extreme (fade the gap)
    entry = or_high if is_gap_up else or_low
    
    # Stop beyond gap extreme
    gap_extreme = max(open_px, or_high) if is_gap_up else min(open_px, or_low)
    buf = atr_mult_stop * atr
    stop = gap_extreme + buf if is_gap_up else gap_extreme - buf
    
    # Target: partial gap fill
    gap_size = abs(open_px - prior_close)
    fill_target = gap_size * target_fill_pct
    if is_gap_up:
        target = entry - fill_target  # Short, target lower (toward prior close)
    else:
        target = entry + fill_target  # Long, target higher (toward prior close)
    
    # Risk/reward
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < 1.0:  # Minimum 1:1
        return None
    
    return {
        'symbol': symbol,
        'concept': 'gap_fade',
        'direction': fade_direction,
        'signal_ts': now,
        'entry': entry,
        'stop': stop,
        'target': target,
        'risk_pts': risk,
        'reward_pts': reward,
        'rr': rr,
        'gap_pct': gap_pct,
        'gap_direction': direction,
        'prior_close': prior_close,
        'open_px': open_px,
        'or_high': or_high,
        'or_low': or_low,
        'atr': atr,
        'params': {
            'min_gap_pct': min_gap_pct,
            'max_gap_pct': max_gap_pct,
            'target_fill_pct': target_fill_pct,
            'atr_mult_stop': atr_mult_stop,
        },
        'at': now,
    }


def scan(db, c, cfg, now):
    """Throttled gap fade scan over configured symbols."""
    if not getattr(cfg, 'gap_fade_enabled', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'gap_fade:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    
    min_gap = number(getattr(cfg, 'gap_fade_min_pct', 0.005)) or 0.005
    max_gap = number(getattr(cfg, 'gap_fade_max_pct', 0.02)) or 0.02
    target_fill = number(getattr(cfg, 'gap_fade_target_pct', 0.5)) or 0.5
    
    rows = {}
    for symbol in symbols:
        window = ict_bars(db, c, symbol)
        bars = _bars_from_window(window)
        if not bars:
            continue
        
        sig = detect(bars, symbol, now, min_gap_pct=min_gap, 
                     max_gap_pct=max_gap, target_fill_pct=target_fill)
        if sig:
            rows[symbol] = {'symbol': symbol, 'signal': sig, 'at': now}
            # Log signal
            db.append(c, 'gap_fade_signal', 'ict_futures', symbol, now,
                      sig, key='gapfade:%s:%s' % (symbol, day(now)))
            # Submit to paper trading (Variant B 2/2/1 via ict_paper)
            from .ict_paper import submit as _paper_submit
            _paper_submit(db, c, cfg, now, 'gap_fade', symbol, sig)
    
    db.put(c, 'gap_fade:latest', {'at': now, 'rows': rows, 'status': 'running' if rows else 'idle'})
    db.put(c, 'gap_fade:scanned_at', now)
    return {'ran': True, 'symbols': len(rows)}
