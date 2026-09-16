from copy import deepcopy
from datetime import datetime,date,timezone,timedelta
import pandas as pd
import pytest
from sqlalchemy import select,func
from test_projects import db
from native_morning_fixtures import signal_event,checkpoint_event
from compass.native_morning_worker import initialize,accept,enrich
from compass.native_morning import store as ms
from compass.native_outbox import outbox
from compass.native_smoothers import make_signal,schedule,monitor,premium,weekly,save,VERSION
from compass.smoothers_math import compute_smoother

NOW=datetime(2026,9,15,14,30,tzinfo=timezone.utc).timestamp()


def test_native_morning_idempotent_conflicts_and_no_sender(db):
    initialize(db)
    event=signal_event(stamp=int(NOW*1000))
    assert accept(db,event,NOW)['status']=='accepted'
    assert accept(db,event,NOW+1)['status']=='duplicate'
    bad=deepcopy(event);bad['signal']['price']=101
    with pytest.raises(ms.PayloadConflict):accept(db,bad,NOW+2)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(ms.signals)).scalar_one()==1
        assert c.execute(select(outbox.c.status)).scalar_one()=='shadow'
        assert c.execute(select(ms.outbox.c.status)).scalar_one()=='shadow_previewed'


def test_native_options_do_not_depend_on_discord_identity(db):
    initialize(db);event=signal_event(stamp=int(NOW*1000));accept(db,event,NOW)
    class Provider:
        def clock(self):return int((NOW+1)*1000)
        def lookup(self,signal,policy):
            return dict(status='available',quote_at_ms=int((NOW+1)*1000),bid=1,ask=1.1,midpoint=1.05,
                ask_contract_cost=110,spread_pct=9,contract=dict(symbol='DEMO260918C00100000',underlying='DEMO',strike=100,expiration='2026-09-18',multiplier=100))
    assert enrich(db,Provider(),NOW)
    assert not enrich(db,Provider(),NOW+1)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(ms.option_samples)).scalar_one()==60
        assert c.execute(select(ms.option_jobs.c.message_id)).scalar_one() is None
        assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow'}
        assert c.execute(select(func.count()).select_from(outbox)).scalar_one()==2


def test_checkpoint_recovery_cannot_enqueue_initial_option_job(db):
    initialize(db);event=signal_event(stamp=int(NOW*1000))
    accept(db,checkpoint_event(event,5),NOW+300)
    class Provider:
        def lookup(self,*args):raise AssertionError('Unexpected option lookup')
    assert not enrich(db,Provider(),NOW+300)


def hourly_fixture():
    dates=pd.bdate_range('2026-07-01','2026-09-14',tz='America/New_York')
    idx=pd.DatetimeIndex([d+pd.Timedelta(hours=9,minutes=30) for d in dates]).tz_convert('UTC')
    values=[100+i*.2+(i%3)*.1 for i in range(len(idx))]
    return pd.DataFrame(dict(open=values,high=[v+.5 for v in values],low=[v-.5 for v in values],close=values,volume=100,parts=2),index=idx)


def test_smoothers_weekly_restart_freezes_config_and_no_replay(db):
    first={'date':'2026-09-14','open':datetime(2026,9,14,13,30,tzinfo=timezone.utc).timestamp(),'close':datetime(2026,9,14,20,tzinfo=timezone.utc).timestamp()}
    now=first['open']+3900
    config=dict(ticker='DEMO',s1=2,s2=2,s3=2,pm=.01,enabled=True,backtest_wr=.7)
    class Provider:
        calls=0
        def calendar(self,monday):return [first,dict(date='2026-09-18',open=first['open']+4*86400,close=first['close']+4*86400)]
        def hourly(self,symbol,asof):self.calls+=1;return hourly_fixture()
        def daily(self,symbol,asof):return pd.DataFrame()
        def contract(self,*args):return None
    provider=Provider()
    with db.tx() as c:db.put(c,VERSION+':config',dict(configs=[config],alltime=[],revision='v1',received_at=now))
    schedule(db,provider,now)
    with db.tx() as c:db.put(c,VERSION+':config',dict(configs=[],revision='v2',received_at=now+1))
    schedule(db,provider,now+1);schedule(db,provider,now+2)
    assert provider.calls==1
    with db.tx() as c:
        job=db.get(c,VERSION+':week:2026-09-14')
        assert job['config']['revision']=='v1' and job['state']=='complete'
        assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow'}
    with db.tx() as c:db.put(c,VERSION+':week:2026-09-14',None)
    schedule(db,provider,now+86400)
    assert provider.calls==1


