"""Research coverage must survive crowded paper holdings and slow chain turns."""
import asyncio
from types import SimpleNamespace

from compass.config import Config
from compass.providers import Collectors
from compass.store import Store
from compass.universe import focus_symbols


def test_index_research_survives_crowded_paper_focus(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'focus.db'))
    db.initialize()
    names=[f'NAME{i}' for i in range(20)]
    cfg=SimpleNamespace(watch_symbols=('SPY','QQQ','IWM',*names),stocks=('SPY','QQQ','IWM'))
    try:
        with db.tx() as c:
            for i,symbol in enumerate(names):
                db.put(c,f'position:{i}',dict(status='open',asset='stock',symbol=symbol))
                db.put(c,'focus:'+symbol,dict(symbol=symbol,priority=100,at=1000))
            focused=focus_symbols(db,c,cfg,1000,12)
            assert focused[:3]==['SPY','QQQ','IWM']
            assert len(focused)==12
            assert db.get(c,'position:0')['status']=='open'
    finally:
        db.engine.dispose()


def test_slow_chain_refresh_rotates_oldest_due_focus(tmp_path,monkeypatch):
    db=Store('sqlite:///'+str(tmp_path/'chains.db'))
    db.initialize()
    clock=[10000.]
    monkeypatch.setattr('compass.providers.time.time',lambda:clock[0])
    monkeypatch.setattr('compass.providers.focus_symbols',lambda *args:['SPY','QQQ','IWM'])
    async def run():
        collector=Collectors(db,Config(local=True,stocks=('SPY','QQQ','IWM'),massive='fixture'))
        calls=[]
        async def chain(symbol):
            calls.append(symbol)
            clock[0]+=60  # Each HTTP chain takes longer than the refresh target.
            return [],True
        collector.massive_chain=chain
        try:
            with db.tx() as c:
                for symbol in ('SPY','QQQ','IWM'):
                    db.put(c,'chain:'+symbol,dict(asof=clock[0]-100,contracts=[]))
            for _ in range(3):
                await collector.chains()
            assert calls[:3]==['SPY','QQQ','IWM']
        finally:
            await collector.close()
    try:
        asyncio.run(run())
    finally:
        db.engine.dispose()


def test_slow_carried_background_does_not_hold_index_lane(tmp_path,monkeypatch):
    db=Store('sqlite:///'+str(tmp_path/'lanes.db'));db.initialize()
    monkeypatch.setattr('compass.providers.focus_symbols',lambda *a:['SPY','AAA'])
    monkeypatch.setattr('compass.swing_ideas.active_underlyings',lambda c:['OLD'])
    async def run():
        collector=Collectors(db,Config(local=True,stocks=('SPY','AAA','OLD'),massive='fixture'))
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def chain(symbol):
            calls.append(symbol)
            if symbol=='OLD':
                entered.set();await release.wait()
            return [],True
        collector.massive_chain=chain
        background=asyncio.create_task(collector.chains('background'))
        try:
            await asyncio.wait_for(entered.wait(),2)
            await asyncio.wait_for(collector.chains('indices'),2)
            assert calls==['OLD','SPY']
            assert not background.done()
            with db.tx() as c:assert db.get(c,'chain:SPY')['asof']>0
        finally:
            release.set();await background;await collector.close()
    try:asyncio.run(run())
    finally:db.engine.dispose()
