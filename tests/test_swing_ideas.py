import asyncio
from datetime import datetime, date, timedelta
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from compass.config import Config
from compass.app import create_app
from compass.market import NY, day, session
from compass.store import flow_records
from compass.providers import Collectors
from compass.option_ideas import OptionIdeas
from compass.swing_ideas import SwingIdeas, swings, snapshot, stream_requests, active_underlyings
from compass.swing_signals import daily_context, minute_context, technical_setups, summarize_flow, confirms, trading_seconds, hold_deadline, sessions_between
from compass.alert_format import alert_identity, message_for
from test_scanner import db
from test_option_ideas import seed as seed_intraday, signal as intraday_signal


def stamp(d='2026-09-14', hour=10, minute=15, second=0):
    return datetime.fromisoformat(d).replace(hour=hour,minute=minute,second=second,tzinfo=NY).timestamp()


NOW=stamp()
CALL='O:SPY261016C00100000'
PUT='O:SPY261016P00100000'


@pytest.fixture
def cfg():
    return Config(local=True,stocks=('SPY',),futures=(),extra_futures=(),massive='fixture',
        alpaca_key='',alpaca_secret='',databento='',matrix='',,discord='',scanner_paper=False)


def quote(t=NOW,bid=100,ask=100.02):
    return dict(ts=t,bid=bid,ask=ask,bid_size=20,ask_size=20,source='massive')


def daily(now=NOW):
    dates=sessions_between((date.fromisoformat(day(now))-timedelta(days=180)).isoformat(),day(now))
    through=[d for d in dates if d<day(now)][-1]
    return dict(status='ready',day=day(now),through=through,close=100,sma20=99,sma50=98,
        atr14=2,rsi14=50,high20=100,low20=90,low5=97,high5=103,weekly_bias='bullish',
        weekly_through=through,weekly_close=100,weekly_sma10=98)


def flow_rows(now=NOW,sentiment='Bullish',kind='Call',expiry='2026-10-16'):
    return [dict(first_seen=now,payload=dict(vendor_id=str(i),symbol='SPY',source_ts=now-i,
        premium=75000,option_type=kind,expiry=expiry,sentiment=sentiment)) for i in range(2)]


def flow(now=NOW,side='long'):
    return summarize_flow(flow_rows(now,'Bullish' if side=='long' else 'Bearish'),now)['SPY']


def signal(now=NOW,side='long'):
    return dict(symbol='SPY',side=side,rule='daily_breakout',strategy='swing-ideas-v1:daily_breakout',
        signal_time=now,signal_price=100,stop=96 if side=='long' else 104,
        target=108 if side=='long' else 92,daily=daily(now),weekly_aligned=True,reason='Daily breakout')


def contract(side='long'):
    return dict(symbol=CALL if side=='long' else PUT,underlying='SPY',expiry='2026-10-16',strike=100,
        type='call' if side=='long' else 'put',multiplier=100,oi=500,volume=100,delta=.5 if side=='long' else -.5)


def records(c):
    return list(c.execute(select(swings.c.payload).order_by(swings.c.created)).scalars())


def seed(db,c,now=NOW,side='long'):
    o=contract(side)
    db.put(c,'matrix:unusual_activity',dict(source_ts=now,received=now))
    db.put(c,'quote:SPY',quote(now))
    db.put(c,'quote:'+o['symbol'],quote(now,2,2.05))
    db.put(c,'chain:SPY',dict(asof=now,source='massive',complete=True,contracts=[o]))
    db.put(c,'options:subscriptions',dict(at=now,symbols=[o['symbol']]))
    ref=daily(now)['through']
    db.append(c,'daily','alpaca','SPY',stamp(ref,0,0),dict(o=99,h=101,l=98,c=100,v=1000))


def open_swing(db,c,cfg,now=NOW,side='long'):
    svc=SwingIdeas(db,cfg)
    seed(db,c,now,side)
    svc.queue(c,signal(now,side),flow(now,side),now)
    svc.pending(c,records(c)[-1],now)
    return svc,records(c)[-1]


def prior_close_fixture(svc,c,p,d):
    # A persisted path already observed to the final seconds of a prior session.
    last=session(d)[1]-2
    p.update(last_option_ts=last,last_underlying_ts=last,last_quote=quote(last,2,2.05),
        last_underlying_quote=quote(last),sessions_observed=[d])
    svc.save(c,p,last)
    return p


