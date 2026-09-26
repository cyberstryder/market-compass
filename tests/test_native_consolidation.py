import copy
from datetime import datetime,timezone
from types import SimpleNamespace
import json
import pytest
from sqlalchemy import select,func
from test_projects import db
from native_morning_fixtures import signal_event,checkpoint_event
from compass.native_morning_worker import initialize,accept,enrich
from compass.native_morning import store as m
from compass.native_reports import report
from compass.native_config import bootstrap,revise,validate,effective,revisions,KEY,RevisionConflict
from compass.native_smoothers import refresh_config,weekly,save
from compass.native_routing import change,mode_for,receipts
from compass.native_outbox import outbox
from compass.projects import ingest

NOW=datetime(2026,9,16,14,30,tzinfo=timezone.utc).timestamp()
ROW=dict(ticker='DEMO',s1=10,s2=20,s3=30,pm=.02,enabled=True,backtest_wr=.7)
AFTER_CLOSE=datetime(2026,9,16,22,tzinfo=timezone.utc).timestamp()


def reconciled_session(db,origin='direct'):
    """Synthetic paired evidence exercises the real report and handoff checks."""
    from compass.morning_history import save as mirror
    initialize(db)
    ev=signal_event(stamp=int(NOW*1000))
    accept(db,ev,NOW,origin=origin)
    class Unavailable:
        def lookup(self,*args, **kwargs):return dict(status='unavailable',reason='synthetic_missing_quote')
        def clock(self):return int((NOW+1)*1000)
    enrich(db,Unavailable(),NOW)
    bars=[dict(open_at_ms=int((NOW+minute*60)*1000),open=100.,high=100.1,low=99.9,close=100.,volume=10.) for minute in range(60)]
    with db.tx() as c:
        m.insert_many_once(c,m.stock_bars,[dict(signal_id='fixture-1',open_at_ms=b['open_at_ms'],bar_json=m.canonical(b),bar_hash=m.digest(b),received_at_ms=b['open_at_ms']+60000) for b in bars])
        mirror(db,c,'stock_inventory','fixture-1','fixture-1',{'payload':{'bars':[{'payload':b} for b in bars]}},AFTER_CLOSE,AFTER_CLOSE)
        ingest(db,c,'morning',dict(id='fixture-1',symbol='NASDAQ:DEMO',source_ts=NOW,original=ev['signal'],
            initial_received_at_ms=int(NOW*1000),option={'status':'unavailable'},
            delivery={'schema_version':1,'events':[{'event_id':'fixture-1'+suffix,'status':'delivered'} for suffix in ('-signal','-signal-option')]}),AFTER_CLOSE)
        db.put(c,'health:project_morning',dict(status='connected',checked_at=AFTER_CLOSE))
    return ev


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


def test_smoothers_missing_export_fields_stay_unknown():
    from compass.native_reports import smoother_comparison
    p=dict(direction='CALL',entry_price=100,target_price=102,signal_type='MAIN',status='OPEN',
        quality_score=58.5,quality_tier='B',quality_rank=32,featured_rank=None,is_featured=False)
    source={**p,'is_featured':None}
    r=smoother_comparison(p,source)
    assert r['status']=='incomplete_source_fields'
    assert r['missing_source_fields']==['is_featured'] and not r['differences']
    source['quality_score']=50.71
    r=smoother_comparison(p,source)
    assert r['status']=='different' and set(r['differences'])=={'quality_score'}
    source['is_featured']=True
    assert smoother_comparison(p,source)['differences']['is_featured']=={'native':False,'source':True}


def test_smoothers_compares_original_persisted_featured_flag_without_rewriting_source():
    from compass.native_reports import smoother_comparison
    native=dict(direction='CALL',entry_price=100,target_price=102,signal_type='MAIN',status='OPEN',
        quality_score=58.5,quality_tier='B',quality_rank=32,featured_rank=None,is_featured=False)
    source={k:v for k,v in native.items() if k!='is_featured'}
    source['quality_featured']=False
    result=smoother_comparison(native,source)
    assert result['status']=='matched_fields' and result['missing_source_fields']==[]
    assert result['source_field_aliases']=={'is_featured':'quality_featured'}
    assert 'is_featured' not in source
    source['quality_featured']=True
    assert smoother_comparison(native,source)['differences']['is_featured']=={'native':False,'source':True}
    source['quality_featured']='false'
    assert smoother_comparison(native,source)['missing_source_fields']==['is_featured']