def test_missing_opening_half_hour_blocks_selection():
    bars=hourly_fixture();bars.loc[bars.index[-1],'parts']=1
    with pytest.raises(ValueError,match='incomplete_opening_hour'):
        make_signal(dict(ticker='DEMO',s1=2,s2=2,s3=2,pm=.01),bars,pd.DataFrame(),dict(date='2026-09-14',open=bars.index[-1].timestamp()),NOW,'2026-09-14')


def test_weekly_closure_marks_incomplete_observations_unresolved(db):
    now=NOW+3*86400+6*3600+301
    last={'date':'2026-09-18','open':now-24000,'close':now-301}
    with db.tx() as c:
        db.put(c,VERSION+':week:2026-09-14',{'sessions':[last]})
        save(db,c,dict(id='one',week='2026-09-14',ticker='DEMO',status='OPEN',model_entry_time=NOW,coverage_missing=True,last_seen_at=None))
    monitor(db,object(),now)
    with db.tx() as c:
        assert c.execute(select(weekly.c.status)).scalar_one()=='UNRESOLVED'
        assert c.execute(select(outbox.c.status)).scalar_one()=='shadow'


def test_sender_cannot_send_shadow_or_ambiguous_retry(db):
    import httpx
    from compass.native_outbox import queue,deliver_one
    calls=[]
    def handle(request):calls.append(request);raise httpx.ReadTimeout('test')
    url='https://discord.com/api/webhooks/123/test'
    with db.tx() as c:
        queue(db,c,'morning','old',{'content':'old'},NOW)
        db.put(c,'native:ownership:morning',dict(owner='compass',previous_sender_paused=True,accepted_at=NOW,epoch='a'))
        queue(db,c,'morning','new',{'content':'new'},NOW+1)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert not deliver_one(db,client,{'morning':url},False,NOW+2)
        assert deliver_one(db,client,{'morning':url},True,NOW+2)
        assert not deliver_one(db,client,{'morning':url},True,NOW+60)
    assert len(calls)==1
    with db.tx() as c:assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow','ambiguous'}


def test_sender_option_edit_requires_parent_message_identity(db):
    import httpx
    from compass.native_outbox import queue,deliver_one
    requests=[]
    def handler(r):requests.append(r);return httpx.Response(200,json={'id':'12345'})
    with db.tx() as c:
        db.put(c,'native:ownership:morning',dict(owner='compass',previous_sender_paused=True,accepted_at=NOW,epoch='a'))
        queue(db,c,'morning','signal',{'content':'entry'},NOW)
        queue(db,c,'morning','option',{'content':'entry + option'},NOW+1,parent_event='signal')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,{'morning':'https://discord.com/api/webhooks/123/test'},True,NOW+2)
        assert deliver_one(db,client,{'morning':'https://discord.com/api/webhooks/123/test'},True,NOW+3)
    assert [r.method for r in requests]==['POST','PATCH']
    assert requests[-1].url.path.endswith('/messages/12345')


def test_minute_monitor_covers_whole_cohort_and_ignores_forming_bar(db):
    start=NOW;now=start+135
    session=dict(date='2026-09-15',open=start-3600,close=start+3600)
    class Data:
        calls=[]
        def bar_batch(self,symbols,frame,begin,end):
            self.calls.append(symbols)
            return {s:pd.DataFrame(dict(open=[100,100,100],high=[101,101,105],low=[99,99,99],close=[100,100,100]),index=pd.to_datetime([start,start+60,start+120],unit='s',utc=True)) for s in symbols}
    data=Data()
    with db.tx() as c:
        db.put(c,VERSION+':week:2026-09-14',dict(sessions=[session]))
        for i in range(45):save(db,c,dict(id=str(i),week='2026-09-14',ticker='T'+str(i),status='OPEN',direction='CALL',target_price=104,model_entry_time=start,last_seen_at=None,premium_model={'kept':True}))
    monitor(db,data,now)
    assert [len(x) for x in data.calls]==[20,20,5]
    with db.tx() as c:
        rows=c.execute(select(weekly.c.payload)).scalars().all()
        assert len(rows)==45
        assert all(p['status']=='OPEN' and p['last_seen_at']==start+60 and not p['coverage_missing'] and p['premium_model']=={'kept':True} for p in rows)


