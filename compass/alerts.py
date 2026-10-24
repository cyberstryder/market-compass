import asyncio
import logging
import time
import uuid
from urllib.parse import urlparse, parse_qsl, urlencode
import httpx
from sqlalchemy import select, func, update
from .store import events, discord_jobs
from .alert_format import message_for, alert_identity
from .alert_routes import ROUTES, ORIGINAL_SENDERS, PUBLICATION_ROUTES, route_for, destination, manifest
from .operating_mode import paper_message
from . import alert_ownership


class DeliveryError(Exception):
    """detail: human text. retry: seconds before this route is tried again.
    permanent: Discord rejected the payload itself (4xx other than 408/429);
    retrying can never succeed, so the job is marked failed instead of
    re-POSTed every 15s forever. ambiguous: the request may have been
    delivered (timeout after send) — never blindly resend."""
    def __init__(self, detail, retry=15, permanent=False, ambiguous=False):
        super().__init__(detail)
        self.retry = retry
        self.permanent = permanent
        self.ambiguous = ambiguous


def _gamma_levels_line(db, row):
    """Gamma dealer levels for 0DTE option alerts; None when unavailable."""
    import re
    try:
        identity = alert_identity(row)
    except Exception:
        return None
    if identity.get('category') != 'options_0dte':
        return None
    symbol = row.get('symbol') or ''
    m = re.match(r'^(?:O:)?([A-Z.]+)\d{6}[CP]\d{8}$', symbol)
    underlying = m.group(1) if m else symbol.split('@')[0]
    try:
        with db.tx() as c:
            item = db.get(c, 'gamma_levels:' + underlying, {}) or {}
    except Exception:
        return None
    if item.get('status') != 'ok' or not item.get('levels'):
        return None
    try:
        from .gamma_levels import describe
        return describe(item)
    except Exception:
        return None


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
        # Publication categories predate the condensed channels; map the two
        # retired families to their current routes before queuing.
        route=PUBLICATION_ROUTES.get(decision.get('category'), decision.get('category')) or route_for(row)
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
        gamma_line = await asyncio.to_thread(_gamma_levels_line, db, row)
        body = {'content': message_for(row, gamma_context=gamma_line),
            'username': 'Market Compass · ' + alert_identity(row)['label'].title(),
            'allowed_mentions': {'parse': []}}
    try:
        response = await client.post(confirmed_url(webhook), json=body)
    except httpx.ConnectError as e:
        # Request never left: safe to retry.
        raise DeliveryError('Discord unreachable (%s); alert remains queued' % type(e).__name__, 15)
    except httpx.TimeoutException as e:
        # May or may not have been delivered: caller must not blindly resend.
        raise DeliveryError('Discord timeout (%s); delivery outcome unknown' % type(e).__name__, 15, ambiguous=True)
    if response.status_code == 429:
        try: retry = max(1, min(60, float(response.json().get('retry_after', 5))))
        except (ValueError, TypeError): retry = 5
        raise DeliveryError('Discord rate limit; alert remains queued', retry)
    if 400 <= response.status_code < 500 and response.status_code != 408:
        # Bad payload / dead webhook: retrying the same body cannot succeed.
        raise DeliveryError(f'Discord HTTP {response.status_code}; message rejected', 60, permanent=True)
    if response.status_code != 200:
        raise DeliveryError(f'Discord HTTP {response.status_code}; message not confirmed')
    try: message = response.json()
    except ValueError: raise DeliveryError('Discord returned no message confirmation', ambiguous=True) from None
    message_id = message.get('id') if isinstance(message, dict) else None
    if not isinstance(message_id, str) or not message_id.isdigit():
        raise DeliveryError('Discord returned no message ID; alert remains queued', ambiguous=True)
    now = time.time()
    def _confirm():
        with db.tx() as c:
            confirmation = {'event_id': row['id'], 'message_id': message_id, 'at': now}
            if route:
                confirmation['route'] = route
                c.execute(update(discord_jobs).where(discord_jobs.c.event_id == row['id'])
                          .values(status='sent', confirmation=confirmation))
                db.put(c, 'outbox:discord:confirmation:' + route, confirmation)
                # 'sending' rows (now marked for every job) are unacknowledged
                # too; the boundary must not jump past a concurrent send.
                first = c.execute(select(func.min(discord_jobs.c.event_id))
                                  .where(discord_jobs.c.status.in_(('pending', 'sending')))).scalar_one()
                # Keep a contiguous legacy acknowledgement boundary. Other route receipts
                # remain authoritative even when this boundary is held back by a failed route.
                boundary = first-1 if first is not None else db.get(c, 'outbox:discord:ingested', row['id'])
                db.put_max(c, 'outbox:discord', boundary)
            else:
                db.put_max(c, 'outbox:discord', row['id'])
            db.put(c, 'outbox:discord:confirmation', confirmation)
            if p.get('status') == 'notification_test':
                db.put(c, 'outbox:discord:test_confirmation', confirmation)
            # No alert_delivery event is written: that log's only consumer was
            # the dashboard alerts page (audited 2026-10-21); the authoritative
            # receipts are the discord_jobs/outbox confirmation keys above.
        db.health('discord:' + route if route else 'discord', 'delivered',
                  'Discord confirmed a saved message; event IDs identify possible retries', now)
    await asyncio.to_thread(_confirm)


