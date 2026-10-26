"""Durable previews and a gated sender. Old shadow intents are never promoted."""
import logging
import asyncio
from copy import deepcopy
import re
import time
import uuid
from contextlib import contextmanager
import httpx
from sqlalchemy import Table,Column,String,Float,JSON,select,func,text
from .store import meta,identity
from .alert_routes import PUBLICATION_ROUTES

outbox=Table('native_program_outbox_v1',meta,
    Column('id',String(64),primary_key=True),Column('program',String(20),nullable=False),
    Column('event_key',String(600),nullable=False),Column('created',Float,nullable=False),
    Column('status',String(24),nullable=False),Column('payload',JSON,nullable=False),Column('delivery',JSON,nullable=False))


def owner(db,c,program,now=None):
    o=db.get(c,'native:ownership:'+('smoothers' if program=='smoothers_shared' else program),{})
    now=time.time() if now is None else now
    return o if o.get('effective_from',0)<=now and o.get('owner')=='compass' and o.get('previous_sender_paused') is True and o.get('accepted_at') and o.get('epoch') else None


def delivery_lock(c,shared=False):
    if c.dialect.name=='postgresql':
        name='pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
        c.execute(text('SELECT '+name+'(8675309062)'))


@contextmanager
def delivery_guard(db,shared=False):
    with db.tx() as c:
        delivery_lock(c,shared)
        yield c


def queue(db,c,program,event_key,payload,now,parent_event=None,event_time=None,cohort_time=None,publication=None):
    delivery_lock(c,shared=True)
    key=identity('native-outbox-v1',program,event_key)
    ownership=owner(db,c,program,now)
    if ownership and ownership.get('effective_from') and (event_time is None or event_time<ownership['effective_from']):ownership=None
    # A late recovery/closure for the old sender's cohort never changes owner.
    if ownership and program=='smoothers' and (cohort_time is None or cohort_time<ownership.get('effective_from',0)):ownership=None
    c.execute(db.insert(outbox).values(id=key,program=program,event_key=event_key,created=now,
        status='pending' if ownership else 'shadow',payload=payload,
        delivery={'attempts':0,'owner_epoch':ownership['epoch'] if ownership else None,'parent_event':parent_event,
                  'event_time':event_time,'cohort_time':cohort_time,'publication_input':publication})
        .on_conflict_do_nothing(index_elements=['id']))
    return key



def channel_entry_delivered(c,program,publication):
    trade_id=publication.get('trade_id')
    if not trade_id:return False
    return bool(c.execute(select(outbox.c.id).where(outbox.c.program==program,
        outbox.c.status=='delivered',
        outbox.c.delivery['publication']['trade_id'].as_string()==trade_id,
        outbox.c.delivery['publication']['event'].as_string()=='ENTRY').limit(1)).first())


def mirror_smoothers(db,c,row,delivery,webhooks,now):
    """Freeze an independently receipted copy before the primary network send.

    Only newly validated live intents are copied. Historical shadow/delivered
    rows are never replayed when a second destination is configured.
    """
    url=webhooks.get('smoothers_shared','')
    if row['program']!='smoothers' or not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',url):return
    primary=webhooks.get(delivery.get('publication',{}).get('category','smoothers'),'')
    if url==primary:return
    d=deepcopy(delivery)
    d.update(attempts=0,parent_event=None,publication_input=None,mirror=True,
        destination_fingerprint=identity(url),mirror_expires_at=(row['delivery'].get('publication_input') or {}).get('expires_at'))
    for key in ('lease','lease_until','message_id','finished_at','next_attempt','error'):d.pop(key,None)
    c.execute(db.insert(outbox).values(id=identity('native-outbox-v1','smoothers_shared',row['event_key']),
        program='smoothers_shared',event_key=row['event_key'],created=now,status='pending',
        payload=row['payload'],delivery=d).on_conflict_do_nothing(index_elements=['id']))

def _undeliverable(c,row,ownership):
    """Why a pending row can never be delivered, or None if it still can be.

    Two permanent classes (audit #5). Both were plain `continue` skips in
    deliver_one, re-scanned on every pass forever and never recorded:

    - parent_not_delivered / parent_missing: the row edits a parent message
      (parent_event) whose row is terminally non-delivered (or gone), so the
      parent Discord message id it must PATCH can never exist.
    - stale_owner_epoch: the row was queued under an ownership epoch that is
      no longer current. The epoch binds an intent to the ownership grant
      that covered it, and this module's rule is that old intents are never
      promoted across a grant change — so the row is only revivable by an
      explicit repair (repair_stale), never by the drainer itself.
    """
    d=row['delivery'] or {}
    parent_event=d.get('parent_event')
    if parent_event:
        parent=c.execute(select(outbox.c.status).where(
            outbox.c.id==identity('native-outbox-v1',row['program'],parent_event))).scalar()
        if parent is None:return 'parent_missing'
        if parent not in ('pending','sending','delivered'):return 'parent_not_delivered'
    if ownership and not d.get('repair_bypass') and ownership.get('epoch')!=d.get('owner_epoch'):
        return 'stale_owner_epoch'
    return None


