from copy import deepcopy
from html.parser import HTMLParser
import pytest
from sqlalchemy import select,func,update
from fastapi.testclient import TestClient
from compass.app import create_app
from compass.config import Config
from compass.market import day,session,NY
from compass.store import Store,events,state,identity,flow_records,swing_trials as trials,swing_checkpoints as checkpoints
from compass.swing_ideas import SwingIdeas,swings
from compass.swing_study import flow_snapshot,classify,register,inventory,observe,targets,Study,VERSION,HORIZONS
from compass.swing_study_report import report,record_page
from compass.universe import data_symbols
from test_smoothers_postgres_handoff import pg
from test_swing_ideas import NOW,signal,quote,flow_rows,daily,stamp


@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'study.db'));db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture(params=['sqlite','postgres'])
def storage(request):return request.getfixturevalue('db' if request.param=='sqlite' else 'pg')


def cfg():return Config(local=True,stocks=('SPY',),futures=(),extra_futures=())


def snapshot(age=20,**changes):
    rows=flow_rows(NOW-age)
    for r in rows:
        r.update(first_seen=NOW,last_seen=NOW)
        r['payload']['expiry']='10/16/26'
    matrix=dict(source_ts=NOW-age,received=NOW,**changes)
    return flow_snapshot(rows,matrix,dict(query_truncated=False,vendor_limited=False,recovery={'estimated_gap':0}),NOW,14,60)


def event(db,c,ident='one',side='long',symbol='SPY',at=NOW):
    s=signal(at,side);s['symbol']=symbol
    db.append(c,'swing_candidate','swing_research',symbol,at,
        dict(signal=s,flow_confirmed=False,flow={},coverage={}),ident)
    return c.execute(select(events).where(events.c.key==ident)).mappings().one()


def seed(db,c,at=NOW,price=100):
    db.put(c,'quote:SPY',quote(at,price,price+.02))
    db.append(c,'daily','alpaca','SPY',stamp(daily(at)['through'],0,0),dict(c=100))


@pytest.mark.parametrize('age,vendor_stale,group',[(20,False,'fresh_flow'),(300,False,'fresh_flow'),
    (301,False,'delayed_flow'),(900,True,'delayed_flow'),(1801,False,'no_confirming_flow'),(20,True,'fresh_vendor_blocked')])
def test_pre_gate_groups_keep_source_delay_separate_from_vendor_gate(age,vendor_stale,group):
    s=snapshot(age,vendor_stale=vendor_stale)
    assert classify(s,'SPY','long',NOW)['group']==group
    assert classify(s,'SPY','short',NOW)['group']=='no_confirming_flow'


@pytest.mark.parametrize('fault',['poll','snapshot','truncated','limited','gap','future_correction'])
def test_missing_or_partial_flow_does_not_become_no_flow(fault):
    s=snapshot(900)
    if fault=='poll':s['matrix']['received']=NOW-61
    if fault=='snapshot':s['at']=NOW-31
    if fault=='truncated':s['coverage']['query_truncated']=True
    if fault=='limited':s['coverage']['vendor_limited']=True
    if fault=='gap':s['coverage']['recovery']['estimated_gap']=1
    if fault=='future_correction':
        rows=flow_rows();rows[0]['last_seen']=NOW+1
        s=flow_snapshot(rows,s['matrix'],s['coverage'],NOW,14,60)
        assert s['future_snapshots']==1
    result=classify(s,'SPY','long',NOW)
    assert result['group']=='unavailable' and result['reasons']


