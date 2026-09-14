from datetime import datetime
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from compass.config import Config
from compass.store import Store
from compass.engine import Engine, spec
from compass.setup_study import SetupStudy, trials, report_for
from compass.market import CT
from compass.futures import selection, futures_session, risk_day
from compass.instruments import future_root, configured
from compass.secondary import contract_code
from compass.app import create_app
from compass.alert_format import message_for

NOW=datetime(2026,9,14,9,46,tzinfo=CT).timestamp()

@pytest.fixture
def cfg():
    return Config(local=True,stocks=('SPY',),futures=('MES.c.0',),extra_futures=(),databento='',
                  alpaca_key='',alpaca_secret='',matrix='',massive='',discord='',openai='')

@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'study.db'))
    db.initialize()
    yield db
    db.engine.dispose()

def quote(now=NOW,bid=100,ask=100.25):
    return dict(ts=now,bid=bid,ask=ask,bid_size=10,ask_size=10)

def signal(id='a',side='long',symbol='MESZ6@1'):
    return dict(id=id,symbol=symbol,strategy='study-fixture',side=side,signal_time=NOW,
                signal_price=100,stop_distance=2,track='intraday')

def rows(c):
    return c.execute(select(trials.c.payload).order_by(trials.c.source_id)).scalars().all()

def test_entry_cap_zero_allows_more_than_ten_without_erasing_history(db,cfg):
    engine=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'risk:'+risk_day(NOW),dict(entries=100,realized=-1))
        db.put(c,'quote:MESZ6@1',quote())
        assert engine.enter(c,signal(),NOW)
        assert db.get(c,'risk:'+risk_day(NOW))['entries']==101
    cfg.max_entries=2
    with db.tx() as c:
        reason,_=engine.entry_check(c,signal(),NOW)
        assert reason=='Configured simulated entry limit'

def test_all_overlapping_setups_survive_portfolio_and_loss_limits(db,cfg):
    engine=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        db.put(c,'risk:'+risk_day(NOW),dict(entries=500,realized=-10000))
        for i in range(15): assert not engine.enter(c,signal(str(i)),NOW)
        assert len(rows(c))==15 and all(p['status']=='open' for p in rows(c))
        assert db.prefix(c,'position:')=={}
        assert db.get(c,'risk:'+risk_day(NOW))==dict(entries=500,realized=-10000)

def test_dedup_does_not_change_frozen_trial_or_cohort(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'),alerted=False,primary=False)
        before=rows(c)[0]
        study.start(c,{**signal(),'stop_distance':10},NOW+1,spec('MESZ6@1'))
        assert rows(c)==[before]

@pytest.mark.parametrize('side',['long','short'])
def test_target_result_capped_at_limit_and_net_costs(db,cfg,side):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(side=side),NOW,spec('MESZ6@1'))
        p=rows(c)[0]
        bid=p['target']+10 if side=='long' else p['target']-10
        db.put(c,'quote:MESZ6@1',quote(NOW+2,bid,bid+.25))
        study.tick(c,NOW+2)
        p=rows(c)[0]
        assert p['exit']==p['target'] and p['exit_reason']=='target'
        assert p['pnl']==17 and p['outcome']=='win'
        assert p['r_multiple']==pytest.approx(17/13)
        message=message_for(db.recent(c,'alert')[0],NOW+2)
        assert 'SETUP RESULT · WIN' in message and 'Not account performance' in message

def test_stop_gap_is_adverse_and_short_is_scored_correctly(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(side='short'),NOW,spec('MESZ6@1'))
        db.put(c,'quote:MESZ6@1',quote(NOW+2,110,110.25))
        study.tick(c,NOW+2)
        p=rows(c)[0]
        assert p['exit']==110.5 and p['pnl']<0 and p['exit_reason']=='stop'

@pytest.mark.parametrize('case',['stale','future','invalidated','missing','spread'])
def test_unusable_entry_excluded_and_never_counted_as_loss(db,cfg,case):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        q=quote(NOW-6 if case=='stale' else NOW+1 if case=='future' else NOW)
        if case=='spread': q['ask']=105
        if case!='missing': db.put(c,'quote:MESZ6@1',q)
        sig=signal()
        if case=='invalidated': sig['invalidation']=100.25
        study.start(c,sig,NOW,spec('MESZ6@1'))
        p=rows(c)[0]
        assert p['status']=='excluded'
        group=report_for(c,NOW)['groups'][0]
        assert group['closed']==group['losses']==0 and group['win_rate'] is None

