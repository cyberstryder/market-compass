"""Store.prefix range-bound semantics (code-health 2026-10-29).

prefix() used to be a LIKE 'prefix%' scan. It is now key range bounds
(>= prefix, < prefix+'\\uffff'), which must equal plain str.startswith
semantics for every key that does not contain '\\uffff' — including the
cases LIKE got wrong (an '_' in the prefix acted as a wildcard) and the
boundary cases (the bare prefix as a key, a longer stem sharing it).
"""
import pytest
from compass.store import Store

@pytest.fixture
def db(tmp_path):
    s=Store('sqlite:///'+str(tmp_path/'prefix.db'))
    s.initialize()
    yield s
    s.engine.dispose()

KEYS={
    'position:SPY':1,'position:MES.c.0':2,'position:':3,
    'positions:SPY':4,'position':5,
    'trade:1':6,'trade:ict-9':7,'trade:':8,'trade_1':9,
    'quote:SPY':10,'a_b:x':11,'aXb:x':12,'ab:x':13,
    'health:engine':14,'seen:abc':15,
}

def seed(db):
    with db.tx() as c:
        for k,v in KEYS.items():
            db.put(c,k,v)

def test_prefix_equals_startswith_on_edge_corpus(db):
    seed(db)
    with db.tx() as c:
        for p in ('position:','position','positions:','trade:','trade',
                  'quote:','health:','seen:','a_b','a','ab:',''):
            got=db.prefix(c,p)
            want={k:v for k,v in KEYS.items() if k.startswith(p)}
            assert got==want, p

def test_prefix_underscore_is_literal_not_wildcard(db):
    # LIKE 'a_b%' would also match 'aXb:x'; range bounds must not.
    seed(db)
    with db.tx() as c:
        assert db.prefix(c,'a_b')=={'a_b:x':11}

def test_prefix_absent_returns_empty(db):
    seed(db)
    with db.tx() as c:
        assert db.prefix(c,'nope:')=={}
        assert db.prefix(c,'position:ZZZ')=={}

def test_recent_trades_bounded_newest_first(db):
    with db.tx() as c:
        for i in range(10):
            db.put(c,f'trade:t{i}',{'id':f't{i}','entered_at':1000.0+i})
        db.put(c,'trade:no_ts',{'id':'no_ts'})  # missing entered_at counts as 0
        db.put(c,'position:SPY',{'id':'not-a-trade','entered_at':99999.0})
        got=db.recent_trades(c,limit=3)
        assert [t['id'] for t in got]==['t9','t8','t7']
        got=db.recent_trades(c,limit=100)
        assert [t['id'] for t in got]==[f't{i}' for i in range(9,-1,-1)]+['no_ts']
        assert all(t['id']!='not-a-trade' for t in got)
