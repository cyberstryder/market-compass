import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from starlette.requests import Request

from test_projects import db
from native_morning_fixtures import signal_event
from compass import native_morning_worker as morning
from compass.native_routing import receipts
from compass.store import events
from compass.option_subscriptions import SubscriptionTrace
from compass.providers import Collectors
from compass.morning_history import poll_with_retry, record_failure
from test_morning_history import FIXTURE, page, NOW

SYMBOL='O:SPY260918C00600000'


def test_contract_ack_is_distinct_from_auth_and_first_quote():
    t=SubscriptionTrace(10,secret='private-key')
    t.request('subscribe',{SYMBOL},11);t.sent('subscribe',{SYMBOL},12)
    t.status({'status':'auth_success','message':'private-key authenticated'},13)
    assert t.snapshot(SYMBOL)['Q']['acknowledged_at'] is None
    t.status({'status':'success','message':'subscribed to: T.'+SYMBOL},14)
    assert t.snapshot(SYMBOL)['T']['acknowledged_at']==14
    assert t.snapshot(SYMBOL)['Q']['acknowledged_at'] is None
    t.data('Q',SYMBOL,15,2)  # Receiving an old quote does not prove freshness.
    assert t.snapshot(SYMBOL)['Q']['first_source_ts']==2
    assert t.snapshot(SYMBOL)['Q']['acknowledged_at'] is None
    t.status({'status':'success','message':'subscribed to: Q.'+SYMBOL},16)
    assert t.snapshot(SYMBOL)['Q']['acknowledged_at']==16
    t.request('unsubscribe',{SYMBOL},17);t.sent('unsubscribe',{SYMBOL},18)
    t.status({'status':'success','message':'subscribed to: Q.'+SYMBOL},19)
    assert t.snapshot(SYMBOL)['Q']['acknowledged_at'] is None
    assert list(t.queue)[-1]['matches_current_request'] is False
    assert 'private-key' not in json.dumps(list(t.queue))
    t.prune(set())
    assert not t.channels and len(t.queue)>10
    fresh=SubscriptionTrace(20)
    assert fresh.connection_id!=t.connection_id and not fresh.snapshot(SYMBOL)['Q']


def test_subscription_journal_is_idempotent_and_overflow_explicit(db):
    t=SubscriptionTrace(1,capacity=2)
    t.request('subscribe',{SYMBOL},2)
    assert len(t.queue)==2 and t.dropped==1
    c=SimpleNamespace(db=db)
    Collectors.subscription_batch(c,list(t.queue))
    Collectors.subscription_batch(c,list(t.queue))
    with db.tx() as conn:
        rows=list(conn.execute(select(events).where(events.c.kind=='option_subscription')).mappings())
        assert len(rows)==2
        assert all(r['payload']['connection_id']==t.connection_id for r in rows)


def test_morning_attempt_ids_survive_duplicate_and_do_not_expose_credentials(db,caplog):
    caplog.set_level('INFO',logger='uvicorn.error')
    morning.initialize(db)
    app=FastAPI();morning.install(app,db,SimpleNamespace(morning_enabled=True,native_morning_intake_token='private-token',morning_token=''))
    event=signal_event(stamp=int(time.time()*1000))
    with TestClient(app) as client:
        first=client.post('/hooks/native/morning/private-token',json=event,headers={'x-railway-request-id':'proxy-123'})
        second=client.post('/hooks/native/morning/private-token',json=event)
        assert first.status_code==second.status_code==200
        assert first.json()['status']=='accepted' and second.json()['status']=='duplicate'
        assert first.json()['request_id']!=second.json()['request_id']
        assert first.headers['x-compass-request-id']==first.json()['request_id']
    with db.tx() as c:
        rows=list(c.execute(select(events.c.payload).where(events.c.kind=='morning_request')).scalars())
        assert len(rows)==2 and {r['event_id'] for r in rows}=={event['event_id']}
        assert c.execute(select(func.count()).select_from(receipts)).scalar_one()==1
        assert c.execute(select(receipts.c.payload)).scalar_one()['request_id']==first.json()['request_id']
    logs='\n'.join(r.message for r in caplog.records if r.message.startswith('Morning request:'))
    assert 'private-token' not in logs and 'proxy-123' in logs
    assert logs.count('"stage": "committed"')==2
    assert logs.count('"stage": "response_ready"')==2


