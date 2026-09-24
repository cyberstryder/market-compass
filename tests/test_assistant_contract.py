import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from compass.app import create_app
from compass.assistant_options import request, research
from compass.assistant_contract import contract_symbol
from compass.config import Config
from compass.providers import Collectors

NOW=datetime(2026,9,23,20,tzinfo=timezone.utc).timestamp()
QUESTION='There is a 10/16/26 48C for $U, is that a good play?'


def test_exact_intent_and_invalid_requests():
    req=request(QUESTION,NOW)
    assert (req['side'],req['strike'],req['expiry_start'],req['expiry_end'])==('call',48,'2026-10-16','2026-10-16')
    assert contract_symbol('U',req)=='U261016C00048000'
    assert request('10/16/2026 48.5P for $U',NOW)['strike']==48.5
    assert request('2/30/26 48C for $U',NOW)['status']=='invalid_expiry'
    assert request('10/16/26 48C and 50P for $U',NOW)['status']=='ambiguous_contract'
    assert request('10/16/26 48C puts for $U',NOW)['status']=='ambiguous_contract'


@pytest.mark.parametrize('provider',['alpaca','massive'])
@pytest.mark.parametrize('wrong_contract',[False,True])
def test_exact_lookup_never_substitutes_and_retains_clocks(monkeypatch,provider,wrong_contract):
    calls=[]
    clock=time.time()-3600
    ticker='U261016C00048000'
    async def get(self,url,headers=None,params=None):
        calls.append(url)
        if '/v2/stocks/' in url:
            if url.endswith('/bars'): return {'bars':[]}
            return {'latestQuote':{'bp':47,'ap':48,'t':clock},'latestTrade':{'p':47.5,'t':clock}}
        if provider=='massive':
            return {'results':{'details':{'ticker':'O:'+ticker,'expiration_date':'2026-10-16',
                'strike_price':49 if wrong_contract else 48,'contract_type':'call','shares_per_contract':100},
                'last_quote':{'bid':2,'ask':2.1,'last_updated':clock},'greeks':{'delta':.5,'theta':-.03},
                'implied_volatility':.6}}
        if '/contracts/' in url:
            return {'symbol':ticker,'expiration_date':'2026-10-16','strike_price':'49' if wrong_contract else '48','type':'call','size':100}
        assert params=={'symbols':ticker,'feed':'opra'}
        return {'snapshots':{ticker:{'latestQuote':{'bp':2,'ap':2.1,'t':clock},'greeks':{'delta':.5,'theta':-.03},'impliedVolatility':.6}}}
    monkeypatch.setattr(Collectors,'get',get)
    cfg=Config(local=True,alpaca_key='fake',alpaca_secret='fake',massive='fake' if provider=='massive' else '')
    result=asyncio.run(research(cfg,['U'],request(QUESTION,NOW),{},NOW))['symbols']['U']
    assert result['requested_contract']==ticker
    assert result['underlying_evidence']['snapshot']['quote_ts']==clock
    assert result['underlying_evidence']['daily_history']['status']=='warming_up'
    if wrong_contract:
        assert result['status']=='exact_contract_unavailable' and result['candidates']==[]
    else:
        candidate=result['candidates'][0]
        assert candidate['quote_ts']==clock and candidate['quote_status']=='stale'
        assert candidate['greeks']['theta']==-.03 and candidate['implied_volatility']==.6
    assert all('/orders' not in u for u in calls)


def test_option_failure_does_not_discard_underlying(monkeypatch):
    async def get(self,url,headers=None,params=None):
        if '/options/' in url: raise RuntimeError('secret')
        return {'bars':[]} if url.endswith('/bars') else {'latestTrade':{'p':48,'t':NOW}}
    monkeypatch.setattr(Collectors,'get',get)
    cfg=Config(local=True,alpaca_key='fake',alpaca_secret='fake')
    result=asyncio.run(research(cfg,['U'],request(QUESTION,NOW),{},NOW))['symbols']['U']
    assert result['status']=='lookup_unavailable'
    assert result['underlying_evidence']['snapshot']['last_trade']==48
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('endpoint',['/api/ask','/api/ask/stream'])
def test_original_question_reaches_answer_with_u_only(tmp_path,monkeypatch,endpoint):
    calls=[]
    async def lookup(cfg,scope,req,quotes,now,db=None):
        assert scope==['U']
        assert req['strike']==48 and req['side']=='call' and req['expiry_start']=='2026-10-16'
        return dict(request=req,symbols={'U':dict(status='available',underlying_evidence={
            'snapshot':{'status':'available','source':'alpaca','bid':47,'ask':48,'quote_ts':NOW},
            'daily_history':{'status':'ready','source':'alpaca','through':'2026-09-22','sma20':46}})})
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,headers,json):
            calls.append(json)
            return httpx.Response(200,json={'status':'completed','output':[{'content':[{'type':'output_text','text':'U research'}]}]})
    monkeypatch.setattr('compass.app.option_research',lookup)
    monkeypatch.setattr('compass.app.httpx.AsyncClient',lambda **kwargs:Client())
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'ask.db'),password='long-test-password',
               secret='long-test-signing-secret',openai='fake')
    with TestClient(create_app(cfg)) as client:
        client.post('/login',json={'password':cfg.password})
        response=client.post(endpoint,json={'question':QUESTION})
        assert response.status_code==200
        assert '2026-10-16' in response.text and 'U' in response.text
    assert calls==[]  # Exact trade assessment uses verified fields and deterministic arithmetic.


def test_assessment_units_and_no_automated_eligibility():
    from compass.assistant_contract import assessment
    req=request(QUESTION,NOW)
    ctx={'question_scope':{'symbols':['U']},'option_research':{'request':req,'symbols':{'U':{
        'candidates':[{'symbol':'O:U261016C00048000','bid':1.3,'ask':1.41,'multiplier':100,'quote_status':'stale','quote_ts':NOW,
                       'greeks':{'theta':-.0447},'source':'massive'}],
        'underlying_evidence':{'snapshot':{'last_trade':46.3954,'trade_ts':NOW,'source':'alpaca'},
                               'daily_history':{'status':'ready','close':43.11,'sma20':42.658,'sma50':39.432,'through':'2026-09-22'}}}}}}
    answer=assessment(QUESTION,ctx)
    assert '$141.00 per contract' in answer and 'break-even $49.41' in answer
    assert 'time decay works against' in answer and 'theta -0.0447' in answer
    assert 'automated' not in answer and 'eligibility' not in answer
    assert 'stale' in answer and 'not a synchronized trade setup' in answer
    assert assessment('What is the quote?',ctx) is None
