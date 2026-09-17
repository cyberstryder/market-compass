import asyncio
from copy import deepcopy
from datetime import datetime
from html.parser import HTMLParser
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func,update
from fastapi.testclient import TestClient
from compass.app import create_app
from compass.config import Config
from compass.market import day,session,NY
from compass.store import Store,state,events,discord_jobs,identity,spy_checks as checks,spy_options as observations,spy_marks as marks
from compass.spy_brief import BriefWorker
from compass.spy_confirmation import report_key
from compass.spy_study import VERSION,KEY,register,inventory,audit_schedule,contract_for,Paper,Study,stream_requests
from compass.spy_study_report import report,record_page,mark_page,COHORTS
from compass.providers import Collectors
from compass.option_recovery import targets as recovery_targets
from compass.universe import data_symbols
from test_smoothers_postgres_handoff import pg
from test_spy_brief import seed as brief_seed


def stamp(text):return datetime.fromisoformat(text).replace(tzinfo=NY).timestamp()
OPEN=stamp('2026-09-17T09:30:00');NOW=OPEN+907
CALL='O:SPY260917C00100000';PUT='O:SPY260917P00100000'


@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'spy.db'));db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture(params=['sqlite','postgres'])
def storage(request):return request.getfixturevalue('db' if request.param=='sqlite' else 'pg')


@pytest.fixture
def clock(monkeypatch):
    value=[NOW];monkeypatch.setattr('compass.store.time.time',lambda:value[0]);return value


def cfg():return Config(local=True,stocks=('SPY',),futures=(),extra_futures=(),massive='fixture')


def q(t,bid=100,ask=100.02):
    return dict(ts=t,bid=bid,ask=ask,bid_size=5,ask_size=5,source='fixture',collection_version='option-reliability-v4',received=t)


def plan(side='long',phase='opening',now=NOW,decision=None):
    m={'opening':15,'followup_30':30,'followup_45':45,'followup_60':60}.get(phase,15)
    d=day(now);opening=session(d)[0];kind='call' if side=='long' else 'put'
    signal='CALL SETUP CONFIRMED' if side=='long' else 'PUT SETUP CONFIRMED'
    return dict(id=report_key(d,phase),day=d,phase=phase,generated_at=now,version='spy-morning-brief-v2',
        decision=decision or signal,decision_state='confirmed' if decision is None else 'waiting',
        confirmation_kind='opening' if m==15 else 'later',check_minute=m,
        expires_at=opening+m*60+125,context=dict(spot=100,session_open=opening,session_close=session(d)[1],
            confirmation_candle={'minute':m,'end':opening+m*60,'close':100,'complete':True}),
        frozen_premarket=dict(day=d,high=99.8,low=99.4,frozen_at=now),
        plans={kind:dict(entry_reference=100,stop=99 if side=='long' else 101,target=102 if side=='long' else 98)},
        options={kind:dict(symbol=CALL if side=='long' else PUT,expiry=d)})


def contract(side='long'):
    return dict(symbol=CALL if side=='long' else PUT,underlying='SPY',expiry='2026-09-17',
        type='call' if side=='long' else 'put',strike=100,multiplier=100,delta=.5 if side=='long' else -.5,oi=500)


def archive(db,c,symbol,quote,received=None):
    db.append(c,'quote','fixture',symbol,quote['ts'],quote)
    c.execute(update(events).where(events.c.symbol==symbol,events.c.ts==quote['ts']).values(received=received if received is not None else quote['ts']))


def seed(db,c,side='long',now=NOW):
    r=plan(side,now=now);db.put(c,r['id'],r);register(db,c,r,now)
    db.append(c,'alert','spy_brief','SPY',now,r,r['id'])
    event_id=c.execute(select(events.c.id).where(events.c.key==r['id'])).scalar_one()
    c.execute(db.insert(discord_jobs).values(event_id=event_id,route='spy_morning',status='sent',queued_at=now,
        confirmation=dict(event_id=event_id,message_id='123456789',at=now,route='spy_morning')))
    o=contract(side)
    db.put(c,'chain:SPY',dict(asof=now,complete=True,source='fixture',contracts=[o]))
    db.put(c,'quote:SPY',q(now));db.put(c,'quote:'+o['symbol'],q(now,2,2.05))
    for delta in (25,20,15,10,5,0):
        archive(db,c,'SPY',q(now-delta));archive(db,c,o['symbol'],q(now-delta,2,2.05))
    return c.execute(select(observations.c.payload)).scalar_one()


