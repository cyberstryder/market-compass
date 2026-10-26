"""Read option events independently of bounded, batched persistence.

AUTHORITATIVE OPTION STREAM (2026-10-15, Audit P1):
This is the PRIMARY option stream. It uses Massive (Polygon) for OPRA data.
- Subscribed: ~100 contracts across 16 tickers (OPRA_TICKERS)
- Health key: health:option_stream (via "Option stream timing" logs)
- Status endpoint: /api/alpaca-stream-status prefers this stream if subscribed > 0

The OTHER stream is compass/alpaca_option_stream.py (Alpaca OPRA).
- It is a FALLBACK, disabled by default (ALPACA_OPTION_STREAM_ENABLED=false).
- It only starts if ALPACA_OPTION_STREAM_ENABLED=true AND Alpaca keys are set.
- Health key: health:alpaca_option_stream
- As of 2026-10-15, the Alpaca stream shows 0 subscribed (dark). Massive is the
  working stream. Do NOT treat Alpaca 0-subscribed as a failure - it's the
  fallback, not the primary.
- The two streams are NOT redundant - Massive is primary, Alpaca is fallback
  if Massive fails. Do not remove Alpaca stream without a replacement fallback.
"""
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
        # Bounded-degradation counters (2026-10-21 incident fix): a full
        # persistence queue drops and counts instead of raising — see offer().
        self.dropped_quotes, self.dropped_trades = 0, 0
        self._drop_warned = set()
        # Shadow iceberg detector tap (2026-10-25): an observer attached
        # by consume() receives every accepted trade print and every
        # pre-sample quote. It is pure in-memory aggregation — see
        # _notify_observer for the never-raise contract.
        self.trade_observer = None
        self.observer_errors = 0

    def _notify_observer(self, kind, symbol, stamp, item):
        """Feed the shadow iceberg detector without ever endangering the
        stream: the observer contract is already never-raise, and this
        wrapper is the belt-and-braces layer — any escape is counted and
        swallowed, exactly like a full persistence queue (2026-10-21)."""
        obs = self.trade_observer
        if obs is None:
            return
        try:
            if kind == 'Q':
                obs.observe_quote(symbol, stamp, item.get('bp'), item.get('ap'))
            else:
                obs.observe_trade(symbol, stamp, item)
        except Exception:
            self.observer_errors += 1
            try:
                obs.note_error()
            except Exception:
                pass

    def _note_drop(self, kind):
        if kind == 'quotes':
            self.dropped_quotes += 1
        else:
            self.dropped_trades += 1
        if kind not in self._drop_warned:
            self._drop_warned.add(kind)
            logging.getLogger('uvicorn.error').warning(
                'Option %s persistence queue full; dropping samples with an '
                'explicit observation gap (counted in stream health)', kind)

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
            # Pre-sample tap: the detector needs the NBBO at print time,
            # including quotes the 1 Hz sampler will coalesce away.
            self._notify_observer('Q', symbol, stamp, item)
            value=(symbol,dict(ts=stamp,bid=item['bp'],ask=item['ap'],
                bid_size=item.get('bs',0),ask_size=item.get('as',0),socket_read_at=now,collection_version=COLLECTION_VERSION,
                src=item.get('src','massive')),True)
            if monotonic-self.last_sample.get(symbol, float('-inf')) < 1:
                self.pending[symbol]=value
                return
            self.pending.pop(symbol,None)
            if len(self.quotes) >= self.capacity:
                # NEVER raise here (2026-10-21 incident): the raise fired in
                # the socket read task, consume() ends on FIRST_COMPLETED, so
                # one full queue tore the whole stream down; the supervisor
                # restarted it into the same slow database and the queue
                # refilled before any meaningful batch landed — both option
                # streams committed ~zero all morning while flapping.
                # Quotes are latest-state samples: dropping the new sample
                # loses one history row, the next sample a second later
                # supersedes it, and sources[symbol] already advanced so
                # coverage self-heals. The drop is counted, not silent —
                # the gap stays explicit in stream health.
                self._note_drop('quotes')
                return
            self.quotes.append(value)
            self.last_sample[symbol] = monotonic
        else:
            if len(self.trades) >= self.capacity:
                # Same rule for trade prints: a bounded, counted drop beats
                # tearing down the stream and losing everything in the
                # reconnect gap plus the resubscribe storm.
                self._note_drop('trades')
                return
            self._notify_observer('T', symbol, stamp, item)
            self.trades.append((symbol,stamp,item))

    def flush(self, monotonic):
        for symbol,value in list(self.pending.items()):
            if monotonic-self.last_sample.get(symbol,float('-inf')) < 1: continue
            if len(self.quotes)>=self.capacity:
                # Drop the coalesced sample instead of raising (see offer):
                # it occupies this second's sampling slot, and the next
                # offer for the symbol replaces it.
                self._note_drop('quotes')
                del self.pending[symbol]
                self.last_sample[symbol]=monotonic
                continue
            self.quotes.append(value)
            del self.pending[symbol]
            self.last_sample[symbol]=monotonic

    def prune(self, wanted):
        for mapping in (self.last_sample,self.sources,self.diagnostics,self.pending):
            for symbol in set(mapping)-wanted:
                del mapping[symbol]


