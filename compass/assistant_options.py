"""Read-only, question-scoped option evidence, independent of strategy eligibility."""
import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from .market import number
from .providers import Collectors

POLICY = (
    'Ask Compass supports research from 0DTE through 21 DTE, independently of automated strategies. '
    'Answer the requested direction and horizon first using supplied underlying and option evidence. '
    'Give conditional bullish/bearish scenarios and invalidation using recorded levels where available. '
    'Strategy min/max DTE, zero_dte max_entries=0, and a missing simulated fill are not research prohibitions. '
    'entry_evidence_incomplete concerns verification of automated entry eligibility, not permission to analyze. '
    'For an exact strike request, candidates are exact matches, not a near-spot sample. '
    'Use supplied underlying_evidence for the requested symbol even when it is outside the watchlist. '
    'After hours, distinguish latest recorded evidence from an executable live quote. '
    'Do not substitute another expiration or ask permission to discuss the requested horizon. '
    'Separate market thesis, contract quote availability, and automated strategy eligibility. '
    'If option quotes are missing or stale, still analyze available underlying evidence, but do not invent '
    'a contract price, executable entry, Greeks, or expected option profit. '
    'Use quote_status and source quote timestamps; fetched_at is not the quote time. '
    'Only discuss paper eligibility when the user asks about automation, simulated entries or that scheduled plan. '
    'Never ask the user to enable simulated entries to receive research. '
    'A configured max_entries of 0 means no entry-count limit, not zero allowed entries. '
    'End with the analysis and specific missing evidence, not an offer to watch, scan or simulate later. '
    'Use technical_context.asof for technical observations, not context capture time; label old observations. '
    'Do not turn a requested call or put into evidence that the market is bullish or bearish. '
    'Option quote availability and liquidity provide pricing evidence only, not directional support. '
    'Do not invent trigger levels or list downside targets above the triggering level. '
    'These candidates are a bounded near-spot sample, not ranked recommendations or complete chain coverage. '
    'Copy the requested expiration dates and DTE bounds from option_research.request exactly; never recalculate or expand them. '
    'For multiple horizons, handle each option_research.requests item separately; never silently choose one. '
    'No tools or follow-up scans can be invoked by the answer; never promise to run one. '
    'A closed scheduled SPY plan remains closed; independent research is allowed and is not a signal under that plan. '
)


def request(question, now):
    q = question.lower().replace('‑', '-').replace('—', '-').replace('–', '-')
    shorthand = re.findall(r'(?<![\w./])([0-9]+(?:\.[0-9]{1,3})?)\s*([cp])\b', q)
    if not shorthand and not re.search(r'\b(options?|puts?|calls?|\d+\s*dte)\b', q):
        return None
    today = datetime.fromtimestamp(now, ZoneInfo('America/New_York')).date()
    lo, hi, basis = 0, 21, 'No expiry specified; sample listed expirations from today through 21 calendar days.'
    dates = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', q)
    slash_dates = re.findall(r'\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b', q)
    try:
        dates += [date(int(y) if len(y)==4 else 2000+int(y),int(m),int(d)).isoformat() for m,d,y in slash_dates]
    except ValueError:
        return dict(status='invalid_expiry', note='Invalid calendar expiration date.')
    dte = re.search(r'\b(\d+)\s*(?:-|to)\s*(\d+)\s*(?:dte|(?:calendar\s+)?days?(?:\s+to\s+expir(?:ation|y))?)\b', q)
    exact = re.search(r'\b(\d+)\s*dte\b', q)
    duration = re.search(r'\b(\d+)\s*[- ]?\s*(days?|weeks?|months?|years?)\b', q)
    if dte:
        lo, hi = map(int, dte.groups()); basis = 'Explicit calendar DTE range.'
    elif dates:
        try:
            days = [(date.fromisoformat(d)-today).days for d in dates[:2]]
            lo, hi = min(days), max(days)
        except ValueError:
            return dict(status='invalid_expiry', note='Invalid calendar expiration date; clarify the requested date.')
        basis = 'Explicit expiration date(s).'
    elif exact:
        lo = hi = int(exact[1]); basis = 'Explicit calendar DTE.'
    elif 'today' in q or 'same day' in q or 'same-day' in q:
        lo = hi = 0; basis = 'Today in the exchange timezone.'
    elif 'tomorrow' in q:
        lo = hi = 1; basis = 'Tomorrow in calendar days.'
    elif duration:
        n, unit = duration.groups(); days = int(n)*({'d':1,'w':7,'m':30,'y':365}[unit[0]])
        lo, hi = max(0, days-7), days+7; basis = 'Approximate stated horizon, plus/minus seven calendar days.'
    if lo < 0 or hi < lo or hi > 21:
        return dict(status='invalid_expiry', note='Requested expiration must be between today and 21 calendar days.')
    side = 'put' if re.search(r'\bputs?\b',q) else 'call' if re.search(r'\bcalls?\b',q) else None
    if shorthand:
        if len(set(shorthand)) != 1 or (side and side != {'p':'put','c':'call'}[shorthand[0][1]]):
            return dict(status='ambiguous_contract', note='Multiple or conflicting strikes/directions; no substitute contract selected.')
        side = {'p':'put','c':'call'}[shorthand[0][1]]
        strike = float(shorthand[0][0])
        if not 0 < strike < 100000:
            return dict(status='invalid_contract', note='Invalid strike.')
    else:
        strike = None
    return dict(strike=strike, status='requested', min_dte=lo, max_dte=hi, side=side, basis=basis,
                expiry_start=(today+timedelta(days=lo)).isoformat(), expiry_end=(today+timedelta(days=hi)).isoformat())


