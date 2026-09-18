from copy import deepcopy
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func, update
from compass.config import Config
from compass.store import Store, state, events, discord_jobs
from compass.market import session
from compass.spy_timeframes import VERSION, TIMEFRAMES, Study, Paper, activate, arms, checks, marks, days, stream_requests
from compass.spy_timeframe_report import report, records, metrics
from compass.option_recovery import targets
from test_spy_brief import seed, OPEN
from test_spy_study import archive, q
from test_smoothers_postgres_handoff import pg


@pytest.fixture
def db(tmp_path):
    value=Store('sqlite:///'+str(tmp_path/'timing.db'));value.initialize()
    yield value
    value.engine.dispose()


@pytest.fixture(params=['sqlite','postgres'])
def storage(request):return request.getfixturevalue('db' if request.param=='sqlite' else 'pg')


@pytest.fixture
def clock(monkeypatch):
    value=[OPEN-10];monkeypatch.setattr('compass.store.time.time',lambda:value[0]);return value


def cfg():return Config(local=True, stocks=('SPY',), futures=(), extra_futures=())


def start(db,clock,at=OPEN-10):
    clock[0]=at;worker=Study(db,cfg());worker.tick(at);return worker


def tick(db,clock,worker,now,*,inside=False):
    clock[0]=now;seed(db,now)
    if inside:
        with db.tx() as c:
            rows=db.get(c,'bar_window:SPY')
            for r in rows:
                if r[0]>=OPEN:r[1:5]=[760,760.2,759.8,760]
            db.put(c,'bar_window:SPY',rows)
    worker.tick(now)


def all_arms(db):
    with db.tx() as c:return {p['timeframe']:p for p in c.execute(select(arms.c.payload)).scalars()}


def test_activation_freezes_twenty_full_sessions_and_never_restarts(storage,clock):
    with storage.tx() as c:
        a=activate(storage,c,OPEN+100)
        assert len(a['sessions'])==20 and a['sessions'][0]['open']>OPEN
        assert activate(storage,c,OPEN+86400)==a
        assert a['evaluation_end']==a['sessions'][-1]['close']


def test_each_interval_uses_its_own_completed_boundary_with_one_shared_range(storage,clock):
    worker=start(storage,clock)
    for minute in range(1,16):
        tick(storage,clock,worker,OPEN+minute*60+6)
        if minute==1:assert all_arms(storage)[1]['status']=='pending'
        if minute==2:assert all_arms(storage)[3]['status']=='waiting'
    ps=all_arms(storage)
    assert {tf:p['check_minute'] for tf,p in ps.items()}=={1:1,3:3,5:5,15:15}
    assert len({p['frozen_context_id'] for p in ps.values()})==1
    with storage.tx() as c:
        for r in c.execute(select(checks.c.payload).where(checks.c.status=='confirmed')).scalars():
            assert len(r['candle_bars'])==r['timeframe']
            assert max(b[0]+60 for b in r['candle_bars'])==r['boundary']
        assert storage.prefix(c,'risk:')=={} and storage.prefix(c,'paper_risk:')=={}
        assert storage.prefix(c,'position:')=={}
        assert not c.execute(select(discord_jobs)).first()
        assert not c.execute(select(events.c.id).where(events.c.kind=='alert')).first()


def test_no_intrabar_entries_or_reconstructed_earlier_checks(db,clock):
    worker=start(db,clock)
    tick(db,clock,worker,OPEN+64)
    with db.tx() as c:assert c.execute(select(func.count()).select_from(checks)).scalar_one()==0
    tick(db,clock,worker,OPEN+126)
    p=all_arms(db)[1]
    assert p['missed_checks']==1 and p['status']=='waiting' and 'entry' not in p
    with db.tx() as c:
        rows=c.execute(select(checks.c.payload).where(checks.c.arm_id==p['id']).order_by(checks.c.boundary)).scalars().all()
    assert [r['status'] for r in rows]==['missed','blocked']


def test_missing_bar_can_arrive_within_deadline_but_saved_decision_is_immutable(db,clock):
    worker=start(db,clock)
    now=OPEN+66;clock[0]=now;seed(db,now)
    with db.tx() as c:
        original=db.get(c,'bar_window:SPY');db.put(c,'bar_window:SPY',[r for r in original if r[0]!=OPEN])
    worker.tick(now)
    assert all_arms(db)[1]['checked_minute']==0
    tick(db,clock,worker,OPEN+76)
    with db.tx() as c:
        saved=deepcopy(c.execute(select(checks.c.payload)).scalar_one())
        frozen=deepcopy(c.execute(select(days.c.payload)).scalar_one()['frozen'])
    tick(db,clock,worker,OPEN+80,inside=True)
    with db.tx() as c:
        assert c.execute(select(checks.c.payload)).scalar_one()==saved
        assert c.execute(select(days.c.payload)).scalar_one()['frozen']==frozen


