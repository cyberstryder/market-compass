"""Native Morning receiver and independent option workers; shadow delivery only."""
import asyncio
import json
import logging
import os
import secrets
import time
import uuid
import httpx
from sqlalchemy import select, func
from .native_morning import store as source
from .native_morning.models import Event
from .native_morning.frames import Frame, accept_frame, FRAME_LIMIT
from .native_morning.candidate_models import ResearchBatch
from .native_morning.candidate_store import accept_research
from .native_morning.discord import signal_message
from .native_morning.options import AlpacaQuotes, OptionsConfig, POLICY, option_text, PENDING_TEXT
from .native_morning.option_history import schedule_history, sample_batch
from .native_outbox import queue
from .projects import records

VERSION='native-morning-v1'


def initialize(db):
    source.metadata.create_all(db.engine)


def accept(db,raw,now,body_size=0,origin='direct'):
    kind=raw.get('event_type')
    payload=(Frame if kind=='frame' else ResearchBatch if kind=='research' else Event).model_validate(raw)
    stamp=payload.observed_at_ms if kind in ('frame','research') else payload.observation.observed_at_ms if payload.observation else payload.signal.signal_at_ms
    if stamp>(now+60)*1000: raise ValueError('Future input clock')
    received=int(now*1000)
    if kind=='frame':
        if body_size>FRAME_LIMIT: raise ValueError('Frame too large')
        result=accept_frame(db.engine,payload,received,body_size,False,POLICY)
    elif kind=='research': result=accept_research(db.engine,payload,received)
    else:
        message=signal_message(payload.signal,received,POLICY) if kind=='signal' else None
        result=source.accept_event(db.engine,payload,message,False,received)
    flush_previews(db,now)
    with db.tx() as c:
        db.put(c,'native_morning:last_intake',{'at':now,'kind':kind,'origin':origin,'status':result['status']})
    return result


def flush_previews(db,now):
    # Resume even if a crash occurred after the source event committed.
    with db.tx() as c:
        pending=c.execute(select(source.outbox).where(source.outbox.c.status=='disabled').limit(200).with_for_update(skip_locked=True)).mappings().all()
        for row in pending:
            message=json.loads(row['payload_json'])
            if isinstance(message,dict):
                message.pop('_option_policy',None)
                queue(db,c,'morning',row['event_id'],message,now)
            c.execute(source.outbox.update().where(source.outbox.c.event_id==row['event_id']).values(status='shadow_previewed'))


def bridge(db,now):
    """Source summary relay supplies fresh signals until direct TradingView cutover.

    Receipt is Compass receipt, never rewritten to the original source receipt.
    Signals recovered without original entry receipt never fabricate an entry.
    """
    with db.tx() as c:
        activation=db.get(c,VERSION+':activation')
        if activation is None:
            activation=now;db.put(c,VERSION+':activation',activation)
        found=c.execute(select(records.c.payload).where(records.c.project=='morning',
            records.c.source_ts>=max(activation,now-120)).order_by(records.c.source_ts).limit(100)).scalars().all()
    for p in found:
        with db.engine.connect() as c:
            if c.execute(select(source.signals.c.signal_id).where(source.signals.c.signal_id==p['id'])).first():continue
        if not p.get('initial_received_at_ms'): continue
        raw={'schema_version':1,'event_type':'signal','event_id':p['id']+'-signal','signal':p['original']}
        accept(db,raw,now,origin='source_summary_relay')