def history(now=NOW):
    dates=sessions_between((date.fromisoformat(day(now))-timedelta(days=150)).isoformat(),day(now))
    rows=[]
    for i,d in enumerate(dates):
        close=80+i*.2+(1 if i%2 else -1)
        rows.append(dict(id=i+1,ts=stamp(d,0,0),payload=dict(o=close-.1,h=close+.5,l=close-.5,c=close,v=1000)))
    return rows


def test_daily_context_excludes_today_and_honors_holiday_week():
    rows=history()
    context=daily_context(rows,NOW)
    assert context['status']=='ready' and context['through']=='2026-09-11'
    assert context['weekly_through']=='2026-09-11'  # Four-session Labor Day week.
    rows[-1]['payload'].update(o=10000,h=11000,l=9000,c=10500)
    assert daily_context(rows,NOW)==context
    revised=dict(rows[-2],id=999,payload={**rows[-2]['payload'],'c':rows[-2]['payload']['c']+.1})
    assert daily_context(rows+[revised],NOW)['close']==revised['payload']['c']


def test_missing_daily_session_blocks_instead_of_looking_through_gap():
    rows=history()
    assert daily_context(rows[:-3]+rows[-2:],NOW)['status']=='warming_up'
    assert daily_context(rows[-40:],NOW)['status']=='warming_up'


def test_two_completed_minutes_can_trigger_swing_without_intraday_warmup():
    bars=[dict(id=1,ts=NOW-120,payload=dict(c=99.9)),dict(id=2,ts=NOW-60,payload=dict(c=100.1)),
          dict(id=3,ts=NOW,payload=dict(c=105))]
    minute=minute_context(bars,NOW)
    assert minute['bar']['c']==100.1 and technical_setups('SPY',daily(),minute,NOW)
    assert minute_context([bars[0],bars[2]],NOW)['status']=='waiting_for_price'
    assert minute_context(bars,NOW+181)['status']=='waiting_for_price'


@pytest.mark.parametrize('side',['long','short'])
@pytest.mark.parametrize('rule',['daily_breakout','pullback_reclaim','daily_reversal'])
def test_both_directions_have_explicit_daily_triggers_and_positive_risk(side,rule):
    d=daily()
    sign=1 if side=='long' else -1
    d.update(sma20=100,sma50=98 if sign==1 else 102,high20=100,low20=100,
        weekly_bias='bullish' if sign==1 else 'bearish')
    if rule=='daily_reversal':
        d.update(close=99 if sign==1 else 101,sma50=102 if sign==1 else 98,weekly_bias='neutral')
    minute=dict(status='ready',asof=NOW,bar_start=NOW-60,
        previous_bar=dict(c=100-sign*.1),bar=dict(c=100+sign*.1))
    rows=technical_setups('SPY',d,minute,NOW)
    p=next(p for p in rows if p['rule']==rule and p['side']==side)
    assert sign*(p['signal_price']-p['stop'])>0
    assert sign*(p['target']-p['signal_price'])>0
    assert technical_setups('SPY',d,minute,NOW+91)==[]
    assert technical_setups('SPY',{**d,'day':'2026-09-11'},minute,NOW)==[]


def test_flow_is_deduplicated_attributed_dated_and_directional():
    rows=flow_rows()
    rows.append(rows[0])
    f=summarize_flow(rows,NOW)['SPY']
    assert f['call_premium']==150000 and confirms(f,'long',NOW) and not confirms(f,'short',NOW)
    assert not confirms(f,'long',NOW+301)
    assert 'filtered' in f['basis'] and 'not all market' in f['basis']
    # Calls can be vendor-bearish; calls are not automatically bullish purchases.
    f=summarize_flow(flow_rows(sentiment='Bearish'),NOW)['SPY']
    assert not confirms(f,'long',NOW) and confirms(f,'short',NOW)


@pytest.mark.parametrize('expiry',['10/16/26','10/16/2026','2026-10-16'])
def test_live_vendor_expiration_formats_qualify_swing_flow(expiry):
    assert confirms(summarize_flow(flow_rows(expiry=expiry),NOW)['SPY'],'long',NOW)


@pytest.mark.parametrize('problem',['unknown','0dte','future','old','seen_later','opposing'])
def test_missing_or_non_swing_flow_cannot_confirm(problem):
    rows=flow_rows()
    if problem=='unknown':
        for r in rows:r['payload']['sentiment']=None
    if problem=='0dte':
        for r in rows:r['payload']['expiry']='2026-09-14'
    if problem=='future':
        for r in rows:r['payload']['source_ts']=NOW+1
    if problem=='old':
        for r in rows:r['payload']['source_ts']=NOW-1801
    if problem=='seen_later':
        for r in rows:r['first_seen']=NOW+1
    if problem=='opposing':
        rows.append(dict(payload={**rows[0]['payload'],'vendor_id':'opponent','premium':100000,'sentiment':'Bearish'}))
    f=summarize_flow(rows,NOW).get('SPY',{})
    assert not confirms(f,'long',NOW)


