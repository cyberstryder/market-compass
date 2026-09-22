"""Regression: a direction question must not become a paper admission check."""
import copy
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from compass.app import create_app
from compass.assistant_context import build_input, MAX_INPUT_BYTES
from compass.assistant_research import request_profile
from compass.config import Config


def test_direction_keeps_market_evidence_without_unrelated_plan_gates():
    source = dict(asof=123, technical_context={'SPY':{'asof':120,'status':'ready','ema9':774,'ema21':773}},
        exposure={'SPY':{'source':'massive','asof':119,'coverage':.4,'gex':12}},
        risk={'many_accounts':'x'*20000}, limits={'daily_loss':300}, positions=[{'symbol':'MNQ'}],
        spy_brief={'symbol':'SPY','generated_at':100,'decision':'WAIT','options':{'call':{'status':'unavailable'}},
            'plans':{'call':{'rule':'15-minute breakout'}},
            'chart_levels':[{'label':'PM high','spy':774.67},{'label':'Call stop','spy':773.2}]})
    original=copy.deepcopy(source)
    value, diagnostic=build_input('Will SPY rise or fall today?',source)
    ctx=json.loads(value)['market_context']
    assert ctx['research_request']['mode']=='market_research'
    assert ctx['technical_context']==source['technical_context']
    assert ctx['exposure']==source['exposure']
    assert ctx['spy_brief']['generated_at']==100
    assert ctx['spy_brief']['chart_levels']==[{'label':'PM high','spy':774.67}]
    assert not {'options','decision','plans'} & ctx['spy_brief'].keys()
    assert not {'risk','limits','positions'} & ctx.keys()
    assert not ctx['assistant_coverage']['entry_evidence_incomplete']
    assert 'risk' in ctx['assistant_coverage']['not_requested_sections']
    assert 'risk' not in diagnostic['omitted_sections']
    assert source==original


def test_actual_market_omission_is_not_automated_entry_failure():
    source={'technical_context':{'SPY':{'asof':12,'status':'stale','detail':'x'*20000}},
            'quotes':{'SPY':{'bid':100,'ask':101,'ts':10,'source':'alpaca'}}}
    ctx=json.loads(build_input('SPY outlook?',source)[0])['market_context']
    assert ctx['quotes']==source['quotes']
    coverage=ctx['assistant_coverage']
    assert coverage['market_evidence_omitted']==['technical_context']
    assert not coverage['entry_evidence_incomplete']
    assert coverage['section_sizes']['technical_context']['supplied_bytes']==0
    assert 'not a provider outage' in coverage['omission_reason']


@pytest.mark.parametrize('question', ['Why was SPY paper entry blocked?', 'Explain the SPY morning brief',
                                       'Is an entry advisable?', 'Why were QQQ calls skipped?'])
def test_entry_and_strategy_review_keep_the_original_evidence(question):
    source={'risk':{'realized':-400},'limits':{'loss':300},
            'spy_brief':{'decision':'NO ENTRY','generated_at':100,'options':{'call':{'status':'unavailable'}}}}
    ctx=json.loads(build_input(question,source)[0])['market_context']
    assert ctx['research_request']['mode']!='market_research'
    for key in source: assert ctx[key]==source[key]


@pytest.mark.parametrize('question', ['Will SPY rise or fall today?', 'QQQ 0DTE puts outlook?',
                                       'SPY LEAPS calls', 'SPY trend versus VWAP?'])
def test_research_questions_do_not_request_account_admission(question):
    assert request_profile(question)['mode']=='market_research'


@pytest.mark.parametrize('endpoint', ['/api/ask','/api/ask/stream'])
def test_direction_endpoint_supplies_scoped_technicals_and_exposure(tmp_path,monkeypatch,endpoint):
    calls=[]
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,headers,json):
            calls.append(json)
            return httpx.Response(200,json={'status':'completed','output':[{'content':[
                {'type':'output_text','text':'SPY has mixed intraday evidence.'}]}]})
    async def unwanted_option_lookup(*args,**kwargs):
        raise AssertionError('An underlying direction question needs no contract lookup')
    monkeypatch.setattr('compass.app.option_research',unwanted_option_lookup)
    monkeypatch.setattr('compass.app.httpx.AsyncClient',lambda **kwargs:Client())
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'research.db'),
        password='test-password-with-enough-length',secret='test-signing-secret-with-enough-length',openai='fake')
    app=create_app(cfg); now=time.time()
    technical={'symbol':'SPY','asof':now-60,'status':'ready','vwap':773.4,'ema9':773.8,'ema21':773.6,
        'bar':{'c':773.83},'ema21_previous':773.5,'rvol20':1.2,'htf15_bias':1}
    with TestClient(app) as client:
        with app.state.db.tx() as c:
            db=app.state.db
            db.put(c,'scanner_features:SPY',technical)
            db.put(c,'quote:SPY',{'bid':773.87,'ask':773.89,'ts':now,'source':'alpaca'})
            db.put(c,'exposure:SPY',{'asof':now-30,'source':'massive','coverage':.7,'status':'partial',
                'strikes':[{'strike':700+i,'gex':i,'vex':None} for i in range(1000)]})
            db.put(c,'spy-brief:latest',{'generated_at':now-300,'decision':'WAIT',
                'options':{'call':{'status':'unavailable'}},'chart_levels':[{'label':'PM high','spy':774.67}]})
            for i in range(40):
                db.put(c,'quote:MESZ6@'+str(i),{'bid':100,'ask':101,'ts':now,'source':'databento','details':'x'*300})
        client.post('/login',json={'password':cfg.password})
        response=client.post(endpoint,json={'question':'Will SPY rise or fall today?'})
        assert response.status_code==200
        assert 'SPY has mixed intraday evidence.' in response.text
    assert len(calls)==1
    ctx=json.loads(calls[0]['input'])['market_context']
    assert ctx['technical_context']['SPY']==technical
    assert set(ctx['quotes'])=={'SPY'}
    assert ctx['exposure']['SPY']['coverage']==.7
    assert len(ctx['exposure']['SPY']['strikes'])==12
    assert ctx['exposure']['SPY']['assistant_sample']['total_strikes']==1000
    assert ctx['exposure']['SPY']['asof']==now-30
    assert 'risk' not in ctx and 'options' not in ctx['spy_brief']
    assert not ctx['assistant_coverage']['entry_evidence_incomplete']
    assert len(calls[0]['input'].encode())<=MAX_INPUT_BYTES
    assert 'A future candle that has not closed is not missing' in calls[0]['instructions']


def test_named_futures_resolve_to_dated_quotes_without_unrelated_contracts():
    from compass.assistant_research import mentioned_futures
    quotes={'MNQZ6@1':{},'MESZ6@2':{},'GCZ6@3':{},'SPY':{}}
    assert mentioned_futures('SPY versus mnq today?',quotes)==['MNQZ6@1']
    assert mentioned_futures('Will SPY rise or fall?',quotes)==[]
    assert mentioned_futures('GCZ6 outlook?',quotes)==['GCZ6@3']
