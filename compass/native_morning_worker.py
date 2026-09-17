"""Native Morning receiver and independent option workers; shadow delivery only."""
import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import nullcontext
import httpx
from sqlalchemy import select, func, text
from .native_morning import store as source
from .native_morning.models import Event
from .native_morning.frames import Frame, accept_frame, FRAME_LIMIT, TransactionEngine
from .native_morning.candidate_models import ResearchBatch
from .native_morning.candidate_store import accept_research
from .native_morning.discord import signal_message
from .native_morning.options import AlpacaQuotes, OptionsConfig, POLICY, option_text, PENDING_TEXT
from .native_morning.option_history import schedule_history, sample_batch
from .native_outbox import queue, owner as notification_owner
from .projects import records
from .native_routing import routing_guard, allowed, record, day

VERSION='native-morning-v1'


def initialize(db):
    with db.engine.begin() as c:
        if c.dialect.name=='postgresql':
            c.execute(text('SELECT pg_advisory_xact_lock(8675309001)'))
        source.metadata.create_all(c)


def accept(db,raw,now,body_size=0,origin='direct',request_meta=None):
    kind=raw.get('event_type')
    payload=(Frame if kind=='frame' else ResearchBatch if kind=='research' else Event).model_validate(raw)
    stamp=payload.observed_at_ms if kind in ('frame','research') else payload.observation.observed_at_ms if payload.observation else payload.signal.signal_at_ms
    if stamp>(now+60)*1000: raise ValueError('Future input clock')
    session_stamp=payload.session.session_open_ms if kind in ('frame','research') else payload.signal.signal_at_ms
    with routing_guard(db,shared=True) as guard:
        if not allowed(db,guard,origin,day(session_stamp/1000)):
            if origin=='source_summary_relay':return {'status':'route_disabled'}
            raise source.PayloadConflict('Direct intake is disabled for this session')
        result=commit_input(db,payload,kind,now,body_size,guard)
        record(db,guard,payload,origin,now,result,request_meta=request_meta)
        if request_meta:
            # Per-HTTP-attempt identity is separate from deduplicated source
            # receipts. Commit it atomically, including successful duplicates.
            audit={**request_meta,'event_id':payload.event_id,'event_type':payload.event_type,
                   'status':result['status'],'body_bytes':body_size,'write_at':time.time()}
            db.append(guard,'morning_request','tradingview','MORNING',now,audit,
                      'morning-request:'+request_meta['request_id'])
        db.put(guard,'native_morning:last_intake',{'at':now,'kind':kind,'origin':origin,'status':result['status']})
    if request_meta:
        # Runs in the worker even if the HTTP client has disconnected while the
        # transaction completes. This log is emitted only after commit returns.
        logging.getLogger('uvicorn.error').info('Morning request: %s',json.dumps(
            {**audit,'stage':'committed','committed_at':time.time()},sort_keys=True))
    return result


def commit_input(db,payload,kind,now,body_size,connection):
    received=int(now*1000)
    # Keep the routing lock, input, preview and receipt on one connection.
    # Holding a guard connection while each webhook checks out another can
    # exhaust the entire pool during a simultaneous TradingView burst.
    engine=TransactionEngine(connection)
    if kind=='frame':
        if body_size>FRAME_LIMIT: raise ValueError('Frame too large')
        result=accept_frame(engine,payload,received,body_size,False,POLICY)
    elif kind=='research': result=accept_research(engine,payload,received)
    else:
        message=signal_message(payload.signal,received,POLICY) if kind=='signal' else None
        result=source.accept_event(engine,payload,message,False,received)
    flush_previews(db,now,connection)
    return result


def flush_previews(db,now,connection=None):
    # Resume even if a crash occurred after the source event committed.
    with (nullcontext(connection) if connection is not None else db.tx()) as c:
        pending=c.execute(select(source.outbox).where(source.outbox.c.status=='disabled').limit(200).with_for_update(skip_locked=True)).mappings().all()
        for row in pending:
            message=json.loads(row['payload_json'])
            if isinstance(message,dict):
                message.pop('_option_policy',None)
                stamp=c.execute(select(source.signals.c.signal_at_ms).where(source.signals.c.signal_id==row['event_id'].removesuffix('-signal'))).scalar_one_or_none()
                queue(db,c,'morning',row['event_id'],message,now,event_time=stamp/1000 if stamp else None)
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
    quote=provider.lookup(signal,POLICY,compare=True) if job['attempts']<3 else {'status':'unavailable','reason':'native_retry_limit'}
    completed=provider.clock()
    with db.engine.begin() as c:
        updated=c.execute(source.option_jobs.update().where(source.option_jobs.c.event_id==job['event_id'],source.option_jobs.c.lease_token==lease)
            .values(status='complete',quote_json=source.canonical(quote),lease_until_ms=0,lease_token=None).returning(source.option_jobs.c.event_id)).first()
        if updated:
            schedule_history(c,signal,quote,completed)
            message=signal_message(signal,int(now*1000),None)
            message['content']+='\n\n'+option_text(quote)
            queue(db,c,'morning',job['event_id']+'-option',message,completed/1000,parent_event=job['event_id'],event_time=signal['signal_at_ms']/1000)
    return True