def test_data_block_is_not_a_no_signal_or_a_trade(db,clock):
    worker=start(db,clock);clock[0]=OPEN+66;seed(db,clock[0])
    with db.tx() as c:
        rows=db.get(c,'bar_window:SPY');db.put(c,'bar_window:SPY',[r for r in rows if r[0]>=OPEN-60*200])
    worker.tick(clock[0])
    with db.tx() as c:
        p=c.execute(select(checks.c.payload)).scalar_one()
        assert p['status']=='blocked' and 'premarket_coverage_incomplete' in p['data_blocks']
    for minute in range(2,61):tick(db,clock,worker,OPEN+minute*60+6)
    assert all(p['status']=='data_blocked' for p in all_arms(db).values())


def test_no_signal_day_requires_every_check_and_full_session_capture(db,clock):
    worker=start(db,clock)
    for minute in range(1,61):tick(db,clock,worker,OPEN+minute*60+6,inside=True)
    ps=all_arms(db)
    assert all(p['status']=='no_signal' for p in ps.values())
    assert {tf:p['checks'] for tf,p in ps.items()}=={1:60,3:20,5:12,15:4}
    with db.tx() as c:
        r=report(db,c,OPEN+3610)
        assert all(x['matched_days']==1 for x in r['paired'] if x['cohort']=='evaluation')
        assert all(x['results'] is None for x in r['paired'] if x['cohort']=='evaluation')


def test_midday_activation_is_warmup_unobserved_not_zero_return(db,clock):
    start(db,clock,OPEN+7200)
    assert all(p['cohort']=='warmup' and p['status']=='not_observed' for p in all_arms(db).values())


def test_restart_and_entire_missed_session_are_accounted_without_backfill(db,clock):
    worker=start(db,clock)
    tick(db,clock,worker,OPEN+66,inside=True)
    # Expire lease as a real restart would; no risk or research state reset.
    clock[0]=OPEN+7200
    from compass.store import leases
    with db.tx() as c:c.execute(update(leases).where(leases.c.key==VERSION).values(until=0))
    Study(db,cfg()).tick(clock[0])
    assert all(p['status']=='data_blocked' and p['missed_checks']>0 for p in all_arms(db).values())
    with db.tx() as c:
        a=db.get(c,VERSION+':activation');last=a['sessions'][-1]
    clock[0]=last['close']+60
    with db.tx() as c:c.execute(update(leases).where(leases.c.key==VERSION).values(until=0))
    Study(db,cfg()).tick(clock[0])
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(arms)).scalar_one()==80
        assert c.execute(select(func.count()).select_from(checks)).scalar_one()==20*(60+20+12+4)
        assert report(db,c,clock[0])['evaluation_locked'] is False


def open_option(db,clock):
    worker=start(db,clock);tick(db,clock,worker,OPEN+66)
    clock[0]=OPEN+68;p=all_arms(db)[1]
    symbol='O:SPY260916C00761000'
    with db.tx() as c:
        for delta in (25,20,15,10,5,0):
            for name,quote in [('SPY',q(clock[0]-delta,760.99,761.01)),(symbol,q(clock[0]-delta,2,2.05))]:
                archive(db,c,name,quote)
        db.put(c,'quote:SPY',q(clock[0],760.99,761.01));db.put(c,'quote:'+symbol,q(clock[0],2,2.05))
        Paper(db,cfg()).pending(c,p,clock[0])
    return p,symbol


def test_research_entry_uses_actual_quotes_without_a_fake_delivery_and_closes_after_costs(storage,clock):
    p,symbol=open_option(storage,clock)
    assert p['status']=='open' and 'delivery' not in p
    assert p['entry']==2.06 and p['entry_delay_after_delivery'] is None
    with storage.tx() as c:
        assert stream_requests(c,clock[0],'open')==[symbol]
        Paper(storage,cfg()).observe_pair(c,p,clock[0]+5,q(clock[0]+5,3,3.05),q(clock[0]+5,763,763.02))
        assert p['status']=='closed' and p['exit_reason']=='underlying_target'
        assert p['pnl']==pytest.approx((2.99-2.06)*100-1.30)
        assert c.execute(select(func.count()).select_from(marks)).scalar_one()==2
        from compass.store import spy_options,spy_marks
        assert not c.execute(select(spy_options)).first() and not c.execute(select(spy_marks)).first()
        assert not c.execute(select(discord_jobs)).first()


