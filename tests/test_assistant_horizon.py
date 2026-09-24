import asyncio
import copy
import json
import time
import pytest
from fastapi.testclient import TestClient
from compass.app import create_app
from compass.assistant_horizon import restrict,missing_history_answer
from compass.assistant_options import request,sample
from compass.config import Config


def context():
    now=time.time();req=request('QQQ LEAPS calls 365-900 DTE',now)
    return dict(question_scope={'symbols':['QQQ']},option_research={'request':req,'symbols':{'QQQ':dict(status='available',
        **sample([dict(symbol='fixture',type='call',expiry=req['expiry_start'],strike=740,quote={'ts':now,'bid':1,'ask':1.1})],req,740,now,'fixture'))}},
        technical_context={'QQQ':{'ema9':750,'ema21':740}},scanner={'bullish':True},
        swing_technical_context={'QQQ':{'status':'warming_up'}},exposure={'QQQ':{'gex':999}})


def test_missing_long_history_cannot_be_replaced_by_intraday_or_quotes():
    original=context();saved=copy.deepcopy(original);trimmed=restrict(original)
    assert original==saved
    assert not {'technical_context','scanner','exposure'} & trimmed.keys()
    answer=missing_history_answer(trimmed)
    assert answer.startswith('**Insufficient long-term directional evidence.')
    assert '1 fresh' in answer and 'pricing evidence only' in answer
    trimmed['swing_technical_context']['QQQ']['status']='ready'
    assert missing_history_answer(trimmed) is None


def test_intraday_and_mixed_requests_retain_their_research_context():
    for question in ('QQQ 0DTE puts','QQQ 0DTE puts and QQQ LEAPS calls'):
        ctx=context();ctx['option_research']['request']=request(question,time.time())
        assert restrict(ctx) is ctx and missing_history_answer(ctx) is None


@pytest.mark.parametrize('endpoint',['/api/ask','/api/ask/stream'])
def test_missing_long_history_returns_source_facts_without_model_inference(tmp_path,monkeypatch,endpoint):
    async def lookup(cfg,scope,req,quotes,now,db=None):
        result=context()['option_research'];result['request']=req;return result
    monkeypatch.setattr('compass.app.option_research',lookup)
    def forbidden(*a,**kw):pytest.fail('Missing long-term history must not reach the model')
    monkeypatch.setattr('compass.app.httpx.AsyncClient',forbidden)
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'guard.db'),openai='fake',
        password='test-password-with-enough-length',secret='test-signing-secret-with-enough-length')
    with TestClient(create_app(cfg)) as client:
        client.post('/login',json={'password':cfg.password})
        response=client.post(endpoint,json={'question':'QQQ LEAPS calls 365-900 DTE: what is the direction?'})
    assert response.status_code==200
    assert 'Insufficient long-term directional evidence' in response.text
    assert 'fixture' in response.text
