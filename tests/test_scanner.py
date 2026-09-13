import asyncio
from datetime import datetime
from types import SimpleNamespace
import json
import pytest
import httpx
from fastapi.testclient import TestClient
from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.engine import Engine,spec
from compass.scanner import (features,technical_candidates,exposure_candidates,
    flow_candidate,Scanner,snapshot)
from compass.research import Feed,normalize,apex_levels,collect,FEEDS
from compass.providers import Collectors,FeedError
from compass.flow_recovery import collect as collect_flow
from compass.alerts import message_for
from compass.app import create_app
from compass.universe import symbols,saved_watchlist

NOW=datetime(2026,9,14,10,15,tzinfo=NY).timestamp()


@pytest.fixture
def db(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'scanner.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def quote(t=NOW,bid=101.10,ask=101.11):
    return {'ts':t,'bid':bid,'ask':ask,'bid_size':100,'ask_size':100,'source':'fixture'}


def fact():
    return {'symbol':'SPY','status':'ready','asof':NOW,'bar_start':NOW-60,
        'session_open':NOW-2700,'session_close':NOW+20000,'day':'2026-09-14',
        'bar':{'o':100,'h':101.2,'l':99.8,'c':101,'v':200},
        'previous_bar':{'o':100,'h':100.5,'l':99.9,'c':100,'v':100},
        'atr14':1,'ema9':100.5,'ema21':100.3,'ema21_previous':100.2,
        'vwap':100.2,'htf15_bias':1,'rvol20':2,'high20':100.8,'low20':99.5,
        'high5':100.8,'low5':99.7,'or_complete':False,'prior_complete':False}


def test_full_watchlist_and_explicit_overrides():
    assert len(saved_watchlist())==144
    assert symbols('NASDAQ:NVDA, NVDA\nNYSE:TSLA')==('NVDA','TSLA')
    cfg=Config(local=False,stocks=('SPY',),watchlist=())
    assert len(cfg.watch_symbols)==144
    assert Config(local=True,stocks=('SPY',),watchlist=('TSLA','NVDA')).watch_symbols==('SPY','TSLA','NVDA')
    with pytest.raises(ValueError): symbols('SPY,https://attacker.invalid')


def test_full_size_specs_are_not_micro_or_stock_specs():
    assert spec('ESZ6@100')['multiplier']==50
    assert spec('NQZ6@200')['multiplier']==20
    assert spec('MESZ6@300')['multiplier']==5
    assert spec('MNQZ6@400')['multiplier']==2
    assert spec('ESHA')['asset']=='stock'
    Config(local=True,futures=('ES.c.0','NQ.v.0')).validate()


def test_vendor_http_time_never_becomes_market_time():
    feed=Feed('fixture','Fixture','/fixture',60)
    result=normalize(feed,{'results':[{'symbol':'SPY','price':100}],'cached':False},NOW)
    assert result['received']==NOW and result['source_ts'] is None
    assert result['items'][0]['source_ts'] is None
    assert result['status']=='source_time_unknown'


@pytest.mark.parametrize('problem',['stale','future','unknown','new_level','vendor_stale'])
def test_exposure_cannot_trigger_from_unusable_or_newly_moved_level(problem):
    f=fact()
    apex={'source_ts':NOW-30,'received':NOW-120,'levels':[{'price':100.7,'kind':'apex','score':100,'known_at':NOW-120}]}
    if problem=='stale': apex['source_ts']=NOW-601
    if problem=='future': apex['source_ts']=NOW+1
    if problem=='unknown': apex['source_ts']=None
    if problem=='new_level': apex['levels'][0]['known_at']=NOW-10
    if problem=='vendor_stale': apex['vendor_stale']=True
    assert exposure_candidates(f,apex,NOW,.01)==[]


def test_dated_preexisting_exposure_level_requires_a_price_cross():
    f=fact()
    apex={'source_ts':NOW-30,'received':NOW-120,'levels':[{'price':100.7,'kind':'vex_concentration','known_at':NOW-120}]}
    result=exposure_candidates(f,apex,NOW,.01)
    assert len(result)==1 and result[0]['side']=='long'
    f['previous_bar']['c']=100.9
    assert exposure_candidates(f,apex,NOW,.01)==[]


def test_apex_preserves_unknown_flip_and_rejects_wrong_symbol():
    payload={'success':True,'data':{'symbol':'SPY','snapshotTime':'2026-09-14T14:15:00Z',
        'gammaFlip':None,'levels':[{'strike':100,'score':100}]}}
    assert len(apex_levels(payload,'SPY',NOW)['levels'])==1
    with pytest.raises(ValueError): apex_levels(payload,'QQQ',NOW)


def test_flow_needs_fresh_post_print_price_and_recent_structure():
    f=fact()
    row={'source_ts':NOW-1,'premium':200000,'score':90,'sentiment':'Bullish','vendor_id':'one'}
    assert flow_candidate(f,row,quote(),NOW,.01)
    assert not flow_candidate(f,row,quote(t=NOW-3),NOW,.01)
    assert not flow_candidate(f,{**row,'source_ts':NOW-121},quote(),NOW,.01)
    assert not flow_candidate({**f,'asof':NOW-91},row,quote(),NOW,.01)
    assert not flow_candidate(f,{**row,'sentiment':'Neutral'},quote(),NOW,.01)
    assert not flow_candidate(f,row,quote(bid=100.2,ask=100.21),NOW,.01)


def test_orb_retest_is_a_later_bar_and_resets_each_session():
    f=fact()|{'or_complete':True,'or_high':100.7,'or_low':99.5,'rvol20':1}
    result,arms=technical_candidates(f,{},NOW,.01)
    assert not any(r['rule']=='orb_retest' for r in result)
    later=f|{'asof':NOW+60,'bar_start':NOW,'previous_bar':f['bar'],
        'bar':{'o':101,'h':101.3,'l':100.6,'c':101.1,'v':100}}
    result,_=technical_candidates(later,arms,NOW+60,.01)
    assert any(r['rule']=='orb_retest' for r in result)
    result,_=technical_candidates(later,{'session':0,'orb_long':arms['orb_long']},NOW+60,.01)
    assert not any(r['rule']=='orb_retest' for r in result)


def test_missing_prior_coverage_prevents_prior_level_fade():
    f=fact()|{'prior_high':101,'prior_low':99,'prior_complete':False,'rvol20':1}
    f['bar']={'o':101,'h':102,'l':99,'c':99.5,'v':100}
    result,_=technical_candidates(f,{},NOW,.01)
    assert not any(r['rule']=='session_sweep_reclaim' for r in result)
    f['prior_complete']=True
    result,_=technical_candidates(f,{},NOW,.01)
    assert any(r['rule']=='session_sweep_reclaim' for r in result)


def test_unfinished_bar_does_not_create_pattern():
    rows=[{'id':i,'ts':NOW-60*(40-i),'payload':{'o':100,'h':100.1,'l':99.9,'c':100,'v':100}} for i in range(40)]
    baseline=features('SPY',rows,NOW)
    rows.append({'id':100,'ts':NOW,'payload':{'o':100,'h':200,'l':50,'c':190,'v':10000}})
    changed=features('SPY',rows,NOW)
    assert changed['bar']==baseline['bar'] and changed['asof']==NOW


def test_research_failure_isolated_and_last_good_snapshot_retained(db):
    async def run():
        cfg=Config(local=True,stocks=('SPY',),matrix='fixture')
        collector=Collectors(db,cfg)
        with db.tx() as c:
            for feed in FEEDS:
                if feed.key!='signals': db.put(c,'research_job:'+feed.key,{'attempted_at':NOW})
            db.put(c,'research_job:apex_SPY',{'attempted_at':NOW})
            db.put(c,'research:signals',{'source_ts':NOW-60,'data':{'previous':True}})
        async def failure(*args): raise FeedError('HTTP 503',503)
        collector.matrix_request=failure
        assert await collect(collector,NOW) is None
        with db.tx() as c:
            assert db.get(c,'research:signals')['data']['previous']
            assert db.get(c,'research_job:signals')['error']=='HTTP 503'
        await collector.close()
    asyncio.run(run())


def test_small_flow_budget_makes_forward_progress(db):
    async def run():
        collector=Collectors(db,Config(local=True))
        seen=[]
        async def request(path,label):
            page=int(path.split('&page=')[1].split('&')[0]);seen.append(page)
            return {'page':page,'pageSize':100,'total':500,'data':[
                {'id':page,'ticker':'SPY','tradeTime':'2026-09-14T14:15:00Z','premium':200000}]},NOW
        collector.matrix_request=request
        for _ in range(3): await collect_flow(collector,NOW,page_cap=2)
        assert seen==[1,2,1,3,1,4]
        await collector.close()
    asyncio.run(run())


def test_setup_dedup_survives_restart_and_has_no_order_route(db):
    cfg=Config(local=True,stocks=('SPY',),futures=(),scanner_paper=False)
    f=fact()
    with db.tx() as c:
        db.put(c,'scanner_features:SPY',f)
        db.put(c,'quote:SPY',quote())
        db.put(c,'matrix:unusual_activity',{'rows':[{'symbol':'SPY','source_ts':NOW-1,'premium':200000,'score':90,'sentiment':'Bullish','vendor_id':'one'}]})
    for _ in range(2):
        with db.tx() as c: Scanner(db,cfg).scan(c,NOW,Engine(db,cfg))
    with db.tx() as c:
        alerts=[r for r in db.recent(c,'alert') if r['payload'].get('status')=='setup_triggered']
        assert len(alerts)==1
        assert db.prefix(c,'position:')=={}
        assert alerts[0]['payload']['stop']<alerts[0]['payload']['entry']<alerts[0]['payload']['target']


def test_pending_options_expire_without_fabricated_fill(db):
    cfg=Config(local=True,stocks=('SPY',),futures=())
    signal={'id':'one','symbol':'SPY','side':'long','invalidation':100,'signal_price':101,'context':{'atr14':1}}
    with db.tx() as c:
        db.put(c,'pending_options:one',{'signal':signal,'expires_at':NOW-1,'status':'waiting'})
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg))
        assert db.get(c,'pending_options:one')['status']=='expired'
        assert db.prefix(c,'position:')=={}


