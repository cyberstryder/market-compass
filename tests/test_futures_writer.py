import threading
from types import SimpleNamespace
import pytest
from compass.futures_writer import FuturesWriter


def test_slow_persistence_does_not_block_receipt_and_preserves_every_sample():
    entered,release=threading.Event(),threading.Event()
    received=[]
    def quotes(source,items):
        entered.set(); assert release.wait(2)
        received.extend(items)
    writer=FuturesWriter(SimpleNamespace(quote_batch=quotes),'fixture')
    writer.quote('databento','ESZ6@1',{'ts':100},1)
    assert entered.wait(1)
    for i in range(10): writer.quote('databento','ESZ6@1',{'ts':101+i},1)
    assert len(writer.jobs)==10  # Receipt returned despite the blocked database.
    release.set();writer.close()
    assert [q['ts'] for _,q,_ in received]==list(range(100,111))
    assert all(q['instrument_id']==1 for _,q,_ in received)


def test_queue_overflow_is_explicit_and_drain_preserves_accepted_records():
    entered,release=threading.Event(),threading.Event(); result=[]
    def bars(source,items):
        entered.set();assert release.wait(2);result.extend(items)
    writer=FuturesWriter(SimpleNamespace(bars=bars),'fixture',capacity=1)
    writer.bars('databento',[1]);assert entered.wait(1)
    writer.bars('databento',[2])
    with pytest.raises(RuntimeError,match='queue full'):writer.bars('databento',[3])
    release.set();writer.close()
    assert result==[1,2]


def test_background_failure_propagates_to_stream():
    entered=threading.Event()
    def failed(source,items): entered.set();raise ValueError('fixture')
    writer=FuturesWriter(SimpleNamespace(bars=failed),'fixture')
    writer.bars('databento',[1]);assert entered.wait(1)
    with pytest.raises(RuntimeError,match='worker failed'): writer.close()


def test_transient_write_retries_same_batch_without_losing_following_quotes():
    from sqlalchemy.exc import OperationalError
    calls=[]; received=[]
    def quotes(source,items):
        calls.append(list(items))
        if len(calls)==1: raise OperationalError('fixture',{},Exception('disconnect'))
        received.extend(items)
    writer=FuturesWriter(SimpleNamespace(quote_batch=quotes),'retry')
    for i in range(4): writer.quote('databento','ESZ6@1',{'ts':i+100},1)
    writer.close()
    assert calls[0]==calls[1]
    assert [q['ts'] for _,q,_ in received]==list(range(100,104))
    assert writer.committed_quotes==4


def test_persistent_database_failure_is_bounded_and_classified():
    from sqlalchemy.exc import OperationalError
    from compass.futures_writer import transient_database_error
    calls=[]
    def quotes(source,items):
        calls.append(1); raise OperationalError('fixture',{},Exception('disconnect'))
    writer=FuturesWriter(SimpleNamespace(quote_batch=quotes),'failure')
    writer.quote('databento','ESZ6@1',{'ts':100},1)
    with pytest.raises(RuntimeError) as caught: writer.close()
    assert len(calls)==3 and writer.committed_quotes==0
    assert transient_database_error(caught.value)
    assert not transient_database_error(ValueError('not transient'))


def test_telemetry_database_failure_does_not_stop_quote_persistence():
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError
    entered=threading.Event(); release=threading.Event(); received=[]
    class DB:
        @contextmanager
        def tx(self):
            raise OperationalError('fixture',{},Exception('disconnect'))
            yield
    def quotes(source,items):
        entered.set(); assert release.wait(2); received.extend(items)
    writer=FuturesWriter(SimpleNamespace(db=DB(),quote_batch=quotes),'telemetry')
    writer.last_log=0
    writer.quote('databento','ESZ6@1',{'ts':100},1)
    assert entered.wait(1)
    writer.quote('databento','ESZ6@1',{'ts':101},1)
    release.set();writer.close()
    assert writer.committed_quotes==2 and writer.error is None


def test_startup_replay_cannot_block_live_quotes_or_mapping_and_both_lanes_drain():
    replay_started,release,quote_saved,mapped=[threading.Event() for _ in range(4)]
    saved_quotes=[];saved_bars=[]
    def bars(source,items):
        replay_started.set();assert release.wait(3);saved_bars.extend(items)
    def quotes(source,items):
        saved_quotes.extend(items);quote_saved.set()
    writer=FuturesWriter(SimpleNamespace(bars=bars,quote_batch=quotes),'startup')
    try:
        writer.bars('databento',list(range(500)))
        assert replay_started.wait(1)
        writer.bars('databento',list(range(500,1000)))
        writer.metadata(mapped.set)
        writer.quote('databento','ESZ6@1',{'ts':100,'callback_at':99.9},1)
        assert mapped.wait(1) and quote_saved.wait(1)
        assert saved_bars==[] and saved_quotes[0][1]['ts']==100
        assert saved_quotes[0][1]['callback_at']==99.9
    finally:
        release.set();writer.close()
    assert saved_bars==list(range(1000)) and writer.committed_bars==1000
    assert not writer.thread.is_alive() and not writer.bar_thread.is_alive()


def test_shutdown_waits_for_both_accepted_lanes():
    from concurrent.futures import ThreadPoolExecutor
    bar_started,release,closing=[threading.Event() for _ in range(3)]
    saved=[]
    def bars(source,items):bar_started.set();assert release.wait(3);saved.extend(items)
    writer=FuturesWriter(SimpleNamespace(bars=bars),'drain')
    writer.bars('databento',[1,2]);assert bar_started.wait(1)
    def close():closing.set();writer.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        done=pool.submit(close)
        try:
            assert closing.wait(1) and not done.done()
        finally:release.set()
        done.result(timeout=2)
    assert saved==[1,2]
