"""Account limits must never censor forward setup evidence."""
from sqlalchemy import select
from compass.engine import Engine, spec
from compass.setup_study import trials
from compass.option_ideas import ideas
from compass.paper_risk import key, ledgers
from test_setup_study import db, cfg, NOW, quote, signal

CALL='O:SPY260914C00100000'


def seed_options(db,c):
    db.put(c,'quote:SPY',quote(bid=100,ask=100.01))
    db.put(c,'quote:'+CALL,quote(bid=2,ask=2.02))
    db.put(c,'chain:SPY',dict(asof=NOW,contracts=[dict(symbol=CALL,expiry='2026-09-14',
        type='call',multiplier=100,strike=100)]))


def test_0dte_overlapping_observations_ignore_every_account_gate(db,cfg):
    cfg.max_entries=1
    cfg.risk=.01
    engine=Engine(db,cfg)
    with db.tx() as c:
        seed_options(db,c)
        risk=ledgers(db,c,NOW,persist=True)['option']
        risk.update(entries=999,realized=-100000)
        db.put(c,key(NOW,'option'),risk)
        for ticker in [CALL,'O:A','O:B']:
            db.put(c,'position:'+ticker,dict(status='open',asset='option',symbol=ticker))
        for i in range(12):
            s={**signal(str(i),symbol='SPY'),'invalidation':99}
            audit={}
            assert engine.options(c,s,NOW,quiet=True,research_only=True,diagnostics=audit)
            assert audit['status']=='observed'
        rs=c.execute(select(trials.c.payload)).scalars().all()
        assert len(rs)==12 and all(p['status']=='open' and p['asset']=='option' for p in rs)
        assert db.get(c,key(NOW,'option'))==risk
        assert len(db.prefix(c,'position:'))==3
        # Each overlapping observation receives its own target exit.
        target=max(p['target'] for p in rs)
        db.put(c,'quote:'+CALL,quote(NOW+1,bid=target,ask=target+.01))
        engine.study.tick(c,NOW+1)
        rs=c.execute(select(trials.c.payload)).scalars().all()
        assert all(p['status']=='closed' and p['pnl']>0 for p in rs)


def test_non_alerted_candidates_queue_both_option_horizons_without_paper(db,cfg):
    cfg.scanner_paper=False
    engine=Engine(db,cfg)
    with db.tx() as c:
        seed_options(db,c)
        for name in ['pullback','breakout']:
            s={**signal(name,symbol='SPY'),'invalidation':99,'context':{'atr14':1}}
            engine.observe_setup(c,s,NOW)
        assert len(db.prefix(c,'pending_research_options:'))==2
        assert len(c.execute(select(ideas.c.id)).all())==2
        engine.scanner.retry_options(c,{'SPY':db.get(c,'quote:SPY')},NOW,engine)
        assert all(p['status']=='observed' for p in db.prefix(c,'pending_research_options:').values())
        assert len(c.execute(select(trials.c.id)).all())==4
        assert not db.prefix(c,'position:')
        # Restart/dedup cannot reopen a completed selection or erase its audit.
        engine.observe_setup(c,s,NOW+1)
        assert db.get(c,'pending_research_options:breakout')['status']=='observed'


def test_wide_spread_measured_but_missing_quotes_not_invented(db,cfg):
    engine=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote(bid=100,ask=103))
        engine.study.start(c,{**signal(),'stop_distance':5},NOW,spec('MESZ6@1'))
        engine.study.start(c,signal('missing',symbol='MNQZ6@1'),NOW,spec('MNQZ6@1'))
        rs={p['source_id']:p for p in c.execute(select(trials.c.payload)).scalars()}
        assert rs['a']['status']=='open' and rs['a']['entry_spread']==3
        assert rs['missing']['status']=='excluded' and rs['missing']['pnl'] is None


def test_open_option_trials_remain_subscribed_without_paper_positions(db,cfg,monkeypatch):
    import asyncio
    from compass.providers import Collectors
    engine=Engine(db,cfg)
    cfg.stream_limit=10
    with db.tx() as c:
        seed_options(db,c)
        assert engine.options(c,signal('pin',symbol='SPY'),NOW,quiet=True,research_only=True)
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW)
    async def run():
        collector=Collectors(db,cfg)
        try:
            collector.background_option_symbols=['background'+str(i) for i in range(20)]
            await collector.refresh_option_subscriptions()
            assert CALL in collector.option_symbols
        finally:
            await collector.close()
    asyncio.run(run())


def test_late_cash_research_has_no_paper_entry_cutoff(db,cfg):
    from compass.market import session,day
    from compass.scanner import evaluation_session_open,session_open
    end=session(day(NOW))[1]
    at=end-120
    assert not session_open('SPY',at)
    assert evaluation_session_open('SPY',at)
    engine=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:SPY',quote(at))
        s={**signal('late',symbol='SPY'),'signal_time':at}
        engine.observe_setup(c,s,at)
        p=c.execute(select(trials.c.payload)).scalar_one()
        assert p['status']=='open' and p['flatten_at']==end
