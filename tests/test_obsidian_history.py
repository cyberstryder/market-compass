import asyncio
import hashlib
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select,func
from compass.obsidian_history import analyze,step,collect,health_status,KEY,CUTOFF
from compass.obsidian import ingest_page,ideas
from compass.store import events
from test_obsidian import event
from test_secondary import db,NOW

ROW=dict(contract='O:JNJ260918P00202500',source_ts=NOW+30,expiry='2026-09-18')
def bar(ts,o=2,h=2.2,l=1.8,c=2.1):return dict(t=ts*1000,o=o,h=h,l=l,c=c,v=10)
def body(bars,**kw):return dict(ticker=ROW['contract'],status='OK',results=bars,**kw)


def test_excludes_pre_idea_minute_and_post_cutoff():
    r=analyze(ROW,body([bar(NOW,h=99),bar(NOW+60),bar(CUTOFF,h=99)]))
    assert r['bars']==1 and r['anchor_delay_seconds']==30
    assert r['highest_pct']==pytest.approx(10) and r['unexpired']


def test_put_uses_long_option_economics_and_same_bar_order_unknown():
    r=analyze(ROW,body([bar(NOW+60,h=2.6,l=1.4)]))
    assert r['scenario_25']=='same_bar_ambiguous' and r['highest_pct']==30


def test_checkpoints_missing_not_carried_forward():
    r=analyze(ROW,body([bar(NOW+60),bar(NOW+14*60,c=2.2)]))
    assert r['checkpoints']['15']==10
    assert r['checkpoints']['30'] is None and r['missing_minutes']==12


def test_empty_mismatch_truncation():
    assert analyze(ROW,body([]))['status']=='no_bars'
    assert analyze(ROW,{'ticker':'wrong','status':'OK'})['status']=='invalid_response'
    assert analyze(ROW,body([bar(NOW+60)],next_url='not-followed'))['status']=='partial'


def test_sparse_late_baseline_remains_explicit():
    r=analyze(ROW,body([bar(NOW+3600)]))
    assert r['anchor_delay_seconds']==3570 and r['checkpoints']['15'] is None


@pytest.mark.parametrize('code',[200,403])
def test_durable_job_no_repeat_or_live_quote_writes(db,code):
    url='https://www.accessobsidian.com/api/feed/'+'test_private_token_x'
    feed=hashlib.sha256(url.encode()).hexdigest()
    ingest_page(db,feed,dict(events=[event()],cursor=116),NOW+300)
    requests=[]
    def handle(req):
        requests.append(req)
        assert req.url.host=='api.massive.com' and req.headers['Authorization']=='Bearer test'
        assert 'apiKey' not in req.url.params
        return httpx.Response(code,json=body([bar(NOW+60)]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            c=SimpleNamespace(db=db,cfg=SimpleNamespace(obsidian_url=url,massive='test'),client=client,history_owner='fixture')
            await step(c);await step(c);await step(c)
    asyncio.run(run())
    with db.tx() as c:
        state=db.get(c,KEY)
        assert state['complete'] and len(state['results'])==1 and len(requests)==1
        result=next(iter(state['results'].values()))
        assert result['status']==('observed' if code==200 else 'access_denied')
        assert c.execute(select(ideas.c.measurements)).scalar_one()['anchor_status']=='late_import'
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='quote')).scalar_one()==0


def test_completed_audit_replaces_old_configuration_warning_without_provider_request(db):
    with db.tx() as c:
        db.put(c,KEY,dict(complete=True,queue=[{'id':'one'}],results={'one':{'status':'observed'}}))
    db.health('obsidian_history','not_configured','API credentials required')
    # A finished audit is still finished if its one-time switch/credentials are removed.
    collector=SimpleNamespace(db=db,cfg=SimpleNamespace(obsidian_history=False,obsidian_url='',massive=''))
    asyncio.run(collect(collector))
    with db.tx() as c:
        h=db.get(c,'health:obsidian_history')
        assert h['status']=='complete' and '1/1 ideas checked' in h['detail']
        assert db.get(c,KEY)['results']=={'one':{'status':'observed'}}


@pytest.mark.parametrize('enabled,url,key,state,cursor,status',[
    (False,'','',{},0,'disabled'),
    (True,'','',{},0,'not_configured'),
    (True,'feed','key',{},115,'waiting'),
    (True,'feed','key',{},116,'running'),
    (True,'feed','key',{'complete':True,'blocked':'access_denied'},116,'error'),
])
def test_audit_health_distinguishes_disabled_waiting_and_access_failure(enabled,url,key,state,cursor,status):
    cfg=SimpleNamespace(obsidian_history=enabled,obsidian_url=url,massive=key)
    assert health_status(cfg,state,cursor)[0]==status


def test_health_is_refreshed_when_a_running_audit_finishes(db,monkeypatch):
    cfg=SimpleNamespace(obsidian_history=True,obsidian_url='feed',massive='key')
    with db.tx() as c:
        db.put(c,'obsidian:cursor:'+hashlib.sha256(b'feed').hexdigest(),116)
    async def finish(collector):
        with db.tx() as c:db.put(c,KEY,{'complete':True,'queue':[],'results':{}})
    monkeypatch.setattr('compass.obsidian_history.step',finish)
    asyncio.run(collect(SimpleNamespace(db=db,cfg=cfg)))
    with db.tx() as c:assert db.get(c,'health:obsidian_history')['status']=='complete'