def opened(db,c,side='long',now=NOW):
    p=seed(db,c,side,now);Paper(db,cfg()).pending(c,p,now)
    assert p['status']=='open';return p


def test_brief_saves_exact_plan_and_ledger_atomically_without_extra_alerts(db,clock):
    brief_seed(db,NOW)
    r=BriefWorker(db,cfg()).tick(NOW)
    with db.tx() as c:
        frozen=c.execute(select(checks.c.payload)).scalar_one()
        assert frozen['report']==r and frozen['source_key']==r['id']
        assert len(db.recent(c,'alert'))==2
        assert c.execute(select(observations.c.status)).scalar_one()=='pending'
    assert BriefWorker(db,cfg()).tick(NOW+1) is None


def test_report_is_frozen_restart_safe_and_one_observation_per_session(storage,clock):
    with storage.tx() as c:
        p=seed(storage,c);frozen=deepcopy(c.execute(select(checks.c.payload)).scalar_one())
        changed=plan();changed['plans']['call']['target']=500
        assert not register(storage,c,changed,NOW+1)
        assert c.execute(select(checks.c.payload)).scalar_one()==frozen
        later=plan(phase='followup_30',now=OPEN+1807)
        register(storage,c,later,OPEN+1807)
        assert c.execute(select(func.count()).select_from(observations)).scalar_one()==1
        last=c.execute(select(checks.c.payload).where(checks.c.source_key==later['id'])).scalar_one()
        assert last['eligibility_reason']=='session_observation_already_exists' and last['prior_observation_id']==p['id']


def test_opening_wait_can_link_to_a_later_confirmation_without_opening_trade(db,clock):
    with db.tx() as c:
        register(db,c,plan(decision='WAIT — inside range'),NOW)
        assert not c.execute(select(observations)).first()
        later=plan(phase='followup_30',now=OPEN+1807)
        register(db,c,later,OPEN+1807)
        p=c.execute(select(observations.c.payload)).scalar_one()
        assert p['phase']=='followup_30' and p['check_minute']==30 and p['expires_at']==OPEN+1925
        assert c.execute(select(func.count()).select_from(checks)).scalar_one()==2


def test_option_premium_alone_does_not_add_a_new_exit_rule(db,clock):
    with db.tx() as c:
        p=opened(db,c)
        Paper(db,cfg()).observe_pair(c,p,NOW+5,q(NOW+5,5,5.05),q(NOW+5))
        assert p['status']=='open' and p['mark_pct']>100 and p['samples']==2


@pytest.mark.parametrize('mutation,reason',[(lambda r:r.update(generated_at=NOW+1),'report_clock_or_deadline'),
    (lambda r:r.update(expires_at=NOW+1000),'report_clock_or_deadline'),
    (lambda r:r['plans']['call'].update(stop=103),'invalid_frozen_bracket')])
def test_bad_report_clock_or_bracket_never_enters(db,clock,mutation,reason):
    r=plan();mutation(r)
    with db.tx() as c:
        register(db,c,r,NOW)
        p=c.execute(select(observations.c.payload)).scalar_one()
        assert p['status']=='excluded' and p['exit_reason']==reason and p['entry'] is None


@pytest.mark.parametrize('side',['long','short'])
def test_ask_entry_and_costed_bid_exit_preserve_separate_underlying_result(storage,clock,side):
    with storage.tx() as c:
        p=opened(storage,c,side);service=Paper(storage,cfg())
        assert p['entry']==2.06 and p['entry_debit']==pytest.approx(206.65)
        assert p['entry_chain']['asof']==NOW and p['entry_chain']['complete'] and p['entry_contract_metadata']==p['contract']
        assert p['entry_spread']==pytest.approx(.05) and p['contract']['expiry']==day(NOW)
        assert not storage.prefix(c,'position:') and not storage.prefix(c,'risk:') and len(storage.recent(c,'alert'))==1
        at=NOW+5;stock=q(at,102,102.02) if side=='long' else q(at,97.98,98)
        service.observe_pair(c,p,at,q(at,2.5,2.55),stock)
        assert p['status']=='closed' and p['exit_reason']=='underlying_target'
        assert p['pnl']==pytest.approx((2.49-2.06)*100-1.30)
        assert p['return_pct']==pytest.approx(p['pnl']/206.65*100)
        assert p['underlying_directional_pct']>0 and p['return_pct']!=p['underlying_directional_pct']
        assert c.execute(select(func.count()).select_from(marks)).scalar_one()==2
        rows=report(storage,c,at)['groups']
        assert next(r for r in rows if r['dimension']=='confirmation')['completed']==1
        assert p['best_pct']>0 and p['worst_pct']<0


