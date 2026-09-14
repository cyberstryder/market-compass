import asyncio
from datetime import datetime, timezone
import pytest
from sqlalchemy import select
from compass.config import Config
from compass.engine import Engine, spec
from compass.flow_recovery import ingest, freshness
from compass.providers import Collectors, FeedError
from compass.research import FEEDS, normalize, catalog, observation_time
from compass.secondary import Secondary, snapshot as secondary_snapshot, source_candidate, reviews
from compass.setup_study import SetupStudy, trials
from compass.simulation import FILL_VERSION
from compass.store import Store, events, flow_records
from compass.vendor import flow_page
from compass.vendor_schedule import matrix_target
from compass.swing_ideas import swings  # Register the production schema before initialize().

NOW = datetime(2026,9,14,15,0,tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///'+str(tmp_path/'reliability.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def q(stamp=NOW, bid=100, ask=100.25):
    return dict(ts=stamp,bid=bid,ask=ask,bid_size=10,ask_size=10)


def signal(side='long', identity='fixture'):
    return dict(id=identity,symbol='MESZ6@1',strategy='fixture',side=side,signal_time=NOW,
                signal_price=100,stop_distance=2.13,track='intraday')


def stored_trial(c):
    return c.execute(select(trials.c.payload)).scalar_one()


def archive(db,c,stamp,bid=100,received=None):
    payload=q(stamp,bid,bid+.25)
    c.execute(db.insert(events).values(key='fixture-quote:'+str(stamp),kind='quote',source='databento',
        symbol='MESZ6@1',ts=stamp,received=stamp+.05 if received is None else received,payload=payload))


def test_keyed_index_clocks_remain_per_symbol_and_mixed(db):
    feed=next(f for f in FEEDS if f.key=='gamma_market')
    payload={'success':True,'cached':True,'data':{
        'SPY':{'symbol':'SPY','lastUpdated':'2026-09-14T14:59:59Z','totalGEX':10},
        'USO':{'symbol':'USO','lastUpdated':'2026-09-14T14:40:00Z','totalGEX':20},
        'marketSummary':{'totalGEX':30}}}
    result=normalize(feed,payload,NOW)
    assert result['source_ts'] is None and len(result['items'])==2
    assert result['items'][0]['source_ts']==NOW-1
    assert result['items'][1]['source_ts']==NOW-1200
    with db.tx() as c:
        db.put(c,'research:gamma_market',result)
        row=next(r for r in catalog(db,c,Config(local=True),NOW) if r['key']=='gamma_market')
    assert row['status']=='mixed' and row['clock_basis']=='per_item'
    assert [r['status'] for r in row['item_clocks']]==['current','stale']


@pytest.mark.parametrize('stamp',['2026-09-14T14:59:59',None,'not-a-time'])
def test_unknown_clocks_stay_unknown(stamp):
    assert observation_time({'lastUpdated':stamp},index_gamma=True) is None
    assert observation_time({'lastUpdated':'2026-09-14T14:59:59Z','date':'2027-01-01'}) is None


def test_core_matrices_survive_continuous_focus_churn(db):
    names=tuple('S'+chr(65+i) for i in range(20))
    cfg=Config(local=True,stocks=('SPY','QQQ','IWM'),watchlist=names,option_focus=5)
    seen={s:[] for s in cfg.watch_symbols}
    with db.tx() as c:
        for i in range(120):
            now=NOW+i*5
            for n,s in enumerate(names):
                db.put(c,'focus:'+s,dict(symbol=s,priority=100+n,at=now))
            symbol=matrix_target(db,c,cfg,now,i)
            if symbol:
                seen[symbol].append(now)
                db.put(c,'matrix_job:'+symbol,dict(attempted_at=now))
        for s in ('SPY','QQQ','IWM'):
            assert len(seen[s])>=9
            assert max(b-a for a,b in zip(seen[s],seen[s][1:]))<=70
        assert all(seen[s] for s in names)


def test_failed_core_symbol_yields_to_other_matrices(db):
    cfg=Config(local=True,stocks=('SPY','QQQ','IWM'),matrix='fixture')
    async def run():
        collector=Collectors(db,cfg)
        called=[]
        async def request(path,label):
            called.append(path)
            raise FeedError('fixture unavailable',503)
        collector.matrix_request=request
        try:
            await collector.matrix()
            await collector.matrix()
        finally:
            await collector.close()
        return called
    called=asyncio.run(run())
    assert len(called)==2 and called[0]!=called[1]
    assert all(p.endswith('/matrix') for p in called)


def test_flow_fetch_does_not_make_old_events_current(db):
    def part(premium):
        return flow_page({'page':1,'pageSize':100,'total':1,'data':[{
            'id':'one','ticker':'SPY','premium':premium,'tradeTime':'2026-09-14T14:40:00Z'}]},NOW)
    ingest(db,'2026-09-14',1,part(60000),NOW)
    ingest(db,'2026-09-14',1,part(70000),NOW+5)
    with db.tx() as c:
        row=c.execute(select(flow_records)).mappings().one()
        cp=db.get(c,'recovery:flow:2026-09-14')
    assert row['first_seen']==NOW and row['last_seen']==NOW+5
    assert cp['last_new_id_at']==NOW and cp['last_correction_at']==NOW+5
    assert cp['corrections_observed']==1
    state=freshness({'source_ts':NOW-1200,'received':NOW+5},NOW+6)
    assert state['status']=='event_stale' and not state['eligible_for_live_confirmation']
    assert freshness({'source_ts':NOW,'received':NOW},NOW+61)['status']=='poll_stale'


def test_flow_future_and_empty_clocks_are_not_current():
    assert freshness({'source_ts':NOW+1,'received':NOW},NOW)['status']=='clock_error'
    assert freshness({'received':NOW},NOW)['status']=='no_events'


@pytest.mark.parametrize('side',['long','short'])
def test_paper_and_study_share_tick_aligned_brackets_and_target_fills(db,side):
    cfg=Config(local=True)
    engine=Engine(db,cfg)
    sig=signal(side)
    sig['invalidation']=97.87 if side=='long' else 102.13
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',q())
        assert engine.enter(c,sig,NOW)
        paper=db.get(c,'position:MESZ6@1')
        study=stored_trial(c)
        for key in ('entry','stop','target','fill_version'):
            assert paper[key]==study[key]
        assert paper['fill_version']==FILL_VERSION
        bid=paper['target']+10 if side=='long' else paper['target']-10
        db.put(c,'quote:MESZ6@1',q(NOW+2,bid,bid+.25))
        engine.exits(c,NOW+2)
        engine.study.tick(c,NOW+2)
        paper=db.get(c,'position:MESZ6@1')
        study=stored_trial(c)
        assert paper['exit']==paper['target']==study['exit']
        assert paper['pnl']/paper['qty']==pytest.approx(study['pnl'])


def test_retained_path_catches_target_before_later_reversal(db):
    study=SetupStudy(db,Config(local=True))
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',q())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        target=stored_trial(c)['target']
        archive(db,c,NOW+5)
        archive(db,c,NOW+10,target+2)
        archive(db,c,NOW+15,90)
        db.put(c,'quote:MESZ6@1',q(NOW+29,90,90.25))
        study.tick(c,NOW+30)
        row=stored_trial(c)
        assert row['exit_reason']=='target' and row['exit']==target
        assert row['exit_quote_ts']==NOW+10 and row['finished']==NOW+30
        assert row['replayed_samples']==2


@pytest.mark.parametrize('late',[False,True])
def test_actual_or_late_backfill_gap_remains_unresolved(db,late):
    study=SetupStudy(db,Config(local=True))
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',q())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        if late:
            for offset in (5,10,15,20):
                archive(db,c,NOW+offset,received=NOW+29)
        db.put(c,'quote:MESZ6@1',q(NOW+29))
        study.tick(c,NOW+30)
        row=stored_trial(c)
        assert row['status']=='unresolved' and row['pnl'] is None
        assert row['gap_detail']['reason']=='missing_recorded_path'


def test_legacy_trial_and_paper_exit_semantics_are_preserved(db):
    engine=Engine(db,Config(local=True))
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',q())
        engine.enter(c,signal(),NOW)
        p=db.get(c,'position:MESZ6@1')
        p.pop('fill_version')
        db.put(c,'position:MESZ6@1',p)
        trial=stored_trial(c)
        trial.pop('observation_model')
        trial['version']='setup-outcomes-v1'
        c.execute(trials.update().values(version='setup-outcomes-v1',payload=trial))
        archive(db,c,NOW+5,p['target']+2)
        db.put(c,'quote:MESZ6@1',q(NOW+29,p['target']+3,p['target']+3.25))
        engine.exits(c,NOW+30)
        engine.study.tick(c,NOW+30)
        assert db.get(c,'position:MESZ6@1')['exit']>p['target']
        assert stored_trial(c)['status']=='unresolved'
        engine.study.start(c,signal(),NOW+31,spec('MESZ6@1'))
        assert len(c.execute(select(trials.c.id)).all())==1


def test_secondary_uses_clock_after_reads_and_rejects_actual_future(db):
    worker=Secondary(db,Config(local=True),clock=lambda:NOW+2)
    candidate=source_candidate('morning',dict(id='original',symbol='SPY',source_ts=NOW,
        strategy='fixture',side='long',status='tracking',original={}), 'fixture-source')
    with db.tx() as c:
        db.put(c,'quote:SPY',q(NOW+1,100,100.02))
        db.put(c,'scanner_features:SPY',dict(status='ready',asof=NOW-1,atr14=2,
            htf15_bias=1,vwap=99,ema9=100,ema21=99))
        worker.review(c,candidate,{},NOW)
        row=c.execute(select(reviews)).mappings().one()
        assert row['decision']['data_checks']['quote']['ready']
        assert row['decided_at']==row['inputs']['captured_at']==NOW+2
        assert row['inputs']['capture_started_at']==NOW
        db.put(c,'secondary:data_status',dict(at=NOW,rows=[dict(symbol='SPY',projects=['morning'],
            collection_enabled=True,minute_ready=True,minute_asof=NOW)]))
        assert secondary_snapshot(db,c,NOW,clock=lambda:NOW+2)['data_readiness']['rows'][0]['quote_ready']
        db.put(c,'quote:SPY',q(NOW+3))
        result=secondary_snapshot(db,c,NOW,clock=lambda:NOW+2)['data_readiness']['rows'][0]
        assert not result['quote_ready'] and result['quote_age']==-1


def test_unknown_insert_rowcount_cannot_drop_the_quote_archive(db,monkeypatch):
    from sqlalchemy.engine import Connection
    execute=Connection.execute
    class UnknownCount:
        rowcount=-1
        def __init__(self,result): self.result=result
        def __getattr__(self,name): return getattr(self.result,name)
    def unknown_count(connection,statement,*args,**kwargs):
        result=execute(connection,statement,*args,**kwargs)
        return UnknownCount(result) if getattr(statement,'is_insert',False) else result
    monkeypatch.setattr(Connection,'execute',unknown_count)
    async def run():
        collector=Collectors(db,Config(local=True))
        try:
            collector.quote('databento','MESZ6@1',q(),1)
            collector.quote('databento','MESZ6@1',q(NOW-1),1)
            collector.quote_batch('alpaca',[('SPY',q(),True)])
        finally:
            await collector.close()
    asyncio.run(run())
    with db.tx() as c:
        assert db.get(c,'quote:MESZ6@1')['ts']==NOW
        assert len(db.recent(c,'quote','MESZ6@1'))==1
        assert len(db.recent(c,'quote','SPY'))==1
        assert db.append(c,'flow','fixture','SPY',NOW,{'premium':100000},'same-print')
        assert not db.append(c,'flow','fixture','SPY',NOW,{'premium':100000},'same-print')
