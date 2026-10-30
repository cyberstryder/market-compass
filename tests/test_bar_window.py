"""Bar-window cache equivalence (code-health bundle 2, 2026-10-30).

Engine._bar_rows must return EXACTLY
dedup(db.recent(c,"bar",symbol,limit=8000,since=now-8*86400),now)
on the same recorded bar set: cold start, incremental appends,
duplicate-ts bars (higher id wins), late old-ts bars, the 8-day window
slide, the 8000-row cap, the increment-overflow fallback, and a
poisoned cache entry self-healing via the full fetch.
"""
import pytest
from compass.config import Config
from compass.engine import Engine
from compass.market import dedup
from compass.store import Store

BASE=1_700_000_000.0

@pytest.fixture
def cfg():
    return Config(local=True,risk=100,paper_trading=True,stock_paper_trades=True,stocks=('SPY',),
                  futures=('MES.c.0',),extra_futures=(),databento='',
                  alpaca_key='',alpaca_secret='',matrix='',massive='',
                  discord='',)

@pytest.fixture
def db(tmp_path):
    s=Store('sqlite:///'+str(tmp_path/'bars.db'))
    s.initialize()
    yield s
    s.engine.dispose()

def seed(db,symbol,rows):
    with db.tx() as c:
        for ts,close in rows:
            db.append(c,"bar","test",symbol,ts,dict(o=close,h=close,l=close,c=close,v=1))

def reference(db,symbol,now):
    with db.tx() as c:
        return dedup(db.recent(c,"bar",symbol,limit=8000,since=now-8*86400),now)

def cached(eng,db,symbol,now):
    with db.tx() as c:
        return eng._bar_rows(c,symbol,now)

def sig(rows):
    return [(r["id"],r["ts"],r["payload"]) for r in rows]

def test_cold_start_and_increments_match_reference(db,cfg):
    eng=Engine(db,cfg)
    seed(db,'SPY',[(BASE+i*60,100.0+i) for i in range(30)])
    now=BASE+30*60+3600
    assert sig(cached(eng,db,'SPY',now))==sig(reference(db,'SPY',now))
    # New bar, a duplicate-ts bar (higher id must win in dedup), and a
    # late-arriving bar with an old ts landing between existing bars.
    seed(db,'SPY',[(BASE+30*60,999.0),(BASE+29*60,555.0),(BASE+5*60+30,777.0)])
    now2=now+120
    assert sig(cached(eng,db,'SPY',now2))==sig(reference(db,'SPY',now2))
    # Pure cache hit, nothing appended since.
    assert sig(cached(eng,db,'SPY',now2+60))==sig(reference(db,'SPY',now2+60))

def test_window_slide_evicts_on_incremental_path(db,cfg):
    eng=Engine(db,cfg)
    now0=BASE+8*86400
    # One bar just inside the 8-day window, two recent bars.
    seed(db,'SPY',[(now0-8*86400+600,1.0),(now0-120,2.0),(now0-60,3.0)])
    assert sig(cached(eng,db,'SPY',now0))==sig(reference(db,'SPY',now0))
    # Advance 1200s (< 3600 cache age, so the incremental path runs):
    # the boundary bar falls out of [now-8d, ...) in both.
    now1=now0+1200
    assert sig(cached(eng,db,'SPY',now1))==sig(reference(db,'SPY',now1))
    assert all(r["ts"]>=now1-8*86400 for r in cached(eng,db,'SPY',now1))

def test_cap_8000_rows(db,cfg):
    eng=Engine(db,cfg)
    seed(db,'MSFT',[(BASE+i*60,50.0) for i in range(8100)])
    now=BASE+8100*60+3600
    got=cached(eng,db,'MSFT',now)
    assert sig(got)==sig(reference(db,'MSFT',now))
    assert len(got)==8000
    seed(db,'MSFT',[(BASE+8100*60,51.0),(BASE+8101*60,52.0),(BASE+8102*60,53.0)])
    now2=now+180
    assert sig(cached(eng,db,'MSFT',now2))==sig(reference(db,'MSFT',now2))

def test_increment_overflow_falls_back_to_full_fetch(db,cfg):
    eng=Engine(db,cfg)
    seed(db,'QQQ',[(BASE+i*60,10.0) for i in range(10)])
    now=BASE+10*60+3600
    assert sig(cached(eng,db,'QQQ',now))==sig(reference(db,'QQQ',now))
    # 2005 appends exceed the 2000-row increment limit: the cache must
    # abandon the merge and rebuild from a full fetch, still exact.
    seed(db,'QQQ',[(BASE+(10+i)*60,11.0) for i in range(2005)])
    now2=now+600
    assert sig(cached(eng,db,'QQQ',now2))==sig(reference(db,'QQQ',now2))

def test_poisoned_cache_self_heals(db,cfg):
    eng=Engine(db,cfg)
    seed(db,'SPY',[(BASE+i*60,100.0) for i in range(5)])
    now=BASE+5*60+3600
    eng._bar_cache['SPY']={'rows':[{'ts':'bad','id':1,'payload':{}}],
                           'max_id':1,'built':now}
    assert sig(cached(eng,db,'SPY',now))==sig(reference(db,'SPY',now))