@pytest.mark.parametrize('case',['stale_underlying','future_underlying','future_at_receipt','old_option','wide_option','zero_size','continuity','incomplete_chain','wrong_expiry','already_reversed','already_target'])
def test_entry_exclusions_have_no_fill_or_pnl(db,clock,case):
    with db.tx() as c:
        p=seed(db,c)
        if case=='stale_underlying':db.put(c,'quote:SPY',q(NOW-6))
        if case=='future_underlying':db.put(c,'quote:SPY',q(NOW+1))
        if case=='future_at_receipt':db.put(c,'quote:SPY',{**q(NOW),'received':NOW-1})
        if case=='old_option':db.put(c,'quote:'+CALL,q(NOW-1,2,2.05))
        if case=='wide_option':db.put(c,'quote:'+CALL,q(NOW,2,2.5))
        if case=='zero_size':db.put(c,'quote:'+CALL,{**q(NOW,2,2.05),'ask_size':0})
        if case=='continuity':c.execute(events.delete())
        if case in ('incomplete_chain','wrong_expiry'):
            ch=db.get(c,'chain:SPY')
            if case=='incomplete_chain':ch['complete']=False
            else:ch['contracts'][0]['expiry']='2026-09-18'
            db.put(c,'chain:SPY',ch)
        if case=='already_reversed':db.put(c,'quote:SPY',q(NOW,98.98,99))
        if case=='already_target':db.put(c,'quote:SPY',q(NOW,102,102.02))
        svc=Paper(db,cfg());svc.pending(c,p,NOW)
        assert p['status'] in ('pending','excluded') and p['entry'] is None and p['last_attempt']['reason']
        if p['status']=='pending':svc.pending(c,p,p['expires_at'])
        assert p['status']=='excluded' and p['pnl'] is None


def test_contract_choice_is_listed_same_day_whole_dollar_and_freezes(db,clock):
    with db.tx() as c:
        p=seed(db,c);p['report_contract']=None;p['selection_reference']=100.5
        ch=db.get(c,'chain:SPY');second={**contract(),'symbol':'O:SPY260917C00101000','strike':101}
        fractional={**contract(),'symbol':'O:SPY260917C00100500','strike':100.5}
        ch['contracts']=[second,fractional,contract()]
        o,reason=contract_for(ch,p,NOW);assert not reason and o['strike']==100
        p['contract']=o;p['selection_reference']=101
        assert contract_for(ch,p,NOW)[0]['strike']==100
        ch['contracts']=[second]
        assert contract_for(ch,p,NOW)[0] is None


@pytest.mark.parametrize('issue',['missing','pending','future','old','wrong_route'])
def test_entry_requires_confirmed_delivery_of_the_plan(db,clock,issue):
    with db.tx() as c:
        p=seed(db,c);job=c.execute(select(discord_jobs)).mappings().one()
        confirmation=dict(job['confirmation'])
        if issue=='missing':c.execute(discord_jobs.delete())
        elif issue=='pending':c.execute(update(discord_jobs).values(status='pending'))
        elif issue=='wrong_route':c.execute(update(discord_jobs).values(route='research'))
        else:
            confirmation['at']=NOW+1 if issue=='future' else NOW-1
            c.execute(update(discord_jobs).values(confirmation=confirmation))
        Paper(db,cfg()).pending(c,p,NOW)
        assert p['status']=='pending' and p['entry'] is None and p['waiting_reason']=='confirmed_plan_delivery_required'


def test_entry_quotes_must_follow_delivery_and_receipt_is_frozen(db,clock):
    with db.tx() as c:
        p=seed(db,c);delivery=dict(event_id=1,message_id='987654321',at=NOW+8,route='spy_morning')
        c.execute(update(discord_jobs).values(confirmation=delivery))
        clock[0]=NOW+8
        db.put(c,'quote:SPY',q(NOW+8));db.put(c,'quote:'+CALL,q(NOW+7,2,2.05))
        worker=Paper(db,cfg());worker.pending(c,p,NOW+8)
        assert p['status']=='pending' and p['entry'] is None
        clock[0]=NOW+10
        for symbol,quote in [('SPY',q(NOW+10)),(CALL,q(NOW+10,2,2.05))]:
            db.put(c,'quote:'+symbol,quote);archive(db,c,symbol,quote)
        worker.pending(c,p,NOW+10)
        assert p['status']=='open' and p['entry_delay_after_delivery']==2 and p['delivery']==delivery
        assert record_page(c,NOW+10)['records'][0]['delivery_status']=='sent'