def sample(rows, req, spot, now, source):
    eligible = []
    for o in rows:
        if not req['expiry_start'] <= str(o.get('expiry','')) <= req['expiry_end']:
            continue
        if o.get('type') not in ('put','call') or req['side'] and o['type'] != req['side']:
            continue
        if not o.get('symbol') or not (number(o.get('strike')) or 0)>0:
            continue
        if req.get('strike') is not None and number(o.get('strike')) != req['strike']:
            continue
        eligible.append(o)
    # Round-robin expirations prevents the first expiry from crowding out farther expirations.
    groups = {}
    for o in eligible: groups.setdefault(o['expiry'], []).append(o)
    expiries = sorted(groups)
    if len(expiries)>4:
        expiries = [expiries[round(i*(len(expiries)-1)/3)] for i in range(4)]
    selected = []
    for expiry in expiries:
        candidates = sorted(groups[expiry], key=lambda o: (abs(o['strike']-spot) if spot else 0, o['symbol']))[:2]
        for o in candidates:
            q = o.get('quote') or {}; t = number(q.get('ts')); bid, ask = number(q.get('bid')), number(q.get('ask'))
            valid = bid is not None and ask is not None and 0 < bid <= ask
            status = 'fresh' if valid and t is not None and 0 <= now-t <= 60 else 'stale' if valid and t is not None and t <= now else 'unavailable'
            selected.append(dict(symbol=o['symbol'], expiry=expiry, strike=o['strike'], type=o['type'],
                source=source, bid=bid, ask=ask, quote_ts=t, quote_status=status,
                multiplier=o.get('multiplier'), open_interest=o.get('oi'), oi_date=o.get('oi_date'),
                greeks={k:o.get(k) for k in ('delta','gamma','theta','vega')}, implied_volatility=o.get('iv'),
                volume=o.get('volume'), bid_size=q.get('bid_size'), ask_size=q.get('ask_size'),
                metadata_timestamp_note='Greeks/IV observation time unavailable unless explicitly supplied; OI uses oi_date.'))
    return dict(matching_contracts=len(eligible), candidates=selected, sample_only=True,
                evidence_role='Contract pricing and availability only; no directional inference from this sample.')


