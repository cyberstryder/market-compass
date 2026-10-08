import asyncio
from types import SimpleNamespace
from datetime import datetime,timezone
from sqlalchemy import select
from compass.store import events,gap_followups
from compass.option_ideas import OptionIdeas,ideas
from compass.option_recovery import recover_stocks
from compass.active_observations import select_contracts,inventory
from compass.observation_recovery import enable,tick as followups
from compass.reliability_report import report
from test_option_ideas import db,cfg,NOW,CALL,q,seed,signal,rows


def opened(db,c,cfg):
    seed(db,c)
    worker=OptionIdeas(db,cfg)
    worker.queue(c,signal(),NOW);worker.tick(c,NOW)
    p=rows(c)[0];assert p['status']=='open'
    enable(p);p['collection_version']='option-reliability-v5'
    worker.save(c,p,NOW)
    return worker,p


def archive(db,c,symbol,t,bid=2,ask=2.05,received=None):
    quote=q(t,bid,ask)
    db.append(c,'quote','test',symbol,t,quote)
    c.execute(events.update().where(events.c.symbol==symbol,events.c.ts==t).values(received=t+.1 if received is None else received))
    db.put(c,'quote:'+symbol,quote)


def test_active_subscription_stability_and_capacity_is_explicit():
    selected,missing=select_contracts(['new','spy','held'],['held','spy'],['pending'],['background'],2)
    assert selected==['held','spy'] and missing==['new']
    selected,missing=select_contracts(['spy'],[],['pending'],['background'],2)
    assert selected==['spy','pending'] and not missing


def test_gap_grace_replays_late_visible_fresh_storage_without_relaxing_gap(db,cfg):
    with db.tx() as c:
        worker,p=opened(db,c,cfg)
        worker.observe(c,p,NOW+16)
        assert p['status']=='open' and p['gap_pending']['first_detected_at']==NOW+16
        # Simulate a transaction becoming visible later: original storage was fresh.
        for offset in (5,10,15,20):
            archive(db,c,CALL,NOW+offset)
            archive(db,c,'SPY',NOW+offset,100,100.02)
        worker.observe(c,p,NOW+20)
        assert p['status']=='open' and 'gap_pending' not in p
        assert p['recovered_pending_gaps']==1
        assert not c.execute(select(gap_followups)).all()


def test_real_gap_stays_unresolved_but_later_measurements_continue(db,cfg):
    with db.tx() as c:
        worker,p=opened(db,c,cfg)
        worker.observe(c,p,NOW+16)
        worker.observe(c,p,NOW+200)
        assert p['status']=='unresolved' and p['pnl'] is None
        row=c.execute(select(gap_followups.c.payload)).scalar_one()
        assert row['status']=='active' and row['samples']==0
        assert CALL in inventory(db,c,NOW+205)['options']
        archive(db,c,CALL,NOW+210,5,5.05) # Beyond target, but unknown earlier path.
        archive(db,c,'SPY',NOW+210,103,103.02)
        followups(db,c,NOW+210)
        row=c.execute(select(gap_followups.c.payload)).scalar_one()
        assert row['samples']==1 and row['latest']['original_outcome']=='unresolved'
        assert rows(c)[0]['status']=='unresolved' and rows(c)[0]['pnl'] is None
        followups(db,c,p['flatten_at']+1)
        assert c.execute(select(gap_followups.c.status)).scalar_one()=='complete'
        assert CALL not in inventory(db,c,p['flatten_at']+1)['options']
        r=report(db,c,NOW-1,p['flatten_at']+2)
        v=next(x for x in r['cohorts'] if x['study']=='options')
        assert v['gaps']==1 and v['opened']==1 and v['gap_pct']==100
        assert r['followups']==[dict(status='complete',records=1,samples=1)]


def test_late_stored_quotes_do_not_repair_outcomes(db,cfg):
    with db.tx() as c:
        worker,p=opened(db,c,cfg)
        worker.observe(c,p,NOW+16)
        for offset in (5,10,15):
            archive(db,c,CALL,NOW+offset,received=NOW+30)
        worker.observe(c,p,NOW+200)
        assert p['status']=='unresolved' and p['pnl'] is None
        assert p['archive_check']['option']['stale_or_invalid_when_recorded']==3


