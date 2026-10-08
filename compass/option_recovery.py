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
    from .active_observations import inventory
    with collector.db.tx() as c:
        live=set(inventory(collector.db,c,now,include_followups=False)['options'])
        symbols=inventory(collector.db,c,now)['options']
        collector.live_recovery_symbols=live
        attempts=collector.option_recovery_attempts
        candidates=[s for s in symbols if now-collector.db.get(c,'quote:'+s,{}).get('ts',0)>2
                    and now-attempts.get(s,0)>=(2 if s in live else 60)]
        # Live paths have a 15-second gap budget. Post-gap studies sample once a
        # minute and must not occupy the recovery queue ahead of open paths.
        candidates.sort(key=lambda s:(s not in live,attempts.get(s,0),s))
        collector.option_recovery_attempts={s:t for s,t in attempts.items() if s in symbols}
        return candidates[:100],len(candidates),len(candidates)>100


async def recover(collector):
    now=time.time()
    if not hasattr(collector,'option_recovery_attempts'):collector.option_recovery_attempts={}
    selected,waiting,truncated=await asyncio.to_thread(targets,collector,now)
    # OPRA batch coverage is independent of the Massive REST backoff and timeout.
    opra=await recover_alpaca(collector,selected)
    if opra:
        collector.option_recovery_attempts.update({s:now for s in selected})
    massive_attempts=getattr(collector,'option_massive_attempts',{})
    collector.option_massive_attempts={s:t for s,t in massive_attempts.items() if s in selected}
    recovered={r.get('symbol') for r in opra if r.get('fresh_rows')}
    fallback=sorted((s for s in selected if s not in recovered),key=lambda s:(s not in getattr(collector,'live_recovery_symbols',set(selected)),massive_attempts.get(s,0)))[:4]
    async def fetch(symbol):
        requested_at=time.time()
        collector.option_recovery_attempts[symbol]=requested_at
        collector.option_massive_attempts[symbol]=requested_at
        started=time.monotonic()
        try:
            data=await asyncio.wait_for(collector.get('https://api.massive.com/v3/quotes/'+symbol,
                params={'apiKey':collector.cfg.massive,'timestamp.gte':str(int((requested_at-5)*1e9)),
                    'timestamp.lte':str(int(requested_at*1e9)),'sort':'timestamp','order':'desc','limit':32}),2)
            fetched_at=time.time()
            request_seconds=time.monotonic()-started
            quotes=parse_quotes(data,fetched_at)
            if quotes:await asyncio.to_thread(collector.quote_batch,'massive_rest',[(symbol,q,True) for q in quotes])
            return dict(symbol=symbol,status='fresh_quotes' if quotes else 'no_fresh_quotes',
                fresh_rows=len(quotes),latest_source_ts=quotes[-1]['ts'] if quotes else None,
                requested_at=requested_at,fetched_at=fetched_at,request_seconds=request_seconds,
                storage_seconds=max(0,time.monotonic()-started-request_seconds),
                truncated=bool(data.get('next_url')))
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=getattr(exc,'status_code',None)
            if code in (401,403,429):collector.option_recovery_backoff=time.time()+(300 if code in (401,403) else 60)
            return dict(symbol=symbol,status='unavailable',http_status=code,error=type(exc).__name__)
    results=await asyncio.gather(*(fetch(s) for s in fallback)) if (getattr(collector.cfg,'massive','')
        and now>=getattr(collector,'option_recovery_backoff',0)) else []
    report=dict(at=time.time(),collection_version=COLLECTION_VERSION,requests=len(results),opra_contracts=len(selected),opra_requests=int(bool(opra)),waiting=waiting,truncated=truncated,results=results,opra_results=opra,
        backoff_until=getattr(collector,'option_recovery_backoff',0),
        note='Independent option recovery: one OPRA batch up to 100 contracts, then at most four Massive fallbacks using request-time windows. Stock recovery runs on its own cycle. Original clocks and gap rules remain unchanged.')
    await asyncio.to_thread(collector.db.health,'option_recovery','running','Bounded original-timestamp quote recovery',None,**report)
    if now-getattr(collector,'option_recovery_last_log',0)>=30:
        LOG.info('Option recovery: %s',__import__('json').dumps(report,sort_keys=True))
        collector.option_recovery_last_log=now