def test_provider_batch_paginates_every_symbol_and_rejects_conflicts():
    import httpx
    from types import SimpleNamespace
    from compass.native_smoothers_data import Data
    calls=[]
    bar=dict(t='2026-09-15T14:30:00Z',o=100,h=101,l=99,c=100,v=10)
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'bars':{'A':[bar]},'next_page_token':'next'} if len(calls)==1 else {'bars':{'B':[bar]}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        data=Data(SimpleNamespace(alpaca_key='test',alpaca_secret='test'),client)
        got=data.bar_batch(['A','B'],'1Min',NOW,NOW+60)
        assert len(got['A'])==len(got['B'])==1 and len(calls)==2
    with pytest.raises(ValueError,match='Conflicting duplicate'):
        Data.normalize([bar,dict(bar,c=100.5)])


def test_missed_previous_week_closes_after_restart(db):
    end=NOW-4*86400
    with db.tx() as c:
        db.put(c,VERSION+':week:2026-09-07',dict(sessions=[dict(date='2026-09-11',open=end-23400,close=end)]))
        save(db,c,dict(id='old',week='2026-09-07',ticker='OLD',status='OPEN',model_entry_time=end-23400,last_seen_at=end-600))
    monitor(db,object(),NOW)
    with db.tx() as c:assert c.execute(select(weekly.c.status)).scalar_one()=='UNRESOLVED'


def test_native_route_uses_scoped_auth_without_dashboard_cookie(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'native.db'),password='owner-private-test-secret',morning_token='native-test-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
    event=signal_event(stamp=int(time.time()*1000))
    with TestClient(create_app(cfg)) as client:
        assert client.post('/hooks/native/morning',json=event).status_code==401
        assert client.post('/hooks/native/morning/native-test-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',json=event).status_code==200
        assert client.post('/hooks/native/morning',headers={'Authorization':'Bearer native-test-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'},json=event).json()['status']=='duplicate'
        assert client.post('/hooks/native/morning/native-test-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',content=b'x'*32769).status_code==413
        assert client.post('/hooks/native/morning/native-test-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',json={}).status_code==422


def test_dedicated_native_credential_isolated_from_source_and_dashboard(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    source_token='source-feed-test-token-'+'s'*32
    intake_token='native-intake-test-token-'+'n'*32
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'dedicated.db'),
        password='owner-private-test-secret',morning_token=source_token,
        native_morning_intake_token=intake_token)
    event=signal_event(stamp=int(time.time()*1000))
    with TestClient(create_app(cfg)) as client:
        # A dedicated intake credential supersedes, rather than adds to, the
        # legacy source credential. Neither grants owner management access.
        assert client.post('/hooks/native/morning/'+source_token,json=event).status_code==401
        assert client.post('/hooks/native/morning',headers={'Authorization':'Bearer '+source_token},json=event).status_code==401
        assert client.post('/hooks/native/morning/'+intake_token,json=event).status_code==200
        assert client.post('/hooks/native/morning',headers={'Authorization':'Bearer '+intake_token},json=event).json()['status']=='duplicate'
        assert client.get('/api/native/morning/routing',headers={'Authorization':'Bearer '+intake_token}).status_code==401
        assert client.post('/api/native/morning/routing',headers={'Authorization':'Bearer '+intake_token},json={'action':'cancel_future','reason':'test'}).status_code==401
    assert cfg.morning_token==source_token
    assert intake_token not in repr(cfg)


def test_dedicated_native_credential_requires_entropy_length(monkeypatch):
    from compass.config import Config
    monkeypatch.setenv('NATIVE_MORNING_INTAKE_TOKEN','too-short')
    with pytest.raises(ValueError,match='Integration tokens'):
        Config(local=True).validate()


def test_premium_eligible_rows_not_starved_by_rows_without_options(db):
    class Options:
        def snapshots(self,contracts):
            assert len(contracts)==1
            return {'valid':dict(status='available',midpoint=1.1)}
    class Data:options=Options()
    with db.tx() as c:
        db.put(c,VERSION+':week:2026-09-14',dict(sessions=[dict(date='2026-09-15',open=NOW-3600,close=NOW+3600),dict(date='2026-09-18',open=NOW+3*86400-3600,close=NOW+3*86400+3600)]))
        for i in range(20):save(db,c,dict(id=str(i),week='2026-09-14',ticker='T'+str(i),status='OPEN'))
        save(db,c,dict(id='valid',week='2026-09-14',ticker='VALID',status='OPEN',contract=dict(symbol='valid',expiration='2026-09-18',strike=100),entry_premium=1,last_underlying=100,target_price=102,direction='CALL'))
    premium(db,Data(),NOW)
    with db.tx() as c:assert c.execute(select(weekly.c.payload).where(weekly.c.id=='valid')).scalar_one()['premium_checked_at']==NOW


def test_native_preview_resumes_after_intake_commit(db):
    from compass.native_morning_worker import flush_previews
    from compass.native_morning.models import Event
    from compass.native_morning.discord import signal_message
    initialize(db)
    event=Event.model_validate(signal_event(stamp=int(NOW*1000)))
    ms.accept_event(db.engine,event,signal_message(event.signal,int(NOW*1000)),False,int(NOW*1000))
    flush_previews(db,NOW+1);flush_previews(db,NOW+2)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(outbox)).scalar_one()==1
        assert c.execute(select(outbox.c.status)).scalar_one()=='shadow'