def sweep_jammed(db,c,now,disabled_programs=(),limit=200):
    """Terminally resolve pending rows that can never be delivered (audit #5).

    Resolved rows land in 'suppressed' — the existing terminal vocabulary —
    with the reason (and, for epoch staleness, both epochs) recorded on the
    delivery, so a jam shows up in status counts and the native_outbox_jam
    health row instead of silently consuming candidate slots forever.
    Epoch-stale rows stay revivable through repair_stale(); parent-dead
    rows are final, because the message they would edit can never exist.
    """
    rows=c.execute(select(outbox).where(outbox.c.status=='pending',
        outbox.c.program.not_in(disabled_programs)).order_by(outbox.c.created).limit(limit)).mappings().all()
    owners={}
    resolved=[]
    for row in rows:
        program=row['program']
        if program not in owners:owners[program]=owner(db,c,program,now)
        reason=_undeliverable(c,row,owners[program])
        if not reason:continue
        d=dict(row['delivery'] or {})
        d.update(error=reason,resolved_at=now)
        if reason=='stale_owner_epoch':
            d['stale_owner_epoch']=d.get('owner_epoch')
            d['current_owner_epoch']=owners[program]['epoch']
        c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=d))
        resolved.append(dict(id=row['id'],program=program,event_key=row['event_key'],reason=reason))
    return resolved


def repair_stale(db,now=None,program=None,limit=100):
    """Explicitly revive epoch-stale outbox rows under current ownership.

    The repair pattern established by 7f9e750 and
    /api/admin/repair-smoothers-alerts, generalized: that endpoint only
    covers current-week smoothers ':target' rows, so jammed rows of every
    other kind — notably Friday ':close' exits — had no repair path at all.
    Any row the jam sweep resolved (or would resolve) solely for a stale
    owner epoch is re-armed exactly the way the endpoint arms its rows
    (repair_bypass + current owner_epoch, status 'pending') and then flows
    through deliver_one's normal gates and lease claim, so it is sent at
    most once.

    Idempotent: a row already armed (repair_bypass under the current
    epoch), already delivered, gated for any non-jam reason, or blocked by
    a dead parent is left untouched, so running repair twice never
    double-sends. Rows for a program without current Compass ownership
    cannot be armed.
    """
    now=time.time() if now is None else now
    repaired=[]
    with delivery_guard(db) as c:
        q=select(outbox).where(outbox.c.status.in_(['pending','suppressed']))
        if program:q=q.where(outbox.c.program==program)
        rows=c.execute(q.order_by(outbox.c.created).limit(limit)).mappings().all()
        owners={}
        for row in rows:
            d=dict(row['delivery'] or {})
            if d.get('repair_bypass'):continue
            if row['status']=='suppressed' and d.get('error')!='stale_owner_epoch':continue
            prog=row['program']
            if prog not in owners:owners[prog]=owner(db,c,prog,now)
            ownership=owners[prog]
            if not ownership:continue
            # Recompute the jam class now: only purely epoch-stale rows are
            # revivable. A parent-dead row stays terminal even if its epoch
            # also went stale.
            if _undeliverable(c,row,ownership)!='stale_owner_epoch':continue
            d.update(repair_bypass=True,repaired_at=now,owner_epoch=ownership['epoch'],error=None)
            d['repair_count']=d.get('repair_count',0)+1
            for key in ('next_attempt','lease','lease_until'):d.pop(key,None)
            c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='pending',delivery=d))
            repaired.append(dict(id=row['id'],program=prog,event_key=row['event_key']))
    return repaired