def test_late_discord_setup_is_explicitly_expired():
    row={'id':42,'symbol':'SPY','ts':NOW,'payload':{'status':'setup_triggered','expires_at':NOW+120,
        'side':'long','reason':'Fixture','matched_rules':['orb_retest'],'entry':100,'stop':99,'target':102}}
    text=message_for(row,NOW+121)
    assert 'EXPIRED SETUP' in text and 'historical notification' in text and 'Event #42' in text
    assert len(text)<=1900


def test_scanner_and_research_routes_remain_private(db):
    cfg=Config(local=True,role='web',db=str(db.engine.url),stocks=('SPY',),futures=(),
        password='fixture-password-16-chars',secret='fixture-session-secret')
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/scanner').status_code==401
        assert client.get('/api/research?key=signals').status_code==401
        client.post('/login',json={'password':cfg.password})
        assert client.get('/api/research?key=https://attacker.invalid').status_code==400
        assert client.get('/api/scanner').json()['status']['mode']=='SIMULATED'
        assert client.get('/api/research?key=signals').json()['status']=='waiting'


def test_bad_history_symbol_does_not_starve_other_symbols(db):
    async def run():
        collector=Collectors(db,Config(local=True,stocks=('SPY','BAD','TSLA')))
        await collector.client.aclose()
        def reply(request):
            names=request.url.params['symbols'].split(',')
            if 'BAD' in names:
                return httpx.Response(400,json={'error':'invalid symbol'})
            return httpx.Response(200,json={'bars':{symbol:[{'t':'2026-09-14T14:14:00Z',
                'o':100,'h':101,'l':99,'c':100,'v':100}] for symbol in names}})
        collector.client=httpx.AsyncClient(transport=httpx.MockTransport(reply))
        await collector.history()
        with db.tx() as c:
            assert db.get(c,'bar_window:SPY') and db.get(c,'bar_window:TSLA')
            assert db.get(c,'health:alpaca_history')['status']=='partial'
            assert db.get(c,'health:alpaca_history')['failed_batches'][0]['symbols']==['BAD']
        await collector.close()
    asyncio.run(run())


