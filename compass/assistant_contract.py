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
        from .assistant_quote import recover
        snapshot_quote=dict(row['quote'])
        row['quote'],recovery=await recover(collector,ticker,row['quote'],source,time.time())
        result=sample([row],req,None,time.time(),row['quote']['source'])
        for candidate in result['candidates']:
            candidate.update(snapshot_source=source,snapshot_quote=snapshot_quote,quote_recovery=recovery,
                             quote_method=row['quote']['method'])
        return dict(status='available',**result)

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


def assessment(question, context):
    """Source-backed exact-contract assessment; arithmetic never delegated to prose generation."""
    import re
    from .readiness import clock
    if not re.search(r'\b(?:good (?:play|trade)|worth buying)\b',question,re.I):
        return None
    data=context.get('option_research') or {}
    req=data.get('request') or {}
    if (req.get('status')!='requested' or req.get('strike') is None or
        req.get('expiry_start')!=req.get('expiry_end') or len(context.get('question_scope',{}).get('symbols',[]))!=1):
        return None
    symbol=context['question_scope']['symbols'][0]
    item=data.get('symbols',{}).get(symbol) or {}
    evidence=item.get('underlying_evidence') or {}
    history=evidence.get('daily_history') or context.get('swing_technical_context',{}).get(symbol) or {}
    stock=evidence.get('snapshot') or {}
    candidates=item.get('candidates') or []
    option=candidates[0] if candidates else {}
    side,strike=req['side'],req['strike']
    lines=[f"**{symbol} {req['expiry_start']} ${strike:g} {side} — {req['min_dte']} calendar days to expiry.**"]
    lines.append('A contract listing alone cannot establish a good trade. Here is the available trend evidence and what the option requires.')
    if history.get('status')=='ready':
        close,sma20,sma50=(number(history.get(k)) for k in ('close','sma20','sma50'))
        if all(v is not None for v in (close,sma20,sma50)):
            trend='above both averages' if close>max(sma20,sma50) else 'below both averages' if close<min(sma20,sma50) else 'between the averages'
            alignment='supports' if (side=='call' and close>max(sma20,sma50)) or (side=='put' and close<min(sma20,sma50)) else 'does not clearly support'
            lines.append(f"**Underlying evidence:** completed close ${close:.2f}, 20-day average ${sma20:.2f}, 50-day average ${sma50:.2f}; price was {trend}. "
                         f"This daily trend {alignment} the requested {side} direction, but does not establish an edge or predict the expiration price. "
                         f"Source: {history.get('source','recorded daily bars')}, through {history.get('through','date unavailable')}.")
        weekly,average=(number(history.get(k)) for k in ('weekly_close','weekly_sma10'))
        if weekly is not None and average is not None:
            lines.append(f"Completed weekly close ${weekly:.2f} versus 10-week average ${average:.2f}, through {history.get('weekly_through','date unavailable')}. "
                         'These are historical trend references, not automatic entry or exit levels.')
    else:
        lines.append('**Underlying evidence:** completed daily/weekly history is unavailable or incomplete; no directional conclusion is established.')
    trade,trade_at=number(stock.get('last_trade')),stock.get('trade_ts')
    if trade is not None:
        lines.append(f"Latest returned {symbol} trade: ${trade:.4f}, {stock.get('source','alpaca')}, {clock(trade_at) or 'source time unavailable'}. "
                     'This is a recorded observation, not a live executable price.')
    if not option:
        lines.append('**Exact contract:** no verified pricing returned ('+str(item.get('status',data.get('status','unavailable')))+'). No different strike or expiration was substituted.')
        return '\n\n'.join(lines)
    bid,ask=number(option.get('bid')),number(option.get('ask'))
    multiplier=number(option.get('multiplier'))
    lines.append(f"**Exact contract:** {option['symbol']}. Bid ${bid:.2f} / ask ${ask:.2f}. "
                 f"Source: {option.get('source',item.get('source','unavailable'))}; quote {clock(option.get('quote_ts')) or 'time unavailable'}; {option.get('quote_status','unavailable')}."
                 if bid is not None and ask is not None else f"**Exact contract:** {option['symbol']}; usable bid/ask unavailable.")
    recovery=option.get('quote_recovery') or {}
    if recovery.get('status')=='recovered':
        lines.append('Recovered quote: newest valid two-sided quote among the bounded provider results. '
                     'The snapshot was stale or unusable. The source timestamp above is retained; this is not a live premarket offer. '
                     'Contract metadata and Greeks remain from '+str(option.get('snapshot_source','the original snapshot'))+'.')
    elif recovery and option.get('quote_status')!='fresh':
        lines.append('No newer valid quote was recovered from the bounded lookups. Missing pricing remains unresolved.')
    if bid is not None and ask is not None and 0<bid<=ask and option.get('quote_status') in ('fresh','stale') and multiplier is not None and multiplier>0:
        breakeven=strike+ask if side=='call' else strike-ask
        lines.append(f"**At that recorded ask:** premium ${ask*multiplier:.2f} per contract (multiplier {multiplier:g}); expiration break-even ${breakeven:.2f}, before fees. "
                     f"Maximum long-option premium loss: ${ask*multiplier:.2f}, plus fees. These are illustrative calculations, not a current entry quote. "
                     'Expiration break-even is not a required price for selling the option profitably before expiry.')
        if trade and trade>0:
            move=(breakeven/trade-1)*100
            lines.append(f"That expiration break-even is {move:+.2f}% from the latest returned underlying trade. "
                         'The underlying and option observations may be from different times; this is not a synchronized trade setup.')
    greeks=option.get('greeks') or {}
    metrics=[]
    for name in ('delta','gamma','theta','vega'):
        value=number(greeks.get(name))
        if value is not None:metrics.append(f'{name} {value:.4f}')
    iv=number(option.get('implied_volatility'))
    if iv is not None:metrics.append(f'IV {100*iv:.2f}%')
    if metrics:
        lines.append('**Returned estimates:** '+', '.join(metrics)+'. Greeks/IV observation times were not supplied; the quote timestamp does not verify their freshness.')
    oi,volume=number(option.get('open_interest')),number(option.get('volume'))
    if oi is not None or volume is not None:
        lines.append(f"Returned open interest: {oi:g} (as of {option.get('oi_date') or 'date unavailable'}); " if oi is not None else 'Open interest unavailable; ')
        if volume is not None:lines[-1]+=f'returned session volume {volume:g}. These counts do not establish trade direction or executable liquidity.'
    lines.append('**Trade-off:** time decay works against a purchased call or put. A favorable underlying move or volatility change must offset the premium and decay; neither is guaranteed. '
                 'A trend-aligned contract is a research candidate, not a validated profitable strategy.')
    if option.get('quote_status')!='fresh':
        lines.append('**Before judging an entry:** refresh the exact contract bid/ask and underlying price during the options session. '
                     'The returned quote is not fresh enough to establish an executable price. Missing intraday timing or catalyst evidence remains unknown, not a passed check.')
    return '\n\n'.join(lines)
