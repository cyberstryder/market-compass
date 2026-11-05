"""Push-on-transition for apex magnet breaks.

The magnet scanner detects transitions every ~120s in its evidence function
(apex_magnet.scan). This module writes one durable outbox row per transition
and a drainer POSTs them to Discord. Transport pattern only: nothing here
touches alerts.py, alert_ownership, entries, paper trading, or order paths.

Push silence (Josh, 2026-11-05): magnet-break pushes no longer post —
see APEX_MAGNET_PUSH_GATE below. Rows are still written, as
'suppressed'; detection and recording upstream are untouched.
"""
import asyncio
import logging
import re
import time

import httpx
from sqlalchemy import select, func

from .store import identity, magnet_push_outbox, push_outbox

outbox = magnet_push_outbox
MAX_ATTEMPTS = 10
WEBHOOK_RE = r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+'

# Apex-magnet push silence (Josh, 2026-11-05). The magnet-break
# verdict (files/apex-magnet-verdict-2026-11-05.md) found the pushed
# transitions NOT TRADEABLE: post-signal signed returns decay to
# negative by the close, the SPY-adjusted +60m return is exactly
# zero, and the time-shift placebo shows the move precedes the alert
# (the detector is a lagged confirmation device). The push text's
# "broke above/below" wording also disagrees with the detector's own
# break direction in 56% of events — silencing makes that defect moot
# on the push surface; the formatter is deliberately NOT fixed here.
# Josh directed the pushes stop. Detection, signal/outcome
# recording, GEX stamps and the pick-checker apex pillar are
# untouched: a transition still writes its outbox row, but as
# 'suppressed' under this gate id (the suppression itself on the
# record), and deliver_one terminally resolves any pre-gate backlog
# row the same way instead of POSTing it. Same shape as the scanner
# (17b96fe), ICT paper (8b657a1) and Wednesday-horizon (694c81e)
# silence builds: gate at enqueue, same predicate at send time.
APEX_MAGNET_PUSH_GATE = 'apex-magnet-push-silence-v1'


def _push_cfg(cfg):
    enabled = bool(getattr(cfg, 'magnet_push_enabled', False))
    signals = tuple(getattr(cfg, 'magnet_push_signals', ('broke_through',)) or ())
    return enabled, signals


def maybe_queue(db, c, cfg, symbol, row, prev_signal, session_day, now):
    """Write one outbox row for a magnet transition, or None.

    Cold start (no previous signal) seeds state silently — no push — so a
    first run doesn't burst every in-radius symbol. Signal filter applies.

    Silence gate (apex-magnet-push-silence-v1): the row is still
    written — payload byte-identical — but lands 'suppressed' with the
    gate id on its delivery record, so the drainer never selects it
    and the suppression itself is auditable.
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
    delivery = {'attempts': 0, 'gate': APEX_MAGNET_PUSH_GATE,
                'error': 'suppressed_by_gate', 'suppressed_at': now}
    if getattr(cfg, 'push_outbox_unified', False):
        c.execute(db.insert(push_outbox).values(
            id=row_id, channel='magnet', status='suppressed', created=now,
            payload=payload, delivery=delivery)
            .on_conflict_do_nothing(index_elements=['id']))
    else:
        c.execute(db.insert(outbox).values(
            id=row_id, status='suppressed', created=now, payload=payload,
            delivery=delivery).on_conflict_do_nothing(index_elements=['id']))
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


def _suppress_backlog(db, box, channel, now):
    """Send-time half of the apex-magnet silence gate.

    Every row this drainer can see is a magnet row, so any candidate
    still in 'pending'/'sending' — including rows queued before the
    gate existed — is terminally resolved as 'suppressed' under the
    gate id instead of being POSTed. A row under an active send lease
    is left to that attempt's own resolution. Returns True if any row
    was resolved.
    """
    resolved = False
    with db.tx() as c:
        q = (select(box).where(box.c.status.in_(['pending', 'sending']))
             .order_by(box.c.created).limit(20))
        if channel is not None:
            q = q.where(box.c.channel == channel)
        for row in c.execute(q).mappings().all():
            d = dict(row['delivery'])
            if row['status'] == 'sending' and d.get('lease_until', 0) >= now:
                continue
            d.update(gate=APEX_MAGNET_PUSH_GATE, error='suppressed_by_gate',
                     suppressed_at=now, lease_until=0)
            c.execute(box.update().where(box.c.id == row['id'])
                      .values(status='suppressed', delivery=d))
            resolved = True
    return resolved


def deliver_one(db, client, url, now=None, max_attempts=MAX_ATTEMPTS,
                table=None, channel=None):
    """Attempt one pending row. Returns True if a row was attempted.

    table/channel (bundle 3): when the unified drainer calls in, read
    and update push_outbox rows for `channel` instead of this module's
    own table. The delivery logic itself is unchanged either way.

    Silence gate: _suppress_backlog runs first, so while
    apex-magnet-push-silence-v1 stands, no magnet row — new or
    backlog — is ever POSTed; the claim/post path below is the
    pre-gate transport, kept for the day the gate is lifted.
    """
    box = table if table is not None else outbox
    now = time.time() if now is None else now
    suppressed = _suppress_backlog(db, box, channel, now)
    if not re.fullmatch(WEBHOOK_RE, url or ''):
        return suppressed
    with db.tx() as c:
        q = (select(box).where(box.c.status.in_(['pending', 'sending']))
             .order_by(box.c.created).limit(20))
        if channel is not None:
            q = q.where(box.c.channel == channel)
        candidates = c.execute(q).mappings().all()
        job = None
        for row in candidates:
            d = dict(row['delivery'])
            if row['status'] == 'sending' and d.get('lease_until', 0) >= now:
                continue
            if d.get('next_attempt', 0) > now:
                continue
            d.update(attempts=d.get('attempts', 0) + 1, lease_until=now + 30)
            c.execute(box.update().where(box.c.id == row['id'])
                      .values(status='sending', delivery=d))
            job = dict(row, delivery=d)
            break
    if job is None:
        return suppressed
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
        c.execute(box.update().where(box.c.id == job['id'])
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
                # Sync POST + sync db.tx(): off the event loop (ict_push pattern).
                await asyncio.to_thread(deliver_one, db, client, url)
            except Exception as e:
                try:
                    db.health('magnet_push', 'error', type(e).__name__)
                except Exception:
                    logging.getLogger('uvicorn.error').exception('magnet_push health write failed')
            await asyncio.sleep(1)