def test_recovery_underlying_is_bounded_preserves_source_time_and_ignores_stale(db,cfg,monkeypatch):
    from compass import active_observations
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    monkeypatch.setattr(active_observations,'inventory',lambda *a,**kw:dict(stocks=['SPY','QQQ']+[f'S{i}' for i in range(140)],options=[]))
    cfg.alpaca_key='fixture';cfg.alpaca_secret='fixture'
    saved=[];calls=[]
    async def get(url,headers,params):
        calls.append(params)
        assert url.endswith('/v2/stocks/quotes/latest') and params['feed']==cfg.feed
        return {'quotes':{s:dict(t=datetime.fromtimestamp(NOW-(1 if s=='QQQ' else 30),timezone.utc).isoformat(),
            bp=100,ap=100.01,bs=1,**{'as':1}) for s in params['symbols'].split(',')}}
    collector=SimpleNamespace(db=db,cfg=cfg,alpaca_headers={},get=get,quote_batch=lambda source,items:saved.append((source,items)))
    asyncio.run(recover_stocks(collector))
    assert len(calls[0]['symbols'].split(','))==100
    assert saved[0][0]=='alpaca_stock_recovery' and len(saved[0][1])==1
    assert saved[0][1][0][1]['ts']==NOW-1
    assert saved[0][1][0][1]['collection_version']=='option-reliability-v8'


def test_stock_backoff_does_not_disable_option_recovery(db,cfg,monkeypatch):
    from compass.providers import FeedError
    from compass import active_observations
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    monkeypatch.setattr(active_observations,'inventory',lambda *a,**kw:dict(stocks=['SPY'],options=[]))
    cfg.alpaca_key='fixture';cfg.alpaca_secret='fixture';calls=[]
    async def get(*a,**k):calls.append(1);raise FeedError('rate',429)
    collector=SimpleNamespace(db=db,cfg=cfg,alpaca_headers={},get=get)
    asyncio.run(recover_stocks(collector));asyncio.run(recover_stocks(collector))
    assert len(calls)==1 and collector.stock_recovery_backoff==NOW+60
    assert not hasattr(collector,'option_recovery_backoff')
    with db.tx() as c:
        h=db.get(c,'health:stock_recovery')
        assert h['status']=='error' and h['results'][0]['http_status']==429
        assert h['backoff_until']==NOW+60


def test_idle_stock_recovery_clears_old_partial_but_waiting_retry_stays_visible(db,cfg,monkeypatch):
    from compass import active_observations
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    demanded=[]
    monkeypatch.setattr(active_observations,'inventory',lambda *a,**kw:dict(stocks=demanded,options=[]))
    cfg.alpaca_key='fixture';cfg.alpaca_secret='fixture'
    collector=SimpleNamespace(db=db,cfg=cfg,stock_recovery_attempts={})
    db.health('stock_recovery','partial','Old failed recovery')
    asyncio.run(recover_stocks(collector))
    with db.tx() as c:
        h=db.get(c,'health:stock_recovery')
        assert h['status']=='idle' and h['active_symbols']==0 and h['checked_at']==NOW
    demanded.append('SPY');collector.stock_recovery_attempts['SPY']=NOW
    asyncio.run(recover_stocks(collector))
    with db.tx() as c:
        h=db.get(c,'health:stock_recovery')
        assert h['status']=='partial' and h['waiting']==1
        db.put(c,'quote:SPY',q(NOW))
    asyncio.run(recover_stocks(collector))
    with db.tx() as c:
        h=db.get(c,'health:stock_recovery')
        assert h['status']=='idle' and h['active_symbols']==1 and h['waiting']==0
        db.put(c,'quote:SPY',{**q(NOW),'bid':0})
    asyncio.run(recover_stocks(collector))
    with db.tx() as c:
        assert db.get(c,'health:stock_recovery')['status']=='partial'


def test_grace_does_not_cross_unknown_interval_to_award_target(db,cfg):
    with db.tx() as c:
        worker,p=opened(db,c,cfg)
        archive(db,c,CALL,NOW+16,5,5.05)
        archive(db,c,'SPY',NOW+16,103,103.02)
        worker.observe(c,p,NOW+16)
        assert p['status']=='open' and p['pnl'] is None
        worker.observe(c,p,NOW+200)
        assert p['status']=='unresolved' and p['pnl'] is None
        assert p['samples']==0


