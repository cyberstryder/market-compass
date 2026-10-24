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


def test_stock_source_and_socket_receipt_clocks_are_distinct():
    b=StockBuffer(['SPY'],['SPY'])
    b.offer(q(),NOW+.25)
    sample=b.take(0)[0][0][1]
    assert sample['ts']==NOW and sample['socket_read_at']==NOW+.25
    assert sample['collection_version']=='option-reliability-v8'
    assert b.diagnostics['SPY']['source_age_at_read']==.25


def test_slow_bar_storage_does_not_stop_quote_storage():
    async def run():
        bar_started,release,quote_saved=threading.Event(),threading.Event(),threading.Event()
        class WS:
            async def send(self,*args):pass
            def __aiter__(self):return self.rows()
            async def rows(self):
                yield json.dumps([dict(T='success',msg='authenticated'),dict(T='b',S='SPY',t=NOW,o=100,h=101,l=99,c=100,v=50)])
                assert await asyncio.to_thread(bar_started.wait,2)
                yield json.dumps([q(i=500)])
                await asyncio.Event().wait()
        def bars(*args):
            bar_started.set()
            assert release.wait(3)
        def quotes(source,batch):
            if batch[0][1]['ts']==NOW+.5:quote_saved.set()
        collector=SimpleNamespace(cfg=SimpleNamespace(stocks=('SPY',),feed='sip'),
            stock_symbols=lambda:('SPY',),quote_batch=quotes,bars=bars,db=SimpleNamespace(health=lambda *a,**k:None))
        task=asyncio.create_task(consume(WS(),collector))
        try:
            assert await asyncio.to_thread(quote_saved.wait,2)
            assert bar_started.is_set() and not release.is_set()
        finally:
            release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_bars_are_batched_and_corrections_replace_only_same_symbol_minute():
    b=StockBuffer(['SPY','QQQ'],['SPY'])
    bar=dict(T='b',S='SPY',t=NOW,o=100,h=102,l=99,c=101,v=500)
    b.offer(bar);b.offer({**bar,'T':'u','c':102});b.offer({**bar,'S':'QQQ'})
    b.offer({**bar,'t':NOW+60})
    quotes,bars=b.take(0)
    assert not quotes and len(bars)==3
    assert next(p for s,t,p in bars if s=='SPY' and t==NOW)['c']==102


def test_bar_writer_overflow_drops_oldest_and_counts_instead_of_tearing_down():
    # Pre-2026-10-23 the buffer raised StockStreamError here; that fired in
    # the socket read task and tore the whole stream down (option-stream
    # incident of 2026-10-21). Now: bounded, counted drop of the OLDEST bar.
    b=StockBuffer(['SPY'],['SPY'])
    for i in range(4100):b.offer(dict(T='b',S='SPY',t=NOW+i*60,c=100))
    assert len(b.bars)==4096 and b.dropped_bars==4
    assert ('SPY',NOW) not in b.bars and ('SPY',NOW+4099*60) in b.bars
    b.offer(dict(T='b',S='SPY',t=NOW+4099*60,c=101))  # correction of a queued bar: no drop
    assert b.dropped_bars==4 and b.bars[('SPY',NOW+4099*60)]['c']==101


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


def test_database_failure_drops_batch_and_stream_survives(monkeypatch):
    # A DB exception from quote_batch used to propagate out of the writer,
    # ending consume() on FIRST_COMPLETED: one transient database error cost
    # a full stream teardown. Now the batch is retried once, then dropped and
    # counted (health row + stream report) while reading continues.
    original_sleep=asyncio.sleep
    async def fast(seconds):await original_sleep(.01 if seconds>=.25 else seconds)
    monkeypatch.setattr('compass.option_stream.asyncio.sleep',fast)
    monkeypatch.setattr('compass.stock_stream.asyncio.sleep',fast)
    attempts=[];saved=[];health=[]
    recovered=threading.Event()
    class WS:
        async def send(self,*a):pass
        def __aiter__(self):return self.rows()
        async def rows(self):
            yield json.dumps([dict(T='success',msg='authenticated'),q()])
            await asyncio.to_thread(lambda:len(attempts)>=2 or threading.Event().wait(2))
            yield json.dumps([q(i=5)])
            await asyncio.Event().wait()
    def write(source,batch):
        attempts.append(batch)
        if len(attempts)<=2:raise RuntimeError('Database unavailable')
        saved.extend(batch);recovered.set()
    collector=SimpleNamespace(cfg=SimpleNamespace(stocks=('SPY',),feed='sip'),stock_symbols=lambda:('SPY',),
        quote_batch=write,bars=lambda *a:None,db=SimpleNamespace(health=lambda *a,**k:health.append((a,k))))
    async def run():
        task=asyncio.create_task(consume(WS(),collector))
        try:
            assert await asyncio.to_thread(recovered.wait,3)
            assert not task.done()  # the stream survived the failed batch
            assert len(attempts)==3 and attempts[0][0][1]['ts']==NOW and saved[0][1]['ts']==NOW+.005
            drops=[k for a,k in health if a[0]=='alpaca_stocks_persistence']
            assert drops and drops[-1]['dropped_batches']=={'quotes':1,'bars':0}
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_timing_uses_each_committed_chunk_not_the_end_of_the_watchlist(monkeypatch):
    original_sleep=asyncio.sleep
    async def fast_poll(seconds):await original_sleep(.01 if seconds==5 else seconds)
    monkeypatch.setattr('compass.stock_stream.asyncio.sleep',fast_poll)
    async def run():
        reported=threading.Event();reports=[]
        class WS:
            async def send(self,*args):pass
            def __aiter__(self):return self.rows()
            async def rows(self):
                yield json.dumps([dict(T='success',msg='authenticated'),q('SPY'),q('QQQ')])
                await asyncio.Event().wait()
        def write(source,batch):
            return [(s,{**v,'socket_read_at':NOW},NOW+(.2 if s=='SPY' else .8)) for s,v,_ in batch]
        def health(*args,**value):
            if all('last_commit_at' in r for r in value['symbols'].values()):
                reports.append(value);reported.set()
        collector=SimpleNamespace(cfg=SimpleNamespace(stocks=('SPY',),feed='sip'),
            stock_symbols=lambda:('SPY','QQQ'),quote_batch=write,bars=lambda *a:None,
            db=SimpleNamespace(health=health))
        task=asyncio.create_task(consume(WS(),collector))
        try:
            assert await asyncio.to_thread(reported.wait,2)
            rows=reports[-1]['symbols']
            assert rows['SPY']['socket_to_commit_seconds']==pytest.approx(.2)
            assert rows['QQQ']['socket_to_commit_seconds']==pytest.approx(.8)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())
