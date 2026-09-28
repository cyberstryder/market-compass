"""Push-on-transition for apex magnet breaks.

The magnet scanner detects transitions every ~120s in its evidence function
(apex_magnet.scan). This module writes one durable outbox row per transition
and a drainer POSTs them to Discord. Transport pattern only: nothing here
touches alerts.py, alert_ownership, entries, paper trading, or order paths.
"""
import asyncio
import re
import time

import httpx
from sqlalchemy import select, func

from .store import identity, magnet_push_outbox

outbox = magnet_push_outbox
MAX_ATTEMPTS = 10
WEBHOOK_RE = r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+'


def _push_cfg(cfg):
    enabled = bool(getattr(cfg, 'magnet_push_enabled', False))
    signals = tuple(getattr(cfg, 'magnet_push_signals', ('broke_through',)) or ())
    return enabled, signals


def maybe_queue(db, c, cfg, symbol, row, prev_signal, session_day, now):
    """Write one outbox row for a magnet transition, or None.

    Cold start (no previous signal) seeds state silently — no push — so a
    first run doesn't burst every in-radius symbol. Signal filter applies.
    """
    enabled, signals = _push_cfg(cfg)
    if not enabled:
        return None
    if not prev_signal:
        return None
    signal = row.get('signal')
    if signal not in signals:
        return None
    row_id = identity('magnet-push-v1', symbol, session_day, signal, now)
    payload = {
        'symbol': symbol, 'signal': signal, 'session': session_day,
        'magnet': row.get('magnet'), 'spot': row.get('spot'),
        'distance_pct': row.get('distance_pct'), 'role': row.get('role'),
        'vs_flip': row.get('vs_flip'), 'gamma_flip': row.get('gamma_flip'),
        'magnet_score': row.get('magnet_score'), 'tests': row.get('tests'),
        'previous_signal': prev_signal, 'at': now,
    }
    c.execute(db.insert(outbox).values(
        id=row_id, status='pending', created=now, payload=payload,
        delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return row_id


def format_message(payload):
    """Discord-safe message for one transition. No mentions, <2000 chars."""
    direction = 'broke above' if payload.get('role') == 'resistance' else 'broke below' \
        if payload.get('role') == 'support' else str(payload.get('signal'))
    flip = payload.get('gamma_flip')
    lines = [
        '**%s** %s magnet **%.2f**' % (payload.get('symbol'), direction, payload.get('magnet') or 0),
        'spot %.2f (%.2f%% away) · %s' % (
            payload.get('spot') or 0, (payload.get('distance_pct') or 0) * 100,
            payload.get('role') or 'level'),
    ]
    if flip:
        lines.append('gamma flip %.2f (%s)' % (flip, payload.get('vs_flip') or '?'))
    if payload.get('previous_signal'):
        lines.append('was: %s' % payload['previous_signal'])
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'Market Compass',
            'allowed_mentions': {'parse': []}}


def deliver_one(db, client, url, now=None, max_attempts=MAX_ATTEMPTS):
    """Attempt one pending row. Returns True if a row was attempted."""
    if not re.fullmatch(WEBHOOK_RE, url or ''):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        candidates = c.execute(select(outbox)
            .where(outbox.c.status.in_(['pending', 'sending']))
            .order_by(outbox.c.created).limit(20)).mappings().all()
        job = None
        for row in candidates:
            d = dict(row['delivery'])
            if row['status'] == 'sending' and d.get('lease_until', 0) >= now:
                continue
            if d.get('next_attempt', 0) > now:
                continue
            d.update(attempts=d.get('attempts', 0) + 1, lease_until=now + 30)
            c.execute(outbox.update().where(outbox.c.id == row['id'])
                      .values(status='sending', delivery=d))
            job = dict(row, delivery=d)
            break
    if job is None:
        return False
    status, error, message_id = 'ambiguous', None, None
    try:
        response = client.post(url, params={'wait': 'true'},
                               json=format_message(job['payload']))
        if 200 <= response.status_code < 300:
            try:
                message_id = response.json().get('id')
            except (ValueError, AttributeError):
                pass
            if isinstance(message_id, str) and re.fullmatch(r'[0-9]{1,32}', message_id):
                status = 'delivered'
            else:
                error = 'missing_discord_receipt'
        elif response.status_code == 429:
            status, error = 'pending', 'rate_limited'
            try:
                delay = max(1, min(3600, float(response.json().get('retry_after', 30))))
            except (ValueError, TypeError, AttributeError):
                delay = 30
            job['delivery']['next_attempt'] = now + delay
        elif response.status_code >= 500:
            status, error = 'pending', 'server_error'
            job['delivery']['next_attempt'] = now + 30
        else:
            status, error = 'failed', 'http_%d' % response.status_code
    except httpx.HTTPError:
        error = 'network_outcome_unknown'
    if job['delivery']['attempts'] >= max_attempts and status == 'pending':
        status, error = 'failed', 'retry_budget_exhausted'
    with db.tx() as c:
        d = dict(job['delivery'])
        d.update(error=error, message_id=message_id, finished_at=now, lease_until=0)
        c.execute(outbox.update().where(outbox.c.id == job['id'])
                  .values(status=status, delivery=d))
    return True


def snapshot(db, c):
    counts = {}
    for status, count in c.execute(select(outbox.c.status, func.count())
                                   .group_by(outbox.c.status)):
        counts[status] = count
    return {'counts': counts}


async def run(db, cfg):
    """Drainer: 1s poll, 429 backoff, retry budget. Rows accumulate if down."""
    url = (cfg.discord_routes or {}).get('magnets', '')
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                deliver_one(db, client, url)
            except Exception as e:
                db.health('magnet_push', 'error', type(e).__name__)
            await asyncio.sleep(1)