@pytest.mark.parametrize('evidence',['timely','late','hole'])
def test_restart_replay_uses_recorded_path_and_never_repairs_missing_intervals(db,clock,evidence):
    with db.tx() as c:
        p=opened(db,c);service=Paper(db,cfg(),clock=lambda:NOW+50)
        for offset in range(5,41,5):
            if evidence=='hole' and 10<=offset<=25:continue
            receive=NOW+50 if evidence=='late' else NOW+offset
            archive(db,c,CALL,q(NOW+offset,2.5 if offset==40 else 2.1,2.55 if offset==40 else 2.15),receive)
            archive(db,c,'SPY',q(NOW+offset,102 if offset==40 else 100,102.02 if offset==40 else 100.02),receive)
        service.observe(c,p,NOW+50)
        if evidence=='timely':
            assert p['status']=='closed' and p['exit_reason']=='underlying_target' and p['finished_at']==NOW+40
            assert set(c.execute(select(marks.c.processed_at).where(marks.c.at>NOW)).scalars())=={NOW+50}
        else:assert p['status']=='unresolved' and p['pnl'] is None


@pytest.mark.parametrize('side',['long','short'])
def test_adverse_gap_uses_observed_bid_without_stop_price_clamp(db,clock,side):
    with db.tx() as c:
        p=opened(db,c,side)
        uq=q(NOW+5,98,98.02) if side=='long' else q(NOW+5,102,102.02)
        Paper(db,cfg()).observe_pair(c,p,NOW+5,q(NOW+5,.4,.5),uq)
        assert p['exit_reason']=='underlying_stop' and p['exit']==.39 and p['pnl']< -150


@pytest.mark.parametrize('timely',[True,False])
def test_session_exit_uses_a_timely_pair_or_remains_unresolved(db,clock,timely):
    with db.tx() as c:
        p=opened(db,c);p['flatten_at']=NOW+5
        at=NOW+5 if timely else NOW+11
        Paper(db,cfg()).observe_pair(c,p,at,q(at,2.2,2.25),q(at))
        assert p['status']==('closed' if timely else 'unresolved')
        assert p['exit_reason']==('session_flatten' if timely else 'session_exit_quote_missing')


def test_early_close_holiday_and_missed_followups_are_explicit(db,clock):
    early=stamp('2026-11-27T09:45:07');r=plan(now=early)
    with db.tx() as c:
        register(db,c,r,early)
        p=c.execute(select(observations.c.payload)).scalar_one()
        assert p['flatten_at']==stamp('2026-11-27T12:55:00')
        db.put(c,KEY+':activation',{'at':OPEN-700})
        assert audit_schedule(db,c,cfg(),OPEN+60*60+131)==5
        gaps=c.execute(select(checks.c.payload).where(checks.c.kind=='schedule_gap')).scalars().all()
        assert len(gaps)==5 and sum(r['reason']=='blocked_missing_opening_wait' for r in gaps)==3
        assert audit_schedule(db,c,cfg(),OPEN+61*60+131)==0
        db.put(c,KEY+':activation',{'at':stamp('2026-09-07T09:00:00')})
        db.put(c,KEY+':schedule_cursor','2026-09-07')
        assert audit_schedule(db,c,cfg(),stamp('2026-09-07T17:00:00'))==0


def test_historical_reports_stay_separate_and_suppressed_checks_are_not_missed(db,clock):
    with db.tx() as c:
        r=plan();db.put(c,r['id'],r)
        assert inventory(db,c,NOW)==1 and inventory(db,c,NOW)==0
        assert not c.execute(select(observations)).first()
        db.put(c,KEY+':activation',{'at':OPEN-700})
        audit_schedule(db,c,cfg(),OPEN+3600+131)
        gaps=c.execute(select(checks.c.payload).where(checks.c.kind=='schedule_gap')).scalars().all()
        assert sum(r['reason']=='skipped_after_confirmation' for r in gaps)==3
        summary=report(db,c,OPEN+3600+131)
        assert summary['inventory']==dict(saved_reports=1,registered=1,unregistered=0)
        assert summary['counts']['prospective_reports']==0 and summary['counts']['historical_reports']==1


