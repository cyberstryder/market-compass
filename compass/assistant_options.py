"""Read-only, question-scoped option evidence, independent of strategy eligibility."""
import asyncio
import re
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from .market import number
from .providers import Collectors

POLICY = (
    'Ask Compass supports research from 0DTE through LEAPS, independently of automated strategies. '
    'Answer the requested direction and horizon first using supplied underlying and option evidence. '
    'Give conditional bullish/bearish scenarios and invalidation using recorded levels where available. '
    'Strategy min/max DTE, zero_dte max_entries=0, and a missing simulated fill are not research prohibitions. '
    'entry_evidence_incomplete concerns verification of automated entry eligibility, not permission to analyze. '
    'Do not substitute another expiration or ask permission to discuss the requested horizon. '
    'Separate market thesis, contract quote availability, and automated strategy eligibility. '
    'If option quotes are missing or stale, still analyze available underlying evidence, but do not invent '
    'a contract price, executable entry, Greeks, or expected option profit. '
    'Use quote_status and source quote timestamps; fetched_at is not the quote time. '
    'These candidates are a bounded near-spot sample, not ranked recommendations or complete chain coverage. '
    'No tools or follow-up scans can be invoked by the answer; never promise to run one. '
    'A closed scheduled SPY plan remains closed; independent research is allowed and is not a signal under that plan. '
)


def request(question, now):
    q = question.lower()
    if not re.search(r'\b(options?|puts?|calls?|leaps?|\d+\s*dte)\b', q):
        return None
    today = datetime.fromtimestamp(now, ZoneInfo('America/New_York')).date()
    lo, hi, basis = 0, 1095, 'No expiry specified; sample listed expirations from today through three years.'
    dates = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', q)
    dte = re.search(r'\b(\d+)\s*(?:-|to|–)\s*(\d+)\s*dte\b', q)
    exact = re.search(r'\b(\d+)\s*dte\b', q)
    duration = re.search(r'\b(\d+)\s*[- ]?\s*(days?|weeks?|months?|years?)\b', q)
    if dates:
        try:
            days = [(date.fromisoformat(d)-today).days for d in dates[:2]]
            lo, hi = min(days), max(days)
        except ValueError:
            return dict(status='invalid_expiry', note='Invalid calendar expiration date; clarify the requested date.')
        basis = 'Explicit expiration date(s).'
    elif dte:
        lo, hi = map(int, dte.groups()); basis = 'Explicit calendar DTE range.'
    elif exact:
        lo = hi = int(exact[1]); basis = 'Explicit calendar DTE.'
    elif 'today' in q or 'same day' in q or 'same-day' in q:
        lo = hi = 0; basis = 'Today in the exchange timezone.'
    elif 'tomorrow' in q:
        lo = hi = 1; basis = 'Tomorrow in calendar days.'
    elif duration:
        n, unit = duration.groups(); days = int(n)*({'d':1,'w':7,'m':30,'y':365}[unit[0]])
        lo, hi = max(0, days-7), days+7; basis = 'Approximate stated horizon, plus/minus seven calendar days.'
    elif re.search(r'\bleaps?\b', q):
        lo, hi = 365, 1095; basis = 'LEAPS research window: one to three years; listed availability varies.'
    if lo < 0 or hi < lo or hi > 1095:
        return dict(status='invalid_expiry', note='Requested expiration must be between today and 1095 calendar days.')
    side = 'put' if re.search(r'\bputs?\b',q) else 'call' if re.search(r'\bcalls?\b',q) else None
    return dict(status='requested', min_dte=lo, max_dte=hi, side=side, basis=basis,
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
        eligible.append(o)
    # Round-robin expirations prevents the first expiry from crowding out LEAPS.
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
                multiplier=o.get('multiplier'), open_interest=o.get('oi'), oi_date=o.get('oi_date')))
    return dict(matching_contracts=len(eligible), candidates=selected, sample_only=True)


async def research(cfg, scope, req, quotes, now):
    result = dict(request=req, symbols={}, execution='Read-only research; no simulated or broker orders.')
    if req['status'] != 'requested': return result
    source = 'massive' if cfg.massive else 'alpaca' if cfg.alpaca_key and cfg.alpaca_secret else None
    if not source:
        result['status'] = 'provider_not_configured'; return result
    collector = Collectors(None, cfg)
    try:
        async def one(symbol):
            try:
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
