"""Premium-paid R accounting for long options.

A long option's worst case is the premium paid, not the modeled stop
distance (0DTE selection sets stop_distance = 30% of ask, so the old
stop-distance accounting recorded a full premium loss as ~-3.3R).
initial_risk for options is now premium + round-trip fees, so a full
premium loss reads as exactly -1R. Futures and stocks keep the
stop-distance basis.
"""
from datetime import datetime
import pytest
from sqlalchemy import select
from compass.config import Config
from compass.engine import Engine, spec
from compass.setup_study import SetupStudy, trials
from compass.store import Store
from compass.market import CT

NOW=datetime(2026,9,14,9,46,tzinfo=CT).timestamp()

@pytest.fixture
def cfg():
    return Config(local=True,risk=100,paper_trading=True,stock_paper_trades=True,stocks=('SPY',),
                  futures=('MES.c.0',),extra_futures=(),databento='',
                  alpaca_key='',alpaca_secret='',matrix='',massive='',
                  discord='',openai='')

@pytest.fixture
def db(tmp_path):
    s=Store('sqlite:///'+str(tmp_path/'risk.db'))
    s.initialize()
    yield s
    s.engine.dispose()

def quote(now=NOW,bid=100,ask=100.25):
    return dict(ts=now,bid=bid,ask=ask,bid_size=10,ask_size=10)

def rows(c):
    return c.execute(select(trials.c.payload).order_by(trials.c.source_id)).scalars().all()

def option_signal(id='opt'):
    return dict(id=id,symbol='O:TEST',strategy='0dte-fixture',side='long',
                signal_time=NOW,signal_price=2.0,stop_distance=0.5,track='intraday')

def test_option_trial_full_premium_loss_is_minus_one_r(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:O:TEST',quote(NOW,2.0,2.02))
        study.start(c,option_signal(),NOW,spec('O:TEST'))
        p=rows(c)[0]
        assert p['status']=='open'
        assert p['risk_basis']=='premium_paid'
        # entry is ask + one adverse tick: 2.03.
        assert p['entry']==pytest.approx(2.03)
        assert p['initial_risk']==pytest.approx(2.03*100+2*.65)
        # The option expires worthless: the full premium loss is -1R.
        study.finish(c,p,NOW+60,'expired',price=0,quote_ts=NOW+60)
        p=rows(c)[0]
        assert p['pnl']==pytest.approx(-p['initial_risk'])
        assert p['r_multiple']==pytest.approx(-1)

def test_option_trial_win_scales_off_premium(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:O:TEST',quote(NOW,2.0,2.02))
        study.start(c,option_signal(),NOW,spec('O:TEST'))
        p=rows(c)[0]
        # Target is entry + 2x the modeled stop distance, but R is premium-based.
        study.finish(c,p,NOW+60,'target',price=p['target'],quote_ts=NOW+60)
        p=rows(c)[0]
        assert p['r_multiple']==pytest.approx(p['pnl']/p['initial_risk'])
        assert p['r_multiple'] < 2  # 2R on stop distance is far less on premium

def test_future_trial_keeps_stop_distance_basis(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,dict(id='f',symbol='MESZ6@1',strategy='study-fixture',side='long',
                           signal_time=NOW,signal_price=100,stop_distance=2,track='intraday'),
                    NOW,spec('MESZ6@1'))
        p=rows(c)[0]
        assert p['risk_basis']=='stop_distance'
        assert p['initial_risk']==pytest.approx(13)

def test_engine_option_enter_uses_premium_basis(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:O:TEST',quote(NOW,2.0,2.02))
        sig={**option_signal(),'symbol':'O:TEST'}
        assert e.enter(c,sig,NOW)
        p=db.get(c,'position:O:TEST')
        assert p['status']=='open' and p['qty']==1
        assert p['risk_basis']=='premium_paid'
        assert p['initial_risk']==pytest.approx(p['entry']*100+2*.65)

def test_engine_stock_enter_keeps_stop_distance_basis(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:SPY',quote(NOW,100,100.10))
        sig=dict(id='s',symbol='SPY',strategy='study-fixture',side='long',
                 signal_time=NOW,signal_price=100,stop_distance=1,track='intraday')
        assert e.enter(c,sig,NOW)
        p=db.get(c,'position:SPY')
        assert p['risk_basis']=='stop_distance'
        # per_unit = stop distance x multiplier + fees + slippage padding.
        assert p['initial_risk']==pytest.approx((1*1+0+2*.01*1)*p['qty'])
