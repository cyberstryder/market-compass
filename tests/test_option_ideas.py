import asyncio
from datetime import datetime
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from compass.config import Config
from compass.engine import Engine
from compass.providers import Collectors
from compass.market import NY
from compass.option_ideas import OptionIdeas, ideas, eligible_contracts, stream_requests, choose_streams, snapshot
from compass.alert_format import alert_identity, message_for
from compass.app import create_app
from compass.scanner import Scanner
from test_scanner import db, NOW, fact

CALL='O:SPY260918C00100000'
PUT='O:SPY260918P00100000'


@pytest.fixture
def cfg():
    return Config(local=True,stocks=('SPY',),futures=(),extra_futures=(),massive='fixture',
        scanner_paper=False,alpaca_key='',alpaca_secret='',databento='',matrix='',openai='',discord='')


def q(now=NOW,bid=100,ask=100.02):
    return dict(ts=now,bid=bid,ask=ask,bid_size=5,ask_size=5,source='massive')


def contract(side='long'):
    return dict(symbol=CALL if side=='long' else PUT,underlying='SPY',expiry='2026-09-18',
        strike=100,type='call' if side=='long' else 'put',multiplier=100,oi=500,volume=50,
        delta=.45 if side=='long' else -.45)


def signal(side='long',now=NOW):
    return dict(id='source-'+side,symbol='SPY',side=side,track='intraday',
        signal_time=now,signal_price=100,strategy='compass-scanner-v2:volume_breakout',
        rule='volume_breakout',reason='Completed range break',context={'atr14':1},
        stop=99 if side=='long' else 101,target=102 if side=='long' else 98)


def rows(c):
    return c.execute(select(ideas.c.payload).order_by(ideas.c.created)).scalars().all()


def seed(db,c,side='long',now=NOW,subscribed=True):
    o=contract(side)
    db.put(c,'quote:SPY',q(now))
    db.put(c,'chain:SPY',dict(asof=now,source='massive',complete=True,contracts=[o]))
    db.put(c,'quote:'+o['symbol'],q(now,2,2.05))
    from compass.store import events
    for offset in (25,20,15,10,5,0):
        quote=q(now-offset,2,2.05)
        db.append(c,'quote','massive',o['symbol'],now-offset,quote)
        c.execute(events.update().where(events.c.symbol==o['symbol'],events.c.ts==now-offset).values(received=now-offset+.1))
    db.put(c,'options:subscriptions',dict(at=now,symbols=[o['symbol']] if subscribed else []))
    return o


@pytest.mark.parametrize('side',['long','short'])
def test_listed_call_put_selection_and_independent_entry(db,cfg,side):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c,side)
        db.put(c,'risk:2026-09-14',dict(realized=-10000,entries=500))
        service.queue(c,signal(side),NOW)
        service.tick(c,NOW)
        p=rows(c)[0]
        assert p['status']=='open' and p['contract']['type']==('call' if side=='long' else 'put')
        assert p['entry']==2.06 and p['premium_stop']==1.44 and p['premium_target']==3.09
        assert not db.prefix(c,'position:')
        assert db.get(c,'risk:2026-09-14')['entries']==500
        alert=db.recent(c,'alert')[0]
        assert alert_identity(alert,NOW)['label']=='OPTIONS IDEAS'
        text=message_for(alert,NOW)
        assert '2026-09-18' in text and 'Underlying invalidation' in text and 'OPTION IDEA' in text
        assert len(text)<=1900
        assert alert_identity(alert,NOW+121)['event']=='EXPIRED IDEA'


@pytest.mark.parametrize('problem',['0dte','too_far','adjusted','wrong_kind','wrong_strike','wrong_underlying','illiquid','stale_chain','nan_delta'])
def test_contract_metadata_and_expiration_gates(cfg,problem):
    o=contract()
    chain=dict(asof=NOW,contracts=[o])
    if problem=='0dte': o.update(expiry='2026-09-14',symbol='O:SPY260914C00100000')
    if problem=='too_far': o.update(expiry='2026-10-16',symbol='O:SPY261016C00100000')
    if problem=='adjusted': o['multiplier']=10
    if problem=='wrong_kind': o['type']='put'
    if problem=='wrong_strike': o['strike']=101
    if problem=='wrong_underlying': o['underlying']='QQQ'
    if problem=='illiquid': o.update(oi=None,volume=1)
    if problem=='stale_chain': chain['asof']=NOW-121
    if problem=='nan_delta': o['delta']=float('nan')
    result=eligible_contracts(chain,'SPY','long',100,NOW,cfg)
    if problem=='nan_delta':
        # Missing/unusable Greeks are optional; no fabricated delta is used.
        assert len(result)==1
    else:
        assert result==[]


@pytest.mark.parametrize('problem',['stale','future','wide','empty','unsubscribed'])
def test_no_contract_entry_or_new_alert_without_usable_data(db,cfg,problem):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c,subscribed=problem!='unsubscribed')
        oq=q(NOW-6 if problem=='stale' else NOW+1 if problem=='future' else NOW,2,2.05)
        if problem=='wide': oq['ask']=2.5
        if problem=='empty': oq['bid_size']=0
        db.put(c,'quote:'+CALL,oq)
        service.queue(c,signal(),NOW)
        service.tick(c,NOW)
        assert rows(c)[0]['status']=='pending' and not db.recent(c,'alert')
        assert stream_requests(c,NOW)==[CALL]
        service.tick(c,NOW+121)
        assert rows(c)[0]['status']=='excluded'
        assert snapshot(db,c,cfg,NOW+121)['counts'].get('closed',0)==0


