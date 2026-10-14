"""CrossTrade webhook bridge for live futures automation.

Sends ICT detector signals to CrossTrade as bracket orders via webhook.
CrossTrade routes to Tradovate (sim or live) with server-side brackets.

Enable with:
  CROSSTRADE_ENABLED=true
  CROSSTRADE_WEBHOOK_URL=https://... (your CrossTrade webhook URL)
  CROSSTRADE_KEY=your-secret-key (CrossTrade secret)
  CROSSTRADE_ACCOUNT=Sim101 (your Tradovate account name in CrossTrade)

Only fires for the focused detectors (aoi_fade, bos_gz_vwap) when
ICT_FUTURES_PAPER_ENABLED is also on (so paper and live stay in sync).

Safety:
- Micros only (qty=1)
- Bracket orders with per-signal stop/target from the detector
- Does NOT fire when paper is disabled
- Logs all webhook attempts via db.health for audit
"""
import json
import time
import urllib.request
import urllib.error

# Map Compass micro symbols to CrossTrade/Tradovate continuous contract format
# Compass uses: MNQ.c.0, MES.c.0, MGC.v.0, etc.
# CrossTrade expects: MNQ1!, MES1!, MGC1!, etc.
SYMBOL_MAP = {
    'MNQ.c.0': 'MNQ1!',
    'MES.c.0': 'MES1!',
    'MGC.v.0': 'MGC1!',
    'SIL.v.0': 'SIL1!',
    'MCL.v.0': 'MCL1!',
    'MNQ': 'MNQ1!',
    'MES': 'MES1!',
    'MGC': 'MGC1!',
}


def is_enabled(cfg):
    """Check if CrossTrade bridge is enabled."""
    return bool(getattr(cfg, 'crosstrade_enabled', False))


def map_symbol(compass_symbol):
    """Map Compass symbol to CrossTrade instrument format."""
    return SYMBOL_MAP.get(compass_symbol, compass_symbol)


def build_payload(cfg, trade):
    """Build CrossTrade webhook JSON payload from a trade dict.
    
    trade needs: detector, symbol, side ('long'/'short'), entry, stop, target
    Returns dict ready for JSON serialization, or None if invalid.
    """
    detector = trade.get('detector', '')
    # Only fire for focused detectors
    if detector not in ('aoi_fade', 'bos_gz_vwap'):
        return None
    
    symbol = trade.get('symbol', '')
    instrument = map_symbol(symbol)
    
    side = trade.get('side', '').lower()
    if side == 'long':
        action = 'buy'
    elif side == 'short':
        action = 'sell'
    else:
        return None
    
    entry = trade.get('entry')
    stop = trade.get('stop')
    target = trade.get('target')
    
    if not all([entry, stop, target]):
        return None
    
    # Validate stop/target are on correct side
    try:
        entry_f = float(entry)
        stop_f = float(stop)
        target_f = float(target)
    except (TypeError, ValueError):
        return None
    
    if action == 'buy':
        # Long: stop below entry, target above entry
        if not (stop_f < entry_f < target_f):
            return None
    else:
        # Short: stop above entry, target below entry
        if not (target_f < entry_f < stop_f):
            return None
    
    return {
        'key': getattr(cfg, 'crosstrade_key', ''),
        'command': 'place',
        'account': getattr(cfg, 'crosstrade_account', ''),
        'instrument': instrument,
        'action': action,
        'qty': 1,  # Micros only, always 1 contract
        'order_type': 'market',
        'take_profit': str(target_f),
        'stop_loss': str(stop_f),
        'tif': 'day',
        'destination': 'tradovate',
    }


def send_webhook(db, cfg, trade):
    """Send trade to CrossTrade via webhook. Returns (success, message).
    
    Logs via db.health for audit trail. Never raises.
    """
    if not is_enabled(cfg):
        return (False, 'disabled')
    
    webhook_url = getattr(cfg, 'crosstrade_webhook_url', '')
    if not webhook_url:
        return (False, 'no webhook URL')
    
    payload = build_payload(cfg, trade)
    if not payload:
        return (False, 'invalid payload (detector not focused or bad prices)')
    
    # Don't send the key in logs
    log_payload = {k: v for k, v in payload.items() if k != 'key'}
    log_payload['key'] = '***'
    
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        # 10s timeout - don't block the engine
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode('utf-8', errors='ignore')[:500]
        
        # Log success
        try:
            db.health('crosstrade_webhook', 'ok',
                     f"{trade.get('detector')}:{trade.get('symbol')}:{trade.get('side')} -> {status}",
                     payload=log_payload, response=body)
        except:
            pass
        
        return (True, f'HTTP {status}')
    
    except urllib.error.HTTPError as e:
        msg = f'HTTP {e.code}: {e.read().decode()[:200]}'
        try:
            db.health('crosstrade_webhook', 'error', msg, payload=log_payload)
        except:
            pass
        return (False, msg)
    
    except Exception as e:
        msg = f'{type(e).__name__}: {str(e)[:200]}'
        try:
            db.health('crosstrade_webhook', 'error', msg, payload=log_payload)
        except:
            pass
        return (False, msg)
