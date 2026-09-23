import asyncio
import json
from datetime import datetime, timezone

import pytest

from compass.assistant_options import request, sample, research
from compass.assistant_context import build_input
from compass.config import Config
from compass.providers import Collectors

NOW = datetime(2026,9,21,19,tzinfo=timezone.utc).timestamp()

@pytest.mark.parametrize('question,lo,hi,side', [
    ('what do you see for QQQ puts today till close for 0DTE',0,0,'put'),
    ('QQQ LEAPS calls',365,1095,'call'), ('SPY 45 DTE puts',45,45,'put'),
    ('QQQ 7-60 DTE options',7,60,None), ('QQQ 6 month calls',173,187,'call'),
    ('QQQ puts expiring 2026-09-25',4,4,'put'), ('QQQ options',0,1095,None)])
def test_horizon(question,lo,hi,side):
    r=request(question,NOW)
    assert (r['min_dte'],r['max_dte'],r['side'])==(lo,hi,side)


def test_invalid_and_exchange_day():
    assert request('QQQ trend?',NOW) is None
    assert request('QQQ 2026-02-30 puts',NOW)['status']=='invalid_expiry'
    assert request('QQQ 2026-09-20 puts',NOW)['status']=='invalid_expiry'
    assert request('QQQ 2000 DTE calls',NOW)['status']=='invalid_expiry'
    late=datetime(2026,9,22,1,tzinfo=timezone.utc).timestamp()
    assert request('QQQ 0DTE puts',late)['expiry_start']=='2026-09-21'


def row(expiry='2026-09-21',side='put',ts=NOW,bid=1,ask=1.1):
    return dict(symbol='QQQ'+expiry+side,expiry=expiry,type=side,strike=740,
        quote=dict(ts=ts,bid=bid,ask=ask),multiplier=100)


def test_expiry_side_and_quote_evidence_are_preserved_in_budget():
    req=request('QQQ 0DTE puts',NOW)
    rows=[row(),row(side='call'),row(expiry='2026-09-22')]
    data=sample(rows,req,741,NOW,'alpaca')
    assert data['matching_contracts']==1
    assert data['candidates'][0]['quote_status']=='fresh'
    for r,status in [(row(ts=NOW-100),'stale'),(row(ts=NOW+1),'unavailable'),(row(bid=2),'unavailable')]:
        assert sample([r],req,741,NOW,'alpaca')['candidates'][0]['quote_status']==status
    value=dict(request=req,symbols={'QQQ':data})
    packed=json.loads(build_input('QQQ?',{'option_research':value})[0])['market_context']
    assert packed['option_research']==value


def test_lookup_uses_requested_expirations_and_does_not_change_strategy_config(monkeypatch):
    cfg=Config(local=True,alpaca_key='fake',alpaca_secret='fake')
    calls=[]
    async def fetch(self,symbol,lo,hi,page_limit):
        calls.append((symbol,lo,hi,page_limit))
        return [row(expiry=lo)],False
    monkeypatch.setattr(Collectors,'alpaca_chain',fetch)
    for question,expected in [('QQQ 0DTE puts',('2026-09-21','2026-09-21')),
                              ('QQQ LEAPS puts',('2027-09-21','2029-09-20'))]:
        req=request(question,NOW)
        result=asyncio.run(research(cfg,['QQQ'],req,{},NOW))
        assert calls[-1]==('QQQ',*expected,4)
        assert result['symbols']['QQQ']['complete'] is False
        assert len(result['symbols']['QQQ']['candidates'])==1
    assert cfg.ideas_min_dte==1 and cfg.chain_dte==90


def test_provider_error_is_missing_evidence_not_research_prohibition(monkeypatch):
    async def fail(*args,**kwargs): raise RuntimeError('secret-credential')
    monkeypatch.setattr(Collectors,'alpaca_chain',fail)
    result=asyncio.run(research(Config(local=True,alpaca_key='fake',alpaca_secret='fake'),
        ['QQQ'],request('QQQ 0DTE puts',NOW),{},NOW))
    assert result['symbols']['QQQ']['status']=='lookup_unavailable'
    assert 'secret' not in json.dumps(result)

@pytest.mark.parametrize('endpoint',['/api/ask','/api/ask/stream'])
def test_qqq_request_reaches_model_with_independent_research(tmp_path,monkeypatch,endpoint):
    import httpx
    from fastapi.testclient import TestClient
    from compass.app import create_app
    calls=[]
    async def lookup(cfg,scope,req,quotes,now,db=None):
        assert scope==['QQQ'] and req['min_dte']==req['max_dte']==0
        return dict(request=req,symbols={'QQQ':dict(status='available',candidates=[row()])})
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,headers,json):
            calls.append(json)
            return httpx.Response(200,json={'status':'completed','output':[{'content':[
                {'type':'output_text','text':'Conditional QQQ put thesis.'}]}]})
    monkeypatch.setattr('compass.app.option_research',lookup)
    monkeypatch.setattr('compass.app.httpx.AsyncClient',lambda **kwargs:Client())
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'ask.db'),
        password='test-password-with-enough-length',secret='test-signing-secret-with-enough-length',openai='fake')
    app=create_app(cfg)
    with TestClient(app) as client:
        client.post('/login',json={'password':cfg.password})
        response=client.post(endpoint,json={'question':'what do you see for QQQ puts today till close for 0DTE'})
        assert response.status_code==200
    assert len(calls)==1
    context=json.loads(calls[0]['input'])['market_context']
    assert context['option_research']['symbols']['QQQ']['candidates']
    assert 'Strategy min/max DTE, zero_dte max_entries=0' in calls[0]['instructions']
    assert 'not research prohibitions' in calls[0]['instructions']
    assert 'No tools or follow-up scans can be invoked' in calls[0]['instructions']