def test_new_contract_stream_request_does_not_wait_for_chain_http(db,cfg,monkeypatch):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c,subscribed=False)
        service.queue(c,signal(),NOW)
        service.tick(c,NOW)
        assert rows(c)[0]['status']=='pending'
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW+1)
    async def run():
        collector=Collectors(db,cfg)
        async def unexpected(*args,**kwargs): raise AssertionError('No chain HTTP needed')
        collector.get=unexpected
        collector.background_option_symbols=['O:BACKGROUND']
        with db.tx() as c:
            db.put(c,'position:original',dict(status='open',asset='option',symbol='O:HELD'))
        await collector.refresh_option_subscriptions()
        assert {CALL,'O:HELD','O:BACKGROUND'} <= collector.option_symbols
        await collector.close()
    asyncio.run(run())
    with db.tx() as c:
        service.tick(c,NOW+1)
        assert rows(c)[0]['status']=='open'


def test_subscription_capacity_keeps_existing_positions_and_pins_first():
    assert choose_streams(['held'],['idea','idea','other'],['background'],3)==['held','idea','other']


def test_return_updates_are_net_option_returns_and_dedup_survives_restart(db,cfg):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c)
        service.queue(c,signal(),NOW)
        service.tick(c,NOW)
        db.put(c,'quote:'+CALL,q(NOW+2,2.7,2.75))
        db.put(c,'quote:SPY',q(NOW+2))
        service.tick(c,NOW+2)
        p=rows(c)[0]
        assert p['mark_pct']==pytest.approx(61.7/206.65*100)
        assert p['milestones']==[10,25]
        assert len(db.recent(c,'alert'))==2
    with db.tx() as c:
        OptionIdeas(db,cfg).queue(c,signal(),NOW+3)
        OptionIdeas(db,cfg).tick(c,NOW+3)
        assert len(rows(c))==1 and len(db.recent(c,'alert'))==2
        assert 'net return' in message_for(db.recent(c,'alert')[0],NOW+3)


@pytest.mark.parametrize('side',['long','short'])
def test_premium_target_is_capped_and_stop_gaps_are_adverse(db,cfg,side):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        o=seed(db,c,side)
        service.queue(c,signal(side),NOW)
        service.tick(c,NOW)
        db.put(c,'quote:'+o['symbol'],q(NOW+2,4,4.05))
        db.put(c,'quote:SPY',q(NOW+2))
        service.tick(c,NOW+2)
        p=rows(c)[0]
        assert p['exit']==3.09 and p['pnl']==pytest.approx(101.7)
        assert p['best_pct']==pytest.approx(p['return_pct'])
        assert p['outcome']=='win' and p['exit_reason']=='premium_target'
        assert not stream_requests(c,NOW+2)


def test_option_stop_gap_and_underlying_invalidation(db,cfg):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c)
        service.queue(c,signal(),NOW)
        service.tick(c,NOW)
        db.put(c,'quote:'+CALL,q(NOW+2,1,1.05))
        db.put(c,'quote:SPY',q(NOW+2))
        service.tick(c,NOW+2)
        p=rows(c)[0]
        assert p['exit']==.99 and p['pnl']==pytest.approx(-108.3)
        assert p['exit_reason']=='premium_stop' and p['outcome']=='loss'


@pytest.mark.parametrize('missing',['option','underlying'])
def test_missing_observation_path_is_not_a_win_or_loss(db,cfg,missing):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c)
        service.queue(c,signal(),NOW)
        service.tick(c,NOW)
        if missing=='option': db.put(c,'quote:SPY',q(NOW+16))
        else: db.put(c,'quote:'+CALL,q(NOW+16,3,3.05))
        service.tick(c,NOW+16)
        p=rows(c)[0]
        assert p['status']=='unresolved' and p['pnl'] is None and p['return_pct'] is None
        assert 'final option P&L and win/loss are unresolved' in message_for(db.recent(c,'alert')[0],NOW+16)


def test_intraday_cutoff_and_disabling_does_not_strand_open_ideas(db,cfg):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c)
        service.queue(c,signal(),NOW)
        service.tick(c,NOW)
        p=rows(c)[0]
        assert datetime.fromtimestamp(p['flatten_at'],NY).strftime('%H:%M')=='15:45'
        p['flatten_at']=NOW+4
        service.save(c,p,NOW)
        cfg.option_ideas=False
        db.put(c,'quote:SPY',q(NOW+4))
        db.put(c,'quote:'+CALL,q(NOW+4,2,2.05))
        service.tick(c,NOW+4)
        assert rows(c)[0]['exit_reason']=='session_flatten'


def test_actual_scanner_queues_stock_idea_without_portfolio_entry(db,cfg):
    f=fact()
    with db.tx() as c:
        db.put(c,'scanner_features:SPY',f)
        db.put(c,'quote:SPY',q(NOW,101.1,101.11))
        db.put(c,'matrix:unusual_activity',dict(source_ts=NOW-1,received=NOW,rows=[dict(symbol='SPY',source_ts=NOW-1,
            premium=200000,score=90,sentiment='Bullish',vendor_id='one')]))
        Scanner(db,cfg).scan(c,NOW,Engine(db,cfg))
        assert len(rows(c))==1 and rows(c)[0]['status']=='pending'
        assert not db.prefix(c,'position:')


def test_dashboard_api_is_private_and_includes_options_ideas(db,cfg):
    cfg.role='web';cfg.db=str(db.engine.url)
    cfg.password='fixture-password-16-chars';cfg.secret='fixture-session-secret'
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/state').status_code==401
        client.post('/login',json={'password':cfg.password})
        state=client.get('/api/state').json()
        assert state['option_ideas']['enabled']
        assert 'Options ideas' in client.get('/').text
