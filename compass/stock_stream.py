"""Consume the full watchlist continuously while coalescing latest quote writes."""
import asyncio
import json
import logging
import time
from .market import ts, number
from .quote_collection import VERSION as COLLECTION_VERSION

LOG=logging.getLogger('uvicorn.error')


class StockStreamError(RuntimeError):
    pass


class StockBuffer:
    def __init__(self, watch, core):
        self.watch, self.core = set(watch), set(core)
        self.quotes, self.bars, self.last_source, self.written = {}, {}, {}, {}
        self.diagnostics = {}

    def offer(self, item, now=None):
        symbol, kind = item.get('S'), item.get('T')
        if symbol not in self.watch or kind not in ('q','b','u'):
            return
        stamp = ts(item.get('t'))
        if stamp is None:
            return
        if kind=='q':
            now=time.time() if now is None else now
            self.diagnostics[symbol]=dict(last_source_ts=stamp,last_socket_read_at=now,
                source_age_at_read=now-stamp)
            if stamp<=self.last_source.get(symbol,0) or any(number(item.get(k)) is None for k in ('bp','ap')):
                return
            self.last_source[symbol] = stamp
            self.quotes[symbol] = dict(ts=stamp,bid=item['bp'],ask=item['ap'],
                bid_size=item.get('bs',0),ask_size=item.get('as',0),socket_read_at=now,
                collection_version=COLLECTION_VERSION)
        else:
            self.bars[(symbol,stamp)] = {k:item[k] for k in ('o','h','l','c','v','vw') if k in item}
            if len(self.bars)>4096:
                raise StockStreamError('Stock bar writer fell behind; reconnecting with an explicit data gap')

    def take(self, now):
        return self.take_quotes(now),self.take_bars()

    def take_quotes(self, now):
        quotes=[]
        for symbol in list(self.quotes):
            if now-self.written.get(symbol,float('-inf')) >= (.25 if symbol in self.core else 1):
                quotes.append((symbol,self.quotes.pop(symbol),True))
                self.written[symbol]=now
        return quotes

    def take_bars(self):
        bars=[(symbol,stamp,payload) for (symbol,stamp),payload in self.bars.items()]
        self.bars.clear()
        return bars


async def consume(ws, collector):
    watch=await asyncio.to_thread(collector.stock_symbols)
    buffer=StockBuffer(watch,collector.cfg.stocks)
    authenticated=asyncio.Event()
    committed={'quotes':0,'bars':0}
    in_flight={'quotes':0,'bars':0}
    stored={}
    async def read():
        frames=0
        async for raw in ws:
            read_at=time.time()
            for item in json.loads(raw):
                if item.get('T')=='error':
                    raise StockStreamError('Alpaca stream error code '+str(item.get('code')))
                if item.get('T')=='success' and item.get('msg')=='authenticated':
                    await ws.send(json.dumps(dict(action='subscribe',quotes=sorted(buffer.watch),bars=sorted(buffer.watch))))
                    authenticated.set()
                if item.get('T')=='subscription':
                    await asyncio.to_thread(collector.db.health,'alpaca_stocks','connected',
                        'Subscribed; waiting for market events',feed=collector.cfg.feed)
                buffer.offer(item,read_at)
            frames+=1
            if frames%100==0:
                await asyncio.sleep(0)

    async def write(kind):
        while True:
            batch=buffer.take_quotes(time.monotonic()) if kind=='quotes' else buffer.take_bars()
            if batch:
                fn=collector.quote_batch if kind=='quotes' else collector.bars
                in_flight[kind]=len(batch)
                work=asyncio.create_task(asyncio.to_thread(fn,'alpaca',batch))
                cancelled=False
                try:
                    receipts=await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Do not let a reconnect overlap an unfinished transaction.
                    receipts=await work
                    cancelled=True
                committed[kind]+=len(batch)
                in_flight[kind]=0
                if kind=='quotes':
                    committed_at=time.time()
                    for symbol,q,at in (receipts if receipts is not None else
                            [(symbol,q,committed_at) for symbol,q,_ in batch]):
                        stored[symbol]=dict(last_stored_source_ts=q['ts'],last_commit_at=at,
                            socket_to_commit_seconds=at-q['socket_read_at'],source_to_commit_seconds=at-q['ts'])
                if cancelled: raise asyncio.CancelledError
            await asyncio.sleep(.05)

    async def health():
        await authenticated.wait()
        last_log=0
        while True:
            now=time.time()
            rows={s:{**buffer.diagnostics.get(s,{}),**stored.get(s,{}),
                'silence_seconds':now-buffer.diagnostics[s]['last_socket_read_at'] if s in buffer.diagnostics else None}
                for s in buffer.watch}
            latest=max((r['last_source_ts'] for r in rows.values() if 'last_source_ts' in r),default=None)
            report=dict(at=now,collection_version=COLLECTION_VERSION,watch_symbols=len(buffer.watch),
                quote_queue=len(buffer.quotes),bar_queue=len(buffer.bars),in_flight=dict(in_flight),
                committed=dict(committed),symbols=rows,
                basis='Source time, local socket receipt and completed storage measured separately; original quote clocks unchanged')
            await asyncio.to_thread(collector.db.health,'alpaca_stocks','receiving' if latest else 'waiting',
                'Independent stock receipt and quote/bar writers; core quotes up to 4 Hz, other symbols up to 1 Hz',latest,**report)
            if time.monotonic()-last_log>=30:
                LOG.info('Stock stream timing: %s',json.dumps(report,sort_keys=True))
                last_log=time.monotonic()
            await asyncio.sleep(5)

    async def subscriptions():
        await authenticated.wait()
        while True:
            await asyncio.sleep(5)
            wanted=set(await asyncio.to_thread(collector.stock_symbols))
            added=wanted-buffer.watch
            removed=buffer.watch-wanted
            if added:
                buffer.watch.update(added)
                await ws.send(json.dumps(dict(action='subscribe',quotes=sorted(added),bars=sorted(added))))
            if removed:
                await ws.send(json.dumps(dict(action='unsubscribe',quotes=sorted(removed),bars=sorted(removed))))
                buffer.watch.difference_update(removed)
                for symbol in removed:
                    for values in (buffer.quotes,buffer.last_source,buffer.written,buffer.diagnostics,stored):
                        values.pop(symbol,None)
                buffer.bars={k:v for k,v in buffer.bars.items() if k[0] not in removed}

    workers=[asyncio.create_task(fn()) for fn in (read,subscriptions,health)]
    workers.extend(asyncio.create_task(write(kind)) for kind in ('quotes','bars'))
    try:
        done,_=await asyncio.wait(workers,return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in workers:task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
        if buffer.quotes or buffer.bars or any(in_flight.values()):
            LOG.warning('Stock stream stopped with pending quotes=%s bars=%s in-flight=%s; observation continuity remains required',
                len(buffer.quotes),len(buffer.bars),in_flight)