def test_held_contract_is_pinned_and_uses_existing_bounded_recovery(db,clock):
    with db.tx() as c:
        p=opened(db,c)
        assert stream_requests(c,NOW,'open')==[CALL] and stream_requests(c,NOW,'pending')==[]
        other=Config(local=True,stocks=('QQQ',),futures=(),extra_futures=())
        assert 'SPY' in data_symbols(db,c,other,NOW)
    coll=Collectors(db,cfg())
    asyncio.run(coll.refresh_option_subscriptions())
    assert CALL in coll.option_symbols
    coll.option_recovery_attempts={}
    chosen,waiting,truncated=recovery_targets(coll,NOW+3)
    assert chosen==[CALL] and waiting==1 and not truncated


def test_full_window_and_stable_pages_ignore_display_caps(storage,clock):
    with storage.tx() as c:
        rows=[];sources=[]
        for i in range(1005):
            key='spy-brief:fixture:'+str(i);id=identity(VERSION,key)
            rows.append(dict(id=id,version=VERSION,source_key=key,day=day(NOW),phase='opening',kind='report',
                origin='historical_inventory',decision='waiting',created=NOW,payload=dict(id=id,origin='historical_inventory')))
            sources.append(dict(key=key,value={},updated=NOW))
        for offset in range(0,len(rows),200):
            c.execute(storage.insert(checks).values(rows[offset:offset+200]));c.execute(storage.insert(state).values(sources[offset:offset+200]))
        summary=report(storage,c,NOW)
        assert summary['counts']['historical_reports']==1005 and summary['inventory']['registered']==1005
        page=record_page(c,NOW);first=page;ids=[]
        while True:
            ids.extend(r['id'] for r in page['records'])
            if not page['has_more']:break
            page=record_page(c,NOW+1,cursor=page['next_cursor'])
        assert len(ids)==len(set(ids))==1005 and page['total']==1005
        with pytest.raises(ValueError):record_page(c,NOW+1,cohort='confirmed',cursor=first['next_cursor'])


def test_mark_pages_preserve_processing_cutoff_and_both_quote_snapshots(storage,clock):
    with storage.tx() as c:
        p=opened(storage,c);worker=Paper(storage,cfg(),clock=lambda:NOW+300)
        for offset in range(1,211):worker.mark(c,p,NOW+offset,q(NOW+offset,2,2.05),q(NOW+offset),'mark')
        assert mark_page(c,NOW+100,p['id'])['total']==1
        page=mark_page(c,NOW+300,p['id']);first=page;ids=[]
        while True:
            ids.extend(r['id'] for r in page['records'])
            assert all('option_quote' in r['payload'] and 'underlying_quote' in r['payload'] for r in page['records'])
            if not page['has_more']:break
            page=mark_page(c,NOW+400,p['id'],cursor=page['next_cursor'])
        assert len(ids)==len(set(ids))==211
        with pytest.raises(ValueError):mark_page(c,NOW+400,'f'*64,cursor=first['next_cursor'])


def test_owner_read_only_api_filters_and_dashboard_controls(tmp_path):
    cfg_=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'web.db'),password='fixture-password-16',secret='fixture-secret')
    app=create_app(cfg_)
    with TestClient(app) as client:
        for path in ('/api/spy-study','/api/spy-study/records','/api/spy-study/marks/'+'a'*64):assert client.get(path).status_code==401
        client.post('/login',json={'password':cfg_.password})
        assert client.get('/api/spy-study/records?limit=101').status_code==422
        for query in ('cohort=bad','cursor=bad','asof=999999999999'):
            assert client.get('/api/spy-study/records?'+query).status_code==400
        assert client.get('/api/spy-study/marks/no').status_code==400
        assert client.get('/api/spy-study/marks/'+'a'*64).status_code==404
        response=client.get('/api/spy-study/records')
        assert response.json()['total']==0 and 'no-store' in response.headers['cache-control']
        with app.state.db.tx() as c:assert not c.execute(select(checks)).first()
        class Options(HTMLParser):
            active=False
            values=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='select':self.active=attrs.get('id')=='spy-study-cohort'
                if tag=='option' and self.active:self.values.append(attrs['value'])
            def handle_endtag(self,tag):
                if tag=='select':self.active=False
        parser=Options();parser.feed(client.get('/').text)
        assert set(parser.values)==set(COHORTS) and len(parser.values)==len(COHORTS)