@pytest.mark.parametrize('side',['long','short'])
def test_real_quoted_contract_entry_is_independent_and_alert_is_distinct(db,cfg,side):
    with db.tx() as c:
        db.put(c,'risk:2026-09-14',dict(entries=999,realized=-99999))
        svc,p=open_swing(db,c,cfg,side=side)
        assert p['status']=='open' and p['entry']==2.06 and p['contract']['type']==('call' if side=='long' else 'put')
        assert p['premium_stop']==1.34 and p['premium_target']==3.60
        assert p['exit_deadline']==stamp('2026-09-25',15,45)
        assert db.get(c,'risk:2026-09-14')['entries']==999 and not db.prefix(c,'position:')
        alert=db.recent(c,'alert')[0]
        assert alert_identity(alert,NOW)['label']=='SWING IDEAS'
        assert 'HISTORICAL SWING ENTRY' in alert_identity(alert,NOW+121)['event']
        text=message_for(alert,NOW)
        assert '2026-10-16' in text and 'filtered flow' in text and 'Final exit deadline' in text and len(text)<=1900
        assert active_underlyings(c)==['SPY']
        assert snapshot(db,c,cfg,NOW)['groups'][0]['status']=='open'


@pytest.mark.parametrize('problem',['no_stream','wide','stale_quote','incomplete_chain','short_dte','adjusted','wrong_symbol'])
def test_no_option_fill_without_entry_requirements(db,cfg,problem):
    svc=SwingIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c)
        chain=db.get(c,'chain:SPY')
        if problem=='no_stream':db.put(c,'options:subscriptions',dict(at=NOW,symbols=[]))
        if problem=='wide':db.put(c,'quote:'+CALL,quote(NOW,2,3))
        if problem=='stale_quote':db.put(c,'quote:'+CALL,quote(NOW-6,2,2.05))
        if problem=='incomplete_chain':chain['complete']=False
        if problem=='short_dte':chain['contracts'][0].update(expiry='2026-09-18',symbol='O:SPY260918C00100000')
        if problem=='adjusted':chain['contracts'][0]['multiplier']=10
        if problem=='wrong_symbol':chain['contracts'][0]['symbol']='O:QQQ261016C00100000'
        db.put(c,'chain:SPY',chain)
        svc.queue(c,signal(),flow(),NOW)
        svc.pending(c,records(c)[0],NOW)
        assert records(c)[0]['status']=='pending' and not db.recent(c,'alert')
        svc.pending(c,records(c)[0],NOW+121)
        assert records(c)[0]['status']=='excluded' and records(c)[0]['entry'] is None


@pytest.mark.parametrize('previous,next_day',[('2026-09-11','2026-09-14'),('2026-09-04','2026-09-08'),('2026-11-27','2026-11-30'),('2026-10-30','2026-11-02')])
def test_scheduled_closures_and_early_closes_add_no_observation_gap(previous,next_day):
    assert trading_seconds(session(previous)[1]-2,session(next_day)[0]+3)==5
    assert trading_seconds(session(previous)[1],session(next_day)[0])==0
    assert trading_seconds(session(next_day)[0],session(next_day)[0]+61)==61


@pytest.mark.parametrize('side',['long','short'])
def test_weekend_carry_and_adverse_reopen_exit_at_observed_bid(db,cfg,side):
    friday=stamp('2026-09-11',14,0)
    monday=stamp('2026-09-14',9,30,3)
    with db.tx() as c:
        svc,p=open_swing(db,c,cfg,friday,side)
        p=prior_close_fixture(svc,c,p,'2026-09-11')
        svc.observe(c,p,stamp('2026-09-12',12,0))
        p=records(c)[0]
        assert p['status']=='open' and p['observation_state']=='scheduled_market_closure'
        chain=db.get(c,'chain:SPY');chain['asof']=monday;db.put(c,'chain:SPY',chain)
        db.put(c,'quote:'+p['contract']['symbol'],quote(monday,.8,.85))
        db.put(c,'quote:SPY',quote(monday,95 if side=='long' else 105,95.02 if side=='long' else 105.02))
        svc.observe(c,p,monday)
        p=records(c)[0]
        assert p['status']=='closed' and p['exit']==.79 and p['exit']<p['premium_stop']
        assert p['pnl']==pytest.approx(-128.3) and p['outcome']=='loss'
        assert p['overnight_gaps'][0]['option_bid_change']==pytest.approx(-1.2)


