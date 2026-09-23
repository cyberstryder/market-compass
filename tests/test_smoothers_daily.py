from datetime import datetime,date,timedelta
from types import SimpleNamespace
import pandas as pd
from sqlalchemy import select,func
from test_projects import db
from compass.market import session,NY
from compass.store import smoothers_daily,events,discord_jobs
from compass.native_smoothers import make_signal,weekly
from compass.native_config import KEY
from compass.smoothers_daily import activate,schedule,add,observe,report,VERSION
from tests.test_native_programs import hourly_fixture

DAY='2026-09-15'
OPEN,CLOSE=session(DAY)
NOW=OPEN+3900
CONFIG=dict(ticker='DEMO',s1=2,s2=2,s3=2,pm=.01,enabled=True)


def activated(db):
    with db.tx() as c:
        db.put(c,KEY,dict(configs=[CONFIG],revision='frozen'))
        return activate(db,c,OPEN-60)


def quote(db,c,at,symbol='DEMO',price=100,received=None):
    q=dict(ts=at,bid=price-.01,ask=price+.01,bid_size=1,ask_size=1,received=at)
    db.put(c,'quote:'+symbol,q)
    db.append(c,'quote','fixture',symbol,at,q)
    c.execute(events.update().where(events.c.kind=='quote',events.c.ts==at).values(received=received or at))
    return q


def daily_basis(db,c):
    t=session('2026-09-14')[0]
    db.append(c,'daily','fixture','DEMO',t,dict(c=100,o=100,h=101,l=99,v=10))
    c.execute(events.update().where(events.c.kind=='daily').values(received=OPEN-100))


def bars():
    f=hourly_fixture()
    last=f.iloc[-1:].copy();last.index=pd.to_datetime([OPEN],unit='s',utc=True)
    return pd.concat([f,last])


def test_daily_uses_tuesday_opening_hour_without_changing_weekly_formula():
    b=bars();calendar=dict(date=DAY,open=OPEN)
    with __import__('pytest').raises(ValueError,match='wrong_entry_session'):
        make_signal(CONFIG,b,pd.DataFrame(),calendar,NOW,DAY)
    s=make_signal(CONFIG,b,pd.DataFrame(),calendar,NOW,DAY,daily_entry=True)
    assert s['model_entry_time']==OPEN+3600 and s['entry_price']==b.iloc[-1]['close']
    b.loc[b.index[-1],'parts']=1
    with __import__('pytest').raises(ValueError,match='incomplete_opening_hour'):
        make_signal(CONFIG,b,pd.DataFrame(),calendar,NOW,DAY,daily_entry=True)


def test_activation_freezes_parameters_and_skips_partial_day(db):
    with db.tx() as c:
        db.put(c,KEY,dict(configs=[CONFIG],revision='old'))
        a=activate(db,c,NOW)
        assert len(a['sessions'])==20 and a['sessions'][0]['day']=='2026-09-16'
        db.put(c,KEY,dict(configs=[dict(CONFIG,pm=.02)],revision='new'))
        assert activate(db,c,NOW+10)==a and a['configs'][0]['pm']==.01
        assert not a['alerts'] and not a['orders']


def test_schedule_is_quiet_idempotent_and_actual_detection_anchored(db):
    a=activated(db)
    data=SimpleNamespace(hourly=lambda *a:bars(),daily=lambda *a:pd.DataFrame())
    with db.tx() as c:quote(db,c,NOW);daily_basis(db,c)
    schedule(db,data,NOW,clock=lambda:NOW)
    schedule(db,data,NOW+10,clock=lambda:NOW+10)
    with db.tx() as c:
        p=c.execute(select(smoothers_daily.c.payload)).scalar_one()
        assert p['detected_at']==NOW and p['entry']['price']==100
        assert p['evidence']['processing_delay_seconds']==300
        assert c.execute(select(func.count()).select_from(weekly)).scalar_one()==0
        assert c.execute(select(func.count()).select_from(discord_jobs)).scalar_one()==0
        assert not db.recent(c,'alert')
        assert db.get(c,VERSION+':day:'+DAY)['state']=='complete'