def test_freeze_and_restart_do_not_replace_flow_or_price(storage,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    s=snapshot(900)
    with storage.tx() as c:
        seed(storage,c);e=event(storage,c)
        assert register(storage,c,e,NOW,snapshot=s)
        saved=deepcopy(c.execute(select(trials.c.payload)).scalar_one())
        assert saved['flow']['group']=='delayed_flow' and saved['entry_mid']==pytest.approx(100.01)
        assert len(saved['flow']['source_rows'])==2
        s['rows']['SPY'][0]['payload']['premium']=999999
        storage.put(c,'quote:SPY',quote(NOW+1,150,150.02))
        assert not register(storage,c,e,NOW+1,snapshot=s)
        assert c.execute(select(trials.c.payload)).scalar_one()==saved
        assert c.execute(select(func.count()).select_from(checkpoints)).scalar_one()==len(HORIZONS)
        assert not storage.recent(c,'alert')


@pytest.mark.parametrize('case',['stale_quote','future_quote','presignal_quote','late_processing','late_signal','excluded_symbol'])
def test_candidate_clock_failures_stay_unavailable(db,monkeypatch,case):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        seed(db,c);e=dict(event(db,c,symbol='DJT' if case=='excluded_symbol' else 'SPY'))
        at=NOW+31 if case=='late_processing' else NOW
        if case=='stale_quote':db.put(c,'quote:SPY',quote(NOW-6))
        if case=='future_quote':db.put(c,'quote:SPY',quote(NOW+.1))
        if case=='presignal_quote':db.put(c,'quote:SPY',quote(NOW-1))
        if case=='late_signal':e['payload']['signal']['signal_time']=NOW-91
        register(db,c,e,at,snapshot=snapshot())
        saved=c.execute(select(trials)).mappings().one()
        assert saved['status']=='unavailable' and saved['payload']['entry_mid'] is None
        assert c.execute(select(func.count()).select_from(checkpoints).where(checkpoints.c.status=='unavailable')).scalar_one()==6


def test_discovery_keeps_delayed_context_but_does_not_admit_or_alert(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    monkeypatch.setattr('compass.swing_ideas.technical_setups',lambda *args:[signal()])
    with db.tx() as c:
        seed(db,c)
        db.put(c,'swing_daily:SPY',{**daily(),'computed_at':NOW})
        db.put(c,'scanner_features:SPY',dict(status='ready',asof=NOW))
        db.put(c,'matrix:unusual_activity',dict(source_ts=NOW-900,received=NOW))
        for row in snapshot(900)['rows']['SPY']:
            p=row['payload'];c.execute(db.insert(flow_records).values(day=day(NOW),vendor_id=p['vendor_id'],
                source_ts=p['source_ts'],first_seen=NOW,last_seen=NOW,payload=p))
        svc=SwingIdeas(db,cfg(),clock=lambda:NOW);svc.discover(c,NOW);svc.discover(c,NOW)
        assert svc.flow_cache=={}
        saved=c.execute(select(trials.c.payload)).scalar_one()
        assert saved['flow']['group']=='delayed_flow' and saved['original_candidate']['flow']=={}
        assert len(saved['flow']['source_rows'])==2
        assert c.execute(select(func.count()).select_from(swings)).scalar_one()==0
        assert len(db.recent(c,'swing_candidate'))==1 and not db.recent(c,'alert')


def test_history_inventory_catches_up_without_reconstructing_classifications(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        seed(db,c)
        for i in range(4):event(db,c,str(i))
        assert inventory(db,c,NOW,limit=2)==2
        assert inventory(db,c,NOW,limit=2)==2
        assert inventory(db,c,NOW,limit=2)==0
        rows=c.execute(select(trials.c.payload)).scalars().all()
        assert all(r['flow']['group']=='historical_inventory' and r['entry_mid'] is None for r in rows)
    Study(db,cfg()).tick(NOW)
    with db.tx() as c:
        r=db.get(c,VERSION+':report')
        assert r['inventory']==dict(retained_candidates=4,registered=4,unregistered=0)
        assert r['counts']['prospective']==0 and not r['comparisons']


def test_checkpoint_calendar_includes_holidays_and_early_closes():
    friday=stamp('2026-09-04',15,30)
    t=targets(friday)
    assert t['60m']==stamp('2026-09-08',10,0)
    assert t['1session']==session('2026-09-08')[1]-.001
    assert t['5session']==session('2026-09-14')[1]-.001
    t=targets(stamp('2026-11-27',12,30))
    assert t['close']==stamp('2026-11-27',13,0)-.001
    assert t['60m']==stamp('2026-11-30',10,0)
    assert t['9session']==session('2026-12-10')[1]-.001


@pytest.mark.parametrize('side',['long','short'])
@pytest.mark.parametrize('evidence',['timely','late_quote','basis_changed','later_basis_correction'])
def test_checkpoint_uses_only_evidence_available_by_fixed_deadline(db,monkeypatch,side,evidence):
    clock=[NOW];monkeypatch.setattr('compass.store.time.time',lambda:clock[0])
    with db.tx() as c:
        seed(db,c);e=event(db,c,side=side);register(db,c,e,NOW,snapshot=snapshot(900))
        target=c.execute(select(checkpoints.c.target_at).where(checkpoints.c.horizon=='60m')).scalar_one()
        clock[0]=target+8 if evidence=='late_quote' else target
        db.append(c,'quote','fixture','SPY',target-1,quote(target-1,101,101.02))
        if evidence in ('basis_changed','later_basis_correction'):
            clock[0]=target+1 if evidence=='basis_changed' else target+6
            db.append(c,'daily','alpaca','SPY',stamp(daily()['through'],0,0),dict(c=50))
        assert observe(db,c,target+600)==1
        cp=dict(c.execute(select(checkpoints).where(checkpoints.c.horizon=='60m')).mappings().one())
        if evidence in ('timely','later_basis_correction'):
            assert cp['status']=='completed'
            assert cp['directional_return_pct']==pytest.approx((101.01/100.01-1)*100*(1 if side=='long' else -1))
        else:
            assert cp['status']=='unavailable' and cp['directional_return_pct'] is None
        assert observe(db,c,target+1200)==0
        assert dict(c.execute(select(checkpoints).where(checkpoints.c.horizon=='60m')).mappings().one())==cp


def test_pending_symbols_keep_stock_collection_without_option_requests(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        seed(db,c);register(db,c,event(db,c),NOW,snapshot=snapshot())
        other=Config(local=True,stocks=('QQQ',),futures=())
        assert set(('QQQ','SPY'))<=set(data_symbols(db,c,other,NOW))
        assert c.execute(select(func.count()).select_from(swings)).scalar_one()==0


def bulk(db,c,n):
    candidates=[];sources=[];points=[]
    groups=('fresh_flow','delayed_flow','no_confirming_flow','unavailable','fresh_vendor_blocked')
    for i in range(n):
        id=identity(VERSION,i);group=groups[i%5]
        sources.append(dict(id=i+1,key=id,kind='swing_candidate',source='fixture',symbol='SPY',ts=NOW,received=NOW,payload={}))
        p=dict(id=id,origin='prospective',first_seen=NOW,assessed_at=NOW,signal=signal(),flow={},entry_mid=100,
            receipt_day=day(NOW),admission_id=identity('admission',i))
        candidates.append(dict(id=id,version=VERSION,source_id=i+1,symbol='SPY',side='long',rule='daily_breakout',
            first_seen=NOW,assessed_at=NOW,origin='prospective',flow_group=group,status='complete',payload=p))
        points.append(dict(study_id=id,horizon='5session',target_at=NOW,status='completed',directional_return_pct=1))
    for offset in range(0,n,500):
        c.execute(db.insert(events).values(sources[offset:offset+500]))
        c.execute(db.insert(trials).values(candidates[offset:offset+500]))
        c.execute(db.insert(checkpoints).values(points[offset:offset+500]))
    return candidates


def test_full_window_totals_exceed_display_limits_and_keep_unknown_flow_separate(storage):
    with storage.tx() as c:
        bulk(storage,c,10005)
        r=report(storage,c,NOW,cfg());arms={x['arm']:x for x in r['comparisons']}
        assert r['inventory']==dict(retained_candidates=10005,registered=10005,unregistered=0)
        assert r['counts']['prospective']==10005 and r['counts']['flow_unavailable']==2001
        assert arms['technical_only']['records']==8004 and arms['technical_only']['measured']==8004
        assert arms['fresh_flow']['records']==2001 and arms['rejected_by_fresh_flow']['records']==6003
        assert arms['delayed_flow']['mean_directional_pct']==1
        assert sum(x['records'] for x in r['buckets'] if x['dimension']=='flow_group')==10005
        assert r['coverage']=='full_window' and not r['truncated']


def test_equal_time_pages_are_stable_and_option_admissions_remain_separate(storage):
    with storage.tx() as c:
        rows=bulk(storage,c,210)
        c.execute(storage.insert(swings).values(id=rows[0]['payload']['admission_id'],underlying='SPY',status='pending',
            created=NOW,updated=NOW,payload={'status':'pending','entry':None}))
        first=record_page(c,NOW);page=first;ids=[];statuses=[]
        while True:
            ids.extend(r['id'] for r in page['records']);statuses.extend(r['option_status'] for r in page['records'])
            if not page['has_more']:break
            page=record_page(c,NOW+1,cursor=page['next_cursor'])
        assert len(ids)==len(set(ids))==210 and statuses.count('pending')==1
        assert report(storage,c,NOW,cfg())['option_pipeline']==[dict(origin='prospective',status='pending',count=1)]
        c.execute(update(trials).where(trials.c.id==ids[-1]).values(assessed_at=NOW+1))
        assert record_page(c,NOW+2,asof=NOW)['total']==209
        with pytest.raises(ValueError):record_page(c,NOW+2,cohort='fresh_flow',cursor=first['next_cursor'])


def test_owner_api_is_read_only_and_html_exposes_every_checkpoint(tmp_path):
    c=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'web.db'),password='fixture-password-16',secret='fixture-secret')
    app=create_app(c)
    with TestClient(app) as client:
        assert client.get('/api/swing-study').status_code==401
        assert client.get('/api/swing-study/records').status_code==401
        client.post('/login',json={'password':c.password})
        assert client.get('/api/swing-study/records?limit=101').status_code==422
        for query in ('cohort=bad','cursor=bad','asof=999999999999'):
            assert client.get('/api/swing-study/records?'+query).status_code==400
        r=client.get('/api/swing-study/records')
        assert r.status_code==200 and r.json()['total']==0 and 'no-store' in r.headers['cache-control']
        with app.state.db.tx() as con:
            assert con.execute(select(func.count()).select_from(events)).scalar_one()==0
            assert con.execute(select(func.count()).select_from(trials)).scalar_one()==0
        class Selects(HTMLParser):
            selected=False
            values=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='select':self.selected=attrs.get('id')=='swing-study-horizon'
                if tag=='option' and self.selected:self.values.append(attrs.get('value'))
            def handle_endtag(self,tag):
                if tag=='select':self.selected=False
        parser=Selects();parser.feed(client.get('/detailed').text)
        assert set(parser.values)==set(HORIZONS) and len(parser.values)==len(HORIZONS)