def test_favorable_reopen_caps_target_and_tracks_independent_option_result(db,cfg):
    friday=stamp('2026-09-11',14,0);monday=stamp('2026-09-14',9,30,3)
    with db.tx() as c:
        svc,p=open_swing(db,c,cfg,friday)
        p=prior_close_fixture(svc,c,p,'2026-09-11')
        chain=db.get(c,'chain:SPY');chain['asof']=monday;db.put(c,'chain:SPY',chain)
        db.put(c,'quote:'+CALL,quote(monday,5,5.05));db.put(c,'quote:SPY',quote(monday,102,102.02))
        svc.observe(c,p,monday)
        p=records(c)[0]
        assert p['exit']==p['premium_target']==3.60 and p['pnl']==pytest.approx(152.7)
        assert snapshot(db,c,cfg,monday)['groups'][0]['outcome']=='win'


@pytest.mark.parametrize('missing',['minute','session','underlying','metadata'])
def test_missing_open_session_path_is_unresolved_not_a_later_win(db,cfg,missing):
    with db.tx() as c:
        svc,p=open_swing(db,c,cfg)
        later=NOW+61 if missing!='session' else stamp('2026-09-16',9,30,2)
        if missing=='metadata':
            p=prior_close_fixture(svc,c,p,'2026-09-14');later=stamp('2026-09-15',9,31,1)
        db.put(c,'quote:'+CALL,quote(later,5,5.05))
        if missing!='underlying':db.put(c,'quote:SPY',quote(later,110,110.02))
        svc.observe(c,p,later)
        p=records(c)[0]
        assert p['status']=='unresolved' and p['return_pct'] is None and p['pnl'] is None


@pytest.mark.parametrize('change',['adjusted','missing','basis'])
def test_changed_contract_or_split_price_basis_cannot_produce_false_outcome(db,cfg,change):
    friday=stamp('2026-09-11',14,0);monday=stamp('2026-09-14',9,30,3)
    with db.tx() as c:
        svc,p=open_swing(db,c,cfg,friday);p=prior_close_fixture(svc,c,p,'2026-09-11')
        chain=db.get(c,'chain:SPY');chain['asof']=monday
        if change=='adjusted':chain['contracts'][0]['multiplier']=10
        if change=='missing':chain['contracts']=[]
        if change=='basis':db.append(c,'daily','alpaca','SPY',stamp(p['daily']['through'],0,0),dict(o=49,h=51,l=48,c=50,v=1000))
        db.put(c,'chain:SPY',chain)
        db.put(c,'quote:'+CALL,quote(monday,5,5.05));db.put(c,'quote:SPY',quote(monday))
        svc.observe(c,p,monday)
        p=records(c)[0]
        assert p['status']=='unresolved' and p['pnl'] is None
        assert p['exit_reason'] in ('contract_metadata_changed','historical_price_basis_changed')


def test_hold_deadline_respects_holidays_and_expiry_guard():
    assert hold_deadline(stamp('2026-09-04'),'2026-09-18',30)==stamp('2026-09-17',15,45)
    assert hold_deadline(stamp('2026-11-25'),'2026-12-18',2)==stamp('2026-11-27',12,45)


@pytest.mark.parametrize('late',[False,True])
def test_hold_exit_needs_quote_at_deadline(db,cfg,late):
    with db.tx() as c:
        svc,p=open_swing(db,c,cfg)
        p['exit_deadline']=NOW+30;svc.save(c,p,NOW)
        observed=NOW+36 if late else NOW+30
        db.put(c,'quote:'+CALL,quote(observed,2.2,2.25));db.put(c,'quote:SPY',quote(observed))
        svc.observe(c,p,observed)
        assert records(c)[0]['status']==('unresolved' if late else 'closed')
        assert records(c)[0]['exit_reason']==('holding_deadline_quote_missing' if late else 'maximum_hold')


