import asyncio
import threading
import pytest
from compass.feed_loop import FeedLoop


def test_one_blocked_exchange_does_not_block_another_and_loops_close():
    a,b=FeedLoop('A'),FeedLoop('B')
    started,release,received=threading.Event(),threading.Event(),threading.Event()
    def block():
        started.set();release.wait(3)
    try:
        a.loop.call_soon_threadsafe(block)
        assert started.wait(2)
        b.loop.call_soon_threadsafe(received.set)
        assert received.wait(1)
    finally:
        release.set();a.close();b.close()
    assert a.loop.is_closed() and b.loop.is_closed()
    assert not a.thread.is_alive() and not b.thread.is_alive()


@pytest.mark.parametrize('method,connection',[('options','option_connection'),('stocks','stock_connection'),
    ('option_recovery','recovery_connection'),('stock_history','history_connection')])
def test_connection_receives_while_collector_loop_is_blocked(method,connection):
    from compass.providers import Collectors
    received=threading.Event()
    async def receive(self):
        await asyncio.sleep(.05)
        received.set()
        await asyncio.Event().wait()
    Collector=type('Collector',(),{connection:receive})
    async def run():
        task=asyncio.create_task(getattr(Collectors,method)(Collector()))
        await asyncio.sleep(.01)
        # Deliberately block the caller loop as a synchronous SQL call would.
        assert received.wait(2)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    asyncio.run(run())
