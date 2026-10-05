from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
import httpx
import pandas as pd
import pytest
from sqlalchemy import select
from test_projects import db
from compass import smoothers_messages as messages
from compass.native_config import KEY as CONFIG,bootstrap,revise,RevisionConflict
from compass.native_outbox import outbox,queue,owner,deliver_one
from compass.native_smoothers import VERSION,weekly,save,queue_signal,monitor
from compass.native_reports import smoothers
from compass.projects import ingest,records
from compass.smoothers_handoff import prepare,activate,rollback,sessions,OWNER


def completed_week(db,week='2026-09-14'):
    calendar=sessions(week);entry=calendar[0]['open']+3600;finished=entry+400;now=calendar[-1]['close']+3601
    configs=[dict(ticker=t,s1=10,s2=20,s3=30,pm=.02,enabled=True,backtest_wr=.7) for t in ('AAA','BBB')]
    with db.tx() as c:db.put(c,CONFIG,dict(configs=configs,alltime=[],received_at=entry-3601,revision='source'))
    config=bootstrap(db,entry-3601)
    signals=[]
    for i,row in enumerate(configs):
        win=i==0
        p=dict(id=row['ticker'],ticker=row['ticker'],week=week,status='WIN' if win else 'LOSS',
            created_at=entry+300,model_entry_time=entry,direction='CALL' if win else 'PUT',signal_type='MAIN',
            config=row,config_revision=config['revision'],message_version=messages.VERSION,
            entry_price=100.,target_price=102. if win else 98.,pm=.02,
            contract=dict(symbol=row['ticker']+'260918C00100000',strike=100,expiration=calendar[-1]['date']),
            entry_premium=1.,quote=dict(status='available',bid=.95,ask=1.05,midpoint=1.,quote_at_ms=finished*1000),
            quality_score=80.-i,quality_tier='A+' if win else 'A',quality_rank=i+1,is_featured=win,featured_rank=1 if win else None,
            target_atr_mult=.3,quality_atr_score=100,quality_reliability_score=75,est_return_pct=50,
            stats_at_entry=dict(wins=3,losses=1),rolling_at_entry=dict(wins=3,losses=1),coverage_missing=False,
            premium_attempt_at=entry+3600,premium_last_attempt_quote=dict(status='available',midpoint=1.),
            last_seen_at=calendar[-1]['close']-60,last_underlying=99.,
            resolution_time=entry+600 if win else calendar[-1]['close']+300,exit_underlying=102. if win else 99.)
        if win:p.update(touch_bar=dict(open=101,high=102.1,low=100.9,close=101.8),touch_detected_at=entry+675,
                        exit_quote=dict(status='available',bid=1.4,ask=1.6,midpoint=1.5,quote_at_ms=(entry+675)*1000))
        signals.append(p)
    with db.tx() as c:
        for p in signals:
            save(db,c,p)
            original={**p,**p['config'],'occ_symbol':p['contract']['symbol'],
                      'resolution_time':datetime.fromtimestamp(p['resolution_time'],timezone.utc).isoformat()}
            ingest(db,c,'smoothers',dict(id=p['id'],symbol=p['ticker'],source_ts=entry,status=p['status'].lower(),original=original),now)
            if p['is_featured']:queue_signal(db,c,p,'entry',messages.entry(p),finished,entry)
            event='target' if p['status']=='WIN' else 'close'
            queue_signal(db,c,p,event,getattr(messages,event)(p),now,p['resolution_time']+60 if event=='target' else p['resolution_time'])
        previews=messages.roster(signals,week,finished)
        for i,payload in enumerate(previews):queue(db,c,'smoothers',week+':summary'+(':'+str(i+1) if i else ''),payload,finished,event_time=entry,cohort_time=entry)
        db.put(c,VERSION+':week:'+week,dict(week=week,sessions=calendar,state='complete',index=2,errors=[],
            config=config,started=entry+300,finished=finished,signals=2,summary_messages=len(previews),message_version=messages.VERSION))
        db.put(c,'health:project_smoothers',dict(status='connected',checked_at=now))
    return now,signals


def enable(monkeypatch):
    monkeypatch.setenv('NATIVE_PROGRAM_SEND_ENABLED','true')
    monkeypatch.setenv('NATIVE_SMOOTHERS_DISCORD_WEBHOOK','https://discord.com/api/webhooks/123/test')


