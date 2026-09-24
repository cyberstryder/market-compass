"""Read-only exact-symbol quote recovery. Never turn retrieval time into market time."""
import asyncio
from .market import number, ts


def valid(q, now):
    bid,ask,stamp=(number(q.get(k)) for k in ('bid','ask','ts'))
    return bid is not None and ask is not None and 0<bid<=ask and stamp is not None and 0<=now-stamp<=7*86400


async def recover(collector, ticker, original, source, now):
    original=dict(original,source=source,method='snapshot')
    if valid(original,now) and now-original['ts']<=60:
        return original, {'status':'not_needed'}

    async def latest():
        data=await collector.get('https://data.alpaca.markets/v1beta1/options/quotes/latest',collector.alpaca_headers,
                                 {'symbols':ticker,'feed':'opra'})
        q=(data.get('quotes') or {}).get(ticker) or {}
        return [dict(bid=q.get('bp'),ask=q.get('ap'),ts=ts(q.get('t')),bid_size=q.get('bs'),ask_size=q.get('as'),
                     source='alpaca',method='latest_opra_quote')]

    async def history():
        data=await collector.get('https://api.massive.com/v3/quotes/O:'+ticker,params={
            'apiKey':collector.cfg.massive,'timestamp.gte':int((now-7*86400)*1e9),
            'timestamp.lte':int(now*1e9),'sort':'timestamp','order':'desc','limit':5000})
        return [dict(bid=q.get('bid_price'),ask=q.get('ask_price'),ts=ts(q.get('sip_timestamp')),
                     bid_size=q.get('bid_size'),ask_size=q.get('ask_size'),source='massive',method='historical_quote')
                for q in (data.get('results') or [])[:5000]]

    async def attempt(name, fn):
        try:
            async with asyncio.timeout(6):
                rows=await fn()
            return rows, dict(source=name,status='valid_quote_returned' if any(valid(q,now) for q in rows) else 'no_valid_quote_returned')
        except Exception as error:
            return [], dict(source=name,status='timeout' if isinstance(error,TimeoutError) else 'unavailable')

    tasks=[]
    if collector.cfg.alpaca_key and collector.cfg.alpaca_secret:tasks.append(attempt('alpaca',latest))
    if collector.cfg.massive:tasks.append(attempt('massive',history))
    outcomes=await asyncio.gather(*tasks)
    candidates=[original]+[q for rows,_ in outcomes for q in rows]
    candidates=[q for q in candidates if valid(q,now)]
    chosen=max(candidates,key=lambda q:q['ts']) if candidates else original
    if not candidates and number(chosen.get('ts')) is not None and now-number(chosen['ts'])>7*86400:
        chosen=dict(chosen,bid=None,ask=None)  # Raw snapshot remains separately preserved.
    recovered=chosen['method']!='snapshot'
    return chosen, dict(status='recovered' if recovered else 'no_newer_valid_quote',
        attempts=[result for _,result in outcomes],lookback_days=7,max_history_records=5000,
        note='Newest valid quote among bounded returned records; history is not a current executable price.')
