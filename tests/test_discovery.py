from copy import deepcopy
import pytest
from sqlalchemy import select,func
from compass.config import Config
from compass.store import Store,events,flow_records,discovery_trials as trials,discovery_checks as checks
from compass.discovery import assess,admit,observe,requested,refresh_universe,report,Discovery,VERSION,flow_rows as read_flow
from compass.swing_study import flow_snapshot
from compass.universe import data_symbols
from test_swing_ideas import NOW,daily,quote,flow_rows,stamp
from test_smoothers_postgres_handoff import pg

@pytest.fixture
def db(tmp_path):
    d=Store('sqlite:///'+str(tmp_path/'d.db'));d.initialize()
    yield d
    d.engine.dispose()

@pytest.fixture(params=['sqlite','postgres'])
def storage(request):return request.getfixturevalue('db' if request.param=='sqlite' else 'pg')

def cfg(**kw):return Config(local=True,stocks=('SPY',),futures=(),extra_futures=(),discovery=True,**kw)

def snap(age=20,symbol='SPY',side='long',**coverage):
    rows=flow_rows(NOW-age,'Bullish' if side=='long' else 'Bearish')
    for r in rows:r['last_seen']=NOW;r['payload']['symbol']=symbol
    return flow_snapshot(rows,dict(source_ts=NOW-age,received=NOW),coverage,NOW,0,180)

def minute():return dict(status='ready',asof=NOW,bar_start=NOW-60,bar=dict(c=100.2,h=100.3,l=100),previous_bar=dict(c=100.1,h=100.15,l=100))

def candidate():return assess('SPY',daily(),minute(),quote(NOW,100.19,100.21),2000000,snap(),NOW)['candidates'][0]

def test_continuation_does_not_require_recross_and_delayed_is_separate():
    for age,group in [(20,'fresh_flow'),(900,'delayed_flow')]:
        r=assess('SPY',daily(),minute(),quote(NOW,100.19,100.21),2000000,snap(age),NOW)
        assert r['candidates'][0]['rule']=='flow_continuation'
        assert r['candidates'][0]['flow']['group']==group

@pytest.mark.parametrize('side',['long','short'])
def test_bounce_both_directions(side):
    d=daily();m=minute();sign=1 if side=='long' else -1
    d.update(close=98 if sign==1 else 102,sma20=100,sma50=101 if sign==1 else 99,rsi14=35 if sign==1 else 65)
    m['bar']['c']=99 if sign==1 else 101;m['previous_bar']=dict(c=98 if sign==1 else 102,h=98.5 if sign==1 else 102.5,l=97.5 if sign==1 else 101.5)
    r=assess('SPY',d,m,quote(NOW,m['bar']['c']-.01,m['bar']['c']+.01),2e6,snap(side=side),NOW)
    assert any(x['rule']=='flow_bounce' and x['side']==side for x in r['candidates'])

@pytest.mark.parametrize('fault',['daily','minute','future_quote','stale_quote','spread','liquidity','tolerance','truncated','no_sentiment','missing_ohlc'])
def test_missing_or_ineligible_evidence_never_admits(fault):
    d=daily();m=minute();q=quote(NOW,100.19,100.21);v=2e6;s=snap()
    if fault=='daily':d['status']='warming_up'
    if fault=='minute':m['asof']=NOW-91
    if fault=='future_quote':q['ts']=NOW+1
    if fault=='stale_quote':q['ts']=NOW-6
    if fault=='spread':q.update(bid=98,ask=102)
    if fault=='liquidity':v=1e5
    if fault=='tolerance':q.update(bid=102,ask=102.02)
    if fault=='truncated':s=snap(query_truncated=True)
    if fault=='no_sentiment':
        for row in s['rows']['SPY']:row['payload']['sentiment']=''
    if fault=='missing_ohlc':m['previous_bar'].pop('h')
    assert not assess('SPY',d,m,q,v,s,NOW)['candidates']


def test_dynamic_coverage_is_independent_bounded_and_time_safe(storage):
    config=cfg(discovery_seeds=('PONY',),discovery_limit=1)
    r=flow_rows(NOW);r[0]['payload'].update(symbol='NEW',premium=250000)
    r[1]['payload'].update(symbol='DJT',premium=250000)
    with storage.tx() as c:
        pool=refresh_universe(storage,c,config,NOW,r)
        assert pool['symbols']==['SPY','PONY','NEW']
        assert {'PONY','NEW'}<=set(data_symbols(storage,c,config,NOW))
        assert config.watch_symbols==('SPY',) # discovery cannot change portfolio universe
        assert requested(storage,c,config,NOW+301)==('PONY',)
        r[1]['payload']['symbol']='LATER';r[1]['first_seen']=NOW+5
        pool=refresh_universe(storage,c,config,NOW+5,r)
        assert list(pool['dynamic'])==['LATER'] and 'NEW' in pool['excluded_capacity']
        assert not refresh_universe(storage,c,config,NOW+90000,[])['dynamic']


def test_revision_after_decision_is_not_read(storage):
    with storage.tx() as c:
        c.execute(storage.insert(flow_records).values(day='2026-09-14',vendor_id='x',source_ts=NOW-10,first_seen=NOW-5,last_seen=NOW+1,payload={}))
        assert read_flow(c,NOW)==[]