def test_vendor_research_match_prioritizes_symbol_but_is_not_a_trade(db):
    async def run():
        collector=Collectors(db,Config(local=True,stocks=('SPY','TSLA'),matrix='fixture'))
        with db.tx() as c:
            for feed in FEEDS:
                if feed.key!='signals': db.put(c,'research_job:'+feed.key,{'attempted_at':NOW})
            for symbol in ('SPY','TSLA'): db.put(c,'research_job:apex_'+symbol,{'attempted_at':NOW})
        async def request(*args): return {'results':[{'symbol':'TSLA','price':100}]},NOW
        collector.matrix_request=request
        await collect(collector,NOW)
        with db.tx() as c:
            assert db.get(c,'focus:TSLA')['priority']==50
            assert db.get(c,'research_matches:TSLA')['signals']['source_ts'] is None
            assert not db.recent(c,'alert') and not db.prefix(c,'position:')
        await collector.close()
    asyncio.run(run())


def test_chain_failure_retries_are_bounded_and_other_contracts_remain_selected(db,monkeypatch):
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW)
    async def run():
        collector=Collectors(db,Config(local=True,stocks=('SPY','BAD'),massive='fixture'))
        calls=[]
        async def chain(symbol):
            calls.append(symbol)
            if symbol=='BAD': raise FeedError('HTTP 404',404)
            return [{'symbol':'O:SPY','underlying':'SPY','strike':100,'type':'call','multiplier':100,
                'expiry':'2026-09-14','oi':100,'gamma':.1,'iv':.2,'quote':quote(bid=1,ask=1.01)}],True
        collector.massive_chain=chain
        with db.tx() as c: db.put(c,'quote:SPY',quote(bid=99.99,ask=100.01))
        await collector.chains()
        await collector.chains()
        assert calls.count('BAD')==1
        assert 'O:SPY' in collector.option_symbols
        await collector.close()
    asyncio.run(run())


def test_late_snapshot_cannot_rewind_quote_or_bar_cursor(db):
    with db.tx() as c:
        assert db.put_quote(c,'quote:SPY',quote())
        assert not db.put_quote(c,'quote:SPY',quote(t=NOW-10,bid=90,ask=90.01))
        assert db.get(c,'quote:SPY')['bid']==101.10
        db.put_max(c,'latestbar:SPY',NOW)
        db.put_max(c,'latestbar:SPY',NOW-60)
        assert db.get(c,'latestbar:SPY')==NOW


def test_backfilled_window_keeps_later_live_bar(db):
    async def run():
        collector=Collectors(db,Config(local=True))
        price={'o':100,'h':101,'l':99,'c':100,'v':100}
        collector.bars('fixture',[('SPY',NOW,price)])
        collector.bars('fixture',[('SPY',NOW-60,price)])
        with db.tx() as c:
            assert [row[0] for row in db.get(c,'bar_window:SPY')]==[NOW-60,NOW]
            assert db.get(c,'latestbar:SPY')==NOW
        await collector.close()
    asyncio.run(run())
