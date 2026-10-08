"""MU scalp paper trading — underlying price tracking.

Josh 2026-10-08: MU gap fade + spike fade signals are on MU stock price moves.
The generic zero_dte_paper.py trades option contracts via chain lookup, but MU
alerts have no contract (and MU often has no 0DTE expiry). This module tracks
MU scalps as underlying paper trades in the 0DTE category.

Each trade:
- Entry: signal entry price (MU stock)
- Target: signal target (50% retrace for spike, prior close for gap)
- Stop: signal stop (0.3% beyond extreme for spike, 1% for gap)
- Max hold: 15 minutes for spike fade, 11am ET for gap fade
- Size: 100 shares equivalent (1 contract)

P&L in underlying points × 100.
"""

from .market import number


def open_trade(db, c, signal, now):
    """Open a MU paper trade from a signal. Returns trade key or None."""
    try:
        dedupe = signal.get('dedupe_key')
        if not dedupe:
            return None
        key = 'trade:mu-%s' % dedupe
        if db.get(c, key):
            return None  # already open
        
        # Determine max hold
        detector = signal.get('detector', '')
        if 'spike' in detector:
            max_hold = 15 * 60  # 15 minutes
        else:
            # Gap fade: flat by 11am ET — compute seconds until 11am ET
            # For simplicity, use 2.5 hours from 8:30am ET entry
            max_hold = 2.5 * 3600
        
        trade = {
            'id': key,
            'kind': 'mu_scalp',
            'category': 'options_0dte',  # tracked in 0DTE category per Josh
            'symbol': signal.get('symbol', 'MU'),
            'detector': detector,
            'direction': signal.get('direction'),
            'entry': signal.get('entry'),
            'stop': signal.get('stop'),
            'target': signal.get('target'),
            'qty': 100,  # 100 shares = 1 contract equivalent
            'entered_at': now,
            'max_hold_sec': max_hold,
            'expires_at': now + max_hold,
            'status': 'open',
            'signal': signal.get('signal'),
            'gap_pct': signal.get('gap_pct'),
            'spike_pct': signal.get('spike_pct'),
        }
        db.put(c, key, trade)
        return key
    except Exception:
        return None


def manage_trades(db, c, now):
    """Check open MU trades for target/stop/time exits. Returns list of closed trades."""
    closed = []
    try:
        for k in list(db.prefix(c, 'trade:mu-').keys()):
            trade = db.get(c, k)
            if not trade or trade.get('status') != 'open':
                continue
            
            symbol = trade.get('symbol', 'MU')
            # Get current price from quote
            q = db.get(c, 'quote:' + symbol)
            if not q:
                continue
            price = number(q.get('bid')) or number(q.get('ask')) or number(q.get('price'))
            if not price:
                continue
            
            direction = trade.get('direction')
            entry = number(trade.get('entry'))
            stop = number(trade.get('stop'))
            target = number(trade.get('target'))
            
            exit_reason = None
            exit_price = price
            
            # Check target/stop
            if direction == 'long':
                if target and price >= target:
                    exit_reason = 'target'
                elif stop and price <= stop:
                    exit_reason = 'stop'
            elif direction == 'short':
                if target and price <= target:
                    exit_reason = 'target'
                elif stop and price >= stop:
                    exit_reason = 'stop'
            
            # Check max hold
            if not exit_reason and now >= (trade.get('expires_at') or 0):
                exit_reason = 'time'
            
            if exit_reason:
                # Calculate P&L in underlying points × qty
                if direction == 'long':
                    pnl_points = exit_price - entry
                else:
                    pnl_points = entry - exit_price
                pnl = pnl_points * trade.get('qty', 100)
                
                trade['status'] = 'closed'
                trade['exit_reason'] = exit_reason
                trade['exit_price'] = exit_price
                trade['exited_at'] = now
                trade['pnl'] = round(pnl, 2)
                trade['pnl_points'] = round(pnl_points, 4)
                db.put(c, k, trade)
                closed.append(trade)
    except Exception:
        pass
    return closed


def get_open_trades(db, c):
    """Return list of open MU paper trades."""
    result = []
    try:
        for k in db.prefix(c, 'trade:mu-').keys():
            trade = db.get(c, k)
            if trade and trade.get('status') == 'open':
                result.append(trade)
    except Exception:
        pass
    return result


def get_closed_trades(db, c, limit=100):
    """Return list of closed MU paper trades, most recent first."""
    result = []
    try:
        for k in db.prefix(c, 'trade:mu-').keys():
            trade = db.get(c, k)
            if trade and trade.get('status') == 'closed':
                result.append(trade)
        result.sort(key=lambda t: t.get('exited_at', 0), reverse=True)
    except Exception:
        pass
    return result[:limit]
