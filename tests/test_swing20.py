"""swing-options-v1: pullback after 20-day breakout, traded via options.

Changed 2026-10-17: Was swing20-v1 (stock breakout, created stock positions).
Now swing-options-v1 (options pullback-after-breakout, creates options signals).
Stock positions are NOT created — options only per Josh's direction.
"""
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
                  discord='',)

@pytest.fixture
def db(tmp_path):
    s=Store('sqlite:///'+str(tmp_path/'swing.db'))
    s.initialize()
    yield s
    s.engine.dispose()

def bars_with_pullback():
    """24 daily bars: 20 flat, breakout on day 21, pullback on day 22-23."""
    rows=[]
    # Days 0-19: flat, high 100, close 95
    for i in range(20):
        ts=NOW-(24-1-i)*DAY
        rows.append(dict(ts=ts,payload=dict(o=94,h=100,l=90,c=95,v=1000)))
    # Day 20: breakout, close 101 above 20-day high 100
    ts=NOW-(24-1-20)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100,h=102,l=99,c=101,v=1000)))
    # Day 21: pullback to breakout level (100.5, within 1% of 100)
    ts=NOW-(24-1-21)*DAY
    rows.append(dict(ts=ts,payload=dict(o=101,h=101,l=100,c=100.5,v=1000)))
    # Day 22: still near breakout level
    ts=NOW-(24-1-22)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100.5,h=101,l=99.5,c=100.2,v=1000)))
    # Day 23: today, price 100.3 (pullback continues)
    ts=NOW-(24-1-23)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100.2,h=100.8,l=99.8,c=100.3,v=1000)))
    return rows

def bars_breakout_no_pullback():
    """22 daily bars: 21 flat, breakout on last bar, no pullback after."""
    rows=[]
    for i in range(21):
        ts=NOW-(22-1-i)*DAY
        rows.append(dict(ts=ts,payload=dict(o=94,h=100,l=90,c=95,v=1000)))
    # Last bar: breakout, close 101
    ts=NOW
    rows.append(dict(ts=ts,payload=dict(o=100,h=102,l=99,c=101,v=1000)))
    return rows

def load(db,c,rows):
    for i,r in enumerate(rows):
        db.append(c,'daily','test','SPY',r['ts'],r['payload'],key='daily:%d'%i)

def test_breakout_without_pullback_no_signal(db,cfg):
    """Breakout alone does not trigger — need pullback after breakout."""
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars_breakout_no_pullback())
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        # No stock position (options only)
        assert db.get(c,'position:SPY') is None
        # No signal marked as seen (no pullback yet)
        assert db.prefix(c,'seen:')=={}

def test_breakout_with_pullback_marks_seen(db,cfg):
    """Breakout + pullback marks signal as seen (options signal, not stock position)."""
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars_with_pullback())
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        # No stock position (options only — signal goes through options(), not enter())
        assert db.get(c,'position:SPY') is None
        # Signal marked as seen
        seen=[k for k in db.prefix(c,'seen:')]
        assert len(seen)==1
        # A second sweep does not duplicate the signal.
        e.swings(c,NOW+60)
        assert len(db.prefix(c,'seen:'))==1

def test_no_breakout_no_signal(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        # No breakout: all closes below 100
        rows=[]
        for i in range(22):
            ts=NOW-(22-1-i)*DAY
            rows.append(dict(ts=ts,payload=dict(o=94,h=100,l=90,c=95,v=1000)))
        load(db,c,rows)
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.get(c,'position:SPY') is None
        assert db.prefix(c,'seen:')=={}

def test_stale_pullback_no_signal(db,cfg):
    """Code-health 2026-10-29: a pullback 2 bars ago must NOT fire once the
    latest completed bar has run away from the level. Before the fix, the
    stale pullback bar's close became signal_price."""
    e=Engine(db,cfg)
    rows=[]
    for i in range(20):
        ts=NOW-(24-i)*DAY
        rows.append(dict(ts=ts,payload=dict(o=94,h=100,l=90,c=95,v=1000)))
    ts=NOW-(24-20)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100,h=102,l=99,c=101,v=1000)))  # breakout
    ts=NOW-(24-21)*DAY
    rows.append(dict(ts=ts,payload=dict(o=101,h=101,l=100,c=100.5,v=1000)))  # pullback (stale)
    ts=NOW-(24-22)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100.5,h=101,l=99.5,c=100.4,v=1000)))
    ts=NOW-(24-23)*DAY
    rows.append(dict(ts=ts,payload=dict(o=105,h=107,l=104,c=106,v=1000)))  # latest: ran away
    with db.tx() as c:
        load(db,c,rows)
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.prefix(c,'seen:')=={}

def test_fresh_pullback_on_latest_bar_fires(db,cfg):
    """The fix must not kill the legitimate case: the only pullback is on
    the latest completed bar."""
    e=Engine(db,cfg)
    rows=[]
    for i in range(20):
        ts=NOW-(24-i)*DAY
        rows.append(dict(ts=ts,payload=dict(o=94,h=100,l=90,c=95,v=1000)))
    ts=NOW-(24-20)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100,h=102,l=99,c=101,v=1000)))  # breakout
    ts=NOW-(24-21)*DAY
    rows.append(dict(ts=ts,payload=dict(o=103,h=104.5,l=102,c=104,v=1000)))  # ran up, no pullback
    ts=NOW-(24-22)*DAY
    rows.append(dict(ts=ts,payload=dict(o=103.5,h=104,l=102.5,c=103.5,v=1000)))  # still no pullback
    ts=NOW-(24-23)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100.6,h=101,l=99.8,c=100.4,v=1000)))  # latest: pullback
    with db.tx() as c:
        load(db,c,rows)
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert len(db.prefix(c,'seen:'))==1

def test_at_most_one_signal_per_symbol_per_tick(db,cfg):
    """Two breakout bars (one bullish, one bearish) both match the latest
    bar's pullback tests; only the first may fire in a single tick."""
    e=Engine(db,cfg)
    rows=[]
    for i in range(20):
        ts=NOW-(25-i)*DAY
        rows.append(dict(ts=ts,payload=dict(o=100.5,h=101,l=100,c=100.5,v=1000)))
    ts=NOW-(25-20)*DAY
    rows.append(dict(ts=ts,payload=dict(o=101,h=102.5,l=99.0,c=102,v=1000)))  # bullish breakout, level 101
    ts=NOW-(25-21)*DAY
    rows.append(dict(ts=ts,payload=dict(o=101.5,h=102,l=100.3,c=100.8,v=1000)))
    ts=NOW-(25-22)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100.7,h=101,l=100.1,c=100.6,v=1000)))
    ts=NOW-(25-23)*DAY
    rows.append(dict(ts=ts,payload=dict(o=99.5,h=100,l=98.5,c=98.9,v=1000)))  # bearish breakdown, level 99.0
    ts=NOW-(25-24)*DAY
    rows.append(dict(ts=ts,payload=dict(o=100.8,h=101.2,l=100.4,c=100.9,v=1000)))  # latest: within 1% of 101 and of the 20 SMA
    with db.tx() as c:
        load(db,c,rows)
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert len(db.prefix(c,'seen:'))==1

def test_insufficient_history_no_signal(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        load(db,c,bars_with_pullback()[:21])
        db.put(c,'quote:SPY',dict(ts=NOW,bid=100,ask=100.10,bid_size=10,ask_size=10))
        e.swings(c,NOW)
        assert db.get(c,'position:SPY') is None
