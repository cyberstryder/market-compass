"""Bounded live REST recovery for quiet open option observations; original clocks."""
import asyncio
import logging
import time
from sqlalchemy import select
from .option_ideas import ideas
from .market import fresh, ts
from .quote_collection import VERSION as COLLECTION_VERSION

LOG=logging.getLogger('uvicorn.error')


def parse_quotes(data, now):
    rows={}
    for raw in data.get('results',[])[:32]:
        stamp=ts(raw.get('sip_timestamp'))
        q=dict(ts=stamp,bid=raw.get('bid_price'),ask=raw.get('ask_price'),
            bid_size=raw.get('bid_size'),ask_size=raw.get('ask_size'),
            recovery='live_rest',recovery_fetched_at=now,collection_version=COLLECTION_VERSION)
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
        started=time.monotonic()
        try:
            data=await asyncio.wait_for(collector.get('https://api.massive.com/v3/quotes/'+symbol,
                params={'apiKey':collector.cfg.massive,'timestamp.gte':str(int((now-5)*1e9)),
                    'timestamp.lte':str(int(now*1e9)),'sort':'timestamp','order':'desc','limit':32}),2)
            fetched_at=time.time()
            request_seconds=time.monotonic()-started
            quotes=parse_quotes(data,fetched_at)
            if quotes:await asyncio.to_thread(collector.quote_batch,'massive_rest',[(symbol,q,True) for q in quotes])
            return dict(symbol=symbol,status='fresh_quotes' if quotes else 'no_fresh_quotes',
                fresh_rows=len(quotes),latest_source_ts=quotes[-1]['ts'] if quotes else None,
                fetched_at=fetched_at,request_seconds=request_seconds,
                storage_seconds=max(0,time.monotonic()-started-request_seconds),
                truncated=bool(data.get('next_url')))
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=getattr(exc,'status_code',None)
            if code in (401,403,429):collector.option_recovery_backoff=time.time()+(300 if code in (401,403) else 60)
            return dict(symbol=symbol,status='unavailable',http_status=code,error=type(exc).__name__)
    results=await asyncio.gather(*(fetch(s) for s in selected))
    opra=await recover_alpaca(collector,[r['symbol'] for r in results if r.get('fresh_rows',0)==0])
    report=dict(at=time.time(),collection_version=COLLECTION_VERSION,requests=len(selected),opra_requests=int(bool(opra)),waiting=waiting,truncated=truncated,results=results,opra_results=opra,
        backoff_until=getattr(collector,'option_recovery_backoff',0),
        note='Open intraday observations only; max four Massive requests plus one OPRA batch per cycle, five-second per-contract cooldown, two-second timeout. Only original quotes still fresh at receipt; no historical gap rewriting.')
    await asyncio.to_thread(collector.db.health,'option_recovery','running','Bounded original-timestamp quote recovery',None,**report)
    if results:LOG.info('Option recovery: %s',__import__('json').dumps(report,sort_keys=True))


async def recover_alpaca(collector, symbols):
    """One explicit OPRA batch; indicative quotes are never requested."""
    now=time.time()
    if (not symbols or not getattr(collector.cfg,'alpaca_key','') or not getattr(collector.cfg,'alpaca_secret','')
            or now<getattr(collector,'option_opra_backoff',0)):
        return []
    wanted={s.removeprefix('O:'):s for s in symbols[:4]}
    try:
        data=await asyncio.wait_for(collector.get('https://data.alpaca.markets/v1beta1/options/quotes/latest',
            headers=collector.alpaca_headers,params={'symbols':','.join(wanted),'feed':'opra'}),2)
        rows=[];results=[]
        for raw_symbol,symbol in wanted.items():
            raw=data.get('quotes',{}).get(raw_symbol,{})
            stamp=ts(raw.get('t'))
            at=time.time()
            q=dict(ts=stamp,bid=raw.get('bp'),ask=raw.get('ap'),bid_size=raw.get('bs'),ask_size=raw.get('as'),
                recovery='live_rest_opra',recovery_fetched_at=at,collection_version=COLLECTION_VERSION)
            valid=stamp is not None and 0<=at-stamp<=5 and fresh(q,at)
            if valid:rows.append((symbol,q,True))
            results.append(dict(symbol=symbol,status='fresh_quotes' if valid else 'no_fresh_quotes',
                latest_source_ts=stamp,fresh_rows=int(valid)))
        if rows:await asyncio.to_thread(collector.quote_batch,'alpaca_opra_recovery',rows)
        return results
    except asyncio.CancelledError:raise
    except Exception as exc:
        code=getattr(exc,'status_code',None)
        collector.option_opra_backoff=time.time()+(300 if code in (401,403) else 60)
        return [dict(status='unavailable',http_status=code,error=type(exc).__name__,feed='opra',
            backoff_until=collector.option_opra_backoff)]