async def recover_alpaca(collector, symbols):
    """One explicit OPRA batch; indicative quotes are never requested."""
    now=time.time()
    if (not symbols or not getattr(collector.cfg,'alpaca_key','') or not getattr(collector.cfg,'alpaca_secret','')
            or now<getattr(collector,'option_opra_backoff',0)):
        return []
    wanted={s.removeprefix('O:'):s for s in symbols[:100]}
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
            # For gap recovery, accept quotes up to 60s old (vs 5s for normal).
            # Stale quotes are marked data_gap and excluded from win/loss stats,
            # but they're better than force-exiting blind.
            valid=stamp is not None and 0<=at-stamp<=60 and fresh(q,at)
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


async def recover_stocks(collector):
    """One bounded stock batch per cycle with a provider-specific backoff."""
    now=time.time()
    async def health(status,detail,**extra):
        await asyncio.to_thread(collector.db.health,'stock_recovery',status,detail,None,
            at=time.time(),collection_version=COLLECTION_VERSION,
            backoff_until=getattr(collector,'stock_recovery_backoff',0),**extra)
    if not getattr(collector.cfg,'alpaca_key','') or not getattr(collector.cfg,'alpaca_secret',''):
        await health('not_configured','Stock recovery requires Alpaca credentials')
        return []
    if now<getattr(collector,'stock_recovery_backoff',0):
        await health('error','Stock recovery request failed; provider retry backoff active')
        return []
    from .active_observations import inventory
    from .universe import INDEX_UNDERLYINGS
    attempts=getattr(collector,'stock_recovery_attempts',{})
    def select_targets():
        with collector.db.tx() as c:
            live=set(inventory(collector.db,c,now,include_followups=False)['stocks'])
            symbols=[s for s in inventory(collector.db,c,now)['stocks'] if s not in INDEX_UNDERLYINGS]
            needed=[s for s in symbols if not fresh(collector.db.get(c,'quote:'+s,{}),now,age=2)]
            wanted=[s for s in needed if now-attempts.get(s,0)>=(2 if s in live else 60)]
            return sorted(wanted,key=lambda s:(s not in live,attempts.get(s,0),s))[:100],symbols,needed
    wanted,active,needed=await asyncio.to_thread(select_targets)
    collector.stock_recovery_attempts={s:t for s,t in attempts.items() if s in active}
    if not wanted:
        await health('partial' if needed else 'idle',
            'Waiting for the next recovery attempt' if needed else
            'Active underlying quotes are current; no recovery needed' if active else
            'No active underlying observations need recovery',
            active_symbols=len(active),waiting=len(needed),results=[])
        return []
    collector.stock_recovery_attempts.update({s:now for s in wanted})
    try:
        data=await asyncio.wait_for(collector.get('https://data.alpaca.markets/v2/stocks/quotes/latest',
            headers=collector.alpaca_headers,params={'symbols':','.join(wanted),'feed':collector.cfg.feed}),2)
        rows=[];results=[]
        for symbol in wanted:
            raw=data.get('quotes',{}).get(symbol,{})
            stamp=ts(raw.get('t'));at=time.time()
            q=dict(ts=stamp,bid=raw.get('bp'),ask=raw.get('ap'),bid_size=raw.get('bs'),ask_size=raw.get('as'),
                recovery='live_rest_stock',recovery_fetched_at=at,collection_version=COLLECTION_VERSION)
            valid=stamp is not None and 0<=at-stamp<=5 and fresh(q,at)
            if valid:rows.append((symbol,q,True))
            results.append(dict(symbol=symbol,status='fresh_quotes' if valid else 'no_fresh_quotes',latest_source_ts=stamp))
        if rows:await asyncio.to_thread(collector.quote_batch,'alpaca_stock_recovery',rows)
    except asyncio.CancelledError:raise
    except Exception as exc:
        code=getattr(exc,'status_code',None)
        collector.stock_recovery_backoff=time.time()+(300 if code in (401,403) else 60)
        results=[dict(status='unavailable',http_status=code,error=type(exc).__name__)]
    waiting=len(needed)-sum(r['status']=='fresh_quotes' for r in results)
    failed=any(r['status']=='unavailable' for r in results)
    await health('error' if failed else 'partial' if waiting else 'running',
        'Stock recovery request failed; provider retry backoff active' if failed else
        f'Original-timestamp underlying recovery: {waiting} still need fresh quotes',
        active_symbols=len(active),waiting=waiting,results=results,last_attempt_at=now)
    LOG.info('Stock recovery: %s',__import__('json').dumps(results,sort_keys=True))
    return results
