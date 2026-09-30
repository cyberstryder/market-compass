"""swing20-v1: a completed daily close above the preceding 20 daily highs."""
from datetime import datetime
import pytest
from compass.config import Config
from compass.engine import Engine
from compass.store import Store
from compass.market import CT

NOW=datetime(2026,9,14,9,46,tzinfo=CT).timestamp()  # Monday cash session
DAY=86400

@pytest.fixture
def cfg():
    return Config(local=True,risk=100,paper_trading=True,stock_paper_trades=True,stocks=('SPY',),
                  futures=('MES.c.0',),extra_futures=(),databento='',
                  alpaca_key='',alpaca_secret='',matrix='',massive='',
                  discord='',openai='')

@pytest.fixture
def db(tmp_path):
    s=Store('sqlite:///'+str(tmp_path/'swing.db'))
    s.initialize()
    yield s
    s.engine.dispose()

def bars(last_close,last_ts=NOW-DAY,count=22):
    """22 daily bars, oldest first; prior bars peak at 100."""
    rows=[]
    for i in range(count):
        ts=last_ts-(count-1-i)*DAY
        if i==count-1:
            rows.append(dict(ts=ts,payload=dict(o=100,h=last_close+1,l=99,c=last_close,v=1000)))
        else:
            rows.append(dict(ts=ts,payload=dict(o=94,h=100,l=90,c=95,v=1000)))
    return rows

def load(db,c,rows):
    for i,r in enumerate(rows):
        db.append(c,'daily','test','SPY',r['ts'],r['payload'],key='daily:%d'%i)

def test_breakout_opens_swing_and_marks_seen(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars(101))
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        p=db.get(c,'position:SPY')
        assert p and p['status']=='open'
        assert p['strategy']=='swing20-v1' and p['track']=='swing' and p['side']=='long'
        assert p['signal_price']==101
        seen=[k for k in db.prefix(c,'seen:')]
        assert len(seen)==1
        # A second sweep does not duplicate the signal.
        e.swings(c,NOW+60)
        assert db.get(c,'position:SPY')['id']==p['id']
        assert len(db.prefix(c,'seen:'))==1

def test_no_breakout_no_signal(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars(99))
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.get(c,'position:SPY') is None
        assert db.prefix(c,'seen:')=={}

def test_close_equal_to_prior_high_is_not_a_breakout(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars(100))
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.get(c,'position:SPY') is None

def test_insufficient_history_no_signal(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars(101)[:21])
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.get(c,'position:SPY') is None

def test_stale_last_bar_no_signal(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars(101,last_ts=NOW-6*DAY))
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.get(c,'position:SPY') is None
        assert db.prefix(c,'seen:')=={}
