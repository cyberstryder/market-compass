import asyncio
import threading
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
