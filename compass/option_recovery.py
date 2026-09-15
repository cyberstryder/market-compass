"""Bounded live REST recovery for quiet open option observations; original clocks."""
import asyncio
import logging
import time
from sqlalchemy import select
from .option_ideas import ideas
from .market import fresh, ts

LOG=logging.getLogger('uvicorn.error')


def parse_quotes(data, now):
    rows={}
    for raw in data.get('results',[])[:32]:
        stamp=ts(raw.get('sip_timestamp'))
        q=dict(ts=stamp,bid=raw.get('bid_price'),ask=raw.get('ask_price'),
            bid_size=raw.get('bid_size'),ask_size=raw.get('ask_size'),
            recovery='live_rest',recovery_fetched_at=now,collection_version='option-reliability-v1')
        if stamp is not None and 0<=now-stamp<=5 and fresh(q,now):rows[stamp]=q
    return [rows[k] for k in sorted(rows)]


def targets(collector, now):
    with collector.db.tx() as c:
        opened=c.execute(select(ideas.c.payload).where(ideas.c.status=='open')
            .order_by(ideas.c.created).limit(201)).scalars().all()
        symbols=list(dict.fromkeys(p['contract']['symbol'] for p in opened[:200]))
        candidates=[]
        for symbol in symbols:
            q=collector.db.get(c,'quote:'+symbol,{})
            if now-q.get('ts',0)>2 and now-collector.option_recovery_attempts.get(symbol,0)>=5:
                candidates.append(symbol)
        candidates.sort(key=lambda s:(collector.option_recovery_attempts.get(s,0),s))
        collector.option_recovery_attempts={s:t for s,t in collector.option_recovery_attempts.items() if s in symbols}
        return candidates[:4],len(candidates),len(opened)>200


async def recover(collector):
    now=time.time()
    if not hasattr(collector,'option_recovery_attempts'):collector.option_recovery_attempts={}
    if now<getattr(collector,'option_recovery_backoff',0):return
    selected,waiting,truncated=await asyncio.to_thread(targets,collector,now)
    async def fetch(symbol):
        collector.option_recovery_attempts[symbol]=now
        try:
            data=await asyncio.wait_for(collector.get('https://api.massive.com/v3/quotes/'+symbol,
                params={'apiKey':collector.cfg.massive,'timestamp.gte':str(int((now-5)*1e9)),
                    'timestamp.lte':str(int(now*1e9)),'sort':'timestamp','order':'desc','limit':32}),2)
            quotes=parse_quotes(data,time.time())
            if quotes:await asyncio.to_thread(collector.quote_batch,'massive_rest',[(symbol,q,True) for q in quotes])
            return dict(symbol=symbol,status='fresh_quotes' if quotes else 'no_fresh_quotes',
                fresh_rows=len(quotes),latest_source_ts=quotes[-1]['ts'] if quotes else None,
                truncated=bool(data.get('next_url')))
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=getattr(exc,'status_code',None)
            if code in (401,403,429):collector.option_recovery_backoff=time.time()+(300 if code in (401,403) else 60)
            return dict(symbol=symbol,status='unavailable',http_status=code,error=type(exc).__name__)
    results=await asyncio.gather(*(fetch(s) for s in selected))
    report=dict(at=time.time(),requests=len(selected),waiting=waiting,truncated=truncated,results=results,
        backoff_until=getattr(collector,'option_recovery_backoff',0),
        note='Open intraday observations only; max four requests per cycle, five-second per-contract cooldown, two-second timeout. Only original quotes still fresh at receipt; no historical gap rewriting.')
    await asyncio.to_thread(collector.db.health,'option_recovery','running','Bounded original-timestamp quote recovery',None,**report)
    if results:LOG.info('Option recovery: %s',__import__('json').dumps(report,sort_keys=True))
