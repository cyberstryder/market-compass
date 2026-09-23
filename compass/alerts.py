import asyncio
import time
import uuid
from urllib.parse import urlparse, parse_qsl, urlencode
import httpx
from sqlalchemy import select, func, update
from .store import events, discord_jobs
from .alert_format import message_for, alert_identity
from .alert_routes import ROUTES, ORIGINAL_SENDERS, route_for, destination, manifest
from .operating_mode import paper_message
from . import alert_ownership


class DeliveryError(Exception):
    def __init__(self, detail, retry=15):
        super().__init__(detail)
        self.retry = retry


def confirmed_url(webhook):
    url = urlparse(webhook)
    params = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != 'wait']
    return url._replace(query=urlencode(params + [('wait', 'true')]), fragment='').geturl()


def enqueue_routes(db, c, now):
    """Start after the old acknowledged boundary, never replay delivered history."""
    cursor = db.get(c, 'outbox:discord:ingested', db.get(c, 'outbox:discord', 0))
    rows = list(c.execute(select(events).where(events.c.kind == 'alert', events.c.id > cursor)
                          .order_by(events.c.id).limit(200)).mappings())
    for row in rows:
        decision=alert_ownership.assess(db,c,row,now)
        route=decision.get('category') or route_for(row)
        c.execute(db.insert(discord_jobs).values(event_id=row['id'], route=route,
            status='pending' if decision['action'] in ('publish','passthrough') else 'suppressed', queued_at=row['ts'], confirmation=decision)
            .on_conflict_do_nothing(index_elements=['event_id']))
    db.put(c, 'outbox:discord:ingested', rows[-1]['id'] if rows else cursor)


def outbox_status(db, c, now):
    cursor = db.get(c, 'outbox:discord', 0)
    ingested = db.get(c, 'outbox:discord:ingested', cursor)
    count, oldest = c.execute(select(func.count(), func.min(events.c.ts)).where(
        events.c.kind == 'alert', events.c.id > ingested)).one()
    counts = {r.route: r for r in c.execute(select(discord_jobs.c.route,
        func.count().label('pending'), func.min(discord_jobs.c.queued_at).label('oldest'))
        .where(discord_jobs.c.status == 'pending').group_by(discord_jobs.c.route))}
    pending = count + sum(r.pending for r in counts.values())
    stamps = [x for x in [oldest, *[r.oldest for r in counts.values()]] if x is not None]
    routes = []
    saved = db.get(c, 'outbox:discord:routes', [])
    for item in saved:
        route = item['route']; q = counts.get(route)
        health = db.get(c, 'health:discord:' + route, {})
        routes.append({**item, 'pending': q.pending if q else 0,
            'oldest_age': round(now-q.oldest, 1) if q else None,
            'health': health, 'last_confirmation': db.get(c, 'outbox:discord:confirmation:' + route)})
    ambiguous=c.execute(select(func.count()).select_from(discord_jobs).where(discord_jobs.c.status=='ambiguous')).scalar_one()
    return {'pending': pending, 'ambiguous':ambiguous, 'unassigned': count,
        'oldest_age': round(now-min(stamps), 1) if stamps else None,
        'last_acknowledged_event': cursor or None,
        'last_confirmation': db.get(c, 'outbox:discord:confirmation'),
        'last_test_confirmation': db.get(c, 'outbox:discord:test_confirmation'),
        'routes': routes, 'original_senders': ORIGINAL_SENDERS,
        'reserved_categories': ['end_of_day_algo'],
        'note': 'Original Morning and Smoothers remain official senders. Their Compass mirrors go to research.'}


