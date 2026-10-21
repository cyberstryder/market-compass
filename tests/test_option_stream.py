import asyncio
import json
import threading
from types import SimpleNamespace
import pytest
from compass.option_stream import (OptionBuffer, OptionStreamError, consume,
    massive_wanted, persist_batch)

NOW=1789486200
SYMBOL='O:SPY260918C00600000'

def quote(i=0):
    return dict(ev='Q',sym=SYMBOL,t=(NOW+i)*1000,bp=1,ap=1.1,bs=10,**{'as':10})


def test_sample_retains_source_clock_and_overflow_drops_are_counted():
    # 2026-10-21: a full persistence queue used to RAISE out of offer(),
    # which tore the whole stream down (the read task died, consume()
    # ends on FIRST_COMPLETED) and converted a slow database into a
    # zero-data flap loop. Overflow now drops the new sample and counts
    # it; the stream keeps receiving.
    b=OptionBuffer(capacity=2)
    b.offer(quote(),{SYMBOL},NOW+10,0)
    b.offer(quote(1),{SYMBOL},NOW+11,.5)
    b.offer(quote(2),{SYMBOL},NOW+12,1)
    assert [q['ts'] for _,q,_ in b.quotes]==[NOW,NOW+2]
    assert b.quotes[0][1]['socket_read_at']==NOW+10
    b.offer(quote(3),{SYMBOL},NOW+13,2)  # queue full: dropped, no raise
    assert [q['ts'] for _,q,_ in b.quotes]==[NOW,NOW+2]
    assert b.dropped_quotes==1
    # Once the writer drains a slot, new samples land again.
    b.quotes.popleft()
    b.offer(quote(4),{SYMBOL},NOW+14,3)
    assert [q['ts'] for _,q,_ in b.quotes]==[NOW+2,NOW+4]
    assert b.dropped_quotes==1


def test_trade_overflow_and_flush_overflow_drop_without_raising():
    b=OptionBuffer(capacity=1)
    b.offer(dict(ev='T',sym=SYMBOL,t=NOW*1000,p=1,s=1),{SYMBOL},NOW,0)
    b.offer(dict(ev='T',sym=SYMBOL,t=(NOW+1)*1000,p=1,s=1),{SYMBOL},NOW+1,1)
    assert len(b.trades)==1 and b.dropped_trades==1
    # A coalesced pending sample whose flush finds a full queue is
    # dropped and counted, not raised into the writer task.
    b2=OptionBuffer(capacity=1)
    b2.offer(quote(),{SYMBOL},NOW+10,0)
    b2.offer(quote(1),{SYMBOL},NOW+11,.5)  # inside the 1s sample window: pending
    assert b2.pending
    b2.flush(2)
    assert not b2.pending and b2.dropped_quotes==1 and len(b2.quotes)==1


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


def test_persist_batch_retries_and_reports_failure():
    calls=[]
    def broken():
        calls.append(1)
        raise RuntimeError('statement timeout')
    async def run():
        assert await persist_batch(broken,attempts=3,label='test') is False
        assert len(calls)==3  # every attempt ran; failure is reported, not raised
        assert await persist_batch(lambda: 'ok',attempts=3,label='test') is True
    asyncio.run(run())


def test_quote_writer_failure_surfaces_and_stream_survives(caplog):
    # Replaces the pre-2026-10-21 contract (a writer failure propagated
    # and killed consume()). A persistence failure must now be retried,
    # dropped, and surfaced via log + health counters while the stream
    # keeps running — one bad batch against a loaded database must not
    # tear down the feed.
    async def run():
        subscribed=asyncio.Event()
        attempts=[]
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
        def broken(*args):
            attempts.append(1)
            raise RuntimeError('write failed')
        c=SimpleNamespace(option_symbols={SYMBOL},cfg=SimpleNamespace(massive='secret'),
            quote_batch=broken,subscription_batch=lambda rows:None,db=SimpleNamespace(health=lambda *a,**k:None))
        task=asyncio.create_task(consume(WS(),c))
        try:
            for _ in range(80):
                if len(attempts)>=3:break
                await asyncio.sleep(.05)
            assert len(attempts)>=3  # the batch was retried, not fatal on first failure
            assert not task.done()   # and the stream is still alive
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())
    assert 'dropped after retries' in caplog.text


def test_quote_writer_transient_failure_drops_one_batch_then_commits():
    async def run():
        saved=threading.Event()
        attempts={'n':0}
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
                if self.n==3:
                    # Arrive after the first batch's retry window so the
                    # 1s sampler admits it straight into the queue.
                    await asyncio.sleep(1.2)
                    return json.dumps([quote(10)])
                await asyncio.Event().wait()
        def flaky(source,batch):
            attempts['n']+=1
            if attempts['n']<=3:raise RuntimeError('statement timeout')
            saved.set()
        c=SimpleNamespace(option_symbols={SYMBOL},cfg=SimpleNamespace(massive='secret'),
            quote_batch=flaky,subscription_batch=lambda rows:None,db=SimpleNamespace(health=lambda *a,**k:None))
        task=asyncio.create_task(consume(WS(),c))
        try:
            # First batch exhausts 3 attempts and is dropped; the second
            # quote still commits — the queue head did not wedge.
            assert await asyncio.to_thread(saved.wait,6)
            assert attempts['n']==4
            assert not task.done()
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_massive_wanted_caps_in_selection_priority_order():
    c=SimpleNamespace(option_symbols={'O:A','O:B','O:C','O:D'},
        option_symbols_ordered=['O:C','O:A','O:D','O:B'],
        cfg=SimpleNamespace(massive_stream_limit=2))
    assert massive_wanted(c)=={'O:C','O:A'}  # pinned/active first, not set order
    c.cfg.massive_stream_limit=10
    assert massive_wanted(c)==c.option_symbols  # cap above set size: no-op
    c.cfg=SimpleNamespace()  # legacy collector without the knob: uncapped
    assert massive_wanted(c)==c.option_symbols
    c=SimpleNamespace(option_symbols={'O:A','O:B','O:C'},
        cfg=SimpleNamespace(massive_stream_limit=1))
    assert len(massive_wanted(c))==1  # no ordered list: still capped


def test_massive_consume_subscribes_only_its_capped_subset():
    async def run():
        sent=[]
        class WS:
            authed=False
            async def send(self,raw):sent.append(json.loads(raw))
            async def recv(self):
                if not self.authed:
                    self.authed=True
                    return json.dumps([dict(status='auth_success')])
                await asyncio.Event().wait()
        c=SimpleNamespace(option_symbols={'O:AAA','O:BBB','O:CCC'},
            option_symbols_ordered=['O:CCC','O:AAA','O:BBB'],
            cfg=SimpleNamespace(massive='secret',massive_stream_limit=2),
            quote_batch=lambda *a:None,option_trade_batch=lambda b:None,
            subscription_batch=lambda rows:None,db=SimpleNamespace(health=lambda *a,**k:None))
        task=asyncio.create_task(consume(WS(),c))
        try:
            subs=[]
            for _ in range(100):
                subs=[m for m in sent if m.get('action')=='subscribe']
                if subs:break
                await asyncio.sleep(.05)
            assert subs,'no subscribe sent'
            params=subs[0]['params']
            assert 'O:CCC' in params and 'O:AAA' in params
            assert 'O:BBB' not in params  # capped out despite the shared set holding it
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())