async def persist_batch(fn, *args, attempts=3, label='option persistence'):
    """Run one blocking persistence call off-loop for a stream writer.

    Returns True when the batch committed. A failed batch (statement or
    lock timeout against a loaded Postgres, a dropped connection) is
    retried with backoff; after the final attempt it returns False so the
    caller can DROP the batch and continue. The pre-fix writers let the
    exception propagate: it killed the writer task, consume() ends on
    FIRST_COMPLETED, and one transient database error cost a full stream
    teardown + resubscribe cycle while helping nothing — the database,
    not the batch, was the problem (2026-10-21 incident).

    Cancellation keeps the old contract: the underlying thread cannot be
    cancelled, so it is awaited to completion and CancelledError then
    propagates; callers treat the batch as committed in that path.
    """
    delay = 0.25
    for attempt in range(attempts):
        work = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            await asyncio.shield(work)
            return True
        except asyncio.CancelledError:
            await work
            raise
        except Exception as error:
            logging.getLogger('uvicorn.error').warning(
                '%s batch failed (attempt %s/%s): %s',
                label, attempt + 1, attempts, type(error).__name__)
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
                delay *= 2
    return False


def massive_wanted(collector):
    """The Massive stream's subscription set: its own capped subset of the
    shared selection.

    Both streams used to subscribe the full shared set, so
    OPTION_STREAM_CONTRACTS — a knob aimed at Alpaca capacity — also sized
    Massive. The 500 -> 900 raise pushed Massive past its vendor
    subscription limit and it errored instead of streaming; even at 500
    it died on 2026-10-20 when intraday pinned demand peaked the shared
    set (active observations are never dropped, so peak composition is
    the fattest). cfg.massive_stream_limit (MASSIVE_OPTION_STREAM_CONTRACTS)
    caps Massive independently, taking symbols in selection priority
    order (pinned/active first) so a cap never drops the contracts that
    matter most."""
    wanted = set(collector.option_symbols)
    cap = getattr(collector.cfg, 'massive_stream_limit', None)
    if not cap or len(wanted) <= cap:
        return wanted
    ordered = [s for s in getattr(collector, 'option_symbols_ordered', None) or []
               if s in wanted]
    rest = sorted(wanted - set(ordered))
    return set((ordered + rest)[:cap])


async def consume(ws, collector):
    buffer = OptionBuffer()
    buffer.trade_observer = getattr(collector, 'iceberg', None)
    authenticated = asyncio.Event()
    subscribed = set()
    committed = {'quotes':0,'trades':0}
    dropped_batches = {'quotes':0,'trades':0}
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
            wanted = massive_wanted(collector)
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
                # persist_batch owns retry/backoff and the cancellation
                # contract (it awaits the thread, then re-raises
                # CancelledError with the batch committed). A batch that
                # still fails is dropped and counted — never a teardown,
                # never a permanently blocked queue head.
                receipts = {}
                def capture(source, rows):
                    receipts['value'] = collector.quote_batch(source, rows)
                try:
                    if kind=='quotes':
                        ok = await persist_batch(capture, 'massive', batch,
                                                 label='Massive quote persistence')
                    else:
                        ok = await persist_batch(collector.option_trade_batch, batch,
                                                 label='Massive trade persistence')
                except asyncio.CancelledError:
                    for _ in batch: queue.popleft()
                    committed[kind] += len(batch)
                    raise
                for _ in batch: queue.popleft()
                if not ok:
                    dropped_batches[kind] += len(batch)
                    logging.getLogger('uvicorn.error').warning(
                        'Massive %s persistence batch dropped after retries (%s rows)',
                        kind, len(batch))
                    continue
                committed[kind] += len(batch)
                if kind=='quotes':
                    committed_at=time.time()
                    for symbol,q,at in (receipts.get('value') if receipts.get('value') is not None else
                            [(symbol,q,committed_at) for symbol,q,_ in batch]):
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
                persistence_dropped_quotes=buffer.dropped_quotes,persistence_dropped_trades=buffer.dropped_trades,
                persistence_dropped_batches=dict(dropped_batches),
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