def test_weekly_handoff_requires_complete_cycle_and_explicit_source_channel_review(db,monkeypatch):
    now,signals=completed_week(db)
    plan=prepare(db,'2026-09-14','2026-09-21',now)
    assert plan['state']=='prepared',plan['blockers']
    with pytest.raises(ValueError,match='environment'):activate(db,plan['id'],True,True,True,now+1)
    enable(monkeypatch)
    with pytest.raises(ValueError,match='channel'):activate(db,plan['id'],True,True,False,now+1)
    result=activate(db,plan['id'],True,True,True,now+1)
    assert result['plan']['state']=='armed'
    boundary=sessions('2026-09-21')[0]['open']
    with db.tx() as c:
        assert owner(db,c,'smoothers',now+1) is None
        assert owner(db,c,'smoothers',boundary)['effective_from']==boundary
        assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow'}
        assert db.get(c,'native:ownership:morning') is None
        config=db.get(c,CONFIG)
    with pytest.raises(RevisionConflict,match='armed'):revise(db,config['revision'],config['configs'],'Change reviewed config',now+2)
    with pytest.raises(RevisionConflict):activate(db,plan['id'],True,True,True,now+2)


def test_holiday_handoff_starts_on_first_actual_session(db,monkeypatch):
    now,_=completed_week(db,'2026-08-31')
    plan=prepare(db,'2026-08-31','2026-09-07',now)
    assert plan['state']=='prepared',plan['blockers']
    assert plan['effective_session']=='2026-09-08'
    enable(monkeypatch)
    assert activate(db,plan['id'],True,True,True,now+1)['ownership']['effective_from']==sessions('2026-09-07')[0]['open']


@pytest.mark.parametrize('mutation',['missed','partial','calendar','open','coverage','source_missing','contract','rank','config','preview','event_time','stale','source_only','touch','terminal_quote','premium'])
def test_incomplete_or_changed_evidence_blocks_activation(db,monkeypatch,mutation):
    now,signals=completed_week(db);plan=prepare(db,'2026-09-14','2026-09-21',now)
    assert plan['state']=='prepared',plan['blockers']
    with db.tx() as c:
        job=db.get(c,VERSION+':week:2026-09-14');p=signals[0]
        if mutation in ('missed','partial'):job['state']=mutation
        elif mutation=='calendar':job['sessions']=job['sessions'][:-1]
        elif mutation=='open':p['status']='OPEN';save(db,c,p)
        elif mutation=='coverage':p['coverage_missing']=True;save(db,c,p)
        elif mutation=='source_missing':c.execute(records.delete())
        elif mutation=='contract':p['contract']['symbol']='DIFFERENT';save(db,c,p)
        elif mutation=='rank':p['featured_rank']=2;save(db,c,p)
        elif mutation=='config':config=db.get(c,CONFIG);config['revision']='changed';db.put(c,CONFIG,config)
        elif mutation=='preview':c.execute(outbox.update().values(payload={'content':'Placeholder'}))
        elif mutation=='event_time':
            for row in c.execute(select(outbox)).mappings().all():c.execute(outbox.update().where(outbox.c.id==row['id']).values(delivery={**row['delivery'],'event_time':None}))
        elif mutation=='stale':db.put(c,'health:project_smoothers',dict(status='connected',checked_at=now-121))
        elif mutation=='source_only':ingest(db,c,'smoothers',dict(id='extra',symbol='ZZZ',source_ts=p['model_entry_time'],status='win',original={}),now)
        elif mutation=='touch':p['touch_bar']['high']=101;save(db,c,p)
        elif mutation=='terminal_quote':p.pop('exit_quote');save(db,c,p)
        elif mutation=='premium':p=signals[1];p.pop('premium_last_attempt_quote');save(db,c,p)
        db.put(c,VERSION+':week:2026-09-14',job)
    enable(monkeypatch)
    with pytest.raises(RevisionConflict,match='[Ee]vidence'):activate(db,plan['id'],True,True,True,now+1)
    with db.tx() as c:assert db.get(c,OWNER) is None