def report(db,now):
    with db.tx() as c:
        values={name:c.execute(select(func.count()).select_from(table)).scalar_one() for name,table in
                [('signals',source.signals),('events',source.events),('samples',source.option_samples),
                 ('expiration_tracks',source.expiration_tracks),('expiration_samples',source.expiration_samples)]}
        values['expiration_study']='atm_expiration_comparison_v1'
        values['sample_results']={}
        for raw in c.execute(select(source.option_samples.c.quote_json).where(source.option_samples.c.quote_json.is_not(None)).order_by(source.option_samples.c.due_at_ms.desc()).limit(1000)).scalars():
            result=json.loads(raw);status=result.get('status','unknown')
            values['sample_results'][status]=values['sample_results'].get(status,0)+1
        values['sample_result_limit']=1000
        values['option_jobs']={status:n for status,n in c.execute(select(source.option_jobs.c.status,func.count()).group_by(source.option_jobs.c.status))}
        sending=notification_owner(db,c,'morning',now) is not None and os.getenv('NATIVE_PROGRAM_SEND_ENABLED','false').lower()=='true'
        db.put(c,VERSION+':status',dict(values,at=now,mode='official_notifications' if sending else 'shadow',direct_intake_ready=True,bridge='fresh_source_signals',sending_enabled=sending))
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
    from fastapi.responses import JSONResponse
    from starlette.requests import ClientDisconnect
    from sqlalchemy.exc import SQLAlchemyError
    # A dedicated TradingView credential can be rotated without disrupting the
    # read-only source comparison feed. Preserve legacy intake until configured.
    intake_token=cfg.native_morning_intake_token or cfg.morning_token
    @app.post('/hooks/native/morning')
    @app.post('/hooks/native/morning/{token}')
    async def receive(request:Request,token:str=""):
        supplied=("Bearer "+token) if token else request.headers.get('authorization','')
        if not intake_token or not secrets.compare_digest(supplied.encode(),('Bearer '+intake_token).encode()):
            raise HTTPException(401,'Invalid native intake credentials')
        request_id=uuid.uuid4().hex
        proxy_id=request.headers.get('x-railway-request-id') or request.headers.get('x-request-id')
        proxy_id=proxy_id if proxy_id and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',proxy_id) else None
        meta={'request_id':request_id,'proxy_request_id':proxy_id,'received_at':time.time()}
        log=logging.getLogger('uvicorn.error')
        log.info('Morning request: %s',json.dumps({**meta,'stage':'received'},sort_keys=True))
        body=bytearray();outcome='interrupted';code=None;event_id=None
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>32768:raise HTTPException(413,'Input too large')
            raw=json.loads(body)
            if not isinstance(raw,dict): raise ValueError('Expected object')
            candidate=raw.get('event_id')
            if isinstance(candidate,str):event_id=candidate[:600].replace(intake_token,'[redacted]')
            result=await asyncio.to_thread(accept,db,raw,time.time(),len(body),request_meta=meta)
            outcome='response_ready';code=200
            return JSONResponse({**result,'request_id':request_id},headers={'X-Compass-Request-ID':request_id})
        except ClientDisconnect:
            outcome='client_disconnected';code=499
            raise HTTPException(499,'Client disconnected before input completed') from None
        except source.PayloadConflict:
            outcome='identity_conflict';code=409
            raise HTTPException(code,'Native source identity conflict') from None
        except (ValueError,TypeError,KeyError):
            outcome='invalid_payload';code=422
            raise HTTPException(code,'Invalid Morning payload') from None
        except SQLAlchemyError:
            outcome='not_committed';code=503
            raise HTTPException(code,'Native input not committed') from None
        except HTTPException as error:
            outcome='rejected';code=error.status_code
            raise
        finally:
            log.info('Morning request: %s',json.dumps({**meta,'stage':outcome,'http_status':code,
                'event_id':event_id,'body_bytes':len(body),'elapsed_ms':round((time.time()-meta['received_at'])*1000)},sort_keys=True))