async def dispatch(client, db, webhook, row, route=None):
    p = row['payload']
    if p.get('publication'):
        body={'content':alert_ownership.format_message(row),'username':'Market Compass','allowed_mentions':{'parse':[]}}
    elif p.get('status') == 'spy_morning_brief':
        from .spy_brief import delivery_payload
        body = delivery_payload(row, time.time())
    elif p.get('status') == 'spy_chart_prompt':
        from .spy_chart import delivery_payload
        body = delivery_payload(row, time.time())
    else:
        body = {'content': message_for(row),
            'username': 'Market Compass · ' + alert_identity(row)['label'].title(),
            'allowed_mentions': {'parse': []}}
    response = await client.post(confirmed_url(webhook), json=body)
    if response.status_code == 429:
        try: retry = max(1, min(60, float(response.json().get('retry_after', 5))))
        except (ValueError, TypeError): retry = 5
        raise DeliveryError('Discord rate limit; alert remains queued', retry)
    if response.status_code != 200:
        raise DeliveryError(f'Discord HTTP {response.status_code}; message not confirmed')
    try: message = response.json()
    except ValueError: raise DeliveryError('Discord returned no message confirmation') from None
    message_id = message.get('id') if isinstance(message, dict) else None
    if not isinstance(message_id, str) or not message_id.isdigit():
        raise DeliveryError('Discord returned no message ID; alert remains queued')
    now = time.time()
    with db.tx() as c:
        confirmation = {'event_id': row['id'], 'message_id': message_id, 'at': now}
        if route:
            confirmation['route'] = route
            c.execute(update(discord_jobs).where(discord_jobs.c.event_id == row['id'])
                      .values(status='sent', confirmation=confirmation))
            db.put(c, 'outbox:discord:confirmation:' + route, confirmation)
            first = c.execute(select(func.min(discord_jobs.c.event_id))
                              .where(discord_jobs.c.status == 'pending')).scalar_one()
            # Keep a contiguous legacy acknowledgement boundary. Other route receipts
            # remain authoritative even when this boundary is held back by a failed route.
            boundary = first-1 if first is not None else db.get(c, 'outbox:discord:ingested', row['id'])
            db.put_max(c, 'outbox:discord', boundary)
        else:
            db.put_max(c, 'outbox:discord', row['id'])
        db.put(c, 'outbox:discord:confirmation', confirmation)
        if p.get('status') == 'notification_test':
            db.put(c, 'outbox:discord:test_confirmation', confirmation)
        db.append(c, 'alert_delivery', 'discord', row['symbol'], now, confirmation, key='discord:' + message_id)
    db.health('discord:' + route if route else 'discord', 'delivered',
              'Discord confirmed a saved message; event IDs identify possible retries', now)


