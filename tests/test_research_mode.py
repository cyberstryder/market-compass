"""Research operation never admits paper entries or emits account rejections."""
import asyncio
import json
import httpx
import pytest
from sqlalchemy import select
from compass.config import Config
from compass.engine import Engine
from compass.store import events, discord_jobs, swing_trials, swing_checkpoints
from compass.setup_study import trials
from compass.alerts import DeliveryWorker, enqueue_routes
from compass.alert_format import message_for
from compass.daily_results import build
from test_setup_study import db, cfg, NOW, quote, signal
from test_unrestricted_evaluation import seed_options, CALL


def test_default_is_research_even_with_legacy_scanner_flag(monkeypatch):
    monkeypatch.delenv('PAPER_TRADING_ENABLED',raising=False)
    monkeypatch.setenv('SCANNER_PAPER_TRADES','true')
    assert not Config().paper_trading


@pytest.mark.parametrize('symbol',['MNQZ6@1','NQZ6@2','SPY'])
def test_overlapping_research_enters_and_exits_without_account_calls(db,cfg,monkeypatch,symbol):
    from compass.native_morning_worker import initialize
    initialize(db)
    cfg.paper_trading=False; cfg.scanner_paper=True; cfg.max_entries=1; cfg.risk=.01
    e=Engine(db,cfg)
    monkeypatch.setattr(e,'entry_check',lambda *a:pytest.fail('Paper admission invoked'))
    with db.tx() as c:
        db.put(c,'quote:'+symbol,quote())
        db.put(c,'risk:fixture',dict(entries=999,realized=-100000))
        for i in range(12):
            assert not e.enter(c,signal(str(i),symbol=symbol),NOW)
        rows=c.execute(select(trials.c.payload)).scalars().all()
        assert len(rows)==12 and all(p['status']=='open' for p in rows)
        assert not db.prefix(c,'position:') and not db.prefix(c,'trade:')
        assert not db.prefix(c,'paper_risk:')
        notices=db.recent(c,'alert')
        assert len(notices)==12 and all(r['payload']['status']=='research_observation' for r in notices)
        assert all('RESEARCH TRACKING' in message_for(r,NOW) and 'SKIPPED' not in message_for(r,NOW) for r in notices)
        target=max(p['target'] for p in rows)
        db.put(c,'quote:'+symbol,quote(NOW+1,target,target+.25))
        e.study.tick(c,NOW+1)
        assert all(p['status']=='closed' for p in c.execute(select(trials.c.payload)).scalars())
        report=build(db,c,cfg,NOW+2,'2026-09-14')
        assert not report['operating_policy']['paper_entries_enabled']
        assert sum(r['total'] for r in report['setup_context'])==12


def test_missing_quote_notice_is_unavailable_not_tracking_or_loss(db,cfg):
    cfg.paper_trading=False
    with db.tx() as c:
        Engine(db,cfg).enter(c,signal(),NOW)
        row=db.recent(c,'alert')[0]
        assert row['payload']['study_status']=='excluded'
        assert 'RESEARCH DATA UNAVAILABLE' in message_for(row,NOW)
        p=c.execute(select(trials.c.payload)).scalar_one()
        assert p['pnl'] is None and p['entry'] is None


def test_scanner_notice_does_not_call_an_excluded_row_active():
    row=dict(id=1,source='scanner',symbol='SPY',ts=NOW,payload=dict(
        status='setup_triggered',expires_at=NOW+120,setup_trial_id='trial',research_status='excluded'))
    text=message_for(row,NOW)
    assert 'Independent research: excluded' in text
    assert 'tracking active' not in text and 'Portfolio simulation' not in text


def test_option_path_forces_research_and_pauses_old_paper_retries(db,cfg,monkeypatch):
    cfg.paper_trading=False; cfg.scanner_paper=True
    e=Engine(db,cfg)
    monkeypatch.setattr(e,'entry_check',lambda *a:pytest.fail('Paper admission invoked'))
    with db.tx() as c:
        seed_options(db,c)
        s={**signal(symbol='SPY'),'invalidation':99}
        assert e.options(c,s,NOW)
        assert not db.prefix(c,'position:')
        e.observe_setup(c,{**s,'id':'new'},NOW)
        db.put(c,'pending_options:old',dict(signal=s,expires_at=NOW+120,status='waiting'))
        e.scanner.retry_options(c,{'SPY':db.get(c,'quote:SPY')},NOW,e)
        assert db.get(c,'pending_options:old')['status']=='paused'
        assert db.get(c,'pending_research_options:new')['status']=='observed'


def test_legacy_positions_finish_silently_without_erasing_history(db,cfg):
    cfg.paper_trading=True
    e=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        assert e.enter(c,signal(),NOW)
        original=db.get(c,'position:MESZ6@1')
        alerts=len(db.recent(c,'alert'))
        cfg.paper_trading=False
        db.put(c,'quote:MESZ6@1',quote(NOW+1,original['target'],original['target']+.25))
        e.exits(c,NOW+1)
        assert db.get(c,'position:MESZ6@1')['status']=='closed'
        assert db.get(c,'trade:a')['entry']==original['entry']
        assert len(db.recent(c,'alert'))==alerts
        assert db.recent(c,'paper_decision')[0]['payload']['status']=='closed'


def test_queued_paper_messages_suppressed_without_touching_other_categories(db,cfg):
    cfg.paper_trading=False;cfg.discord='https://discord.com/api/webhooks/123/test'
    cfg.discord_routes={}
    with db.tx() as c:
        for status in ['skipped','entered','closed','options_skipped','management_blocked']:
            db.append(c,'alert','engine','MNQZ6@1',NOW,dict(status=status,reason='Daily simulated loss limit'),status)
        # Already queued by the prior release, including a missing webhook route.
        enqueue_routes(db,c,NOW)
        for category in ['futures','options_ideas','swing','morning','smoothers','spy_morning','unusual_options','exposure']:
            db.append(c,'alert','research','SPY',NOW,dict(status='research_observation',alert_category=category,
                study_status='open',id=category),category)
    sent=[]
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json={'id':'123','channel_id':'456'})
        sent.append(json.loads(request.content)['content']);return httpx.Response(200,json={'id':str(1000+len(sent))})
    async def run():
        worker=DeliveryWorker(db,cfg)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(3):await worker.tick(client,NOW)
    asyncio.run(run())
    assert len(sent)==8 and all('RESEARCH TRACKING' in s for s in sent)
    with db.tx() as c:
        jobs=c.execute(select(discord_jobs)).mappings().all()
        assert sum(j['status']=='suppressed' for j in jobs)==5
        assert sum(j['status']=='sent' for j in jobs)==8
        assert all('message_id' not in j['confirmation'] for j in jobs if j['status']=='suppressed')


@pytest.mark.parametrize('available',[True,False])
def test_daily_breakout_has_independent_swing_checkpoints(db,cfg,monkeypatch,available):
    cfg.paper_trading=False
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        if available:db.put(c,'quote:SPY',quote(bid=100,ask=100.01))
        s={**signal(symbol='SPY'),'track':'swing','signal_time':NOW-86400,
           'daily':{'through':'2026-09-11','close':100}}
        e=Engine(db,cfg)
        e.enter(c,s,NOW);e.enter(c,s,NOW)
        p=c.execute(select(swing_trials)).mappings().one()
        assert p['status']==('pending' if available else 'unavailable')
        checks=c.execute(select(swing_checkpoints)).mappings().all()
        assert len(checks)==6
        assert all(r['status']==('pending' if available else 'unavailable') for r in checks)
        assert not db.prefix(c,'trade:')
        assert len(db.recent(c,'alert'))==1
