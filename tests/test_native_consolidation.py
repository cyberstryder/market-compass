import copy
from datetime import datetime,timezone
from types import SimpleNamespace
import json
import pytest
from sqlalchemy import select,func
from test_projects import db
from native_morning_fixtures import signal_event
from compass.native_morning_worker import initialize,accept
from compass.native_morning import store as m
from compass.native_reports import report
from compass.native_config import bootstrap,revise,validate,effective,revisions,KEY,RevisionConflict
from compass.native_smoothers import refresh_config,weekly,save
from compass.native_routing import change,mode_for,receipts
from compass.native_outbox import outbox
from compass.projects import ingest

NOW=datetime(2026,9,16,14,30,tzinfo=timezone.utc).timestamp()
ROW=dict(ticker='DEMO',s1=10,s2=20,s3=30,pm=.02,enabled=True,backtest_wr=.7)


def own(db):
    with db.tx() as c:db.put(c,KEY,dict(configs=[ROW],alltime=[dict(ticker='DEMO',wins=3,losses=2)],received_at=NOW,revision='source-revision'))
    return bootstrap(db,NOW)


def test_native_report_without_original_app_data(db):
    initialize(db);accept(db,signal_event(stamp=int(NOW*1000)),NOW)
    with db.tx() as c:
        for minute in range(60):
            bar=dict(open_at_ms=int((NOW+minute*60)*1000),open=100.,high=100.1,low=99.9,close=100.,volume=10.)
            m.insert_many_once(c,m.stock_bars,[dict(signal_id='fixture-1',open_at_ms=bar['open_at_ms'],bar_json=m.canonical(bar),bar_hash=m.digest(bar),received_at_ms=bar['open_at_ms']+60000)])
        r=report(db,c,NOW+7200)
    assert r['morning']['summary']['coverage']=={'complete':1}
    row=r['morning']['signals'][0]
    assert all(x['status']=='modeled' for x in row['stock_exits'])
    assert row['source_signal']['status']=='source_unavailable'
    assert row['source_option']['status']=='unavailable'
    assert r['morning']['stock_groups'][0]['paired']==1
    assert row['origins'][0]['origin']=='direct'
    assert r['morning']['delivery'][0]['native_status']=='shadow'
    assert not r['cutover_ready']


def test_missing_intent_and_source_mismatch_visible(db):
    initialize(db);ev=signal_event(stamp=int(NOW*1000));accept(db,ev,NOW)
    with db.tx() as c:
        c.execute(outbox.delete())
        original=copy.deepcopy(ev['signal']);original['price']=101.
        ingest(db,c,'morning',dict(id='fixture-1',symbol='DEMO',source_ts=NOW,original=original,status='tracking',strategy='test'),NOW)
        r=report(db,c,NOW+300)
    assert r['morning']['signals'][0]['source_signal']['differences']['price']=={'native':100.,'source':101.}
    assert r['morning']['delivery'][0]['status']=='missing_native_intent'
    assert r['morning']['stock_groups'][0]['paired']==0


def test_configuration_ownership_stops_remote_dependency_and_rolls_back(db):
    first=own(db)
    class NoNetwork:
        def get(self,*a,**k):raise AssertionError('Owned configuration must not read source')
    refresh_config(db,SimpleNamespace(smoothers_url='https://example.test',smoothers_token='x'),NoNetwork(),NOW+999999)
    changed=revise(db,first['revision'],[dict(ROW,pm=.03)],'Test target edit',NOW+1)
    with pytest.raises(RevisionConflict):revise(db,first['revision'],[ROW],'Stale editor',NOW+2)
    restored=revise(db,changed['revision'],None,'Restore baseline',NOW+3,first['revision'])
    assert restored['configs']==first['configs'] and restored['revision']!=first['revision']
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(revisions)).scalar_one()==3
        assert c.execute(select(revisions.c.payload).where(revisions.c.id==first['revision'])).scalar_one()['configs'][0]['pm']==.02


@pytest.mark.parametrize('field,value',[('s1',1.5),('s2',True),('pm',float('nan')),('backtest_wr',1.01),('enabled',1)])
def test_config_rejects_invalid_coercion(field,value):
    with pytest.raises(ValueError):validate([dict(ROW,**{field:value})])


