"""Trend Rider: captures long trending moves with ATR trailing stops.

Unlike scalping detectors (20-50pt targets), the trend rider aims for the
multi-hundred-point streaks by using a trailing stop instead of a fixed target.
Enters on trend-regime confirmation + momentum break, trails by 2x ATR from
the extreme, flattens at 15:45 CT per the session rule.

Re-entry: if flattened at 15:45 CT but trend regime still valid next day,
the detector will signal re-entry (captures multi-day trends via daily re-entry,
no overnight risk).
"""
import time

from .market import number, day
from .ict_common import ict_bars, atr

SCAN_THROTTLE = 120
ATR_LEN = 14
TRAIL_ATR_MULT = 2.0

def _bars_from_recent(rows):
    out = []
    for r in rows or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'], 'l': p['l'], 'c': p['c']})
    return sorted(out, key=lambda b: b['ts'])

def _atr(bars, length=ATR_LEN):
    if len(bars) < length:
        return None
    trs = []
    prev_c = None
    for b in bars[-length:]:
        h, l, c = b['h'], b['l'], b['c']
        if prev_c is None:
            trs.append(h - l)
        else:
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    return sum(trs) / len(trs) if trs else None

def _manage_trailing(db, c, symbol, bars, now):
    """Update trailing stop on open trend_rider position."""
    pos_key = 'position:ict:trend_rider:%s' % symbol
    pos = db.get(c, pos_key, {}) or {}
    if pos.get('status') != 'open':
        return
    
    side = pos.get('side')
    atr_v = _atr(bars)
    if not atr_v:
        return
    
    # Track the extreme since entry
    extreme_key = 'highest_high' if side == 'long' else 'lowest_low'
    current_extreme = pos.get(extreme_key)
    
    last_bar = bars[-1]
    if side == 'long':
        new_extreme = max(current_extreme or last_bar['h'], last_bar['h'])
        new_stop = new_extreme - TRAIL_ATR_MULT * atr_v
        # Only tighten, never loosen
        if new_stop > pos.get('stop', 0):
            pos['stop'] = new_stop
            pos[extreme_key] = new_extreme
            pos['trail_updated_at'] = now
            db.put(c, pos_key, pos)
    else:
        new_extreme = min(current_extreme or last_bar['l'], last_bar['l'])
        new_stop = new_extreme + TRAIL_ATR_MULT * atr_v
        if new_stop < pos.get('stop', float('inf')):
            pos['stop'] = new_stop
            pos[extreme_key] = new_extreme
            pos['trail_updated_at'] = now
            db.put(c, pos_key, pos)

def scan(db, c, cfg, now):
    """Scan for trend-rider entries and manage trailing stops."""
    if not getattr(cfg, 'ict_trend_rider', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'ict_trend_rider:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    db.put(c, 'ict_trend_rider:scanned_at', now)
    
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ()) if s]
    if not symbols:
        return {'ran': False, 'reason': 'no_symbols'}
    
    from .regime import classify as _classify
    
    rows = {}
    for symbol in symbols:
        bars = ict_bars(db, c, symbol)
        if not bars:
            bars = _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))
        if len(bars) < 120:
            continue
        
        # Manage trailing stop on any open position first
        _manage_trailing(db, c, symbol, bars, now)
        
        # Check regime - only enter in strong trends
        reg = _classify(bars)
        regime = reg.get('regime')
        if regime not in ('trend_up', 'trend_down'):
            continue
        
        # Don't re-enter if already have a position
        pos_key = 'position:ict:trend_rider:%s' % symbol
        pos = db.get(c, pos_key, {}) or {}
        if pos.get('status') == 'open':
            continue
        
        # Entry trigger: momentum break of recent range in trend direction
        # Use 20-bar high/low break with ATR confirmation
        lookback = 20
        if len(bars) < lookback + 1:
            continue
        
        recent = bars[-lookback-1:-1]  # Exclude current forming bar
        curr = bars[-1]
        atr_v = _atr(bars)
        if not atr_v:
            continue
        
        if regime == 'trend_up':
            range_high = max(b['h'] for b in recent)
            # Break of range high with momentum (close > high + 0.25 ATR)
            if curr['c'] > range_high + 0.25 * atr_v:
                entry = curr['c']
                stop = entry - TRAIL_ATR_MULT * atr_v
                # Wide target - trailing stop will exit first (10R)
                risk = entry - stop
                target = entry + 10 * risk
                sig = {
                    'id': 'trend-rider-%s-%d' % (symbol.replace('.', '-'), int(now)),
                    'symbol': symbol, 'direction': 'long', 'side': 'long',
                    'entry': entry, 'stop': stop, 'target': target,
                    'signal_ts': now, 'signal_time': now,
                    'strategy': 'trend_rider', 'rule': 'momentum_break',
                    'regime': regime, 'adx': reg.get('adx'),
                    'atr': atr_v, 'trail_atr_mult': TRAIL_ATR_MULT,
                    'track': 'futures',
                }
                rows[symbol] = {'symbol': symbol, 'signals': [sig], 'at': now}
                from .ict_paper import submit as _paper_submit
                _paper_submit(db, c, cfg, now, 'trend_rider', symbol, sig)
        
        elif regime == 'trend_down':
            range_low = min(b['l'] for b in recent)
            if curr['c'] < range_low - 0.25 * atr_v:
                entry = curr['c']
                stop = entry + TRAIL_ATR_MULT * atr_v
                risk = stop - entry
                target = entry - 10 * risk
                sig = {
                    'id': 'trend-rider-%s-%d' % (symbol.replace('.', '-'), int(now)),
                    'symbol': symbol, 'direction': 'short', 'side': 'short',
                    'entry': entry, 'stop': stop, 'target': target,
                    'signal_ts': now, 'signal_time': now,
                    'strategy': 'trend_rider', 'rule': 'momentum_break',
                    'regime': regime, 'adx': reg.get('adx'),
                    'atr': atr_v, 'trail_atr_mult': TRAIL_ATR_MULT,
                    'track': 'futures',
                }
                rows[symbol] = {'symbol': symbol, 'signals': [sig], 'at': now}
                from .ict_paper import submit as _paper_submit
                _paper_submit(db, c, cfg, now, 'trend_rider', symbol, sig)
    
    return {'ran': True, 'symbols': list(rows.keys()), 'at': now}