def test_identical_source_refresh_is_stable_but_quote_changes_require_new_review(db,monkeypatch):
    now,signals=completed_week(db);plan=prepare(db,'2026-09-14','2026-09-21',now)
    with db.tx() as c:
        c.execute(records.update().values(updated=now+1))
        p=signals[0];p['quote']['bid']=.94;save(db,c,p)
    enable(monkeypatch)
    # The roster renders the publication-batch bid/ask, so a quote change both
    # alters the evidence and invalidates the queued preview: activation stays
    # blocked until the data matches the reviewed preview again.
    with pytest.raises(RevisionConflict,match='[Ee]vidence'):activate(db,plan['id'],True,True,True,now+1)
    # Restoring the reviewed quote and re-preparing recovers a clean review.
    with db.tx() as c:
        p=signals[0];p['quote']['bid']=.95;save(db,c,p)
        c.execute(records.update().values(updated=now+2))
    plan=prepare(db,'2026-09-14','2026-09-21',now+1)
    assert plan['state']=='prepared',plan['blockers']
    with db.tx() as c:c.execute(records.update().values(updated=now+2))
    assert activate(db,plan['id'],True,True,True,now+2)['plan']['state']=='armed'


def test_no_midweek_or_skipped_week_or_empty_acceptance(db):
    now,_=completed_week(db)
    with pytest.raises(ValueError):prepare(db,'2026-09-14','2026-09-28',now)
    with pytest.raises(ValueError):prepare(db,'2026-09-14','2026-09-21',now-86400)
    with pytest.raises(ValueError):prepare(db,'2026-09-14','2026-09-22',now)
    with db.tx() as c:c.execute(weekly.delete())
    assert prepare(db,'2026-09-14','2026-09-21',now)['state']=='blocked'