def enrich(db,provider,now):
    """Own option selection, independent of Discord and source option choice."""
    with db.engine.begin() as c:
        found=c.execute(select(source.signals).outerjoin(source.option_jobs,
            source.signals.c.signal_id==source.option_jobs.c.signal_id).where(
                source.option_jobs.c.signal_id.is_(None),source.signals.c.initial_received_at_ms.is_not(None),
                source.signals.c.is_test.is_(False)).order_by(source.signals.c.signal_at_ms).limit(10)).mappings().all()
        for row in found:
            source.insert_once(c,source.option_jobs,dict(event_id=row['signal_id']+'-signal',signal_id=row['signal_id'],
                message_id=None,policy_json=source.canonical(POLICY),status='pending',attempts=0,lease_until_ms=0))
        job=c.execute(select(source.option_jobs).where(
            (source.option_jobs.c.status=='pending') | ((source.option_jobs.c.status=='fetching') & (source.option_jobs.c.lease_until_ms<=int(now*1000))))
            .order_by(source.option_jobs.c.event_id).limit(1).with_for_update(skip_locked=True)).mappings().first()
        if not job:return False
        lease=uuid.uuid4().hex
        c.execute(source.option_jobs.update().where(source.option_jobs.c.event_id==job['event_id']).values(status='fetching',
            attempts=job['attempts']+1,lease_token=lease,lease_until_ms=int((now+30)*1000)))
        signal=json.loads(c.execute(select(source.signals.c.signal_json).where(source.signals.c.signal_id==job['signal_id'])).scalar_one())
    quote=provider.lookup(signal,POLICY) if job['attempts']<3 else {'status':'unavailable','reason':'native_retry_limit'}
    completed=provider.clock()
    with db.engine.begin() as c:
        updated=c.execute(source.option_jobs.update().where(source.option_jobs.c.event_id==job['event_id'],source.option_jobs.c.lease_token==lease)
            .values(status='complete',quote_json=source.canonical(quote),lease_until_ms=0,lease_token=None).returning(source.option_jobs.c.event_id)).first()
        if updated:
            schedule_history(c,signal,quote,completed)
            message=signal_message(signal,int(now*1000),None)
            message['content']+='\n\n'+option_text(quote)
            queue(db,c,'morning',job['event_id']+'-option',message,completed/1000,parent_event=job['event_id'])
    return True


def report(db,now):
    with db.tx() as c:
        values={name:c.execute(select(func.count()).select_from(table)).scalar_one() for name,table in
                [('signals',source.signals),('events',source.events),('samples',source.option_samples)]}
        values['sample_results']={}
        for raw in c.execute(select(source.option_samples.c.quote_json).where(source.option_samples.c.quote_json.is_not(None)).order_by(source.option_samples.c.due_at_ms.desc()).limit(1000)).scalars():
            result=json.loads(raw);status=result.get('status','unknown')
            values['sample_results'][status]=values['sample_results'].get(status,0)+1
        values['sample_result_limit']=1000
        values['option_jobs']={status:n for status,n in c.execute(select(source.option_jobs.c.status,func.count()).group_by(source.option_jobs.c.status))}
        db.put(c,VERSION+':status',dict(values,at=now,mode='shadow',direct_intake_ready=True,bridge='fresh_source_signals',sending_enabled=False))
    logging.getLogger('uvicorn.error').info('Native Morning: %s',values)


async def run(db,cfg,role="intake"):
    owner=uuid.uuid4().hex
    options=OptionsConfig(enabled=bool(cfg.alpaca_key and cfg.alpaca_secret),key_id=cfg.alpaca_key,secret_key=cfg.alpaca_secret)
    with httpx.Client(timeout=2,follow_redirects=False) as client:
        provider=AlpacaQuotes(options,client)
        last=0
        while True:
            try:
                with db.tx() as c: active=db.lease(c,VERSION+":"+role,owner,90)
                if active:
                    if role=="intake":
                        await asyncio.to_thread(flush_previews,db,time.time())
                        if cfg.morning_url and os.getenv("NATIVE_MORNING_RELAY_ENABLED","true").lower()=="true":
                            await asyncio.to_thread(bridge,db,time.time())
                        await asyncio.to_thread(enrich,db,provider,time.time())
                    else:
                        await asyncio.to_thread(sample_batch,db.engine,provider)
                    if time.time()-last>=60:
                        await asyncio.to_thread(report,db,time.time());last=time.time()
            except Exception as e: db.health(VERSION,'error',type(e).__name__)
            await asyncio.sleep(1)


def install(app,db,cfg):
    from fastapi import Request,HTTPException
    from sqlalchemy.exc import SQLAlchemyError
    @app.post('/hooks/native/morning')
    @app.post('/hooks/native/morning/{token}')
    async def receive(request:Request,token:str=""):
        supplied=("Bearer "+token) if token else request.headers.get('authorization','')
        if not cfg.morning_token or not secrets.compare_digest(supplied.encode(),('Bearer '+cfg.morning_token).encode()):
            raise HTTPException(401,'Invalid native intake credentials')
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>32768: raise HTTPException(413,'Input too large')
        try:
            raw=json.loads(body)
            if not isinstance(raw,dict): raise ValueError('Expected object')
            return await asyncio.to_thread(accept,db,raw,time.time(),len(body))
        except source.PayloadConflict: raise HTTPException(409,'Native source identity conflict') from None
        except (ValueError,TypeError,KeyError): raise HTTPException(422,'Invalid Morning payload') from None
        except SQLAlchemyError: raise HTTPException(503,'Native input not committed') from None