def test_restart_dedup_and_no_alert_or_option_admission(storage):
    with storage.tx() as c:
        item=candidate();assert admit(storage,c,item,NOW)
        changed=deepcopy(item);changed['entry_mid']=1000
        assert not admit(storage,c,changed,NOW+5)
        assert c.execute(select(trials.c.payload)).scalar_one()['entry_mid']==item['entry_mid']
        assert c.execute(select(func.count()).select_from(checks)).scalar_one()==4
        assert not storage.recent(c,'alert')
        assert not storage.prefix(c,'position:')
        assert report(storage,c,NOW)['total']==1


def test_checkpoint_missing_never_uses_late_quote_and_pause_keeps_collection(storage):
    with storage.tx() as c:
        admit(storage,c,candidate(),NOW)
        target=c.execute(select(checks.c.target_at).where(checks.c.horizon=='60m')).scalar_one()
        storage.put(c,'quote:SPY',quote(target+61,110,110.02))
        observe(storage,c,target+61)
        row=c.execute(select(checks).where(checks.c.horizon=='60m')).mappings().one()
        assert row['status']=='unavailable' and 'directional_pct' not in row['payload']
        assert 'SPY' in requested(storage,c,cfg(discovery_seeds=()),target+61)
        paused=cfg();paused.discovery=False
        assert 'SPY' in requested(storage,c,paused,target+61)


def test_measured_checkpoint_uses_directional_underlying_midpoint(storage):
    with storage.tx() as c:
        p=candidate();admit(storage,c,p,NOW)
        target=c.execute(select(checks.c.target_at).where(checks.c.horizon=='60m')).scalar_one()
        storage.put(c,'quote:SPY',quote(target+2,101,101.02));observe(storage,c,target+2)
        row=c.execute(select(checks).where(checks.c.horizon=='60m')).mappings().one()
        assert row['status']=='measured'
        assert row['payload']['directional_pct']==pytest.approx((101.01/p['entry_mid']-1)*100)


def test_worker_integrates_candidate_diagnostics_and_durable_dedup(storage,monkeypatch):
    import compass.discovery as module
    monkeypatch.setattr(module,'daily_context',lambda rows,at:daily())
    monkeypatch.setattr(module,'minute_context',lambda rows,at:minute())
    monkeypatch.setattr(module,'liquidity',lambda rows,at:2e6)
    config=cfg(discovery_seeds=());worker=Discovery(storage,config)
    with storage.tx() as c:
        storage.put(c,'matrix:unusual_activity',dict(source_ts=NOW-20,received=NOW))
        storage.put(c,'quote:SPY',quote(NOW,100.19,100.21))
        for i,r in enumerate(flow_rows(NOW-20)):
            c.execute(storage.insert(flow_records).values(day='2026-09-14',vendor_id=str(i),source_ts=r['payload']['source_ts'],first_seen=NOW-10,last_seen=NOW-10,payload=r['payload']))
    worker.tick(NOW);worker.tick(NOW+1)
    with storage.tx() as c:
        assert c.execute(select(func.count()).select_from(trials)).scalar_one()==1
        assert len(storage.recent(c,'discovery_decision'))==1
        assert not storage.recent(c,'alert')
        assert storage.get(c,VERSION+':scan:SPY')['tests']
        assert c.execute(select(trials.c.payload)).scalar_one()['origin']=='base_watchlist'


def test_early_close_and_afterhours_do_not_create_candidates():
    at=stamp('2026-11-27',13,1)
    assert 'outside_entry_window' in assess('SPY',daily(at),minute(),quote(at),2e6,snap(),at)['reasons']


def test_discovery_recovery_includes_daily_requests(db):
    from compass.secondary_data import coverage,plan
    from types import SimpleNamespace
    config=cfg(discovery_seeds=('PONY',));config.swing_ideas=False
    with db.tx() as c:
        rows=coverage(db,c,config,NOW)['rows']
        pony=next(r for r in rows if r['symbol']=='PONY')
        assert 'discovery' in pony['projects'] and pony['collection_enabled'] and not pony['daily_ready']
    jobs=plan(SimpleNamespace(db=db,cfg=config),NOW)
    assert 'PONY' in jobs['daily']


def test_configured_excluded_seed_is_never_collected(db):
    with db.tx() as c:
        config=cfg(discovery_seeds=('DJT','PONY'))
        assert 'DJT' not in refresh_universe(db,c,config,NOW,[])['symbols']
        assert 'DJT' not in requested(db,c,config,NOW)


def test_report_route_and_dashboard_are_wired_without_an_alert(db):
    from compass.app import create_app
    from fastapi.testclient import TestClient
    app=create_app(cfg(role='web',db=str(db.engine.url),password='fixture-password-16',secret='fixture-secret'))
    with TestClient(app) as client:
        assert client.get('/api/discovery').status_code==401
        client.post('/login',json={'password':'fixture-password-16'})
        response=client.get('/api/discovery')
        assert response.status_code==200 and response.json()['version']==VERSION
        assert response.json()['total']==0
        html=client.get('/detailed').text
        assert 'id="discovery"' in html and '/static/discovery.js' in html
        assert client.get('/static/discovery.js').status_code==200
