"""Bounded asynchronous persistence keeps SQL off the SDK's callback loop."""
from collections import deque
import logging
import threading
import time
from sqlalchemy.exc import OperationalError


def transient_database_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, OperationalError): return True
        error = error.__cause__ or error.__context__
    return False


class FuturesWriter:
    def __init__(self, collector, stream, capacity=2048):
        self.collector, self.stream, self.capacity = collector, stream, capacity
        self.jobs = deque()
        self.bar_jobs = deque()
        self.condition = threading.Condition()
        self.closed = False
        self.error = None
        self.max_queue_age = 0
        self.window_max_queue_age = 0
        self.window_max_write_seconds = 0
        self.max_write_seconds = 0
        self.committed_quotes = 0
        self.committed_bars = 0
        self.bar_max_queue_age = 0
        self.last_log = time.monotonic()
        self.thread = threading.Thread(target=self.run,daemon=True,name='futures-writer-'+stream)
        self.bar_thread = threading.Thread(target=lambda:self.run(bars=True),daemon=True,name='futures-bars-'+stream)
        self.thread.start()
        self.bar_thread.start()

    def submit(self, kind, value):
        with self.condition:
            if self.error: raise RuntimeError('Futures persistence worker failed') from self.error
            if self.closed: raise RuntimeError('Futures persistence worker closed')
            queue=self.bar_jobs if kind=='bar' else self.jobs
            if len(queue) >= self.capacity:
                raise RuntimeError('Futures persistence queue full; reconnect required')
            queue.append((kind,value,time.time()))
            self.condition.notify_all()

    def quote(self, source, symbol, quote, instrument_id):
        self.submit('quote',(symbol,{**quote,'instrument_id':instrument_id},True))

    def metadata(self, fn):
        self.submit("metadata",fn)

    def bars(self, source, items):
        self.submit('bar',list(items))

    def run(self,bars=False):
        # Replayed OHLCV must not sit ahead of current BBO or mapping metadata.
        # Each lane retains FIFO order and every accepted item, including drain.
        queue=self.bar_jobs if bars else self.jobs
        try:
            while True:
                with self.condition:
                    while not queue and not self.closed: self.condition.wait()
                    if not queue or self.error: break
                    # Coalesce a short burst without delaying receipt callbacks.
                    if queue[0][0]=='quote' and not self.closed:
                        self.condition.wait(timeout=.025)
                    kind,value,queued = queue.popleft()
                    values = [value]
                    if kind=='quote':
                        while queue and queue[0][0]=='quote' and len(values)<64:
                            values.append(queue.popleft()[1])
                queue_age=time.time()-queued
                if bars:self.bar_max_queue_age=max(self.bar_max_queue_age,queue_age)
                else:
                    self.max_queue_age = max(self.max_queue_age,queue_age)
                    self.window_max_queue_age = max(self.window_max_queue_age,queue_age)
                started=time.monotonic()
                # Keep this batch owned until a transaction succeeds. The store's
                # unique keys make retries safe after an ambiguous commit.
                for attempt in range(3):
                    try:
                        if kind=='quote':
                            self.collector.quote_batch('databento',values)
                        elif kind=='metadata':
                            value()
                        else:
                            self.collector.bars('databento',value)
                        break
                    except OperationalError as error:
                        logging.getLogger('uvicorn.error').warning(
                            'Futures persistence retry: stream=%s lane=%s attempt=%s sqlstate=%s',
                            self.stream,'bars' if bars else 'live',attempt+1,getattr(error.orig,'sqlstate',None))
                        if attempt==2: raise
                        time.sleep(.25*(2**attempt))
                if kind=='quote': self.committed_quotes += len(values)
                if bars:
                    self.committed_bars+=len(value)
                    continue
                write_seconds=time.monotonic()-started
                self.max_write_seconds=max(self.max_write_seconds,write_seconds)
                self.window_max_write_seconds=max(self.window_max_write_seconds,write_seconds)
                if time.monotonic()-self.last_log>=30:
                    self.last_log=time.monotonic()
                    try:
                        if hasattr(self.collector,'db'):
                            with self.collector.db.tx() as c:
                                self.collector.db.put(c,'futures_persistence:'+self.stream,dict(at=time.time(),stream=self.stream,
                                    queue_depth=len(self.jobs),max_queue_age=self.max_queue_age,
                                    window_max_queue_age=self.window_max_queue_age,
                                    window_max_write_seconds=self.window_max_write_seconds,
                                    max_write_seconds=self.max_write_seconds,committed_quotes=self.committed_quotes,
                                    bar_queue_depth=len(self.bar_jobs),bar_max_queue_age=self.bar_max_queue_age,
                                    committed_bars=self.committed_bars,lanes='live_quotes_and_metadata / replay_bars'))
                    except OperationalError:
                        logging.getLogger('uvicorn.error').warning('Futures persistence telemetry unavailable: stream=%s; receipt continues',self.stream)
                    logging.getLogger('uvicorn.error').info(
                        'Futures persistence: stream=%s completed_quote_items=%s queue_depth=%s max_queue_age=%s max_write_seconds=%s window_max_queue_age=%s window_max_write_seconds=%s bar_queue_depth=%s committed_bars=%s',
                        self.stream,self.committed_quotes,len(self.jobs),self.max_queue_age,self.max_write_seconds,
                        self.window_max_queue_age,self.window_max_write_seconds,len(self.bar_jobs),self.committed_bars)
                    self.window_max_queue_age=0
                    self.window_max_write_seconds=0
        except Exception as error:
            with self.condition:
                self.error=error
                self.closed=True
                self.condition.notify_all()
            logging.getLogger('uvicorn.error').error('Futures persistence failed: stream=%s lane=%s error=%s',self.stream,'bars' if bars else 'live',type(error).__name__)

    def close(self):
        with self.condition:
            self.closed=True
            self.condition.notify_all()
        deadline=time.monotonic()+10
        for worker in (self.thread,self.bar_thread):worker.join(timeout=max(0,deadline-time.monotonic()))
        if any(worker.is_alive() for worker in (self.thread,self.bar_thread)):
            raise RuntimeError('Futures persistence drain timed out')
        if self.error: raise RuntimeError('Futures persistence worker failed') from self.error