def test_setup_gap_grace_and_followup_preserve_original_bracket(db,cfg):
    from compass.setup_study import SetupStudy,trials
    from test_setup_study import signal as setup_signal
    from compass.engine import spec
    with db.tx() as c:
        quote=q(NOW,100,100.25);quote['collection_version']='option-reliability-v5'
        db.put(c,'quote:MESZ6@1',quote)
        worker=SetupStudy(db,cfg)
        worker.start(c,{**setup_signal(),'signal_time':NOW},NOW,spec('MESZ6@1'))
        worker.tick(c,NOW+16)
        p=c.execute(select(trials.c.payload)).scalar_one()
        assert p['status']=='open' and p['gap_pending']
        worker.tick(c,NOW+200)
        p=c.execute(select(trials.c.payload)).scalar_one()
        assert p['status']=='unresolved' and p['pnl'] is None
        assert c.execute(select(gap_followups.c.payload)).scalar_one()['symbol']=='MESZ6@1'


def test_expired_followup_never_samples_a_later_session(db,cfg):
    with db.tx() as c:
        worker,p=opened(db,c,cfg)
        worker.observe(c,p,NOW+16);worker.observe(c,p,NOW+200)
        archive(db,c,CALL,p['flatten_at']+60)
        followups(db,c,p['flatten_at']+60)
        row=c.execute(select(gap_followups.c.payload)).scalar_one()
        assert row['status']=='complete' and row['samples']==0


def test_active_spy_timing_displaces_pending_candidates_in_real_reconciliation(db,cfg,monkeypatch):
    from compass.spy_timeframes import arms
    from compass.providers import Collectors
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW)
    cfg.stream_limit=1
    with db.tx() as c:
        p=dict(id='timing',status='open',contract={'symbol':CALL},underlying='SPY')
        c.execute(arms.insert().values(id='timing',day='2026-09-14',timeframe=1,cohort='test',
            status='open',created=NOW,updated=NOW,payload=p))
        db.put(c,'pending_research_options:pending',dict(status='waiting',expires_at=NOW+90,
            signal=dict(symbol='QQQ',side='long',signal_price=100)))
        db.put(c,'chain:QQQ',dict(asof=NOW,contracts=[dict(symbol='O:QQQ260914C00100000',
            type='call',expiry='2026-09-14',multiplier=100,strike=100)]))
    async def run():
        coll=Collectors(db,cfg)
        try:await coll.refresh_option_subscriptions()
        finally:await coll.close()
    asyncio.run(run())
    with db.tx() as c:
        selected=db.get(c,'options:subscriptions')
        assert selected['symbols']==[CALL] and selected['active_missing']==[]
        assert selected['waiting_ideas']==1


def test_partial_writer_failure_can_retry_without_losing_or_duplicating_quotes(db,monkeypatch):
    from compass.providers import Collectors
    original=db.append_quotes;calls=[]
    def write(c,source,items):
        calls.append(len(items))
        if len(calls)==2:raise RuntimeError('storage interruption')
        return original(c,source,items)
    monkeypatch.setattr(db,'append_quotes',write)
    collector=SimpleNamespace(db=db)
    batch=[('SPY',q(NOW+i),True) for i in range(70)]
    import pytest
    with pytest.raises(RuntimeError):Collectors.quote_batch(collector,'alpaca',batch)
    with db.tx() as c:
        assert len(c.execute(select(events)).all())==32
    monkeypatch.setattr(db,'append_quotes',original)
    Collectors.quote_batch(collector,'alpaca',batch)
    with db.tx() as c:
        assert len(c.execute(select(events)).all())==70
        assert db.get(c,'quote:SPY')['ts']==NOW+69


def test_v8_entry_keeps_original_processing_grace_and_gap_limit(db,cfg):
    from compass.quote_collection import VERSION
    with db.tx() as c:
        seed(db,c)
        quote=db.get(c,'quote:'+CALL);quote['collection_version']=VERSION
        db.put(c,'quote:'+CALL,quote)
        worker=OptionIdeas(db,cfg);worker.queue(c,signal(),NOW);worker.tick(c,NOW)
        p=rows(c)[0]
        assert p['status']=='open' and p['gap_policy']=='observation-recovery-v1'
        worker.observe(c,p,NOW+16)
        assert p['status']=='open' and p['gap_pending']['first_detected_at']==NOW+16
        worker.observe(c,p,NOW+200)
        assert p['status']=='unresolved' and p['pnl'] is None
