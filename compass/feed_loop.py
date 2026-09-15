"""A receive loop per futures connection prevents cross-exchange callback stalls."""
import asyncio
import threading


class FeedLoop:
    def __init__(self, name):
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(ready.set)
            self.loop.run_forever()
        self.thread = threading.Thread(target=run, daemon=True, name='feed-'+name)
        self.thread.start()
        if not ready.wait(5):
            raise RuntimeError('Futures receive loop did not start')

    def close(self):
        if self.loop.is_closed(): return
        async def drain():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            asyncio.run_coroutine_threadsafe(drain(), self.loop).result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('Futures receive loop did not stop')
            self.loop.close()
