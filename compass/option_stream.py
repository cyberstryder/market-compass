"""Read option events independently of bounded, batched persistence."""
import asyncio
from collections import deque
import json
import logging
import time
from .market import ts, number
from .diagnostics import redacted_detail
from .quote_collection import VERSION as COLLECTION_VERSION
from .option_subscriptions import SubscriptionTrace


class OptionStreamError(RuntimeError):
    pass


class OptionBuffer:
    def __init__(self, capacity=8192):
        self.capacity = capacity
        self.quotes, self.trades = deque(), deque()
        self.pending = {}
        self.last_sample, self.sources, self.diagnostics = {}, {}, {}

    def offer(self, item, wanted, now, monotonic):
        symbol, kind, stamp = item.get('sym'), item.get('ev'), ts(item.get('t'))
        if symbol not in wanted or stamp is None or kind not in ('Q','T'):
            return
        if kind == 'Q':
            row = self.diagnostics.setdefault(symbol, {})
            row.update(last_source_ts=stamp, last_socket_read_at=now, source_age_at_read=now-stamp)
            if stamp <= self.sources.get(symbol, 0) or any(number(item.get(k)) is None for k in ('bp','ap')):
                return
            self.sources[symbol] = stamp
            value=(symbol,dict(ts=stamp,bid=item['bp'],ask=item['ap'],
                bid_size=item.get('bs',0),ask_size=item.get('as',0),socket_read_at=now,collection_version=COLLECTION_VERSION),True)
            if monotonic-self.last_sample.get(symbol, float('-inf')) < 1:
                self.pending[symbol]=value
                return
            self.pending.pop(symbol,None)
            if len(self.quotes) >= self.capacity:
                raise OptionStreamError('Option quote persistence queue full; explicit observation gap')
            self.quotes.append(value)
            self.last_sample[symbol] = monotonic
        else:
            if len(self.trades) >= self.capacity:
                raise OptionStreamError('Option trade persistence queue full; explicit observation gap')
            self.trades.append((symbol,stamp,item))

    def flush(self, monotonic):
        for symbol,value in list(self.pending.items()):
            if monotonic-self.last_sample.get(symbol,float('-inf')) < 1: continue
            if len(self.quotes)>=self.capacity:
                raise OptionStreamError('Option quote persistence queue full; explicit observation gap')
            self.quotes.append(value)
            del self.pending[symbol]
            self.last_sample[symbol]=monotonic

    def prune(self, wanted):
        for mapping in (self.last_sample,self.sources,self.diagnostics,self.pending):
            for symbol in set(mapping)-wanted:
                del mapping[symbol]