def test_restart_deduplicates_entries_and_milestones_and_disabled_still_manages(db,cfg):
    with db.tx() as c:
        svc,p=open_swing(db,c,cfg)
        later=NOW+2
        db.put(c,'quote:'+CALL,quote(later,2.8,2.85));db.put(c,'quote:SPY',quote(later))
        svc.observe(c,p,later)
        assert records(c)[0]['milestones']==[10,25]
        restarted=SwingIdeas(db,cfg)
        restarted.queue(c,signal(),flow(),later)
        restarted.observe(c,records(c)[0],later)
        assert len(records(c))==1 and len(db.recent(c,'alert'))==2
        cfg.swing_ideas=False
        db.put(c,'quote:'+CALL,quote(later+2,1,1.05));db.put(c,'quote:SPY',quote(later+2))
        restarted.tick(c,later+2)
        assert records(c)[0]['status']=='closed' and records(c)[0]['outcome']=='loss'


def test_fresh_flow_rechecked_before_delayed_contract_entry(db,cfg):
    with db.tx() as c:
        svc=SwingIdeas(db,cfg)
        seed(db,c)
        f=flow(NOW-290)
        svc.queue(c,signal(),f,NOW)
        svc.pending(c,records(c)[0],NOW+20)
        assert records(c)[0]['status']=='excluded' and records(c)[0]['entry'] is None


def test_open_swings_precede_pending_intraday_streams(db,cfg,monkeypatch):
    cfg.stream_limit=2
    with db.tx() as c:
        _,p=open_swing(db,c,cfg)
        seed_intraday(db,c,subscribed=False)
        service=OptionIdeas(db,cfg)
        service.queue(c,intraday_signal(),NOW);service.tick(c,NOW)
        db.put(c,'position:held',dict(status='open',asset='option',symbol='O:ORIGINAL'))
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW)
    async def run():
        collector=Collectors(db,cfg)
        await collector.refresh_option_subscriptions()
        assert collector.option_symbols=={'O:ORIGINAL',CALL}
        await collector.close()
    asyncio.run(run())


def test_discovery_from_daily_history_and_durable_flow_to_quoted_entry(db,cfg,monkeypatch):
    svc=SwingIdeas(db,cfg)
    rows=history();context=daily_context(rows,NOW)
    price=context['high20']+.1;strike=round(price)
    symbol=f'O:SPY261016C{strike*1000:08d}'
    with db.tx() as c:
        db.put(c,'matrix:unusual_activity',dict(source_ts=NOW,received=NOW))
        for row in rows:db.append(c,'daily','alpaca','SPY',row['ts'],row['payload'])
        db.put(c,'scanner_features:SPY',dict(status='ready',asof=NOW,bar_start=NOW-60,
            previous_bar=dict(c=context['high20']-.1),bar=dict(c=price)))
        for row in flow_rows():
            p=row['payload'];c.execute(db.insert(flow_records).values(day=day(NOW),vendor_id=p['vendor_id'],
                source_ts=p['source_ts'],first_seen=NOW,last_seen=NOW,payload=p))
        db.put(c,'quote:SPY',quote(NOW,price,price+.02))
        db.put(c,'chain:SPY',dict(asof=NOW,complete=True,source='massive',contracts=[{**contract(),'symbol':symbol,'strike':strike}]))
        svc.tick(c,NOW)
        assert records(c)[0]['status']=='pending'
        svc.tick(c,NOW+1)
        assert stream_requests(c,NOW+1)==[symbol]
        assert not db.recent(c,'alert')
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW+2)
    async def reconcile():
        collector=Collectors(db,cfg)
        await collector.refresh_option_subscriptions()
        await collector.close()
    asyncio.run(reconcile())
    with db.tx() as c:
        db.put(c,'quote:'+symbol,quote(NOW+2,2,2.05))
        svc.tick(c,NOW+2)
        assert records(c)[0]['status']=='open'
        assert snapshot(db,c,cfg,NOW+2)['daily_ready']==1
        assert len(db.recent(c,'alert'))==1


def test_swing_state_is_owner_only(db,cfg):
    cfg.db=str(db.engine.url);cfg.role='web';cfg.password='a-valid-local-owner-password';cfg.secret='local-secret'
    app=create_app(cfg)
    with TestClient(app) as client:
        assert client.get('/api/state').status_code==401
        assert client.get('/api/state/studies').status_code==401
        assert client.post('/login',json={'password':cfg.password}).status_code==200
        result=client.get('/api/state')
        assert result.status_code==200 and 'swing_ideas' not in result.json()
        studies=client.get('/api/state/studies')
        assert studies.status_code==200 and studies.json()['swing_ideas']['max_hold_sessions']==10
        assert 'swing-ideas' in client.get('/').text