class DeliveryWorker:
    def __init__(self, db, cfg):
        self.db, self.cfg, self.owner = db, cfg, uuid.uuid4().hex
        self.verified, self.retry_at = {}, {}
        self._attempts = {}
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

    def _prepare(self, now):
        """Lease + queue housekeeping. Sync; called via to_thread."""
        with self.db.tx() as c:
            # Same lease as the legacy worker: rolling deployment cannot double-send.
            if not self.db.lease(c, 'discord', self.owner, 90):
                return False
            # With the delivery lease held, a leftover sending row has an unknown
            # receipt. Never replay a potentially successful alert.
            c.execute(update(discord_jobs).where(discord_jobs.c.status=='sending')
                .values(status='ambiguous'))
            # 2026-09-28 channel condensation: repoint still-pending jobs that
            # were queued under retired route names so they drain normally.
            for old_route, new_route in (('swing', 'options_ideas'), ('options_leaps', 'research'),
                                         ('unusual_options', 'intraday'), ('exposure', 'intraday'),
                                         ('system', 'research')):
                c.execute(update(discord_jobs).where(
                    discord_jobs.c.route == old_route, discord_jobs.c.status == 'pending')
                    .values(route=new_route))
            enqueue_routes(self.db, c, now)
            self.suppress_paper(c, now)
            if not self.published:
                self.db.put(c, 'outbox:discord:routes', manifest(self.cfg))
        return True

    def _route_pending(self, route):
        with self.db.tx() as c:
            pending=c.execute(select(func.count()).select_from(discord_jobs).where(
                discord_jobs.c.route==route,discord_jobs.c.status=='pending')).scalar_one()
            owner=self.db.get(c,'native:ownership:smoothers',{}).get('owner','original')
        return pending, owner

    def _next_row(self, route):
        with self.db.tx() as c:
            row = c.execute(select(events).join(discord_jobs, events.c.id == discord_jobs.c.event_id)
                .where(discord_jobs.c.route == route, discord_jobs.c.status == 'pending')
                .order_by(events.c.id).limit(1)).mappings().first()
        return dict(row) if row else None

    def _mark(self, event_id, status, confirmation=None):
        with self.db.tx() as c:
            values = {'status': status}
            if confirmation is not None: values['confirmation'] = confirmation
            c.execute(update(discord_jobs).where(discord_jobs.c.event_id==event_id).values(**values))

    async def tick(self, client, now=None):
        # 2026-10-23: every synchronous DB transaction in this worker used to
        # run on the event loop (6-10 per second), stalling the stream
        # consumers and every other engine task. They now go through
        # asyncio.to_thread; only the HTTP awaits run on the loop.
        now = time.time() if now is None else now
        if not await asyncio.to_thread(self._prepare, now):
            return
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
                pending, owner = await asyncio.to_thread(self._route_pending, route)
                if not pending and route=='smoothers' and owner=='original':
                    self.health(route,'externally_managed','Original Smoothers owns notifications; Compass handoff is not active')
                else:
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
            row = await asyncio.to_thread(self._next_row, route)
            if row:
                sends.append(self.send(client, url, row, route, now))
        # Different routes progress independently. Same-route message order is retained,
        # including the brief followed by its chart prompt.
        await asyncio.gather(*sends)
        await asyncio.to_thread(self.db.health, 'discord', 'connected' if connected else 'not_configured',
            'Category routing active; see individual routes for delivery health' if connected else 'No verified alert destination')

    async def send(self, client, url, row, route, now):
        # Skip futures scanner SETUP noise if disabled (Josh: "that's just noise").
        # This only blocks Discord delivery; the alert is still recorded in the DB.
        payload = row.get('payload', {})
        if (route == 'futures' and payload.get('status') == 'setup_triggered'
                and not self.cfg.futures_scanner_push_enabled):
            await asyncio.to_thread(self._mark, row['id'], 'suppressed', dict(at=now,reason='Futures scanner pushes disabled'))
            return
        # Final guard also covers backlog beyond this turn's suppression batch.
        if not self.cfg.paper_trading and paper_message(row):
            await asyncio.to_thread(self._mark, row['id'], 'suppressed', dict(at=now,reason='Paper notifications disabled in research mode'))
            return
        def _assess():
            with self.db.tx() as c:
                decision=alert_ownership.assess(self.db,c,row,now)
                if decision['action'] not in ('publish','passthrough'):
                    c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                        .values(status='suppressed',confirmation=decision))
                    return None
                out=row
                if decision['action']=='publish':
                    out={**row,'payload':decision['payload']}
                    if decision['event']=='ENTRY':
                        reason=alert_ownership.quote_error(out['payload'].get('last_quote') or out['payload'].get('quote'),now)
                        if now-out['ts']>120:reason='Entry expired in delivery queue'
                        _,active=alert_ownership.read_trade(self.db,c,decision['trade_id'])
                        if active and active['state']!='open':reason='Trade ended before entry delivery'
                        if reason:
                            c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                                .values(status='suppressed',confirmation=dict(decision,reason=reason)))
                            alert_ownership.block_entry(self.db,c,decision['trade_id'],reason,now)
                            return None
                    else:
                        _,trade=alert_ownership.read_trade(self.db,c,decision['trade_id'])
                        if not trade or not alert_ownership.entry_delivered(c,trade):
                            c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                                .values(status='suppressed',confirmation=dict(decision,reason='Entry was not confirmed delivered')))
                            return None
                # 2026-10-23: EVERY job is marked 'sending' before the POST, not
                # only canonical ones. A process death mid-POST is then swept
                # to 'ambiguous' by the next tick instead of being re-sent.
                c.execute(update(discord_jobs).where(discord_jobs.c.event_id==row['id'])
                    .values(status='sending'))
            return out
        row = await asyncio.to_thread(_assess)
        if row is None:
            return
        canonical=bool(row['payload'].get('publication'))
        trade_id=(row['payload'].get('publication') or {}).get('trade_id')
        try:
            await dispatch(client, self.db, url, row, route)
            self.route_health[route] = 'delivered'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Failure policy (2026-10-23):
            #   rate limit            -> pending, retry after retry_after
            #   permanent 4xx         -> failed (never re-POST a rejected body)
            #   ambiguous (timeout)   -> canonical: ambiguous (reconcile);
            #                            other: pending up to 3 attempts, then ambiguous
            #   connect error / other -> pending (request never left)
            is_de = isinstance(error, DeliveryError)
            limited = is_de and str(error).startswith('Discord rate limit')
            permanent = is_de and error.permanent
            ambiguous = (is_de and error.ambiguous) or (not is_de and isinstance(error, httpx.TimeoutException))
            if permanent:
                status, reason = 'failed', 'Discord rejected the message; not retried'
            elif limited:
                status, reason = 'pending', 'rate_limited'
            elif canonical:
                # Canonical trade publications are never re-sent on an
                # unconfirmed outcome (5xx, timeout, bad body): reconcile.
                status, reason = 'ambiguous', 'Delivery outcome requires reconciliation'
            elif ambiguous:
                prior = self._attempts.get(row['id'], 0) + 1
                self._attempts[row['id']] = prior
                if prior >= 3:
                    status, reason = 'ambiguous', 'Delivery outcome unknown after %d attempts' % prior
                else:
                    status, reason = 'pending', 'Delivery timeout; attempt %d of 3' % prior
            else:
                status, reason = 'pending', str(error) if is_de else type(error).__name__
            confirmation = dict(at=now, reason=reason)
            if trade_id: confirmation['trade_id'] = trade_id
            try:
                await asyncio.to_thread(self._mark, row['id'], status, confirmation)
            except Exception as e:
                logging.getLogger('uvicorn.error').exception('discord job status update failed: %s', type(e).__name__)
            retry = error.retry if is_de else 15
            detail = str(error) if is_de else type(error).__name__ + '; alert remains queued'
            if status == 'ambiguous': detail = 'Delivery outcome unknown; held for receipt reconciliation'
            if status == 'failed': detail = str(error)
            self.retry_at[route] = now + retry
            try:
                await asyncio.to_thread(self.db.health, 'discord:' + route, 'error', detail)
            except Exception:
                pass


async def deliver(db, cfg):
    worker = DeliveryWorker(db, cfg)
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                await worker.tick(client)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                try:
                    await asyncio.to_thread(db.health, 'discord', 'error', type(error).__name__ + '; alert remains queued')
                except Exception:
                    pass
                await asyncio.sleep(15)
            await asyncio.sleep(1)
