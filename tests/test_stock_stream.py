import asyncio
import json
import threading
from types import SimpleNamespace
import pytest
from compass.stock_stream import StockBuffer, StockStreamError, consume

NOW=1789395000


def q(symbol='SPY',i=0):
    return dict(T='q',S=symbol,t=NOW+i/1000,bp=100+i/1000,ap=100.02+i/1000,bs=10,**{'as':10})


def test_quote_bursts_remain_bounded_to_latest_per_symbol():
    b=StockBuffer(['SPY','QQQ'],['SPY'])
    for i in range(20000):
        b.offer(q(i=i))
    b.offer(q(i=1));b.offer(q('FOREIGN',30000))
    quotes,bars=b.take(10)
    assert len(quotes)==1 and quotes[0][1]['ts']==NOW+19.999 and not bars
    assert len(b.last_source)==1 and not b.quotes


def test_sampling_rates_preserve_newest_quote_until_next_write():
    b=StockBuffer(['SPY','QQQ'],['SPY'])
    for symbol in ('SPY','QQQ'):b.offer(q(symbol))
    assert len(b.take(0)[0])==2
    for symbol in ('SPY','QQQ'):b.offer(q(symbol,10))
    assert [symbol for symbol,_,_ in b.take(.25)[0]]==['SPY']
    b.offer(q('QQQ',100))
    assert b.take(.99)[0]==[]
    assert b.take(1)[0][0][1]['ts']==NOW+.1


def test_bars_are_batched_and_corrections_replace_only_same_symbol_minute():
    b=StockBuffer(['SPY','QQQ'],['SPY'])
    bar=dict(T='b',S='SPY',t=NOW,o=100,h=102,l=99,c=101,v=500)
    b.offer(bar);b.offer({**bar,'T':'u','c':102});b.offer({**bar,'S':'QQQ'})
    b.offer({**bar,'t':NOW+60})
    quotes,bars=b.take(0)
    assert not quotes and len(bars)==3
    assert next(p for s,t,p in bars if s=='SPY' and t==NOW)['c']==102


def test_bar_writer_overflow_fails_explicitly_instead_of_dropping_history():
    b=StockBuffer(['SPY'],['SPY'])
    with pytest.raises(StockStreamError,match='explicit data gap'):
        for i in range(4097):b.offer(dict(T='b',S='SPY',t=NOW+i*60,c=100))


def test_slow_database_does_not_stop_reading_fresh_stock_quotes():
    async def run():
        writer_started,release,latest_written=threading.Event(),threading.Event(),threading.Event()
        read_burst=asyncio.Event()
        saved,sent=[],[]
        class WS:
            async def send(self,payload):sent.append(json.loads(payload))
            def __aiter__(self):return self.rows()
            async def rows(self):
                yield json.dumps([dict(T='success',msg='authenticated'),q()])
                assert await asyncio.to_thread(writer_started.wait,2)
                for i in range(1,501):yield json.dumps([q(i=i)])
                read_burst.set()
                await asyncio.Event().wait()
        def write(source,batch):
            if not saved:
                writer_started.set()
                assert release.wait(2)
            saved.extend(batch)
            if any(p['ts']==NOW+.5 for _,p,_ in batch):latest_written.set()
        collector=SimpleNamespace(cfg=SimpleNamespace(watch_symbols=('SPY',),stocks=('SPY',),feed='sip'),
            stock_symbols=lambda:('SPY',),quote_batch=write,bars=lambda *a:None,db=SimpleNamespace(health=lambda *a,**k:None))
        task=asyncio.create_task(consume(WS(),collector))
        try:
            await asyncio.wait_for(read_burst.wait(),2)
            assert not saved  # All 500 new quotes read while the first DB write is blocked.
            release.set()
            assert await asyncio.to_thread(latest_written.wait,2)
            assert saved[-1][1]['ts']==NOW+.5 and len(saved)<10
            assert sent==[dict(action='subscribe',quotes=['SPY'],bars=['SPY'])]
        finally:
            release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_vendor_error_propagates_and_cancels_writer():
    class WS:
        def __aiter__(self):return self.rows()
        async def rows(self):yield json.dumps([dict(T='error',code=406)])
    cfg=SimpleNamespace(watch_symbols=('SPY',),stocks=('SPY',),feed='sip')
    async def run():
        with pytest.raises(StockStreamError,match='406'):await consume(WS(),SimpleNamespace(cfg=cfg,stock_symbols=lambda:('SPY',)))
    asyncio.run(run())


def test_writer_failure_propagates_while_reader_is_idle():
    cancelled=[]
    class WS:
        def __aiter__(self):return self.rows()
        async def rows(self):
            try:
                yield json.dumps([q()])
                await asyncio.Event().wait()
            finally:cancelled.append(True)
    def broken(*args):raise RuntimeError('Database unavailable')
    async def run():
        cfg=SimpleNamespace(watch_symbols=('SPY',),stocks=('SPY',),feed='sip')
        with pytest.raises(RuntimeError,match='Database unavailable'):
            await asyncio.wait_for(consume(WS(),SimpleNamespace(cfg=cfg,stock_symbols=lambda:('SPY',),quote_batch=broken)),2)
        assert cancelled
    asyncio.run(run())