def test_missing_observation_path_is_unresolved_even_if_price_later_wins(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        db.put(c,'quote:MESZ6@1',quote(NOW+20,120,120.25))
        study.tick(c,NOW+20)
        p=rows(c)[0]
        assert p['status']=='unresolved' and p['pnl'] is None
        assert report_for(c,NOW+20)['groups'][0]['losses']==0

def test_session_exit_and_no_next_session_fills(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        p=rows(c)[0]
        p['flatten_at']=NOW+4
        study.save(c,p)
        db.put(c,'quote:MESZ6@1',quote(NOW+4,101,101.25))
        study.tick(c,NOW+4)
        assert rows(c)[0]['exit_reason']=='session_flatten'
        assert rows(c)[0]['pnl']==pytest.approx(-1.75)

def test_restarts_keep_active_trial_and_completed_results(db,cfg):
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        SetupStudy(db,cfg).start(c,signal(),NOW,spec('MESZ6@1'))
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote(NOW+3,106,106.25))
        SetupStudy(db,cfg).tick(c,NOW+3)
        assert rows(c)[0]['outcome']=='win'

def test_report_separates_contract_strategy_direction_and_alert_cohort(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        for i,(symbol,side,alerted) in enumerate([('MESZ6@1','long',True),('MESZ6@1','short',True),
                ('MESZ6@1','long',False),('MNQZ6@2','long',True)]):
            db.put(c,'quote:'+symbol,quote())
            study.start(c,signal(str(i),side,symbol),NOW,spec(symbol),alerted=alerted)
        assert len(report_for(c,NOW)['groups'])==4

def test_old_signals_and_swing_option_records_do_not_get_fabricated_trials(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,{**signal(),'signal_time':NOW-3600},NOW,spec('MESZ6@1'))
        study.start(c,{**signal('swing'),'track':'swing'},NOW,spec('MESZ6@1'))
        study.start(c,signal('option'),NOW,dict(asset='option'))
        assert len(rows(c))==1 and rows(c)[0]['status']=='excluded'

@pytest.mark.parametrize('symbol,tick,multiplier',[('MGCZ6@1',.1,10),('GCZ6@2',.1,100),
    ('SILZ6@3',.005,1000),('SIZ6@4',.005,5000),('MCLV6@5',.01,100),('CLV6@6',.01,1000)])
def test_additional_futures_specs_and_secondary_contract_identity(symbol,tick,multiplier):
    assert spec(symbol)['asset']=='future' and spec(symbol)['tick']==tick
    assert spec(symbol)['multiplier']==multiplier
    assert contract_code(symbol,NOW)[0]==future_root(symbol)
    assert spec('CL')['asset']=='stock'

def test_volume_mapping_required_and_cannot_reuse_stale_contract_day():
    alias='MGC.v.0'
    assert not selection((alias,),NOW)[0]['resolved']
    mapping={'contract:MGC':dict(configured_symbol=alias,raw_symbol='MGCZ6',trading_day=risk_day(NOW),
        mapping_start=NOW-100,mapping_end=NOW+100)}
    assert selection((alias,),NOW,mapping)[0]['raw_symbol']=='MGCZ6'
    assert not selection((alias,),NOW+101,mapping)[0]['resolved']

def test_metals_do_not_inherit_index_trading_halt():
    now=datetime(2026,9,14,15,20,tzinfo=CT).timestamp()
    assert not futures_session(now,'MESZ6')['is_open']
    assert futures_session(now,'MGCZ6')['is_open']

def test_new_report_endpoints_require_owner_authentication(tmp_path,cfg):
    cfg.role='web';cfg.db='sqlite:///'+str(tmp_path/'app.db')
    cfg.password='test-password-long-enough';cfg.secret='test-secret-long-enough'
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/setup-study').status_code==401
        assert client.get('/api/setup-study/record?id=anything').status_code==401

def test_extra_configuration_keeps_case_and_limits_validated(cfg):
    defaults=Config(local=True)
    assert 'MGC.v.0' in configured(defaults)
    defaults.validate()
    cfg.max_entries=-1
    with pytest.raises(ValueError): cfg.validate()