def test_quote_gap_remains_unresolved_instead_of_winning_on_a_later_quote(db,clock):
    p,symbol=open_option(db,clock)
    with db.tx() as c:
        Paper(db,cfg()).observe_pair(c,p,clock[0]+20,q(clock[0]+20,5,5.05),q(clock[0]+20,763,763.02))
        assert p['status']=='unresolved' and p['pnl'] is None


def test_recovery_includes_open_timeframe_contracts_without_widening_request_budget(db,clock):
    p,symbol=open_option(db,clock)
    collector=SimpleNamespace(db=db,option_recovery_attempts={})
    selected,waiting,truncated=targets(collector,clock[0]+3)
    assert selected==[symbol] and waiting==1 and not truncated


def test_paired_denominator_excludes_any_unresolved_arm_and_unlocks_only_at_fixed_end(db,clock):
    worker=start(db,clock);tick(db,clock,worker,OPEN+66,inside=True)
    with db.tx() as c:
        for p in c.execute(select(arms.c.payload)).scalars():
            p.update(status='closed',pnl=10,return_pct=5,best_pct=8,worst_pct=-2,
                opened_at=OPEN+70,finished_at=OPEN+100,time_bucket='first_15',gap_regime='flat')
            Paper(db,cfg()).save(c,p,OPEN+100)
        a=db.get(c,VERSION+':activation');end=a['evaluation_end']
        locked=report(db,c,end-1)
        assert all(x['results'] is None for x in locked['groups'] if x['cohort']=='evaluation')
        assert locked['cuts']==[] and locked['winner'] is None
        done=report(db,c,end)
        assert all(x['matched_days']==1 and x['mean_daily_net']==10 for x in done['paired'] if x['cohort']=='evaluation')
        p=c.execute(select(arms.c.payload).where(arms.c.timeframe==1)).scalar_one()
        p.update(status='unresolved',pnl=None);Paper(db,cfg()).save(c,p,end)
        assert all(x['matched_days']==0 and x['mean_daily_net'] is None for x in report(db,c,end)['paired'] if x['cohort']=='evaluation')


def test_full_audit_denominator_exceeds_page_limit(db,clock):
    worker=start(db,clock)
    for minute in range(1,61):tick(db,clock,worker,OPEN+minute*60+6,inside=True)
    with db.tx() as c:
        first=records(c,clock[0],limit=10)
        assert first['total']==96 and len(first['records'])==10 and first['has_more']
        assert records(c,clock[0],timeframe=15)['total']==4
        with pytest.raises(ValueError):records(c,clock[0],timeframe=2)


def test_metric_drawdown_and_giveback_are_not_just_win_rate():
    rows=[dict(id=str(i),day=str(i),status='closed',pnl=x,return_pct=x,best_pct=max(0,x)+5,
        worst_pct=min(0,x),opened_at=10,finished_at=20) for i,x in enumerate((10,-20,5))]
    m=metrics(rows)
    assert m['net_pnl']==-5 and m['closed_equity_drawdown']==20
    assert m['profit_factor']==.75 and m['mean_giveback_pct_points']>5


def test_timeframe_api_is_authenticated_bounded_and_read_only(tmp_path):
    from fastapi.testclient import TestClient
    from compass.app import create_app
    config=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'api.db'),
        password='fixture-password-16',secret='fixture-secret')
    app=create_app(config)
    with TestClient(app) as client:
        assert client.get('/api/spy-timeframes/records').status_code==401
        client.post('/login',json={'password':config.password})
        assert client.get('/api/spy-timeframes/records?timeframe=2').status_code==400
        assert client.get('/api/spy-timeframes/records?limit=101').status_code==422
        assert client.get('/api/spy-timeframes/records?offset=-1').status_code==422
        response=client.get('/api/spy-timeframes/records')
        assert response.json()['total']==0 and 'no-store' in response.headers['cache-control']
        with app.state.db.tx() as c:
            assert app.state.db.get(c,VERSION+':activation') is None
            assert not c.execute(select(arms)).first()


def test_session_bootstrap_respects_holdout_minimum_and_paired_differences():
    from compass.spy_timeframe_report import differences
    rows=[dict(day=str(d),timeframe=tf,pnl=d+(2 if tf==1 else 0)) for d in range(10) for tf in TIMEFRAMES]
    common={str(d) for d in range(10)}
    assert all(r['interval'] is None for r in differences(rows,common,False))
    assert all(r['interval'] is None for r in differences(rows,{'0'},True))
    values=differences(rows,common,True)
    assert values[0]['mean_difference']==2 and values[0]['interval']==[2,2]
    assert values[1]['interval']==[0,0]
