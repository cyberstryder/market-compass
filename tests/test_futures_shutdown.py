"""SDK thread shutdown must drain accepted writes before database teardown."""
import asyncio
import sys
import threading
import time
from types import SimpleNamespace
import pytest
from compass.config import Config
from compass.providers import Collectors
from compass.extra_futures import collect_group
from test_quote_coverage import db


@pytest.mark.parametrize('exchange',('CME','COMEX'))
def test_cancellation_waits_for_replay_writer_drain(db,monkeypatch,exchange):
    entered,release,stopped=[threading.Event() for _ in range(3)]
    saved=[];raw='ESZ6' if exchange=='CME' else 'MGCZ6'
    class Bar:
        instrument_id=1;ts_event=int((time.time()-60)*1e9)
        open=high=low=close=100000000000;volume=1
    class Other:pass
    class Live:
        symbology_map={1:raw}
        def __init__(self,**kwargs):pass
        def add_callback(self,fn,exception_callback):self.callback=fn
        def subscribe(self,**kwargs):pass
        def start(self):self.callback(Bar())
        def block_for_close(self):assert stopped.wait(4)
        def stop(self):stopped.set()
        def terminate(self):stopped.set()
    monkeypatch.setitem(sys.modules,'databento',SimpleNamespace(Live=Live,OHLCVMsg=Bar,
        ErrorMsg=Other,SymbolMappingMsg=Other,MBP1Msg=Other,BentoError=RuntimeError))
    def bars(source,rows):entered.set();assert release.wait(4);saved.extend(rows)
    async def run():
        collector=Collectors(db,Config(local=True,futures=('ES.c.0',),databento='fixture'))
        collector.bars=bars
        task=asyncio.create_task(collector.futures() if exchange=='CME'
            else collect_group(collector,'COMEX',['MGC.v.0']))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            task.cancel()
            assert await asyncio.to_thread(stopped.wait,1)
            await asyncio.sleep(.05)
            assert not task.done()  # Application teardown must still wait here.
            release.set()
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
            assert len(saved)==1 and saved[0][0]==raw+'@1'
        finally:
            release.set();stopped.set();task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await collector.close()
    asyncio.run(run())
