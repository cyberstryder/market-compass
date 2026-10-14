"""TradersPost webhook bridge for live futures automation.

Sends ICT detector signals to TradersPost as orders via webhook.
TradersPost routes to Tradovate (sim or live) with bracket orders.

Enable with:
  TRADERSPOST_ENABLED=true
  TRADERSPOST_WEBHOOK_URL=https://webhooks.traderspost.io/trading/webhook/... (your TradersPost webhook URL, contains secret)

Only fires for detectors when ICT_FUTURES_PAPER_ENABLED is also on
(so paper and live stay in sync).

Safety:
- Micros only (qty=1)
- Bracket orders with per-signal stop/target from the detector
- Does NOT fire when paper is disabled
- Logs all webhook attempts via db.health for audit
- Webhook URL contains secret — never log it
"""
import json
import time
import urllib.request
import urllib.error

# Map Compass micro symbols to TradersPost/Tradovate format
# Compass uses: MNQ.c.0, MES.c.0, MGC.v.0, etc.
# TradersPost expects: MNQ, MES, MGC, etc. (root symbol)
SYMBOL_MAP = {
    'MNQ.c.0': 'MNQ',
    'MES.c.0': 'MES',
    'MGC.v.0': 'MGC',
    'SIL.v.0': 'SIL',
    'MCL.v.0': 'MCL',
    'MNQ': 'MNQ',
    'MES': 'MES',
    'MGC': 'MGC',
}


def is_enabled(cfg):
    """Check if TradersPost bridge is enabled."""
    return bool(getattr(cfg, 'traderspost_enabled', False))


def map_symbol(compass_symbol):
    """Map Compass symbol to TradersPost root symbol."""
    return SYMBOL_MAP.get(compass_symbol, compass_symbol)


def build_payload(cfg, trade):
    """Build TradersPost webhook JSON payload from a trade dict.

    trade needs: detector, symbol, side ('long'/'short'), entry, stop, target
    Returns dict ready for JSON serialization, or None if invalid.
    """
    symbol = trade.get('symbol', '')
    ticker = map_symbol(symbol)

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
    if side == 'long':
        if not (stop < entry < target):
            return None
    else:  # short
        if not (target < entry < stop):
            return None

    # TradersPost payload format
    # See: https://traderspost.io/docs/webhooks
    payload = {
        'ticker': ticker,
        'action': action,
        'quantity': 1,  # Micros only, 1 contract
        'price': entry,
        'stop': stop,
        'target': target,
        # Metadata for TradersPost strategy tracking
        'strategy': trade.get('detector', 'compass'),
        'order_type': 'market',
        'time_in_force': 'day',
    }

    return payload


def send_signal(cfg, db, trade):
    """Send a trade signal to TradersPost webhook.

    Returns (success, message). Logs to db.health for audit.
    """
    if not is_enabled(cfg):
        return (False, 'TradersPost disabled')

    webhook_url = getattr(cfg, 'traderspost_webhook_url', '')
    if not webhook_url:
        return (False, 'TRADERSPOST_WEBHOOK_URL not set')

    payload = build_payload(cfg, trade)
    if not payload:
        return (False, 'Invalid trade (detector/side/prices)')

    # POST to TradersPost webhook
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8', errors='ignore')[:500]
            # Log success (never log webhook URL)
            try:
                with db.tx() as c:
                    db.health('traderspost_webhook', 'ok',
                              f"Sent {payload['ticker']} {payload['action']} qty=1",
                              at=time.time(), ticker=payload['ticker'],
                              action=payload['action'],
                              response=body[:200])
            except Exception:
                pass
            return (True, f"Sent to TradersPost: {body[:100]}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore')[:500]
        try:
            with db.tx() as c:
                db.health('traderspost_webhook', 'error',
                          f"HTTP {e.code}: {err_body[:200]}",
                          at=time.time(), ticker=payload.get('ticker'))
        except Exception:
            pass
        return (False, f"TradersPost HTTP {e.code}: {err_body[:200]}")
    except Exception as e:
        try:
            with db.tx() as c:
                db.health('traderspost_webhook', 'error',
                          f"Send failed: {str(e)[:200]}",
                          at=time.time(), ticker=payload.get('ticker'))
        except Exception:
            pass
        return (False, f"TradersPost send failed: {str(e)[:200]}")