def test_statistics_baseline_counts_only_new_native_resolutions(db):
    config=own(db)
    with db.tx() as c:
        for id,created,status in [('older',NOW-1,'WIN'),('new',NOW+1,'WIN'),('gap',NOW+1,'UNRESOLVED')]:
            save(db,c,dict(id=id,ticker='DEMO',week='2026-09-14',created_at=created,status=status))
        stats=effective(db,c,config)['alltime']
    assert stats==[dict(ticker='DEMO',wins=4,losses=2)]


def test_routes_prepare_schedule_cancel_do_not_enable_sender(db):
    initialize(db)
    prepared=change(db,None,'prepare',NOW,'direct_shadow','2026-09-17','Direct shadow rehearsal')
    assert prepared['effective_mode']=='parallel_shadow' and not prepared['schedule']
    scheduled=change(db,prepared['revision'],'schedule',NOW+1,reason='External routing arranged for tomorrow')
    assert mode_for(scheduled,'2026-09-17')=='direct_shadow'
    next_stamp=datetime(2026,9,17,14,30,tzinfo=timezone.utc).timestamp()
    ev=signal_event('next',stamp=int(next_stamp*1000))
    assert accept(db,ev,next_stamp,origin='source_summary_relay')['status']=='route_disabled'
    assert accept(db,ev,next_stamp)['status']=='accepted'
    with db.tx() as c:
        assert db.get(c,'native:ownership:morning') is None
        assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow'}
    # Cancel while still before the effective session.
    cancelled=change(db,scheduled['revision'],'cancel_future',NOW+2,reason='Cancel rehearsal')
    assert mode_for(cancelled,'2026-09-17')=='parallel_shadow'
    with pytest.raises(RevisionConflict):change(db,prepared['revision'],'cancel_future',NOW+3,reason='Stale screen')


def test_same_day_route_rejected_and_provenance_idempotent(db):
    initialize(db)
    with pytest.raises(ValueError):change(db,None,'prepare',NOW,'direct_shadow','2026-09-16','Too late')
    ev=signal_event(stamp=int(NOW*1000));accept(db,ev,NOW);accept(db,ev,NOW+2)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(receipts)).scalar_one()==1
        assert c.execute(select(receipts.c.received)).scalar_one()==NOW


def test_native_apis_require_owner_and_reject_cross_origin(tmp_path):
    from fastapi.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    app=create_app(Config(local=True,role='web',password='native-owner-test-password',db='sqlite:///'+str(tmp_path/'private.db')))
    with TestClient(app) as client:
        for path in ('/api/native/report','/api/native/smoothers/config','/api/native/morning/routing'):
            assert client.get(path).status_code==401
        assert client.post('/login',json={'password':'native-owner-test-password'}).status_code==200
        assert client.get('/api/native/report?download=true').status_code==200
        assert client.get('/api/native/report?week=2026-09-16').status_code==422
        assert client.post('/api/native/morning/routing',headers={'Origin':'https://wrong.test'},json={}).status_code==403
        assert client.post('/api/native/morning/routing',json={'action':'enable_sender'}).status_code==422


def test_weekly_comparison_exposes_quality_and_ranking_differences(db):
    initialize(db)
    with db.tx() as c:
        save(db,c,dict(id='weekly',ticker='DEMO',week='2026-09-14',created_at=NOW,status='OPEN',direction='CALL',entry_price=100,target_price=102,signal_type='MAIN',quality_score=60,quality_rank=2,is_featured=False))
        original=dict(ticker='DEMO',status='OPEN',direction='CALL',entry_price=100,target_price=102,signal_type='MAIN',quality_score=80,quality_rank=1,is_featured=True)
        ingest(db,c,'smoothers',dict(id='source-weekly',symbol='DEMO',source_ts=NOW,original=original,status='open',strategy='test'),NOW)
        result=report(db,c,NOW)['smoothers']['rows'][0]
    assert result['comparison']['status']=='different'
    assert set(result['comparison']['differences'])=={'quality_score','quality_rank','is_featured'}
