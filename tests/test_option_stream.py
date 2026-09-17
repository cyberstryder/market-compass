import asyncio
import json
import threading
from types import SimpleNamespace
import pytest
from compass.option_stream import OptionBuffer, OptionStreamError, consume

NOW=1789486200
SYMBOL='O:SPY260918C00600000'

def quote(i=0):
    return dict(ev='Q',sym=SYMBOL,t=(NOW+i)*1000,bp=1,ap=1.1,bs=10,**{'as':10})


def test_sample_retains_source_clock_and_detects_overflow():
    b=OptionBuffer(capacity=2)
    b.offer(quote(),{SYMBOL},NOW+10,0)
    b.offer(quote(1),{SYMBOL},NOW+11,.5)
    b.offer(quote(2),{SYMBOL},NOW+12,1)
    assert [q['ts'] for _,q,_ in b.quotes]==[NOW,NOW+2]
    assert b.quotes[0][1]['socket_read_at']==NOW+10
    with pytest.raises(OptionStreamError,match='queue full'):
        b.offer(quote(3),{SYMBOL},NOW+13,2)


def test_reordered_and_unsubscribed_quotes_are_not_saved():
    b=OptionBuffer()
    b.offer(quote(),set(),NOW,0)
    assert not b.quotes
    b.offer(quote(2),{SYMBOL},NOW+2,0)
    b.offer(quote(),{SYMBOL},NOW+3,2)
    assert len(b.quotes)==1
    b.prune(set())
    assert not b.sources and not b.diagnostics


def test_slow_trade_persistence_does_not_block_option_receipt_or_quotes():
    async def run():
        started,release,saved=threading.Event(),threading.Event(),threading.Event()
        subscribed=asyncio.Event()
        class WS:
            n=0
            async def send(self,raw):
                if json.loads(raw)['action']=='subscribe':subscribed.set()
            async def recv(self):
                self.n+=1
                if self.n==1:return json.dumps([dict(status='auth_success')])
                if self.n==2:
                    await subscribed.wait()
                    return json.dumps([dict(ev='T',sym=SYMBOL,t=NOW*1000,p=1,s=1)])
                if self.n==3:
                    assert await asyncio.to_thread(started.wait,2)
                    return json.dumps([quote()])
                await asyncio.Event().wait()
        def trade(batch):
            started.set()
            assert release.wait(3)
        def write(source,batch):
            assert batch[0][1]['ts']==NOW
            saved.set()
        c=SimpleNamespace(option_symbols={SYMBOL},cfg=SimpleNamespace(massive='secret'),
            quote_batch=write,option_trade_batch=trade,subscription_batch=lambda rows:None,db=SimpleNamespace(health=lambda *a,**k:None))
        task=asyncio.create_task(consume(WS(),c))
        try:
            assert await asyncio.to_thread(saved.wait,2)
            assert not release.is_set()
        finally:
            release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_auth_rejection_is_redacted_and_propagates():
    class WS:
        async def recv(self):return json.dumps([dict(status='auth_failed',message='bad secret')])
    c=SimpleNamespace(cfg=SimpleNamespace(massive='secret'),subscription_batch=lambda rows:None)
    async def run():
        with pytest.raises(OptionStreamError,match='auth_failed') as e:
            await consume(WS(),c)
        assert 'secret' not in str(e.value)
    asyncio.run(run())


def test_quote_writer_failure_is_not_hidden_by_idle_reader(caplog):
    async def run():
        subscribed=asyncio.Event()
        class WS:
            n=0
            async def send(self,raw):subscribed.set()
            async def recv(self):
                self.n+=1
                if self.n==1:return json.dumps([dict(status='auth_success')])
                if self.n==2:
                    await subscribed.wait()
                    return json.dumps([quote()])
                await asyncio.Event().wait()
        def broken(*args):raise RuntimeError('write failed')
        c=SimpleNamespace(option_symbols={SYMBOL},cfg=SimpleNamespace(massive='secret'),
            quote_batch=broken,subscription_batch=lambda rows:None,db=SimpleNamespace(health=lambda *a,**k:None))
        with pytest.raises(RuntimeError,match='write failed'):
            await asyncio.wait_for(consume(WS(),c),2)
    asyncio.run(run())
    assert 'pending quotes=1' in caplog.text
