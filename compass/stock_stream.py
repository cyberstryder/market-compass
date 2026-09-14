"""Consume the full watchlist continuously while coalescing latest quote writes."""
import asyncio
import json
import time
from .market import ts, number


class StockStreamError(RuntimeError):
    pass


class StockBuffer:
    def __init__(self, watch, core):
        self.watch, self.core = set(watch), set(core)
        self.quotes, self.bars, self.last_source, self.written = {}, {}, {}, {}

    def offer(self, item):
        symbol, kind = item.get('S'), item.get('T')
        if symbol not in self.watch or kind not in ('q','b','u'):
            return
        stamp = ts(item.get('t'))
        if stamp is None:
            return
        if kind=='q':
            if stamp<=self.last_source.get(symbol,0) or any(number(item.get(k)) is None for k in ('bp','ap')):
                return
            self.last_source[symbol] = stamp
            self.quotes[symbol] = dict(ts=stamp,bid=item['bp'],ask=item['ap'],
                bid_size=item.get('bs',0),ask_size=item.get('as',0))
        else:
            self.bars[(symbol,stamp)] = {k:item[k] for k in ('o','h','l','c','v','vw') if k in item}
            if len(self.bars)>4096:
                raise StockStreamError('Stock bar writer fell behind; reconnecting with an explicit data gap')

    def take(self, now):
        quotes=[]
        for symbol in list(self.quotes):
            if now-self.written.get(symbol,float('-inf')) >= (.25 if symbol in self.core else 1):
                quotes.append((symbol,self.quotes.pop(symbol),symbol in self.core))
                self.written[symbol]=now
        bars=[(symbol,stamp,payload) for (symbol,stamp),payload in self.bars.items()]
        self.bars.clear()
        return quotes,bars


async def consume(ws, collector):
    watch=await asyncio.to_thread(collector.stock_symbols)
    buffer=StockBuffer(watch,collector.cfg.stocks)
    authenticated=asyncio.Event()
    async def read():
        frames=0
        async for raw in ws:
            for item in json.loads(raw):
                if item.get('T')=='error':
                    raise StockStreamError('Alpaca stream error code '+str(item.get('code')))
                if item.get('T')=='success' and item.get('msg')=='authenticated':
                    await ws.send(json.dumps(dict(action='subscribe',quotes=sorted(buffer.watch),bars=sorted(buffer.watch))))
                    authenticated.set()
                if item.get('T')=='subscription':
                    await asyncio.to_thread(collector.db.health,'alpaca_stocks','connected',
                        'Subscribed; waiting for market events',feed=collector.cfg.feed)
                buffer.offer(item)
            frames+=1
            if frames%100==0:
                await asyncio.sleep(0)

    async def write():
        last_health, source = 0, None
        while True:
            quotes,bars=buffer.take(time.monotonic())
            if quotes:
                await asyncio.to_thread(collector.quote_batch,'alpaca',quotes)
                source=max([q['ts'] for _,q,_ in quotes]+([source] if source is not None else []))
            if bars:
                await asyncio.to_thread(collector.bars,'alpaca',bars)
            if source is not None and time.monotonic()-last_health>5:
                await asyncio.to_thread(collector.db.health,'alpaca_stocks','receiving',
                    'Latest quotes coalesced: core up to 4 Hz, watchlist up to 1 Hz; batched completed bars',
                    source,watch_symbols=len(buffer.watch))
                last_health=time.monotonic()
            await asyncio.sleep(.05)

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
                    for values in (buffer.quotes,buffer.last_source,buffer.written):
                        values.pop(symbol,None)
                buffer.bars={k:v for k,v in buffer.bars.items() if k[0] not in removed}

    workers=tuple(asyncio.create_task(fn()) for fn in (read,write,subscriptions))
    try:
        done,_=await asyncio.wait(workers,return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in workers:task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
