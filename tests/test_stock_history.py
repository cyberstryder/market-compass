import asyncio
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from sqlalchemy.exc import OperationalError
from compass.config import Config
from compass.providers import Collectors
from compass.store import Store,events
from compass.stock_history import PREFIX,health_summary,REFRESH_SECONDS,REFRESH_GRACE_SECONDS


@pytest.fixture
def db(tmp_path):
    value=Store('sqlite:///'+str(tmp_path/'history.db'));value.initialize()
    yield value
    value.engine.dispose()


def lock_error():
    original=Exception('sensitive SQL parameters must not be reported')
    original.sqlstate='55P03'
    return OperationalError('sensitive SQL',{},original)


def test_bar_chunks_commit_progress_and_retry_rolled_back_chunk(db,monkeypatch):
    original=db.append_bars
    batches=[]
    def append(c,kind,source,rows):
        batches.append(len(rows))
        original(c,kind,source,rows)
        if len(batches)==2:raise lock_error()
    monkeypatch.setattr(db,'append_bars',append)
    monkeypatch.setattr('compass.providers.time.sleep',lambda _:None)
    rows=[('SPY',float(i),dict(o=1,h=2,l=1,c=2,v=10)) for i in range(601)]
    Collectors.bars(SimpleNamespace(db=db),'alpaca',rows)
    assert batches==[250,250,250,101]
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(events)).scalar_one()==601
        assert db.get(c,'latestbar:SPY')==600
        assert len(db.get(c,'bar_window:SPY'))==601
    Collectors.bars(SimpleNamespace(db=db),'alpaca',rows[:10])
    with db.tx() as c:
        assert db.get(c,'latestbar:SPY')==600
        assert len(db.get(c,'bar_window:SPY'))==601


def test_history_resumes_failed_page_after_restart_and_rotates_daily(db,monkeypatch):
    clock=[2000000000.]
    monkeypatch.setattr('compass.stock_history.time.time',lambda:clock[0])
    async def run():
        calls=[]
        collector=Collectors(db,Config(local=True))
        collector.stock_symbols=lambda:('SPY',)
        async def get(url,headers,params):
            calls.append(dict(params))
            return {'bars':{'SPY':[dict(t='2026-09-22T14:30:00Z',o=1,h=2,l=1,c=2,v=10)]},
                    'next_page_token':'second' if params['timeframe']=='1Min' and not params.get('page_token') else None}
        collector.get=get
        await collector.history(incremental=True)
        clock[0]+=1
        await collector.history(incremental=True)
        assert [p['timeframe'] for p in calls]==['1Min','1Day']
        original=collector.bars
        collector.bars=lambda *a: (_ for _ in ()).throw(lock_error())
        clock[0]+=1
        await collector.history(incremental=True)
        with db.tx() as c:
            job=db.get(c,PREFIX+'1Min:SPY')
            assert job['params']['page_token']=='second'
            assert '55P03' in job['error'] and 'sensitive' not in job['error']
        await collector.close()
        # A replacement process uses saved pagination, not the first page.
        replacement=Collectors(db,Config(local=True));replacement.stock_symbols=lambda:('SPY',);replacement.get=get
        clock[0]+=121
        await replacement.history(incremental=True)
        assert [p.get('page_token') for p in calls]==[None,None,'second','second']
        assert calls[0]['start']==calls[-1]['start']
        with db.tx() as c:
            assert db.get(c,'health:alpaca_history')['status']=='available'
            assert db.get(c,'health:alpaca_history')['completed_batches']==2
        await replacement.close()
    asyncio.run(run())


@pytest.mark.parametrize('row,status',[
    ({'status':'complete','completed_at':9900},'available'),
    ({'status':'complete','completed_at':6300},'refreshing'),
    ({'status':'collecting','completed_at':6300},'refreshing'),
    ({'status':'collecting'},'partial'),
    ({'status':'collecting','completed_at':5000},'stale'),
    ({'status':'error','completed_at':9900,'error':'HTTP 429'},'error'),
])
def test_history_health_keeps_completed_coverage_but_flags_overdue_or_failed_work(row,status):
    state,summary=health_summary([row],10000)
    assert state==status
    assert summary['completed_batches']==int(bool(row.get('completed_at')))


def test_hourly_refresh_retains_completion_and_resumes_after_restart(db,monkeypatch):
    from compass.stock_history import collect
    clock=[2000000000.]
    monkeypatch.setattr('compass.stock_history.time.time',lambda:clock[0])
    calls=[]
    async def get(url,headers,params):
        calls.append(dict(params))
        return {'bars':{},'next_page_token':'resume' if params['timeframe']=='1Min' and not params.get('page_token') else None}
    original=clock[0]-REFRESH_SECONDS-1
    with db.tx() as c:
        for frame in ('1Min','1Day'):
            db.put(c,PREFIX+frame+':SPY',dict(status='complete',completed_at=original,attempted_at=original,
                symbols=['SPY'],frame=frame,params=None))
    def worker():return SimpleNamespace(db=db,stock_symbols=lambda:['SPY'],cfg=Config(local=True),
        get=get,alpaca_headers={},bars=lambda *args:None)
    asyncio.run(collect(worker()))
    with db.tx() as c:
        h=db.get(c,'health:alpaca_history')
        assert h['status']=='refreshing' and h['completed_batches']==2 and h['pending_batches']==0
        assert db.get(c,PREFIX+'1Min:SPY')['completed_at']==original
    # A new process resumes the same committed cursor, with its old coverage intact.
    clock[0]+=1;asyncio.run(collect(worker()))  # Fairly service the daily batch first.
    clock[0]+=1;asyncio.run(collect(worker()))
    assert calls[-1]['page_token']=='resume' and calls[-1]['start']==calls[0]['start']
    with db.tx() as c:
        assert db.get(c,'health:alpaca_history')['status']=='available'
    status,summary=health_summary([dict(status='collecting',completed_at=original)],
        original+REFRESH_SECONDS+REFRESH_GRACE_SECONDS+1)
    assert status=='stale' and summary['overdue_batches']==1


def test_quiet_history_worker_cannot_look_healthy_for_an_hour():
    from compass.readiness import decorate_health
    h=dict(name='alpaca_history',status='refreshing',detail='refresh underway',checked_at=1000)
    assert decorate_health([h],{'worker:collector':{'at':1091}},{},1091)[0]['status']=='stale'
