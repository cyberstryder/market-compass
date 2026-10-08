"""MU intraday scalp detectors for 0DTE paper trading.

Two complementary systems (Josh 2026-10-08, validated on 1M bars):
1. Gap fade (premarket): MU gaps >2% overnight → fade at open, target VWAP,
   1% stop, flat by 11am ET. 73% historical fade rate, 9/9 big-gap days touched
   VWAP by 11am.
2. Morning spike fade (9:45-11:00 ET): ≥0.7% spike over ≤5 min on 1M bars →
   fade, 50% retrace target, 0.3% stop beyond extreme, 15-min max hold.
   55% win rate, PF 1.39 in backtest. Edge dies after 11am.

Both publish alerts with category "options_0dte" so the existing
zero_dte_paper.py picks them up for paper tracking in the 0DTE category.

Paper trades use underlying price moves (not option premium) for simplicity:
the signal is on MU stock, and Josh trades the 0DTE options manually.
P&L is tracked in underlying points × 100 (1 contract equivalent).
"""

from .market import number

# Gap fade params
GAP_THRESHOLD_PCT = 2.0  # alert when |gap| > 2%
GAP_STOP_PCT = 1.0       # 1% stop beyond entry
GAP_EXIT_HOUR_ET = 11    # flat by 11am ET

# Spike fade params
SPIKE_THRESHOLD_PCT = 0.7  # ≥0.7% move over ≤5 min
SPIKE_LOOKBACK_MIN = 5
SPIKE_TARGET_RETRACE = 0.5  # 50% retrace of spike
SPIKE_STOP_PCT = 0.3        # 0.3% beyond spike extreme
SPIKE_MAX_HOLD_MIN = 15
SPIKE_WINDOW_START_ET = "09:45"
SPIKE_WINDOW_END_ET = "11:00"


def _pct(a, b):
    """Percent change from b to a."""
    if not b:
        return 0.0
    return (a - b) / abs(b) * 100.0


def check_gap_fade(db, c, now, symbol="MU"):
    """Premarket gap check. Returns signal dict or None.
    
    Call once premarket (~8:45am ET). Compares premarket price to prior close.
    """
    # Get prior close and current premarket price
    # Prior close from daily bars, premarket from quote
    try:
        bars = db.prefix(c, 'bar:%s:' % symbol)
        if not bars:
            return None
        # Find the most recent daily bar close
        daily_close = None
        for k in sorted(bars.keys(), reverse=True):
            if '1d' in k or 'D' in k:
                daily_close = (bars[k].get('payload') or {}).get('c')
                if daily_close:
                    break
        if not daily_close:
            # Fallback: use the last 5-min bar close from prior day
            return None
        
        q = db.get(c, 'quote:' + symbol)
        if not q:
            return None
        premarket = number(q.get('bid')) or number(q.get('ask')) or number(q.get('price'))
        if not premarket:
            return None
        
        gap_pct = _pct(premarket, daily_close)
        if abs(gap_pct) < GAP_THRESHOLD_PCT:
            return None
        
        # Fade the gap: if gap up, short; if gap down, long
        direction = 'short' if gap_pct > 0 else 'long'
        # Target VWAP (we'll use prior close as proxy premarket; VWAP calculated intraday)
        # Stop 1% beyond entry
        entry = premarket
        stop_pct = GAP_STOP_PCT / 100.0
        if direction == 'short':
            stop = entry * (1 + stop_pct)
            target = daily_close  # fade back toward prior close (VWAP proxy)
        else:
            stop = entry * (1 - stop_pct)
            target = daily_close
        
        return {
            'detector': 'mu_gap_fade',
            'symbol': symbol,
            'direction': direction,
            'entry': entry,
            'stop': stop,
            'target': target,
            'gap_pct': round(gap_pct, 2),
            'signal': 'gap_fade',
            'ts': now,
        }
    except Exception:
        return None


def check_spike_fade(db, c, now, symbol="MU"):
    """Intraday spike fade check. Returns signal dict or None.
    
    Call every minute 9:45-11:00 ET. Looks for ≥0.7% spike over ≤5 min on 1M bars.
    """
    try:
        # Get 1M bars for the last 10 minutes
        bars = []
        for k, v in db.prefix(c, 'bar:%s:' % symbol).items():
            if ':1m:' in k or ':1M:' in k:
                payload = v.get('payload') or {}
                ts = v.get('ts')
                if ts and now - ts <= 600:  # last 10 min
                    bars.append({
                        'ts': ts,
                        'o': number(payload.get('o')),
                        'h': number(payload.get('h')),
                        'l': number(payload.get('l')),
                        'c': number(payload.get('c')),
                    })
        bars = sorted([b for b in bars if b['c']], key=lambda x: x['ts'])
        if len(bars) < SPIKE_LOOKBACK_MIN:
            return None
        
        # Check for spike in last 5 minutes
        recent = bars[-SPIKE_LOOKBACK_MIN:]
        start_price = recent[0]['o']
        end_price = recent[-1]['c']
        move_pct = _pct(end_price, start_price)
        
        if abs(move_pct) < SPIKE_THRESHOLD_PCT:
            return None
        
        # Fade the spike
        direction = 'short' if move_pct > 0 else 'long'
        extreme = max(b['h'] for b in recent) if move_pct > 0 else min(b['l'] for b in recent)
        entry = end_price
        
        # Target: 50% retrace of the spike
        retrace = abs(end_price - start_price) * SPIKE_TARGET_RETRACE
        if direction == 'short':
            target = entry - retrace
            stop = extreme * (1 + SPIKE_STOP_PCT / 100.0)
        else:
            target = entry + retrace
            stop = extreme * (1 - SPIKE_STOP_PCT / 100.0)
        
        return {
            'detector': 'mu_spike_fade',
            'symbol': symbol,
            'direction': direction,
            'entry': entry,
            'stop': stop,
            'target': target,
            'spike_pct': round(move_pct, 2),
            'signal': 'spike_fade',
            'ts': now,
        }
    except Exception:
        return None


def publish_alert(db, c, signal, now):
    """Publish MU scalp signal as 0DTE alert for paper tracking."""
    try:
        key = 'mu-%s-%d' % (signal['detector'], int(now))
        if db.get(c, 'alert:' + key):
            return None  # already alerted
        
        alert = {
            'symbol': signal['symbol'],
            'detector': signal['detector'],
            'direction': signal['direction'],
            'entry': signal['entry'],
            'stop': signal['stop'],
            'target': signal['target'],
            'signal': signal['signal'],
            'ts': now,
            'publication': {
                'category': 'options_0dte',
                'underlying': signal['symbol'],
                'status': 'MU_SCALP',
            },
        }
        db.put(c, 'alert:' + key, alert)
        db.append(c, 'alert', 'mu_scalp', signal['symbol'], now, alert, key=key)
        return key
    except Exception:
        return None
