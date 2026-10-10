"""VWAP Fade: mean reversion to VWAP from 2-3 SD extremes.

Evidence only. This module never alerts, never trades, and never auto-admits
to the live scanner. It logs VWAP fade setups as research rows.

Logic (from r/Daytrading research):
1. Anchor VWAP to 8:30 AM CT RTH open
2. Calculate rolling SD bands at +/-2.0 and +/-3.0 SD
3. Entry: price touches 2SD band -> fade it (short above, long below)
4. Stop: beyond 3SD band
5. Target: VWAP line itself (mean reversion)

"Set and forget" - no active management. Works best in range-bound markets
where price stretches from VWAP and snaps back.
"""
import time
import math

from .market import number, day, CT
from .ict_common import ict_bars

SCAN_THROTTLE = 120

# Session open times in CT (hour, minute)
SESSION_OPENS = {
    'ny': (8, 30),      # 8:30 AM CT (RTH open)
    'london': (2, 0),   # 2:00 AM CT (London open)
    'asian': (19, 0),   # 7:00 PM CT (Tokyo open, prior day)
}

# RTH open in CT (default for NY session)
RTH_OPEN_HOUR = 8
RTH_OPEN_MIN = 30


def _bars_from_window(window):
    """Normalize bar_window rows to dicts with volume."""
    out = []
    for row in window or []:
        try:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
            v = row[5] if len(row) > 5 else 1.0
        except (IndexError, TypeError):
            continue
        if None in (ts, o, h, l, c):
            continue
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': number(v) or 1.0})
    return out


def _session_open_ts(day_ts, session='ny'):
    """Session open timestamp for given day. Session: 'ny', 'london', 'asian'."""
    import datetime
    hour, minute = SESSION_OPENS.get(session, SESSION_OPENS['ny'])
    dt = datetime.datetime.fromtimestamp(day_ts, tz=CT)
    # Asian session starts prior day evening
    if session == 'asian':
        dt = dt - datetime.timedelta(days=1)
    open_dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(open_dt.timestamp())


def _rth_open_ts(day_ts):
    """8:30 AM CT timestamp for given day. (Legacy, use _session_open_ts)"""
    return _session_open_ts(day_ts, 'ny')


def calculate_vwap(bars, anchor_ts):
    """Calculate anchored VWAP from anchor_ts. Returns (vwap, cumulative data) or None."""
    anchored = [b for b in bars if b['ts'] >= anchor_ts]
    if not anchored:
        return None
    
    cum_pv = 0.0
    cum_v = 0.0
    for b in anchored:
        # Use typical price: (h + l + c) / 3
        tp = (b['h'] + b['l'] + b['c']) / 3.0
        v = b['v']
        cum_pv += tp * v
        cum_v += v
    
    if cum_v <= 0:
        return None
    return cum_pv / cum_v


def calculate_sd(bars, anchor_ts, vwap, lookback=20):
    """Calculate rolling SD of price displacement from VWAP."""
    anchored = [b for b in bars if b['ts'] >= anchor_ts]
    if len(anchored) < lookback:
        return None
    
    recent = anchored[-lookback:]
    displacements = [(b['c'] - vwap) for b in recent]
    mean_disp = sum(displacements) / len(displacements)
    variance = sum((d - mean_disp) ** 2 for d in displacements) / len(displacements)
    return math.sqrt(variance) if variance > 0 else None


def detect(bars, symbol, now, sd_entry=2.0, sd_stop=3.0, min_rr=1.5, session='ny'):
    """Pure: detect VWAP fade setup. Returns signal dict or None."""
    import datetime
    dt = datetime.datetime.fromtimestamp(now, tz=CT)
    
    # Session time filter (only for NY session, others run 23h)
    if session == 'ny':
        # Only during RTH (8:30 AM - 3:15 PM CT)
        if dt.hour < 8 or (dt.hour == 8 and dt.minute < 30):
            return None
        if dt.hour > 15 or (dt.hour == 15 and dt.minute > 15):
            return None
    
    day_ts = int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    anchor = _session_open_ts(day_ts, session)
    
    # Need bars after anchor
    if now < anchor + 30 * 60:  # Wait 30 min after open for VWAP to stabilize
        return None
    
    vwap = calculate_vwap(bars, anchor)
    if not vwap:
        return None
    
    sd = calculate_sd(bars, anchor, vwap)
    if not sd or sd <= 0:
        return None
    
    # Get latest bar
    if not bars:
        return None
    latest = max(bars, key=lambda b: b['ts'])
    price = latest['c']
    
    # Check if price is at 2SD extreme
    upper_2sd = vwap + sd_entry * sd
    lower_2sd = vwap - sd_entry * sd
    upper_3sd = vwap + sd_stop * sd
    lower_3sd = vwap - sd_stop * sd
    
    direction = None
    entry = None
    stop = None
    
    if price >= upper_2sd:
        # Above VWAP + 2SD -> short (fade)
        direction = 'short'
        entry = price
        stop = upper_3sd  # Beyond 3SD
        target = vwap
    elif price <= lower_2sd:
        # Below VWAP - 2SD -> long (fade)
        direction = 'long'
        entry = price
        stop = lower_3sd
        target = vwap
    else:
        return None  # Not at extreme
    
    # Risk/reward check
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < min_rr:
        return None
    
    return {
        'symbol': symbol,
        'concept': 'vwap_fade',
        'direction': direction,
        'signal_ts': latest['ts'],
        'entry': entry,
        'stop': stop,
        'target': target,
        'risk_pts': risk,
        'reward_pts': reward,
        'rr': rr,
        'vwap': vwap,
        'sd': sd,
        'sd_entry': sd_entry,
        'sd_stop': sd_stop,
        'displacement_sd': abs(price - vwap) / sd if sd > 0 else 0,
        'params': {
            'sd_entry': sd_entry,
            'sd_stop': sd_stop,
            'min_rr': min_rr,
            'anchor': 'rth_open',
        },
        'at': now,
    }


def scan(db, c, cfg, now):
    """Throttled VWAP fade scan over configured symbols and all sessions."""
    if not getattr(cfg, 'vwap_fade_enabled', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'vwap_fade:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    
    sd_entry = number(getattr(cfg, 'vwap_fade_sd_entry', 2.0)) or 2.0
    sd_stop = number(getattr(cfg, 'vwap_fade_sd_stop', 3.0)) or 3.0
    # Run all three sessions simultaneously for comparison
    # (vwap_fade_session config is deprecated, kept for backwards compat)
    sessions = ['ny', 'london', 'asian']
    
    rows = {}
    for session in sessions:
        for symbol in symbols:
            window = ict_bars(db, c, symbol)
            bars = _bars_from_window(window)
            if not bars:
                continue
            
            sig = detect(bars, symbol, now, sd_entry=sd_entry, sd_stop=sd_stop, session=session)
            if sig:
                # Tag with session for separate tracking
                sig['session'] = session
                detector_name = 'vwap_fade_%s' % session
                key = '%s:%s:%s' % (symbol, session, day(now))
                rows[key] = {'symbol': symbol, 'session': session, 'signal': sig, 'at': now}
                db.append(c, 'vwap_fade_signal', 'ict_futures', symbol, now,
                          sig, key='vwapfade:%s' % key)
                from .ict_paper import submit as _paper_submit
                _paper_submit(db, c, cfg, now, detector_name, symbol, sig)
    
    db.put(c, 'vwap_fade:latest', {'at': now, 'rows': rows, 'status': 'running' if rows else 'idle'})
    db.put(c, 'vwap_fade:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'sessions': sessions}