def test_smoothers_score_evidence_uses_frozen_entry_inputs():
    from compass.native_reports import smoother_score_evidence
    native=dict(stats_at_entry={'wins':3,'losses':1},config={'backtest_wr':.7},
        entry_premium=None,est_return_pct=None,quality_option_adjustment=-7.5,
        quote={'quote_at_ms':1000},contract={'symbol':'AAA'})
    source=dict(alltime_wins_at_entry=4,alltime_losses_at_entry=1,backtest_wr_at_entry=.7,
        entry_premium=1,est_return_pct_at_entry=25,est_return_pct=90,
        quality_option_adjustment=0,option_quote_time='source-clock',occ_symbol='BBB')
    r=smoother_score_evidence(native,source)
    assert r['inputs']['alltime_wins']=={'native':3,'source':4}
    assert r['inputs']['est_return_pct']=={'native':None,'source':25}
    assert r['components']['quality_option_adjustment']=={'native':-7.5,'source':0}
    assert r['native_contract']=='AAA' and r['source_contract']=='BBB'
    assert r['native_quote_at_ms']==1000 and r['source_quote_time']=='source-clock'


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


def test_empty_session_blocks_official_handoff(db,monkeypatch):
    from compass.native_handoff import prepare,activate
    initialize(db)
    plan=prepare(db,'2026-09-15','2026-09-17',NOW)
    assert plan['state']=='blocked' and plan['blockers']
    monkeypatch.setenv('NATIVE_PROGRAM_SEND_ENABLED','true')
    monkeypatch.setenv('NATIVE_MORNING_DISCORD_WEBHOOK','https://discord.com/api/webhooks/123/test')
    with pytest.raises(RevisionConflict):activate(db,plan['id'],True,True,NOW)