async def research(cfg, scope, req, quotes, now, db=None):
    result = dict(request=req, symbols={}, execution='Read-only research; no simulated or broker orders.')
    if req['status'] != 'requested': return result
    source = 'massive' if cfg.massive else 'alpaca' if cfg.alpaca_key and cfg.alpaca_secret else None
    if not source and db is not None:
        return await queued_research(db, scope, req, quotes, now)
    if not source:
        result['status'] = 'provider_not_configured'; return result
    collector = Collectors(None, cfg)
    try:
        async def one(symbol):
            try:
                if req.get('strike') is not None and req['expiry_start']==req['expiry_end']:
                    from .assistant_contract import exact_research
                    return symbol, await exact_research(collector, symbol, req, source)
                async with asyncio.timeout(18):
                    fn = collector.massive_chain if source=='massive' else collector.alpaca_chain
                    rows, complete = await fn(symbol, req['expiry_start'], req['expiry_end'], page_limit=4)
                quote = quotes.get(symbol) or {}
                bid, ask = number(quote.get('bid')), number(quote.get('ask'))
                spot = (bid+ask)/2 if bid and ask and bid<=ask else None
                fetched_at = time.time()
                return symbol, dict(status='available', source=source, fetched_at=fetched_at, complete=complete,
                    **sample(rows, req, spot, fetched_at, source))
            except Exception as error:
                # Provider details can contain credentials; expose only stable labels.
                return symbol, dict(status='lookup_timeout' if isinstance(error,TimeoutError) else 'lookup_unavailable', source=source)
        result['symbols'] = dict(await asyncio.gather(*(one(s) for s in scope[:3])))
        result['omitted_symbols'] = scope[3:]
    finally:
        await collector.close()
    return result


async def queued_research(db, scope, req, quotes, now, timeout=25):
    """Web service has no market credentials; the collector owns the lookup."""
    from .store import state
    key = 'ask-options:' + uuid.uuid4().hex
    def enqueue():
        with db.tx() as c:
            db.put(c, key, dict(status='pending', at=time.time(), expires=time.time()+timeout,
                scope=scope[:3], omitted_symbols=scope[3:], request=req,
                quotes={s:quotes.get(s) for s in scope[:3]}, context_at=now))
    def read():
        with db.tx() as c: return db.get(c,key,{})
    def remove():
        with db.tx() as c: c.execute(state.delete().where(state.c.key==key))
    await asyncio.to_thread(enqueue)
    try:
        async with asyncio.timeout(timeout):
            while True:
                job=await asyncio.to_thread(read)
                if job.get('status')=='complete': return job['result']
                await asyncio.sleep(.4)
    except TimeoutError:
        return dict(request=req,status='collector_lookup_timeout',symbols={},
                    note='Collector did not return option evidence before the request deadline; underlying research remains available.')
    finally:
        await asyncio.to_thread(remove)


async def collect_requests(collector):
    """Bounded read-only research queue shared by web and collector services."""
    from .store import state, leases
    db, now = collector.db, time.time()
    owner=uuid.uuid4().hex
    def claim():
        with db.tx() as c:
            c.execute(state.delete().where(state.c.key.like('ask-options:%'),state.c.updated<now-120))
            c.execute(leases.delete().where(leases.c.key.like('ask-options:%'),leases.c.until<now-120))
            pending=sorted(db.prefix(c,'ask-options:').items(),key=lambda pair:pair[1].get('at',0))
            for key,job in pending:
                if job.get('status')=='pending' and job.get('expires',0)>now:
                    if db.lease(c,key,owner,30): return key,job
    claimed=await asyncio.to_thread(claim)
    if not claimed: return
    key,job=claimed
    result=await research(collector.cfg,job['scope'],job['request'],job['quotes'],job['context_at'])
    result['omitted_symbols']=job['omitted_symbols']
    def finish():
        with db.tx() as c:
            current=db.get(c,key)
            if current and current.get('expires',0)>time.time():
                db.put(c,key,dict(status='complete',result=result))
    await asyncio.to_thread(finish)


def record_evidence(db, result, now):
    """Keep small daily verification records, never user prompts or credentials."""
    from .market import day
    req=result.get('request',{})
    bucket='0dte' if req.get('min_dte')==req.get('max_dte')==0 else 'other'
    symbols=result.get('symbols') or {'request':{'status':result.get('status','unavailable')}}
    with db.tx() as c:
        for symbol,item in symbols.items():
            candidates=item.get('candidates',[])
            db.put(c,'ask-evidence:'+day(now)+':'+symbol+':'+bucket,dict(at=now,symbol=symbol,horizon=bucket,
                status=item.get('status',result.get('status','unknown')),source=item.get('source'),
                expiry_start=req.get('expiry_start'),expiry_end=req.get('expiry_end'),
                returned_expirations=sorted({r['expiry'] for r in candidates if r.get('expiry')}),
                candidate_count=len(candidates),fresh_quotes=sum(r.get('quote_status')=='fresh' for r in candidates),
                basis='Latest read-only provider lookup for this symbol and horizon today; not model-answer validation or an entry.'))