def test_new_week_events_send_once_and_old_cohort_recovery_stays_shadow(db):
    boundary=sessions('2026-09-21')[0]['open'];url='https://discord.com/api/webhooks/123/test';requests=[]
    with db.tx() as c:
        queue(db,c,'smoothers','historical',{'content':'old'},boundary-60,event_time=boundary-60,cohort_time=boundary-60)
        db.put(c,OWNER,dict(owner='compass',previous_sender_paused=True,accepted_at=boundary-1,epoch='new',effective_from=boundary))
        queue(db,c,'smoothers','old-week-recovery',{'content':'old close'},boundary+1,event_time=boundary+1,cohort_time=boundary-86400)
        queue(db,c,'smoothers','missing-time',{'content':'missing'},boundary+1,cohort_time=boundary)
        for _ in range(2):queue(db,c,'smoothers','fresh',{'content':'new'},boundary+1,event_time=boundary+1,cohort_time=boundary)
        queue(db,c,'smoothers','historical',{'content':'do not promote'},boundary+1,event_time=boundary+1,cohort_time=boundary)
    def handler(r):requests.append(r);return httpx.Response(200,json={'id':'123456'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,{'smoothers':url},True,boundary+2)
        assert not deliver_one(db,client,{'smoothers':url},True,boundary+3)
    assert len(requests)==1 and requests[0].method=='POST'
    with db.tx() as c:assert dict(c.execute(select(outbox.c.event_key,outbox.c.status)).all())=={'historical':'shadow','old-week-recovery':'shadow','missing-time':'shadow','fresh':'delivered'}


def test_rollback_cancels_pending_and_marks_in_flight_uncertain_without_touching_morning(db):
    now=1000;url='https://discord.com/api/webhooks/123/test'
    with db.tx() as c:
        ownership=dict(owner='compass',previous_sender_paused=True,accepted_at=now,epoch='x',effective_from=now)
        db.put(c,OWNER,ownership);db.put(c,'native:ownership:morning',ownership)
        for i in range(2):queue(db,c,'smoothers',str(i),{'content':'test'},now+i,event_time=now+i,cohort_time=now)
        queue(db,c,'morning','untouched',{'content':'morning'},now+2,event_time=now+2)
    def handler(r):rollback(db,now+4);return httpx.Response(200,json={'id':'12345'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:assert deliver_one(db,client,{'smoothers':url},True,now+3)
    with db.tx() as c:
        assert dict(c.execute(select(outbox.c.event_key,outbox.c.status)).all())=={'0':'ambiguous','1':'cancelled','untouched':'pending'}
        assert db.get(c,OWNER)['owner']=='original'
        assert db.get(c,'native:ownership:morning')['owner']=='compass'


def test_2xx_without_discord_receipt_is_not_retried_or_claimed_delivered(db):
    with db.tx() as c:
        db.put(c,OWNER,dict(owner='compass',previous_sender_paused=True,accepted_at=1,epoch='a',effective_from=1))
        queue(db,c,'smoothers','test',{'content':'test'},2,event_time=2,cohort_time=2)
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(204))) as client:
        assert deliver_one(db,client,{'smoothers':'https://discord.com/api/webhooks/123/test'},True,3)
        assert not deliver_one(db,client,{'smoothers':'https://discord.com/api/webhooks/123/test'},True,4)
    with db.tx() as c:assert c.execute(select(outbox.c.status)).scalar_one()=='ambiguous'


def test_monitor_captures_completed_touch_bar_and_fresh_quote_then_deduplicates(db):
    now,signals=completed_week(db);p=signals[0];entry=p['model_entry_time'];p.update(status='OPEN',last_seen_at=None,last_underlying=None)
    for key in ('touch_bar','touch_detected_at','exit_quote'):p.pop(key,None)
    class Provider:
        calls=0
        def bar_batch(self,*args):return {'AAA':pd.DataFrame(dict(open=[100],high=[102.1],low=[99.9],close=[101.8]),index=pd.to_datetime([entry],unit='s',utc=True))}
        def quote(self,contract):self.calls+=1;return dict(status='available',bid=1.4,ask=1.6,midpoint=1.5,quote_at_ms=(entry+75)*1000)
    provider=Provider()
    with db.tx() as c:save(db,c,p);c.execute(outbox.delete())
    monitor(db,provider,entry+75);monitor(db,provider,entry+76)
    assert provider.calls==1
    with db.tx() as c:
        saved=c.execute(select(weekly.c.payload).where(weekly.c.id=='AAA')).scalar_one()
        intent=c.execute(select(outbox)).mappings().one()
    assert saved['status']=='WIN' and saved['exit_quote']['midpoint']==1.5
    assert intent['delivery']['event_time']==entry+60 and intent['delivery']['cohort_time']==entry
    fields={x['name']:x['value'] for x in intent['payload']['embeds'][0]['fields']}
    assert fields['1m Close']=='$101.80' and fields['Target Level']=='$102.00'
    assert fields['Option Return vs Entry']=='+50.0%'
    assert 'Observed At' in fields and 'Alert Sent' not in fields


def test_roster_contains_all_76_tickers_with_discord_limits_and_reference_labels(db):
    now,signals=completed_week(db)
    rows=[{**deepcopy(signals[i%2]),'ticker':f'TEST{i:02d}','id':str(i),'quality_tier':messages.TIERS[i%4]} for i in range(76)]
    chunks=messages.roster(rows,'2026-09-14',now)
    assert len(chunks)>1 and all(len(p['content'])<=1900 and p['allowed_mentions']=={'parse':[]} for p in chunks)
    joined='\n'.join(p['content'] for p in chunks)
    assert all(joined.count('**'+r['ticker']+' ')==1 for r in rows)
    payload=messages.entry(signals[0]);fields={x['name'] for x in payload['embeds'][0]['fields']}
    assert {'Contract','Expiration','DTE at Entry','Quality Tier','Live WR','WR (last 4)','Backtest WR','Quote Time'}<=fields
    missing={**signals[0],'exit_quote':{'status':'unavailable'}}
    assert 'Option Quote' in {x['name'] for x in messages.target(missing)['embeds'][0]['fields']}


def test_smoothers_handoff_api_is_private_and_strict(tmp_path):
    from fastapi.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    app=create_app(Config(local=True,role='web',password='test-private-password',db='sqlite:///'+str(tmp_path/'private.db')))
    with TestClient(app) as client:
        path='/api/native/smoothers/handoff'
        assert client.get(path).status_code==401
        assert client.post('/login',json={'password':'test-private-password'}).status_code==200
        assert client.get(path).json()['ownership']=={'owner':'original'}
        assert client.post(path,headers={'Origin':'https://wrong.test'},json={'action':'rollback'}).status_code==403
        assert client.post(path,json={'action':'activate','previous_sender_paused':'true'}).status_code==422


def test_running_worker_publishes_shadow_health_without_enabling_sender(db,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from compass import native_smoothers
    class Stopped(Exception):pass
    async def stop(_):raise Stopped()
    monkeypatch.setattr(native_smoothers,'Data',lambda *args:SimpleNamespace())
    monkeypatch.setattr(native_smoothers,'schedule',lambda *args:None)
    monkeypatch.setattr(asyncio,'sleep',stop)
    with db.tx() as c:db.put(c,CONFIG,dict(owner='compass',configs=[]))
    cfg=SimpleNamespace(alpaca_key='test',alpaca_secret='test')
    with pytest.raises(Stopped):asyncio.run(native_smoothers.run(db,cfg))
    with db.tx() as c:
        assert db.get(c,VERSION+':worker:schedule')['status']=='shadow'
        assert db.get(c,'health:'+VERSION+'_schedule')['status']=='shadow'
        assert db.get(c,OWNER) is None
