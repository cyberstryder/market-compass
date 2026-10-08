"""Central trailing-stop manager for ICT paper positions.

The Runner: when a position reaches 1R profit, activate a 2x ATR trailing stop
from the extreme. This lets winners run (like the 115-pt NQ overnight breakdown)
instead of exiting at fixed 2R targets.

Applies to all ICT detectors centrally — no per-detector changes needed.
"""
from .market import number
from .ict_common import ict_bars, atr

TRAIL_ATR_MULT = 2.0
ACTIVATE_R_MULT = 1.0  # Activate trail at 1R profit

def _bars_from_recent(rows):
    out = []
    for r in rows or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'], 'l': p['l'], 'c': p['c']})
    return sorted(out, key=lambda b: b['ts'])

def manage_all(db, c, now):
    """Apply trailing stops to open ICT trailer legs (2/2/1 scale-out).

    Only positions with is_trailer=True are trailed. The t1 (2R) and t2 (3R)
    legs use fixed targets managed by the engine's exits() loop.
    """
    try:
        positions = db.prefix(c, 'position:ict:')
    except Exception:
        return {'ran': False, 'reason': 'db_error'}
    
    trailed = 0
    for pos_key, pos in positions.items():
        if not isinstance(pos, dict) or pos.get('status') != 'open':
            continue
        # Only trail the runner leg (1 contract); t1/t2 use fixed targets
        if not pos.get('is_trailer'):
            continue
        
        # Skip if trail already active
        if pos.get('trail_active'):
            _update_trail(db, c, pos_key, pos, now)
            trailed += 1
            continue
        
        # Check if we should activate the trail (1R profit reached)
        entry = number(pos.get('entry'))
        stop = number(pos.get('stop'))
        if not entry or not stop:
            continue
        
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        
        # Get current price from quote
        symbol = pos.get('symbol')
        if not symbol:
            # Extract from pos_key: position:ict:<detector>:<symbol>
            parts = pos_key.split(':')
            if len(parts) >= 4:
                symbol = ':'.join(parts[3:])
        
        if not symbol:
            continue
        
        q = db.get(c, 'quote:' + symbol, {}) or {}
        curr_price = number(q.get('bid')) or number(q.get('ask')) or number(q.get('mid'))
        if not curr_price:
            continue
        
        side = pos.get('side')
        if side == 'long':
            profit_r = (curr_price - entry) / risk
        elif side == 'short':
            profit_r = (entry - curr_price) / risk
        else:
            continue
        
        # Activate trail at 1R
        if profit_r >= ACTIVATE_R_MULT:
            pos['trail_active'] = True
            pos['trail_activated_at'] = now
            pos['trail_activated_r'] = round(profit_r, 2)
            # Initialize extreme
            bars = ict_bars(db, c, symbol)
            if not bars:
                try:
                    bars = _bars_from_recent(db.recent(c, 'bar', symbol, limit=100))
                except Exception:
                    bars = []
            if bars:
                if side == 'long':
                    pos['highest_high'] = max(b['h'] for b in bars[-10:])
                else:
                    pos['lowest_low'] = min(b['l'] for b in bars[-10:])
            db.put(c, pos_key, pos)
            _update_trail(db, c, pos_key, pos, now)
            trailed += 1
    
    return {'ran': True, 'trailed': trailed, 'at': now}

def _update_trail(db, c, pos_key, pos, now):
    """Update the trailing stop for a position with active trail."""
    symbol = pos.get('symbol')
    if not symbol:
        parts = pos_key.split(':')
        if len(parts) >= 4:
            symbol = ':'.join(parts[3:])
    if not symbol:
        return
    
    bars = ict_bars(db, c, symbol)
    if not bars:
        try:
            bars = _bars_from_recent(db.recent(c, 'bar', symbol, limit=100))
        except Exception:
            return
    if len(bars) < 15:
        return
    
    # Calculate ATR
    try:
        atr_v = atr(bars)
    except Exception:
        return
    if not atr_v or atr_v <= 0:
        return
    
    side = pos.get('side')
    last_bar = bars[-1]
    
    if side == 'long':
        # Update highest high
        new_high = max(pos.get('highest_high') or last_bar['h'], last_bar['h'])
        pos['highest_high'] = new_high
        new_stop = new_high - TRAIL_ATR_MULT * atr_v
        # Only tighten, never loosen. Also never below breakeven once activated.
        entry = number(pos.get('entry')) or 0
        new_stop = max(new_stop, entry)  # Breakeven floor
        if new_stop > number(pos.get('stop') or 0):
            pos['stop'] = new_stop
            pos['trail_updated_at'] = now
            db.put(c, pos_key, pos)
    elif side == 'short':
        new_low = min(pos.get('lowest_low') or last_bar['l'], last_bar['l'])
        pos['lowest_low'] = new_low
        new_stop = new_low + TRAIL_ATR_MULT * atr_v
        entry = number(pos.get('entry')) or 0
        new_stop = min(new_stop, entry)  # Breakeven ceiling
        if new_stop < number(pos.get('stop') or float('inf')):
            pos['stop'] = new_stop
            pos['trail_updated_at'] = now
            db.put(c, pos_key, pos)
