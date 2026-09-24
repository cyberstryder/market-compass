"""Bounded, read-only exact-contract lookups; never expand a strategy universe."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .market import number, ts
from .swing_signals import daily_context


def contract_symbol(symbol, req):
    expiry = datetime.strptime(req['expiry_start'], '%Y-%m-%d').strftime('%y%m%d')
    strike = int(Decimal(str(req['strike']))*1000)
    return f"{symbol}{expiry}{'C' if req['side']=='call' else 'P'}{strike:08d}"


async def bounded(fn):
    try:
        async with asyncio.timeout(15):
            return await fn()
    except Exception as error:
        return {'status':'lookup_timeout' if isinstance(error,TimeoutError) else 'lookup_unavailable'}


async def exact_research(collector, symbol, req, source):
    from .assistant_options import sample
    ticker = contract_symbol(symbol, req)

    async def option():
        if source == 'massive':
            data = await collector.get(f'https://api.massive.com/v3/snapshot/options/{symbol}/O:{ticker}',
                                       params={'apiKey':collector.cfg.massive})
            x = data.get('results') or {}
            d, g, q = x.get('details') or {}, x.get('greeks') or {}, x.get('last_quote') or {}
            row = dict(symbol=d.get('ticker'), expiry=d.get('expiration_date'), strike=number(d.get('strike_price')),
                type=d.get('contract_type'), multiplier=d.get('shares_per_contract'), oi=x.get('open_interest'),
                oi_date=None, iv=x.get('implied_volatility'), volume=(x.get('day') or {}).get('volume'),
                quote=dict(ts=ts(q.get('last_updated')),bid=q.get('bid'),ask=q.get('ask'),
                           bid_size=q.get('bid_size'),ask_size=q.get('ask_size')))
        else:
            # Exact metadata and keyed snapshot avoid near-spot pagination omissions.
            d, data = await asyncio.gather(
                collector.get('https://paper-api.alpaca.markets/v2/options/contracts/'+ticker,collector.alpaca_headers),
                collector.get('https://data.alpaca.markets/v1beta1/options/snapshots',collector.alpaca_headers,
                              {'symbols':ticker,'feed':'opra'}))
            x = (data.get('snapshots') or {}).get(ticker) or {}
            g, q = x.get('greeks') or {}, x.get('latestQuote') or {}
            row = dict(symbol=d.get('symbol'),expiry=d.get('expiration_date'),strike=number(d.get('strike_price')),
                type=d.get('type'),multiplier=number(d.get('size')),oi=number(d.get('open_interest')),
                oi_date=d.get('open_interest_date'),iv=x.get('impliedVolatility'),volume=None,
                quote=dict(ts=ts(q.get('t')),bid=q.get('bp'),ask=q.get('ap'),bid_size=q.get('bs'),ask_size=q.get('as')))
        if (str(row.get('symbol') or '').removeprefix('O:') != ticker or
            row['expiry'] != req['expiry_start'] or row['strike'] != req['strike'] or row['type'] != req['side']):
            return dict(status='exact_contract_unavailable', candidates=[], matching_contracts=0)
        row.update({k:number(g.get(k)) for k in ('delta','gamma','theta','vega')})
        return dict(status='available',**sample([row],req,None,time.time(),source))

    async def underlying():
        if not (collector.cfg.alpaca_key and collector.cfg.alpaca_secret):
            return dict(status='provider_not_configured',source='alpaca')
        now=time.time()
        start=(datetime.fromtimestamp(now,timezone.utc)-timedelta(days=160)).date().isoformat()
        async def snapshot():
            data=await collector.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/snapshot',
                collector.alpaca_headers,{'feed':collector.cfg.feed})
            q,t=data.get('latestQuote') or {}, data.get('latestTrade') or {}
            clock=ts(q.get('t')); bid,ask=number(q.get('bp')),number(q.get('ap'))
            valid=bid is not None and ask is not None and 0<bid<=ask
            return dict(status='available',source='alpaca',feed=collector.cfg.feed,
                bid=bid,ask=ask,quote_ts=clock,
                quote_status='fresh' if valid and clock and 0<=now-clock<=60 else 'stale' if valid and clock and clock<=now else 'unavailable',
                last_trade=number(t.get('p')),trade_ts=ts(t.get('t')),
                daily_bar=data.get('dailyBar'),previous_daily_bar=data.get('prevDailyBar'))
        async def history():
            data=await collector.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/bars',collector.alpaca_headers,
                {'feed':collector.cfg.feed,'timeframe':'1Day','start':start,'limit':1000,'adjustment':'split','sort':'asc'})
            rows=[dict(ts=ts(b.get('t')),payload=b) for b in data.get('bars') or [] if ts(b.get('t')) is not None]
            result=daily_context(rows,now)
            return dict(result,source='alpaca',feed=collector.cfg.feed,adjustment='split',
                        complete_response=not bool(data.get('next_page_token')))
        snap,hist=await asyncio.gather(bounded(snapshot),bounded(history))
        return dict(snapshot=snap,daily_history=hist,
                    note='Question-scoped observations. Fetch time is not quote time; daily indicators exclude the current session.')

    contract,evidence=await asyncio.gather(bounded(option),bounded(underlying))
    return dict(contract,source=source,requested_contract=ticker,fetched_at=time.time(),
                exact_match_required=True,underlying_evidence=evidence)