async def consume(ws, collector):
    buffer = OptionBuffer()
    authenticated = asyncio.Event()
    subscribed = set()
    committed = {'quotes':0,'trades':0}
    stored = {}
    trace=SubscriptionTrace(time.time(),collector.cfg.massive)

    async def read():
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), 10)
            except asyncio.TimeoutError:
                if not authenticated.is_set():
                    raise OptionStreamError('Massive authentication acknowledgement timed out; reconnecting') from None
                continue
            now, mono = time.time(), time.monotonic()
            for item in json.loads(raw):
                if item.get('ev')=='status' or 'status' in item:trace.status(item,now)
                if item.get('status') in {'auth_failed','error','not_authorized','max_connections'} or item.get('ev')=='error':
                    raise OptionStreamError('Massive options '+redacted_detail(item.get('status') or 'error')+': '+
                        redacted_detail(item.get('message','subscription rejected'),(collector.cfg.massive,),180))
                if item.get('status')=='auth_success':
                    authenticated.set()
                if item.get('sym') in subscribed and item.get('ev') in ('Q','T'):
                    trace.data(item['ev'],item['sym'],now,ts(item.get('t')))
                buffer.offer(item, subscribed, now, mono)
            await asyncio.sleep(0)

    async def subscriptions():
        try:
            await asyncio.wait_for(authenticated.wait(),15)
        except asyncio.TimeoutError:
            raise OptionStreamError('Massive authentication acknowledgement timed out; reconnecting') from None
        while True:
            wanted = set(collector.option_symbols)
            for action,symbols in [('unsubscribe',subscribed-wanted),('subscribe',wanted-subscribed)]:
                if symbols:
                    trace.request(action,symbols,time.time())
                    await ws.send(json.dumps(dict(action=action,params=','.join(
                        f'{kind}.{symbol}' for symbol in sorted(symbols) for kind in ('T','Q')))))
                    trace.sent(action,symbols,time.time())
            subscribed.clear()
            subscribed.update(wanted)
            buffer.prune(wanted)
            trace.prune(wanted)
            for s in set(stored)-wanted:stored.pop(s,None)
            await asyncio.sleep(.5)

    async def write_trace():
        while True:
            batch=list(trace.queue)[:128]
            if not batch:
                await asyncio.sleep(.1)
                continue
            work=asyncio.create_task(asyncio.to_thread(collector.subscription_batch,batch))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                try:
                    await work
                    for _ in batch:trace.queue.popleft()
                finally:raise
            except Exception as error:
                trace.persistence_error=type(error).__name__
                logging.getLogger('uvicorn.error').warning('Option subscription evidence persistence failed: %s',type(error).__name__)
                await asyncio.sleep(1)
                continue
            for _ in batch:trace.queue.popleft()
            trace.persistence_error=None

    async def write(kind):
        queue = buffer.quotes if kind=='quotes' else buffer.trades
        while True:
            if kind=='quotes': buffer.flush(time.monotonic())
            batch = list(queue)[:64]
            if batch:
                if kind=='quotes':
                    work = asyncio.create_task(asyncio.to_thread(collector.quote_batch,'massive',batch))
                else:
                    work = asyncio.create_task(asyncio.to_thread(collector.option_trade_batch,batch))
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    # A cancelled to_thread await does not stop the transaction.
                    await work
                    for _ in batch: queue.popleft()
                    committed[kind] += len(batch)
                    raise
                for _ in batch: queue.popleft()
                committed[kind] += len(batch)
                if kind=='quotes':
                    at=time.time()
                    for symbol,q,_ in batch:
                        stored[symbol]=dict(last_stored_source_ts=q['ts'],last_commit_at=at,
                            socket_to_commit_seconds=at-q['socket_read_at'],source_to_commit_seconds=at-q['ts'])
            else:
                await asyncio.sleep(.05)

    async def health():
        last_log=0
        await authenticated.wait()
        while True:
            now = time.time()
            rows = {s:{**buffer.diagnostics.get(s,{}),**stored.get(s,{}),'subscription':trace.snapshot(s),'silence_seconds':
                now-buffer.diagnostics[s]['last_socket_read_at'] if s in buffer.diagnostics else None}
                for s in subscribed}
            latest = max((r['last_source_ts'] for r in rows.values() if 'last_source_ts' in r),default=None)
            report = dict(at=now,collection_version=COLLECTION_VERSION,subscribed=len(subscribed),quote_queue=len(buffer.quotes),
                trade_queue=len(buffer.trades),pending_quotes=len(buffer.pending),committed=dict(committed),symbols=rows,
                basis='Socket read is local application receipt, not wire arrival; source clocks unchanged')
            report.update(connection_id=trace.connection_id,
                acknowledged_quote_contracts=sum(bool(trace.snapshot(s)['Q'].get('acknowledged_at')) for s in subscribed),
                subscription_evidence_pending=len(trace.queue),subscription_evidence_dropped=trace.dropped,
                subscription_evidence_error=trace.persistence_error)
            await asyncio.to_thread(collector.db.health,'option_stream','receiving' if latest else 'waiting',
                'Independent option receipt; sampled quotes and trades persisted in separate batches',latest,**report)
            if now-last_log>=30:
                logging.getLogger('uvicorn.error').info('Option stream timing: %s',json.dumps(report,sort_keys=True))
                last_log=now
            await asyncio.sleep(5)

    workers = [asyncio.create_task(fn()) for fn in (read,subscriptions,health,write_trace)]
    workers += [asyncio.create_task(write(kind)) for kind in ('quotes','trades')]
    try:
        done,_ = await asyncio.wait(workers,return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in workers: task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
        trace.emit('disconnected','*',time.time(),pending_quotes=len(buffer.quotes),
                   pending_trades=len(buffer.trades),pending_sampled_quotes=len(buffer.pending),
                   evidence_dropped=trace.dropped)
        if trace.queue:
            try:await asyncio.to_thread(collector.subscription_batch,list(trace.queue))
            except Exception as error:
                logging.getLogger('uvicorn.error').warning('Option subscription final evidence unavailable: %s',type(error).__name__)
        # Queued samples cannot silently disappear on normal disconnect/reconnect.
        if buffer.quotes or buffer.trades or buffer.pending:
            logging.getLogger('uvicorn.error').warning('Option stream stopped with pending quotes=%s trades=%s trailing=%s; observation continuity remains required',len(buffer.quotes),len(buffer.trades),len(buffer.pending))
