from datetime import datetime
import pytest
from sqlalchemy import select
from compass.app import create_app
from compass.store import Store,state
from compass.market import NY
from compass.config import Config
from compass.engine import Engine
from compass.weekday_candidates import capture,report,PREFIX,watchlist

NOW=datetime(2026,9,22,11,0,tzinfo=NY).timestamp()

@pytest.fixture
def db(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'weekday.db'));store.initialize()
    yield store
    store.engine.dispose()


def signal():
    return dict(id='test-source',symbol='SPY',side='long',rule='trend_pullback',strategy='test',
        track='intraday',signal_time=NOW-1,signal_price=100,invalidation=99,stop_distance=1,reason='Price reclaim')


def seed(db,c,stamp=NOW-10,received=NOW-5):
    db.put(c,'research:extra_accumulation',dict(received=received,source_ts=stamp,label='Accumulation',
        items=[dict(symbol='SPY',source_ts=stamp,data={'symbol':'SPY','score':80})]))


def test_freezes_first_context_without_later_rewrite_and_keeps_missing_outcomes(db):
    with db.tx() as c:
        seed(db,c);capture(db,c,signal(),NOW,None)
        seed(db,c,stamp=NOW+10,received=NOW+10)
        capture(db,c,signal(),NOW+20,None)
        r=report(db,c,NOW+30)
        assert r['total']==1
        p=r['records'][0]
        assert p['context'][0]['received']==NOW-5
        assert p['matched_sources']==['extra_accumulation']
        assert p['stock']['status']==p['option']['status']=='unavailable'
        assert not db.recent(c,'alert')
        assert len(list(c.execute(select(state).where(state.c.key.like(PREFIX+'%')))))==1


@pytest.mark.parametrize('stamp,received',[(None,NOW-5),(NOW-4000,NOW-5),(NOW+5,NOW-5),(NOW-5,NOW+5)])
def test_unknown_stale_future_clocks_cannot_become_current_match(db,stamp,received):
    with db.tx() as c:
        seed(db,c,stamp,received);capture(db,c,signal(),NOW,None)
        p=report(db,c,NOW)['records'][0]
        assert not p['matched_sources']


def test_engine_links_existing_stock_and_option_observations_quietly(db):
    cfg=Config(local=True,stocks=('SPY',),option_ideas=True,setup_study=True)
    engine=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:SPY',dict(ts=NOW,bid=99.99,ask=100.01,bid_size=100,ask_size=100))
        engine.observe_setup(c,signal(),NOW)
        engine.observe_setup(c,signal(),NOW+1)
        r=report(db,c,NOW+1)
        assert r['total']==1
        assert r['records'][0]['stock']['status']=='open'
        assert r['records'][0]['option']['status']=='pending'
        assert not db.recent(c,'alert') and not db.prefix(c,'position:')


def test_vendor_watchlist_has_no_automatic_direction_or_entry(db):
    with db.tx() as c:
        seed(db,c,None)
        p=watchlist(db,c,NOW)['rows'][0]
        assert p['direction']=='Unconfirmed' and p['freshness']=='source_time_unknown'
        assert p['status']=='Awaiting price trigger' and 'entry' not in p
