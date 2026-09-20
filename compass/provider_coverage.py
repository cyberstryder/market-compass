"""Source inventory, real minute recovery, and independent SPY range verification."""
from datetime import datetime, timezone
import time
import asyncio
from .spy_range import valid_bars, verify
from .market import day, session, NY

VERSION = 'provider-minute-inventory-v1'


def compare(start, end, provider_minutes, stored_minutes, fetched_at, complete):
    expected=set(range(int(start),int(end),60))
    provider={int(t) for t in provider_minutes if t in expected}
    stored={int(t) for t in stored_minutes if t in expected}
    return dict(version=VERSION, start=start, end=end, fetched_at=fetched_at,
        status='verified_inventory' if complete else 'incomplete_provider_response',
        expected_minutes=len(expected), provider_bars=len(provider), stored_bars=len(stored),
        provider_absent_minutes=sorted(expected-provider) if complete else [],
        collector_missing_minutes=sorted(provider-stored),
        stored_only_minutes=sorted(stored-provider) if complete else [],
        provider_inventory_complete=complete, entry_gate_unchanged=True,
        note='Provider absence does not identify the reason for omission. No synthetic bars or changes to frozen research.')


async def collect(collector, now=None):
    now=time.time() if now is None else now
    hours=session(day(now))
    if not hours:
        collector.db.health("provider_coverage","scheduled","Provider inventory comparison resumes next equity session")
        return
    start=datetime.fromisoformat(day(now)).replace(hour=4,tzinfo=NY).timestamp()
    end=min(int(now//60)*60,hours[0])
    if end<=start or now>hours[0]+7200:
        collector.db.health("provider_coverage","scheduled","Independent SPY inventory comparison runs during the morning window")
        return
    params=dict(timeframe='1Min',start=datetime.fromtimestamp(start,timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end-.001,timezone.utc).isoformat(),feed=collector.cfg.feed,
        adjustment='split',sort='asc',limit=1000)
    data=await collector.get('https://data.alpaca.markets/v2/stocks/SPY/bars',collector.alpaca_headers,params)
    minutes=valid_bars(data,start,end,60)
    # Repair actual source bars only; no interpolation or synthetic clock minutes.
    await asyncio.to_thread(collector.bars,'alpaca', [('SPY',t,b) for t,b in minutes.items()])
    stamps=list(minutes)
    proof=None
    if end==hours[0]:
        five_data=await collector.get('https://data.alpaca.markets/v2/stocks/SPY/bars',
            collector.alpaca_headers,{**params,'timeframe':'5Min'})
        five=valid_bars(five_data,start,end,300)
        proof=verify(start,end,minutes,five,time.time(),collector.cfg.feed)
    # Provider I/O completes before opening the short database transaction.
    with collector.db.tx() as c:
        stored=[r[0] for r in collector.db.get(c,'bar_window:SPY',[]) if len(r)>=6]
        result=compare(start,end,stamps,stored,time.time(),not data.get('next_page_token'))
        result.update(symbol='SPY',feed=collector.cfg.feed,request=params)
        collector.db.put(c,'provider_coverage:SPY:'+day(now),result)
        if proof is not None:
            collector.db.put(c,'spy-provider-range:'+day(now),proof)
    collector.db.health("provider_coverage","available" if result["provider_inventory_complete"] else "partial",
        "SPY source inventory and verified-range check; timeframe research unchanged",result["fetched_at"])
    return result