def test_sender_boundary_and_rollback_never_promote_old_messages(db,monkeypatch):
    import httpx
    from compass.native_handoff import rollback
    from compass.native_outbox import queue,deliver_one
    with db.tx() as c:
        db.put(c,'native:ownership:morning',dict(owner='compass',previous_sender_paused=True,accepted_at=NOW-1,epoch='test',effective_from=NOW))
        queue(db,c,'morning','old',{'content':'historical'},NOW+1,event_time=NOW-60)
        queue(db,c,'morning','new',{'content':'new'},NOW+1,event_time=NOW+1)
    def handler(request):
        rollback(db,NOW+3)
        return httpx.Response(200,json={'id':'1234'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,{'morning':'https://discord.com/api/webhooks/123/test'},True,NOW+2)
        assert not deliver_one(db,client,{'morning':'https://discord.com/api/webhooks/123/test'},True,NOW+4)
    with db.tx() as c:
        statuses=dict(c.execute(select(outbox.c.event_key,outbox.c.status)).all())
        assert statuses=={'old':'shadow','new':'ambiguous'}
        assert db.get(c,'native:ownership:morning')['owner']=='original'


def test_handoff_arming_requires_environment_and_next_session(db,monkeypatch):
    from compass.native_handoff import prepare,activate
    from compass.market import session
    reconciled_session(db)
    after_close=AFTER_CLOSE
    opening=session('2026-09-17')[0]
    plan=prepare(db,'2026-09-16','2026-09-17',after_close)
    assert plan['state']=='prepared',plan['blockers']
    with pytest.raises(ValueError,match='environment'):activate(db,plan['id'],True,True,after_close+1)
    monkeypatch.setenv('NATIVE_PROGRAM_SEND_ENABLED','true')
    monkeypatch.setenv('NATIVE_MORNING_DISCORD_WEBHOOK','https://discord.com/api/webhooks/123/test')
    result=activate(db,plan['id'],True,True,after_close+2)
    assert result['plan']['state']=='armed'
    assert result['ownership']['effective_from']==opening
    with db.tx() as c:
        from compass.native_outbox import owner
        assert owner(db,c,'morning',after_close+3) is None
        assert owner(db,c,'morning',opening)['owner']=='compass'
        assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow'}


def test_direct_checkpoint_does_not_prove_direct_entry_delivery(db):
    from compass.native_handoff import prepare
    ev=reconciled_session(db,origin='source_summary_relay')
    accept(db,checkpoint_event(ev),NOW+300)
    with db.tx() as c:
        origins=report(db,c,AFTER_CLOSE)['morning']['signals'][0]['origins']
        assert next(x for x in origins if x['origin']=='direct')['entry_packets']==0
    plan=prepare(db,'2026-09-16','2026-09-17',AFTER_CLOSE)
    assert plan['state']=='blocked'
    assert 'Every reviewed signal needs direct entry-intake provenance' in plan['blockers']
    accept(db,ev,NOW+301)
    assert prepare(db,'2026-09-16','2026-09-17',AFTER_CLOSE)['state']=='prepared'


@pytest.mark.parametrize('mutation',['missing_preview','changed_preview','missing_bar','stale_source','source_only'])
def test_arming_rechecks_evidence_after_preparation(db,monkeypatch,mutation):
    from compass.native_handoff import prepare,activate,OWNER
    reconciled_session(db)
    plan=prepare(db,'2026-09-16','2026-09-17',AFTER_CLOSE)
    assert plan['state']=='prepared'
    with db.tx() as c:
        if mutation=='missing_preview':c.execute(outbox.delete())
        elif mutation=='changed_preview':c.execute(outbox.update().values(payload={'content':'Changed after review'}))
        elif mutation=='missing_bar':c.execute(m.stock_bars.delete())
        elif mutation=='stale_source':db.put(c,'health:project_morning',dict(status='connected',checked_at=NOW))
        else:ingest(db,c,'morning',dict(id='missed-entry',symbol='DEMO',source_ts=NOW,initial_received_at_ms=int(NOW*1000)),AFTER_CLOSE)
    monkeypatch.setenv('NATIVE_PROGRAM_SEND_ENABLED','true')
    monkeypatch.setenv('NATIVE_MORNING_DISCORD_WEBHOOK','https://discord.com/api/webhooks/123/test')
    with pytest.raises(RevisionConflict,match='[Ee]vidence'):activate(db,plan['id'],True,True,AFTER_CLOSE+1)
    with db.tx() as c:assert db.get(c,OWNER) is None


def test_handoff_protects_direct_route_and_restores_rehearsal_on_rollback(db,monkeypatch):
    from compass.native_handoff import prepare,activate,rollback
    from compass.native_routing import KEY as ROUTING
    from compass.morning_history import history
    from compass.projects import records
    reconciled_session(db)
    route=change(db,None,'prepare',AFTER_CLOSE,'relay_shadow','2026-09-18','Later relay rehearsal')
    route=change(db,route['revision'],'schedule',AFTER_CLOSE,reason='Schedule relay')
    plan=prepare(db,'2026-09-16','2026-09-17',AFTER_CLOSE)
    # A fresh import of identical observations must not invalidate operator review.
    with db.tx() as c:
        c.execute(records.update().values(updated=AFTER_CLOSE+1))
        c.execute(history.update().values(imported_at=AFTER_CLOSE+1))
    monkeypatch.setenv('NATIVE_PROGRAM_SEND_ENABLED','true')
    monkeypatch.setenv('NATIVE_MORNING_DISCORD_WEBHOOK','https://discord.com/api/webhooks/123/test')
    activate(db,plan['id'],True,True,AFTER_CLOSE+2)
    with db.tx() as c:assert mode_for(db.get(c,ROUTING),'2026-09-18')=='direct_shadow'
    for action in ('prepare','schedule','cancel_future'):
        with pytest.raises(RevisionConflict,match='Roll back'):
            change(db,route['revision'],action,AFTER_CLOSE+3,'relay_shadow','2026-09-18','Would disable direct path')
    with pytest.raises(RevisionConflict,match='Roll back'):prepare(db,'2026-09-16','2026-09-17',AFTER_CLOSE+3)
    rollback(db,AFTER_CLOSE+4)
    with db.tx() as c:
        restored=db.get(c,ROUTING)
        assert restored['schedule']==route['schedule']
        assert restored['revision']!=route['revision']
        assert mode_for(restored,'2026-09-18')=='relay_shadow'


def test_source_gaps_are_separate_from_native_inventory_loss(db):
    from compass.morning_history import save as mirror
    initialize(db);accept(db,signal_event(stamp=int(NOW*1000)),NOW)
    bar=dict(open_at_ms=int(NOW*1000),open=100.,high=100.1,low=99.9,close=100.,volume=10.)
    with db.tx() as c:
        m.insert_many_once(c,m.stock_bars,[dict(signal_id='fixture-1',open_at_ms=bar['open_at_ms'],bar_json=m.canonical(bar),bar_hash=m.digest(bar),received_at_ms=int((NOW+60)*1000))])
        mirror(db,c,'stock_inventory','fixture-1','fixture-1',{'payload':{'bars':[{'payload':bar}]}},NOW+60,NOW+61)
        r=report(db,c,NOW+7200)['morning']['signals'][0]
        assert r['coverage']['status']=='incomplete'
        assert r['source_candles']['status']=='matched_source_inventory'
        c.execute(m.stock_bars.delete())
        r=report(db,c,NOW+7200)['morning']['signals'][0]
        assert r['source_candles']['status']=='different'
        assert r['source_candles']['missing']==[int(NOW*1000)]


def test_retired_morning_queue_cannot_starve_smoothers(db):
    import httpx
    from compass.native_outbox import queue,deliver_one
    with db.tx() as c:
        for program in ('morning','smoothers'):
            db.put(c,'native:ownership:'+program,dict(owner='compass',previous_sender_paused=True,
                accepted_at=NOW-1,epoch='test',effective_from=NOW))
        for i in range(60):
            queue(db,c,'morning',str(i),{'content':'retired'},NOW+1,event_time=NOW+1)
        queue(db,c,'smoothers','active',{'content':'active'},NOW+2,event_time=NOW+2,cohort_time=NOW+2)
    sent=[]
    def handler(request):
        sent.append(request.content)
        return httpx.Response(200,json={'id':'1234'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,{'smoothers':'https://discord.com/api/webhooks/123/test'},
            True,NOW+3,disabled_programs=('morning',))
    assert len(sent)==1 and b'active' in sent[0]
    with db.tx() as c:
        assert set(c.execute(select(outbox.c.status).where(outbox.c.program=='morning')).scalars())=={'pending'}