def deliver_one(db,client,webhooks,enabled=False,now=None,disabled_programs=()):
    if not enabled:return False
    now=time.time() if now is None else now
    # Audit #5: terminally resolve permanently undeliverable rows before
    # scanning, so they stop being silently skipped (and stop consuming
    # candidate slots) on every pass.
    with delivery_guard(db) as c:
        resolved=sweep_jammed(db,c,now,disabled_programs)
    if resolved:
        db.health('native_outbox_jam','resolved',
            '%d undeliverable outbox row(s) terminally suppressed (%s); epoch-stale rows can be revived via repair'%(
                len(resolved),', '.join(sorted({r['reason'] for r in resolved}))),
            resolved=len(resolved),event_keys=[r['event_key'] for r in resolved][:20])
    with delivery_guard(db,shared=True) as c:
        candidates=c.execute(select(outbox).where(outbox.c.status.in_(['pending','sending']),outbox.c.program.not_in(disabled_programs))
            .order_by(outbox.c.created).limit(50).with_for_update(skip_locked=True)).mappings().all()
        job=None
        for row in candidates:
            d=dict(row['delivery'])
            if row['status']=='sending':
                if d.get('lease_until',0)<now:
                    c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='ambiguous',delivery=dict(d,error='interrupted_send_requires_reconciliation')))
                continue
            ownership=owner(db,c,row['program'],now)
            # 2026-10-19: Fix for repaired alerts going stale after engine restart.
            # The repair endpoint sets owner_epoch, but if the engine restarts
            # (deploy), the epoch changes and repaired alerts are skipped forever.
            # For repair_bypass alerts, update the epoch instead of skipping.
            if not ownership:
                continue
            if ownership['epoch'] != d.get('owner_epoch'):
                if d.get('repair_bypass'):
                    # Repaired alert with stale epoch - update to current
                    d['owner_epoch'] = ownership['epoch']
                    c.execute(outbox.update().where(outbox.c.id==row['id']).values(delivery=d))
                else:
                    continue
            if d.get('next_attempt',0)>now:
                continue
            url=webhooks.get(row['program'],'')
            if not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',url):continue
            # Secondary fallback URL (used if primary returns 404/401)
            secondary_url=webhooks.get(row['program']+'_secondary','')
            if not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',secondary_url):
                secondary_url=''
            if d.get('mirror'):
                from .alert_ownership import quote_error
                decision=d.get('publication',{})
                reason=None
                if d.get('destination_fingerprint')!=identity(url):reason='Destination changed; historical copy not replayed'
                elif decision.get('event')=='ENTRY':
                    p=decision.get('payload',{})
                    if not d.get('mirror_expires_at') or now>=d['mirror_expires_at']:reason='Entry expired before shared delivery'
                    else:reason=quote_error(p.get('last_quote') or p.get('quote'),now)
                elif decision.get('event') and decision['event']!='ENTRY' and not channel_entry_delivered(c,row['program'],decision):reason='Entry not confirmed in this destination'
                if reason:
                    c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=dict(d,error=reason)))
                    continue
            from . import alert_ownership as publication
            meta=d.get('publication_input')
            if meta is not None:
                record=dict(id='native:'+row['id'],source=row['program'],symbol=(meta.get('contract') or {}).get('symbol') or '',ts=row['created'],payload=meta)
                decision=publication.assess(db,c,record,now)
                d['publication']=decision
                if decision['action']!='publish':
                    c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=d))
                    continue
                if decision['event']=='ENTRY':
                    reason=publication.quote_error(meta.get('quote'),now)
                    if reason:
                        publication.block_entry(db,c,decision['trade_id'],reason,now)
                        c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=dict(d,error=reason)))
                        continue
                else:
                    _,trade=publication.read_trade(db,c,decision['trade_id'])
                    # 2026-10-15: Repair bypass also skips the publication entry-delivered check.
                    # Repaired alerts may not have their entry delivered, but the target
                    # should still go out. Without this, the publication gate suppresses
                    # repaired alerts before they reach the channel gate's bypass check.
                    if not d.get('repair_bypass') and not decision.get('exit_admitted_without_entry') and (not trade or not publication.entry_delivered(c,trade)):
                        c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=dict(d,error='Entry was not confirmed delivered')))
                        continue
                if decision['category']!=row['program']:
                    # Publication categories predate the condensed channels.
                    url=webhooks.get(PUBLICATION_ROUTES.get(decision['category'],decision['category']),'')
                    if not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',url):continue
                row=dict(row,payload={'content':publication.format_message(dict(record,payload=decision['payload'])),
                    'username':'Market Compass','allowed_mentions':{'parse':[]}})
            mirror_smoothers(db,c,row,d,webhooks,now)
            # 2026-10-14: Allow repair_bypass flag to skip the entry-delivered check.
            # Repaired alerts (e.g., via /api/admin/repair-smoothers-alerts) may not have
            # their entry alerts delivered, but the target should still go out.
            if row['program']=='smoothers' and d.get('publication',{}).get('event') not in (None,'ENTRY') and not d.get('repair_bypass') and not d.get('publication',{}).get('exit_admitted_without_entry') and not channel_entry_delivered(c,'smoothers',d['publication']):
                c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=dict(d,error='Entry not confirmed in this destination')))
                continue
            message_id=None
            if d.get('parent_event'):
                parent=c.execute(select(outbox).where(outbox.c.id==identity('native-outbox-v1',row['program'],d['parent_event']))).mappings().first()
                if not parent or parent['status']!='delivered':continue
                message_id=parent['delivery'].get('message_id')
                if not isinstance(message_id,str) or not re.fullmatch(r'[0-9]{1,32}',message_id):continue
            lease=uuid.uuid4().hex
            d.update(attempts=d.get('attempts',0)+1,lease=lease,lease_until=now+30)
            c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='sending',delivery=d))
            job=dict(row,delivery=d,webhook_url=url,webhook_secondary=secondary_url);break
    if job is None:return False
    status='ambiguous';error=None
    url=job['webhook_url'];secondary_url=job.get('webhook_secondary','')
    try:
        response=client.patch(url+'/messages/'+message_id,json=job['payload']) if message_id else client.post(url,params={'wait':'true'},json=job['payload'])
        # Fallback to secondary webhook if primary is invalid (404/401)
        used_secondary=False
        if response.status_code in (401,404) and secondary_url:
            url=secondary_url;used_secondary=True
            response=client.patch(url+'/messages/'+message_id,json=job['payload']) if message_id else client.post(url,params={'wait':'true'},json=job['payload'])
        if 200<=response.status_code<300:
            try:message_id=response.json().get('id',message_id)
            except (ValueError,AttributeError):pass
            if isinstance(message_id,str) and re.fullmatch(r'[0-9]{1,32}',message_id):
                status='delivered'
                if used_secondary:error='primary_webhook_failed_used_secondary'
            else:error='missing_discord_receipt'
        elif response.status_code==429:
            status='pending';error='rate_limited'
            try:delay=max(1,min(3600,float(response.json().get('retry_after',30))))
            except (ValueError,TypeError,AttributeError):delay=30
            job['delivery']['next_attempt']=now+delay
        elif response.status_code>=500:
            status='pending' if message_id else 'ambiguous';error='server_error'
            job['delivery']['next_attempt']=now+30
        else:status='failed';error='http_'+str(response.status_code)
    except httpx.HTTPError:error='network_outcome_unknown'
    if status=='pending':
        job['delivery'].setdefault('next_attempt',now+30)
        if job['delivery']['attempts']>=10:status='failed'
    with delivery_guard(db,shared=True) as c:
        current=c.execute(select(outbox.c.delivery).where(outbox.c.id==job['id']).with_for_update()).scalar_one()
        if current.get('lease')==job['delivery']['lease']:
            if current.get('error')=='handoff_rollback_requires_delivery_reconciliation':
                status='ambiguous';error='handoff_rollback_requires_delivery_reconciliation'
            if status=='delivered' and job['delivery'].get('publication',{}).get('event')=='ENTRY':
                from .alert_ownership import read_trade
                path,trade=read_trade(db,c,job['delivery']['publication']['trade_id'])
                if trade:
                    trade['native_delivered']=True;db.put(c,path,trade)
            current.update(job['delivery'],error=error,message_id=message_id,finished_at=now,lease_until=0)
            c.execute(outbox.update().where(outbox.c.id==job['id']).values(status=status,delivery=current))
    return True


