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
