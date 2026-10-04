"""Push alerts for ICT futures detector paper entries.

Follows the magnet_push.py pattern: durable outbox, async Discord drainer.
Alerts fire when a detector opens a paper trade (via ict_paper.submit),
so Josh can see which detectors fire on which instruments at which times.

Enable with ICT_PUSH_ENABLED=true and DISCORD_ICT_WEBHOOK_URL.
"""
import asyncio
import re
import time

import httpx
from sqlalchemy import select

from .store import identity, ict_push_outbox

outbox = ict_push_outbox
MAX_ATTEMPTS = 10
WEBHOOK_RE = r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+'


def maybe_queue(db, c, cfg, trade):
    """Queue a Discord alert for a newly opened ICT paper trade.
    
    Returns the outbox row id, or None if push is disabled.
    """
    if not bool(getattr(cfg, 'ict_push_enabled', False)):
        return None
    
    detector = trade.get('detector', 'unknown')
    symbol = trade.get('symbol', '?')
    side = trade.get('side', '?').upper()
    entry = trade.get('entry', 0)
    stop = trade.get('stop', 0)
    target = trade.get('target', 0)
    strategy = trade.get('strategy', detector)
    
    # Risk/reward
    risk = abs(entry - stop) if entry and stop else 0
    reward = abs(target - entry) if target and entry else 0
    rr = reward / risk if risk > 0 else 0
    
    # Time in CT for readability
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ct = ZoneInfo('America/Chicago')
    entry_time = datetime.fromtimestamp(trade.get('entered_at', time.time()), ct)
    time_str = entry_time.strftime('%a %H:%M CT')
    
    row_id = identity('ict-push-v1', trade.get('id', ''), int(time.time()))
    payload = {
        'detector': detector,
        'strategy': strategy,
        'symbol': symbol,
        'side': side,
        'entry': entry,
        'stop': stop,
        'target': target,
        'rr': round(rr, 2),
        'qty': trade.get('qty', 1),
        'time_str': time_str,
        'at': time.time(),
    }
    c.execute(db.insert(outbox).values(
        id=row_id, status='pending', created=time.time(), payload=payload,
        delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return row_id


def format_message(payload):
    """Discord-safe alert for an ICT paper entry."""
    side_emoji = '🟢' if payload.get('side') == 'LONG' else '🔴'
    lines = [
        '%s **ICT %s** %s %s' % (
            side_emoji,
            payload.get('detector', 'unknown').replace('_', ' ').title(),
            payload.get('side', '?'),
            payload.get('symbol', '?')
        ),
        'Entry `%.2f` → Target `%.2f` | Stop `%.2f`' % (
            payload.get('entry', 0),
            payload.get('target', 0),
            payload.get('stop', 0),
        ),
        'R:R `%.2f` · Qty `%d` · %s' % (
            payload.get('rr', 0),
            payload.get('qty', 1),
            payload.get('time_str', ''),
        ),
        '_Paper trade — watch only_',
    ]
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'ICT Signals',
            'allowed_mentions': {'parse': []}}


def maybe_queue_exit(db, c, cfg, trade):
    """Queue a Discord alert for a closed ICT paper trade.
    
    Returns the outbox row id, or None if push is disabled.
    """
    if not bool(getattr(cfg, 'ict_push_enabled', False)):
        return None
    
    reason = trade.get('exit_reason', 'unknown')
    pnl = trade.get('pnl', 0)
    
    # Map exit reasons to display
    if 'target' in reason:
        emoji, result = '🎯', 'TARGET HIT'
    elif 'stop' in reason:
        emoji, result = '🛑', 'STOPPED OUT'
    elif 'flatten' in reason:
        emoji, result = '⏰', 'FLATTENED'
    else:
        emoji, result = '📊', reason.upper()
    
    pnl_str = '+$%.2f' % pnl if pnl >= 0 else '-$%.2f' % abs(pnl)
    
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ct = ZoneInfo('America/Chicago')
    exit_time = datetime.fromtimestamp(trade.get('exited_at', time.time()), ct)
    time_str = exit_time.strftime('%a %H:%M CT')
    
    row_id = identity('ict-push-v1', 'exit', trade.get('id', ''), int(time.time()))
    payload = {
        'type': 'exit',
        'detector': trade.get('detector', 'unknown'),
        'strategy': trade.get('strategy', ''),
        'symbol': trade.get('symbol', '?'),
        'side': trade.get('side', '?').upper(),
        'entry': trade.get('entry', 0),
        'exit': trade.get('exit', 0),
        'pnl': pnl,
        'pnl_str': pnl_str,
        'result': result,
        'emoji': emoji,
        'time_str': time_str,
        'at': time.time(),
    }
    c.execute(db.insert(outbox).values(
        id=row_id, status='pending', created=time.time(), payload=payload,
        delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return row_id


def format_exit_message(payload):
    """Discord-safe alert for an ICT paper exit."""
    lines = [
        '%s **ICT %s** %s %s — %s' % (
            payload.get('emoji', '📊'),
            payload.get('detector', 'unknown').replace('_', ' ').title(),
            payload.get('side', '?'),
            payload.get('symbol', '?'),
            payload.get('result', '?'),
        ),
        'Entry `%.2f` → Exit `%.2f`' % (
            payload.get('entry', 0),
            payload.get('exit', 0),
        ),
        'P&L **%s** · %s' % (
            payload.get('pnl_str', '$0.00'),
            payload.get('time_str', ''),
        ),
        '_Paper trade — watch only_',
    ]
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'ICT Signals',
            'allowed_mentions': {'parse': []}}


def deliver_one(db, client, url, now=None, max_attempts=MAX_ATTEMPTS):
    """Attempt one pending row. Returns True if a row was attempted."""
    if not re.fullmatch(WEBHOOK_RE, url or ''):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        row = c.execute(
            select(outbox).where(outbox.c.status == 'pending')
            .order_by(outbox.c.created).limit(1)
        ).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(outbox.update().where(outbox.c.id == row['id']).values(
            status='sending', delivery=d))
        job = dict(row, delivery=d)
    
    status = 'failed'
    try:
        formatter = format_exit_message if job['payload'].get('type') == 'exit' else format_message
        resp = client.post(url, params={'wait': 'true'}, json=formatter(job['payload']))
        if 200 <= resp.status_code < 300:
            status = 'delivered'
        elif resp.status_code == 429:
            status = 'pending'
            try:
                delay = max(1, min(3600, float(resp.json().get('retry_after', 30))))
            except:
                delay = 30
            job['delivery']['next_attempt'] = now + delay
        elif resp.status_code >= 500:
            status = 'pending'
            job['delivery']['next_attempt'] = now + 30
    except httpx.HTTPError:
        status = 'pending'
        job['delivery']['next_attempt'] = now + 30
    
    if status == 'pending' and job['delivery'].get('attempts', 0) >= max_attempts:
        status = 'failed'
    
    with db.tx() as c:
        c.execute(outbox.update().where(outbox.c.id == job['id']).values(
            status=status, delivery=job['delivery']))
    return True


async def run(db, cfg):
    """Drainer loop: POST pending ICT alerts to Discord."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if bool(getattr(cfg, 'ict_push_enabled', False)):
                    await asyncio.to_thread(deliver_one, db, client, cfg.ict_push_webhook)
            except Exception as e:
                db.health('ict_push', 'error', type(e).__name__)
            await asyncio.sleep(1)