def snapshot(c):
    counts={}
    for program,status,count in c.execute(select(outbox.c.program,outbox.c.status,func.count()).group_by(outbox.c.program,outbox.c.status)):
        counts.setdefault(program,{})[status]=count
    return {'counts':counts,'basis':'Shadow intents stay shadow; live sends require explicit accepted ownership and sender enablement'}


async def run(db,cfg):
    import os
    enabled=os.getenv('NATIVE_PROGRAM_SEND_ENABLED','false').lower()=='true'
    hooks=dict(cfg.discord_routes)
    # 2026-10-15: Only overwrite with NATIVE_* webhook if it's non-empty.
    # Previously, if NATIVE_SMOOTHERS_DISCORD_WEBHOOK was unset, it would
    # overwrite the valid DISCORD_SMOOTHERS_WEBHOOK_URL with '', causing
    # the drainer to skip all Smoothers alerts (URL regex fails on '').
    for p in ('morning','smoothers'):
        native_url=os.getenv('NATIVE_'+p.upper()+'_DISCORD_WEBHOOK','')
        if native_url:
            hooks[p]=native_url
    hooks['smoothers_shared']=os.getenv('NATIVE_SMOOTHERS_SHARED_DISCORD_WEBHOOK','')
    # Secondary fallback webhooks (tried if primary returns 404/401)
    for p in ('morning','smoothers'):
        secondary_url=os.getenv('NATIVE_'+p.upper()+'_DISCORD_WEBHOOK_SECONDARY','')
        if secondary_url:
            hooks[p+'_secondary']=secondary_url
    if not cfg.morning_enabled: hooks["morning"]=""
    with httpx.Client(timeout=10,follow_redirects=False) as client:
        while True:
            try:await asyncio.to_thread(deliver_one,db,client,hooks,enabled,None,() if cfg.morning_enabled else ("morning",))
            except Exception as e:
                try: db.health('native_outbox','error',type(e).__name__)
                except Exception: logging.getLogger('uvicorn.error').exception('native_outbox health write failed')
            await asyncio.sleep(1)