@pytest.mark.parametrize('provider',['alpaca','massive'])
def test_provider_queries_honor_explicit_range_and_page_bound(provider):
    async def run():
        collector=Collectors(None,Config(local=True))
        calls=[]
        async def get(url,headers=None,params=None):
            calls.append(dict(params))
            if provider=='massive': return {'results':[], 'next_url':'https://api.massive.com/next?cursor=next'}
            return {'option_contracts':[], 'snapshots':{}, 'next_page_token':'next'}
        collector.get=get
        try:
            rows,complete=await getattr(collector,provider+'_chain')('QQQ','2027-09-21','2029-09-20',page_limit=1)
            assert rows==[] and complete is False
            assert len(calls)==(1 if provider=='massive' else 2)
            for params in calls:
                assert '2027-09-21' in params.values() and '2029-09-20' in params.values()
        finally: await collector.close()
    asyncio.run(run())


def test_web_without_credentials_receives_collector_option_evidence(tmp_path,monkeypatch):
    from compass.assistant_options import collect_requests
    from compass.store import Store
    db=Store('sqlite:///'+str(tmp_path/'queue.db')); db.initialize()
    web=Config(local=True,role='web',alpaca_key='',alpaca_secret='',massive='')
    worker=Collectors(db,Config(local=True,alpaca_key='fake',alpaca_secret='fake'))
    async def fetch(self,symbol,lo,hi,page_limit): return [row(expiry=lo)],True
    monkeypatch.setattr(Collectors,'alpaca_chain',fetch)
    async def run():
        task=asyncio.create_task(research(web,['QQQ'],request('QQQ LEAPS puts',NOW),{},NOW,db=db))
        for _ in range(30):
            await asyncio.sleep(.05)
            await collect_requests(worker)
            if task.done(): break
        try:
            result=await asyncio.wait_for(task,2)
            assert result['symbols']['QQQ']['candidates'][0]['expiry']=='2027-09-21'
            assert result['symbols']['QQQ']['source']=='alpaca'
            with db.tx() as c: assert not db.prefix(c,'ask-options:')
        finally: await worker.close()
    asyncio.run(run())


def test_queue_timeout_cleans_request_and_preserves_research_scope(tmp_path):
    from compass.assistant_options import queued_research
    from compass.store import Store
    db=Store('sqlite:///'+str(tmp_path/'queue.db')); db.initialize()
    req=request('QQQ 0DTE puts',NOW)
    result=asyncio.run(queued_research(db,['QQQ'],req,{},NOW,timeout=.02))
    assert result['status']=='collector_lookup_timeout' and result['request']==req
    with db.tx() as c: assert not db.prefix(c,'ask-options:')


def test_calendar_day_range_and_combined_horizons(monkeypatch):
    now=datetime(2026,9,23,19,tzinfo=timezone.utc).timestamp()
    req=request('Read-only QQQ LEAPS calls, 365–900 calendar days to expiration as of 2026-09-23',now)
    assert (req['min_dte'],req['max_dte'],req['expiry_end'])==(365,900,'2029-03-11')
    combined=request('QQQ 0DTE puts and QQQ LEAPS calls, 365–900 calendar days',now)
    assert combined['status']=='multiple'
    calls=[]
    async def fetch(self,symbol,lo,hi,page_limit):
        calls.append((lo,hi))
        return [row(expiry=lo,side='put' if lo=='2026-09-23' else 'call',ts=now)],True
    monkeypatch.setattr(Collectors,'alpaca_chain',fetch)
    result=asyncio.run(research(Config(local=True,alpaca_key='fake',alpaca_secret='fake'),['QQQ'],combined,{},now))
    assert calls==[('2026-09-23','2026-09-23'),('2027-09-23','2029-03-11')]
    from compass.assistant_progress import evidence_summary
    cards=evidence_summary(json.dumps({'market_context':{'question_scope':{'symbols':['QQQ']},'option_research':result}}))['cards']
    options=[c for c in cards if c['label']=='Requested options']
    assert len(options)==2
    assert [next(v['value'] for v in c['values'] if v['label']=='Side') for c in options]==['put','call']
    assert request('QQQ 0DTE puts LEAPS calls',now)['status']=='ambiguous_horizon'
