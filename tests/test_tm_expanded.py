import asyncio
import pytest
from compass.store import Store
from compass.config import Config
from compass.providers import Collectors
from compass.research import Feed, collect, catalog, scheduled_feeds
from compass.tm_expanded import expanded_feeds

@pytest.fixture
def db(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'expanded.db'))
    store.initialize()
    yield store
    store.engine.dispose()


NOW = 1790344800.


def test_expansion_is_bounded_and_separates_expiration_from_time_history():
    rows=expanded_feeds(Feed,['SPY','DJT','TSLA','SPY'])
    assert len({r.key for r in rows})==len(rows)
    assert all(r.research_only for r in rows)
    assert not any('DJT' in r.path for r in rows)
    assert any(r.path=='/gex/SPY/apex/evolution' for r in rows)
    assert any(r.path=='/gex/SPY/apex/history' for r in rows)
    assert any(r.path=='/screeners/accumulation-watch/run' for r in rows)


def test_background_retained_without_promotion_and_budget_survives_worker(db):
    async def run():
        cfg=Config(local=True,stocks=('SPY',),matrix='fixture')
        worker=Collectors(db,cfg)
        with db.tx() as c:
            for f in scheduled_feeds(db,c,cfg,NOW):
                if f.key not in ('extra_accumulation','extra_event_odds'):
                    db.put(c,'research_job:'+f.key,{'attempted_at':NOW})
        calls=[]
        async def request(path,key):
            calls.append(key)
            return {'computedAt':'2026-09-25T14:00:00Z','results':[{'symbol':'SPY','score':99}]},NOW
        worker.matrix_request=request
        await collect(worker,NOW)
        await collect(worker,NOW+1)
        assert len(calls)==1
        await collect(worker,NOW+30)
        assert len(calls)==2
        with db.tx() as c:
            assert db.get(c,'research:extra_accumulation')['research_only']
            assert not db.prefix(c,'research_matches:') and not db.prefix(c,'focus:')
            assert not db.recent(c,'alert')
            rows=[r for r in catalog(db,c,cfg,NOW+30) if r['research_only']]
            assert rows and all(not r['eligible_for_live_confirmation'] for r in rows)
        await worker.close()
    asyncio.run(run())
