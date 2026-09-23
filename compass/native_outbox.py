"""Durable previews and a gated sender. Old shadow intents are never promoted."""
import asyncio
import re
import time
import uuid
from contextlib import contextmanager
import httpx
from sqlalchemy import Table,Column,String,Float,JSON,select,func,text
from .store import meta,identity

outbox=Table('native_program_outbox_v1',meta,
    Column('id',String(64),primary_key=True),Column('program',String(20),nullable=False),
    Column('event_key',String(600),nullable=False),Column('created',Float,nullable=False),
    Column('status',String(24),nullable=False),Column('payload',JSON,nullable=False),Column('delivery',JSON,nullable=False))


def owner(db,c,program,now=None):
    o=db.get(c,'native:ownership:'+program,{})
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


def deliver_one(db,client,webhooks,enabled=False,now=None):
    if not enabled:return False
    now=time.time() if now is None else now
    with delivery_guard(db,shared=True) as c:
        candidates=c.execute(select(outbox).where(outbox.c.status.in_(['pending','sending']))
            .order_by(outbox.c.created).limit(50).with_for_update(skip_locked=True)).mappings().all()
        job=None
        for row in candidates:
            d=dict(row['delivery'])
            if row['status']=='sending':
                if d.get('lease_until',0)<now:
                    c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='ambiguous',delivery=dict(d,error='interrupted_send_requires_reconciliation')))
                continue
            ownership=owner(db,c,row['program'],now)
            if not ownership or ownership['epoch']!=d.get('owner_epoch') or d.get('next_attempt',0)>now:continue
            url=webhooks.get(row['program'],'')
            if not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',url):continue
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
                    if not trade or not publication.entry_delivered(c,trade):
                        c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='suppressed',delivery=dict(d,error='Entry was not confirmed delivered')))
                        continue
                if decision['category']!=row['program']:
                    url=webhooks.get(decision['category'],'')
                    if not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',url):continue
                row=dict(row,payload={'content':publication.format_message(dict(record,payload=decision['payload'])),
                    'username':'Market Compass','allowed_mentions':{'parse':[]}})
            message_id=None
            if d.get('parent_event'):
                parent=c.execute(select(outbox).where(outbox.c.id==identity('native-outbox-v1',row['program'],d['parent_event']))).mappings().first()
                if not parent or parent['status']!='delivered':continue
                message_id=parent['delivery'].get('message_id')
                if not isinstance(message_id,str) or not re.fullmatch(r'[0-9]{1,32}',message_id):continue
            lease=uuid.uuid4().hex
            d.update(attempts=d.get('attempts',0)+1,lease=lease,lease_until=now+30)
            c.execute(outbox.update().where(outbox.c.id==row['id']).values(status='sending',delivery=d))
            job=dict(row,delivery=d);break
    if job is None:return False
    status='ambiguous';error=None
    try:
        response=client.patch(url+'/messages/'+message_id,json=job['payload']) if message_id else client.post(url,params={'wait':'true'},json=job['payload'])
        if 200<=response.status_code<300:
            try:message_id=response.json().get('id',message_id)
            except (ValueError,AttributeError):pass
            if isinstance(message_id,str) and re.fullmatch(r'[0-9]{1,32}',message_id):status='delivered'
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
    hooks.update({p:os.getenv('NATIVE_'+p.upper()+'_DISCORD_WEBHOOK','') for p in ('morning','smoothers')})
    with httpx.Client(timeout=10,follow_redirects=False) as client:
        while True:
            try:await asyncio.to_thread(deliver_one,db,client,hooks,enabled)
            except Exception as e:db.health('native_outbox','error',type(e).__name__)
            await asyncio.sleep(1)