def test_disconnect_before_complete_body_has_no_committed_attempt(db,caplog):
    caplog.set_level('INFO',logger='uvicorn.error')
    morning.initialize(db)
    app=FastAPI();morning.install(app,db,SimpleNamespace(morning_enabled=True,native_morning_intake_token='private-token',morning_token=''))
    endpoint=next(r.endpoint for r in app.routes if r.path=='/hooks/native/morning')
    async def run():
        async def receive():return {'type':'http.disconnect'}
        request=Request({'type':'http','method':'POST','path':'/hooks/native/morning','headers':[(b'authorization',b'Bearer private-token')]},receive)
        with pytest.raises(HTTPException) as error:await endpoint(request)
        assert error.value.status_code==499
    asyncio.run(run())
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='morning_request')).scalar_one()==0
    assert 'client_disconnected' in caplog.text and '"stage": "committed"' not in caplog.text


def test_attempt_journal_failure_rolls_back_the_signal_and_receipt(db,monkeypatch):
    morning.initialize(db)
    event=signal_event(stamp=int(time.time()*1000))
    original=db.append
    def broken(c,kind,*args,**kwargs):
        if kind=='morning_request':raise RuntimeError('journal unavailable')
        return original(c,kind,*args,**kwargs)
    monkeypatch.setattr(db,'append',broken)
    with pytest.raises(RuntimeError,match='journal unavailable'):
        morning.accept(db,event,time.time(),request_meta={'request_id':'atomic-attempt'})
    with db.tx() as c:
        for table in (morning.source.signals,receipts,events):
            assert c.execute(select(func.count()).select_from(table)).scalar_one()==0


def test_http_cancellation_still_records_actual_worker_commit(db,monkeypatch,caplog):
    from threading import Event
    caplog.set_level('INFO',logger='uvicorn.error')
    morning.initialize(db)
    app=FastAPI();morning.install(app,db,SimpleNamespace(morning_enabled=True,native_morning_intake_token='private-token',morning_token=''))
    endpoint=next(r.endpoint for r in app.routes if r.path=='/hooks/native/morning')
    started,release,finished=Event(),Event(),Event()
    original=morning.accept
    def delayed(*args,**kwargs):
        started.set()
        assert release.wait(5)
        try:return original(*args,**kwargs)
        finally:finished.set()
    monkeypatch.setattr(morning,'accept',delayed)
    event=signal_event(stamp=int(time.time()*1000))
    async def run():
        async def receive():return {'type':'http.request','body':json.dumps(event).encode(),'more_body':False}
        request=Request({'type':'http','method':'POST','path':'/hooks/native/morning','headers':[(b'authorization',b'Bearer private-token')]},receive)
        task=asyncio.create_task(endpoint(request))
        assert await asyncio.to_thread(started.wait,2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        release.set()
        assert await asyncio.to_thread(finished.wait,3)
    asyncio.run(run())
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='morning_request')).scalar_one()==1
    stages=[json.loads(r.message.split(': ',1)[1])['stage'] for r in caplog.records if r.message.startswith('Morning request:')]
    assert 'interrupted' in stages and 'committed' in stages and 'response_ready' not in stages


def test_morning_transport_retry_preserves_cursor_and_records_recovery(db):
    calls=[]
    with db.tx() as c:db.put(c,'morning_history:events',{'next':'a-cursor','last_complete_at':NOW-10})
    def handler(request):
        calls.append(request.url.params['after'])
        if len(calls)==1:raise httpx.ConnectTimeout('private URL and token')
        return httpx.Response(200,json=page('events',[FIXTURE['signal']]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await poll_with_retry(db,SimpleNamespace(morning_url='https://source.example',morning_token='token'),client,'events')
    result=asyncio.run(run())
    assert calls==['a-cursor','a-cursor']
    assert result['status']=='scan_complete' and result['failures']==1
    assert result['consecutive_failures']==0 and result['recovered_at']>NOW
    assert result['last_failure']['cursor']=='a-cursor'
    assert 'private URL' not in json.dumps(result)


def test_failure_backoff_keeps_last_success_and_never_clears_cursor(db):
    with db.tx() as c:db.put(c,'morning_history:stock',{'next':'pending','last_success_at':1,'last_complete_at':.5})
    for i in range(5):record_failure(db,'stock',httpx.ReadTimeout('secret'),10+i)
    with db.tx() as c:saved=db.get(c,'morning_history:stock')
    assert saved['next']=='pending' and saved['last_success_at']==1 and saved['last_complete_at']==.5
    assert saved['consecutive_failures']==5 and saved['retry_after']==74
    assert 'secret' not in json.dumps(saved)