def test_dedup_overlap_direction_timing_and_common_outcomes(db):
    a=activated(db)
    with db.tx() as c:
        daily_basis(db,c);quote(db,c,NOW);quote(db,c,NOW+30,price=100)
        assert add(db,c,a,'smoothers_daily','DEMO','long',NOW,NOW,{})
        assert not add(db,c,a,'smoothers_daily','DEMO','long',NOW+30,NOW+30,{})
        assert add(db,c,a,'options_ideas','DEMO','long',NOW+30,NOW+30,{})
        assert add(db,c,a,'intraday_stock','DEMO','short',NOW+30,NOW+30,{})
        quote(db,c,CLOSE-.001,price=102)
        observe(db,c,CLOSE+15)
        r=report(db,c,CLOSE+15)
        assert r['overlap']==[dict(family='options_ideas',matched=1,smoothers_earlier=1,scanner_earlier=0,same_time=0,mean_lead_seconds=30)]
        smooth=next(x for x in r['groups'] if x['family']=='smoothers_daily' and x['horizon']=='close')
        assert smooth['group']=='overlap' and smooth['measured']==1 and abs(smooth['mean_directional_pct']-2)<1e-8
        sixty=next(x for x in r['groups'] if x['family']=='smoothers_daily' and x['horizon']=='60m')
        assert sixty['unavailable']==1


def test_missing_quote_and_incomplete_census_are_not_negative_signals(db):
    a=activated(db)
    with db.tx() as c:
        add(db,c,a,'options_ideas','DEMO','long',NOW,NOW,{})
        r=report(db,c,NOW)
        assert all(x['group']=='smoothers_census_incomplete' and x['unavailable']==1 for x in r['groups'])
        assert not r['overlap']


def test_missed_daily_window_is_recorded_without_backfilled_candidates(db):
    activated(db)
    data=SimpleNamespace(hourly=lambda *a:(_ for _ in ()).throw(AssertionError('must not fetch')))
    schedule(db,data,CLOSE,clock=lambda:CLOSE)
    with db.tx() as c:
        assert db.get(c,VERSION+':day:'+DAY)['state']=='missed'
        assert not c.execute(select(smoothers_daily)).all()


def test_later_confirmation_is_not_a_predictive_filter_at_earlier_entry(db):
    a=activated(db)
    with db.tx() as c:
        quote(db,c,NOW);quote(db,c,NOW+60)
        add(db,c,a,'smoothers_daily','DEMO','long',NOW,NOW,{})
        add(db,c,a,'options_ideas','DEMO','long',NOW+60,NOW+60,{})
        r=report(db,c,NOW+60)
        assert all(x['confirmation']=='later_only' for x in r['groups'] if x['family']=='smoothers_daily')
        assert all(x['confirmation']=='available_at_detection' for x in r['groups'] if x['family']=='options_ideas')


def test_capture_records_stock_zero_dte_and_option_ideas_separately(db):
    from compass.smoothers_daily import capture
    from compass.setup_study import trials as setups
    from compass.option_ideas import ideas
    a=activated(db)
    with db.tx() as c:
        quote(db,c,NOW);daily_basis(db,c)
        for asset,symbol,extra in [('stock','DEMO',{}),('option','O:DEMO260915C00100000',{'underlying':'DEMO','underlying_side':'long'})]:
            p=dict(id=asset,symbol=symbol,asset=asset,side='long',strategy='test',status='open',**extra)
            c.execute(setups.insert().values(id=asset,source_id=asset,symbol=symbol,strategy='test',side='long',version='test',status='open',started=NOW,payload=p))
        c.execute(ideas.insert().values(id='idea',underlying='DEMO',status='pending',created=NOW,updated=NOW,
            payload=dict(underlying='DEMO',underlying_side='long',status='pending',strategy='test')))
        capture(db,c,a,NOW+10);capture(db,c,a,NOW+20)
        assert set(c.execute(select(smoothers_daily.c.family)).scalars())=={'intraday_stock','scanner_0dte','options_ideas'}
        assert c.execute(select(func.count()).select_from(smoothers_daily)).scalar_one()==3


def test_changed_price_basis_excludes_endpoint_instead_of_creating_split_return(db):
    a=activated(db)
    with db.tx() as c:
        daily_basis(db,c);quote(db,c,NOW)
        add(db,c,a,'smoothers_daily','DEMO','long',NOW,NOW,{})
        c.execute(events.update().where(events.c.kind=='daily').values(payload=dict(c=50),received=NOW+20))
        quote(db,c,CLOSE-.001,price=51)
        observe(db,c,CLOSE+15)
        p=c.execute(select(smoothers_daily.c.payload)).scalar_one()
        assert p['checkpoints']['close']['reason']=='historical_price_basis_changed'
        assert 'directional_return_pct' not in p['checkpoints']['close']


def test_report_waits_for_first_session_without_restarting_study(db):
    a=activated(db)
    with db.tx() as c:
        r=report(db,c,OPEN-1)
        assert r['state']=='awaiting_first_session' and r['records']==0
        assert r['next_formula_at']==OPEN+3900
        assert report(db,c,OPEN)['state']=='collecting'
        assert report(db,c,OPEN)['activation']==a
