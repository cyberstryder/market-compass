import asyncio
import hashlib
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select,func
from compass.obsidian_history import analyze,step,KEY,CUTOFF
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