class DeliveryWorker:
    def __init__(self, db, cfg):
        self.db, self.cfg, self.owner = db, cfg, uuid.uuid4().hex
        self.verified, self.retry_at = {}, {}
        self.published, self.route_health = False, {}
        self.ready_routes = set()

    def health(self, route, status, detail, **extra):
        if self.route_health.get(route) != status:
            self.db.health('discord:' + route, status, detail, **extra)
            self.route_health[route] = status

    def suppress_paper(self, c, now):
        if self.cfg.paper_trading:
            return
        rows=c.execute(select(events).join(discord_jobs,events.c.id==discord_jobs.c.event_id)
            .where(discord_jobs.c.status=='pending',events.c.source=='engine')
            .order_by(events.c.id).limit(500)).mappings().all()
        for row in rows:
            if paper_message(row):
                c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                    .values(status='suppressed',confirmation=dict(at=now,reason='Paper notifications disabled in research mode')))
        first=c.execute(select(func.min(discord_jobs.c.event_id)).where(discord_jobs.c.status=='pending')).scalar_one()
        boundary=first-1 if first is not None else self.db.get(c,'outbox:discord:ingested',0)
        if rows:
            self.db.put_max(c,'outbox:discord',boundary)

    async def tick(self, client, now=None):
        now = time.time() if now is None else now
        with self.db.tx() as c:
            # Same lease as the legacy worker: rolling deployment cannot double-send.
            if not self.db.lease(c, 'discord', self.owner, 90):
                return
            # With the delivery lease held, a leftover sending row has an unknown
            # receipt. Never replay a potentially successful option alert.
            c.execute(update(discord_jobs).where(discord_jobs.c.status=='sending')
                .values(status='ambiguous'))
            enqueue_routes(self.db, c, now)
            self.suppress_paper(c, now)
            if not self.published:
                self.db.put(c, 'outbox:discord:routes', manifest(self.cfg))
        if not self.published:
            import logging
            logging.getLogger('uvicorn.error').info('Discord routes configured: %s',
                {r['route']: r['destination_mode'] for r in manifest(self.cfg)})
            self.published = True
        # Verify each distinct destination once without sending a test message.
        destinations = dict(destination(self.cfg, route) for route in ROUTES)
        async def verify(url, mode):
            if mode not in ('dedicated', 'shared_fallback') or url in self.verified or now < self.retry_at.get(url, 0):
                return
            try:
                response = await client.get(urlparse(url)._replace(query='', fragment='').geturl())
                if response.status_code != 200:
                    raise DeliveryError(f'Webhook verification HTTP {response.status_code}', 30)
                data = response.json()
                self.verified[url] = {k: data[k] for k in ('id', 'channel_id', 'guild_id') if k in data}
            except asyncio.CancelledError:
                raise
            except Exception:
                self.retry_at[url] = now + 30
        await asyncio.gather(*(verify(url, mode) for url, mode in destinations.items()))
        connected = False
        sends = []
        for route in ROUTES:
            url, mode = destination(self.cfg, route)
            if mode == 'invalid':
                self.health(route, 'error', 'Invalid dedicated or fallback webhook; route held')
                continue
            if not url:
                self.health(route, 'not_configured', 'Awaiting this channel webhook; messages stay queued')
                continue
            if url not in self.verified:
                self.health(route, 'error', 'Destination verification failed; route remains queued')
                continue
            connected = True
            if now < self.retry_at.get(route, 0):
                continue
            if route not in self.ready_routes:
                self.health(route, 'connected',
                            'Dedicated webhook verified' if mode == 'dedicated' else 'Using shared fallback; channel separation pending',
                            destination=self.verified[url])
                self.ready_routes.add(route)
            with self.db.tx() as c:
                row = c.execute(select(events).join(discord_jobs, events.c.id == discord_jobs.c.event_id)
                    .where(discord_jobs.c.route == route, discord_jobs.c.status == 'pending')
                    .order_by(events.c.id).limit(1)).mappings().first()
            if row:
                sends.append(self.send(client, url, dict(row), route, now))
        # Different routes progress independently. Same-route message order is retained,
        # including the brief followed by its chart prompt.
        await asyncio.gather(*sends)
        self.db.health('discord', 'connected' if connected else 'not_configured',
            'Category routing active; see individual routes for delivery health' if connected else 'No verified alert destination')

    async def send(self, client, url, row, route, now):
        # Final guard also covers backlog beyond this turn's suppression batch.
        if not self.cfg.paper_trading and paper_message(row):
            with self.db.tx() as c:
                c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                    .values(status='suppressed',confirmation=dict(at=now,reason='Paper notifications disabled in research mode')))
            return
        with self.db.tx() as c:
            decision=alert_ownership.assess(self.db,c,row,now)
            if decision['action'] not in ('publish','passthrough'):
                c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                    .values(status='suppressed',confirmation=decision))
                return
            if decision['action']=='publish':
                row={**row,'payload':decision['payload']}
                if decision['event']=='ENTRY':
                    reason=alert_ownership.quote_error(row['payload'].get('last_quote') or row['payload'].get('quote'),now)
                    if now-row['ts']>120:reason='Entry expired in delivery queue'
                    _,active=alert_ownership.read_trade(self.db,c,decision['trade_id'])
                    if active and active['state']!='open':reason='Trade ended before entry delivery'
                    if reason:
                        c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                            .values(status='suppressed',confirmation=dict(decision,reason=reason)))
                        alert_ownership.block_entry(self.db,c,decision['trade_id'],reason,now)
                        return
                else:
                    _,trade=alert_ownership.read_trade(self.db,c,decision['trade_id'])
                    if not trade or not alert_ownership.entry_delivered(c,trade):
                        c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                            .values(status='suppressed',confirmation=dict(decision,reason='Entry was not confirmed delivered')))
                        return
        canonical=bool(row['payload'].get('publication'))
        if canonical:
            with self.db.tx() as c:
                c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                    .values(status='sending'))
        try:
            await dispatch(client, self.db, url, row, route)
            self.route_health[route] = 'delivered'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if canonical:
                limited=isinstance(error,DeliveryError) and str(error).startswith('Discord rate limit')
                with self.db.tx() as c:
                    c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                        .values(status='pending' if limited else 'ambiguous',confirmation=dict(
                            at=now,reason='rate_limited' if limited else 'Delivery outcome requires reconciliation',
                            trade_id=row['payload']['publication']['trade_id'])))
            retry = error.retry if isinstance(error, DeliveryError) else 15
            detail = str(error) if isinstance(error, DeliveryError) else type(error).__name__ + '; alert remains queued'
            if canonical and not limited:detail='Delivery outcome unknown; held for receipt reconciliation'
            self.retry_at[route] = now + retry
            self.db.health('discord:' + route, 'error', detail)


async def deliver(db, cfg):
    worker = DeliveryWorker(db, cfg)
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                await worker.tick(client)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                db.health('discord', 'error', type(error).__name__ + '; alert remains queued')
                await asyncio.sleep(15)
            await asyncio.sleep(1)
