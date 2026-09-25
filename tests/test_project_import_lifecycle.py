import asyncio
import threading

import httpx
import pytest
from sqlalchemy import select,func

from compass.config import Config
from compass import projects
from compass.readiness import decorate_health
from compass.store import leases
from test_projects import db,source,TOKEN

NOW=2000000000.


def config():return Config(local=True,morning_url='https://source.example',morning_token=TOKEN)


def page(rows,next_page=None):
    return dict(schema_version=1,project='morning',mode='observe_only',records=rows,next=next_page)


def test_long_scan_and_304_pages_report_progress_without_claiming_completion(db,monkeypatch):
    clock=[NOW];calls=[];cache={}
    monkeypatch.setattr(projects.time,'time',lambda:clock[0])
    def handle(request):
        cursor=request.url.params.get('after','0');index=int(cursor)
        clock[0]+=12;calls.append(request)
        if index:
            with db.tx() as c:
                h=db.get(c,'health:project_morning')
                assert h['status']=='syncing' and h['pages_checked']==index
                assert projects.source_health(h,clock[0])['status']=='syncing'
                assert decorate_health([h],{'worker:collector':{'at':clock[0]}},{},clock[0])[0]['status']=='syncing'
                assert h.get('last_complete_at')==cache.get('expected_complete')
        if request.headers.get('if-none-match'):
            assert request.headers['if-none-match']==f'"page-{index}"'
            return httpx.Response(304)
        return httpx.Response(200,json=page([source(NOW-86400,id=str(index))],str(index+1) if index<5 else None),
            headers={'etag':f'"page-{index}"'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await projects.poll_source(db,config(),'morning',client,cache)
            with db.tx() as c:
                h=db.get(c,'health:project_morning')
                assert h['status']=='connected' and h['cycle_seconds']==72
                assert h['last_complete_at']==clock[0] and not h['scan_in_progress']
                cache['expected_complete']=clock[0]
            await projects.poll_source(db,config(),'morning',client,cache)
    asyncio.run(run())
    assert len(calls)==12
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(projects.records)).scalar_one()==6
        assert db.get(c,'health:project_morning')['records_changed']==0


@pytest.mark.parametrize('changes,status',[
    ({},'syncing'),({'checked_at':NOW-26},'stale'),
    ({'scan_started_at':NOW-121},'stale'),({'status':'error'},'error')])
def test_page_progress_never_hides_a_stall_deadline_or_error(changes,status):
    h=dict(name='project_morning',status='syncing',detail='progress',checked_at=NOW-1,
        scan_started_at=NOW-30,scan_in_progress=True)|changes
    assert projects.source_health(h,NOW)['status']==status
    assert decorate_health([h],{'worker:collector':{'at':NOW}},{},NOW)[0]['status']==status


def test_partial_page_commit_does_not_advance_etag_and_retry_is_idempotent(db,monkeypatch):
    monkeypatch.setattr(projects.time,'time',lambda:NOW)
    cache={};calls=[];fail=[True];original=projects.ingest
    rows=[source(NOW-86400,id=str(i)) for i in range(60)]
    def ingest(db,c,project,row,now):
        if row['id']=='30' and fail[0]:raise RuntimeError('write interrupted')
        return original(db,c,project,row,now)
    monkeypatch.setattr(projects,'ingest',ingest)
    def handle(request):
        calls.append(request)
        assert 'if-none-match' not in request.headers
        return httpx.Response(200,json=page(rows),headers={'etag':'"page"'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(RuntimeError,match='write interrupted'):
                await projects.poll_source(db,config(),'morning',client,cache)
            assert '' not in cache
            with db.tx() as c:
                assert c.execute(select(func.count()).select_from(projects.records)).scalar_one()==25
            fail[0]=False
            await projects.poll_source(db,config(),'morning',client,cache)
    asyncio.run(run())
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(projects.records)).scalar_one()==60
        assert len(db.recent(c,'project_update',limit=100))==60
        assert db.get(c,'health:project_morning')['records_changed']==35
        assert not db.recent(c,'alert')


def test_cleanup_releases_only_its_own_lease(db):
    assert projects.source_lease(db,'morning','first')
    assert not projects.source_lease(db,'morning','second')
    assert projects.source_lease(db,'morning','first',release=True)==1
    assert projects.source_lease(db,'morning','second')
    assert projects.source_lease(db,'morning','first',release=True)==0
    assert not projects.source_lease(db,'morning','third')


def test_import_loop_keeps_receiving_when_main_collector_is_blocked_and_releases_lease(db,monkeypatch):
    applied=threading.Event();loops=[];original_client=httpx.AsyncClient;original_poll=projects.poll_source
    async def handler(request):
        await asyncio.sleep(.05)
        return httpx.Response(200,json=page([source(NOW-86400)]))
    class Client(original_client):
        def __init__(self,**kwargs):
            loops.append(asyncio.get_running_loop())
            super().__init__(transport=httpx.MockTransport(handler),**kwargs)
    async def poll(*args,**kwargs):
        result=await original_poll(*args,**kwargs)
        if args[2]=='morning':applied.set()
        await asyncio.Event().wait()
        return result
    monkeypatch.setattr(projects.httpx,'AsyncClient',Client)
    monkeypatch.setattr(projects,'poll_source',poll)
    monkeypatch.setattr(projects.time,'time',lambda:NOW)
    async def run():
        main=asyncio.get_running_loop()
        task=asyncio.create_task(projects.run_sources(db,config()))
        try:
            await asyncio.sleep(.01)
            assert applied.wait(3)
            assert all(loop is not main for loop in loops)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    with db.tx() as c:
        assert db.get(c,'health:project_morning')['status']=='connected'
        assert c.execute(select(leases.c.until).where(leases.c.key=='project-morning')).scalar_one()==0
    assert projects.source_lease(db,'morning','replacement')


def test_provider_error_stays_error_and_cancellation_releases_ownership(db,monkeypatch):
    original_client=httpx.AsyncClient;original_health=db.health
    monkeypatch.setattr(projects.time,'time',lambda:NOW)
    class Client(original_client):
        def __init__(self,**kwargs):
            super().__init__(transport=httpx.MockTransport(lambda r:httpx.Response(401)),**kwargs)
    monkeypatch.setattr(projects.httpx,'AsyncClient',Client)
    async def run():
        loop=asyncio.get_running_loop();failed=asyncio.Event()
        def health(*args,**kwargs):
            result=original_health(*args,**kwargs)
            if args[1]=='error':loop.call_soon_threadsafe(failed.set)
            return result
        monkeypatch.setattr(db,'health',health)
        task=asyncio.create_task(projects.run_source(db,config(),'morning'))
        try:await asyncio.wait_for(failed.wait(),2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    with db.tx() as c:assert db.get(c,'health:project_morning')['status']=='error'
    assert projects.source_lease(db,'morning','replacement')
