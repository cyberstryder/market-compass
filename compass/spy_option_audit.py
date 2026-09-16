"""Bounded read-only SPY gamma and options calculation from dated evidence.

This is a two-session feasibility calculation, not a gamma-strategy backtest.
No live state, notification, broker, or strategy module is invoked.
"""
import base64
from datetime import date, timedelta
import gzip
import json
import os
import time

import httpx
from sqlalchemy import select, text

from .exposure import calculate
from .market import number, session, ts
from .research import apex_levels
from .store import Store, events

DATES = ('2026-09-14', '2026-09-15')
WAITS = (3, 5, 10, 15, 20, 30)
POLICY = {
    'dates': DATES, 'waits': WAITS, 'entry_policy': 'candle_confirmation',
    'signals': 'Reuse frozen premarket-breakout study signals; gamma is descriptive only',
    'contract': 'Closest listed standard 100-share strike in an archived chain available at entry; tie lower strike',
    'expiries': 'Same session and next market session; label actual calendar DTE',
    'entry': 'First valid quote 1-6 seconds after signal boundary; buy one contract at ask',
    'exit': 'First valid quote 1-6 seconds after 11:30 ET; sell at bid; fixed time, no premium stops',
    'fees': 'Illustrative $0.65 per contract per side; also report before fees',
    'quote_gates': 'Positive bid <= ask, positive sizes, spread <=20% of midpoint; missing stays missing',
    'provider_budget': 'At most 64 Massive historical quote requests, 5 minutes, no pagination or indicative quotes',
    'limitation': 'Different exit experiment from original intrabar-stop study; cannot compare P&L directly',
}


def emit(section, data):
    print('SPY_OPTION_AUDIT '+json.dumps({'section': section, 'data': data}, allow_nan=False), flush=True)


def prior_event(c, kind, symbol, deadline):
    return c.execute(select(events).where(events.c.kind == kind, events.c.symbol == symbol,
        events.c.ts <= deadline, events.c.received <= deadline)
        .order_by(events.c.received.desc(), events.c.id.desc()).limit(1)).mappings().first()


def chain_at(c, deadline):
    row = prior_event(c, 'chain_archive', 'SPY', deadline)
    if not row:
        return None, {'status': 'no_prior_chain'}
    raw = gzip.decompress(base64.b64decode(row['payload']['data']))
    if len(raw) > 12_000_000:
        raise ValueError('chain_archive_exceeds_bound')
    chain = json.loads(raw)
    info = {'event_id': row['id'], 'recorded_at': row['received'], 'asof': chain.get('asof'),
            'age_seconds': deadline-row['received'], 'contracts': len(chain.get('contracts', [])),
            'complete': chain.get('complete'), 'source': chain.get('source')}
    if not isinstance(chain.get('asof'), (float, int)) or chain['asof'] > deadline or deadline-chain['asof'] > 3600:
        return None, {**info, 'status': 'chain_future_or_older_than_one_hour'}
    return chain, {**info, 'status': 'available'}


def next_session(day):
    d = date.fromisoformat(day)+timedelta(days=1)
    while not session(d.isoformat()):
        d += timedelta(days=1)
    return d.isoformat()


def choose(chain, spot, side, expiry):
    kind = 'call' if side == 1 else 'put'
    contracts = [o for o in chain.get('contracts', []) if o.get('underlying') == 'SPY'
                 and o.get('type') == kind and o.get('expiry') == expiry and o.get('multiplier') == 100
                 and number(o.get('strike')) is not None and o['strike'] > 0]
    return min(contracts, key=lambda o: (abs(o['strike']-spot), o['strike'], o['symbol'])) if contracts else None


def valid_quote(q):
    bid, ask, bs, ass = (number(q.get(k)) for k in ('bid', 'ask', 'bid_size', 'ask_size'))
    return (all(v is not None for v in (bid, ask, bs, ass)) and 0 < bid <= ask and bs >= 1 and ass >= 1
            and (ask-bid)/((ask+bid)/2) <= .2)


def pnl(entry, exit_quote, fee_each=.65):
    gross = (exit_quote['bid']-entry['ask'])*100
    debit = entry['ask']*100
    return {'entry_ask': entry['ask'], 'exit_bid': exit_quote['bid'], 'debit_dollars': debit,
            'before_fees_dollars': gross, 'net_dollars': gross-2*fee_each,
            'net_return_pct': 100*(gross-2*fee_each)/(debit+fee_each),
            'fee_each_side': fee_each, 'entry_quote': entry, 'exit_quote': exit_quote}


class Quotes:
    def __init__(self, key):
        self.key, self.requests, self.cache = key, 0, {}
        self.deadline = time.monotonic()+300
        self.client = httpx.Client(timeout=15, follow_redirects=False)
        self.blocked = None

    def get(self, c, symbol, boundary):
        key = (symbol, boundary)
        if key in self.cache:
            return self.cache[key]
        start, end = boundary+1, boundary+6
        rows = c.execute(select(events.c.ts, events.c.received, events.c.payload).where(
            events.c.kind == 'quote', events.c.symbol == symbol, events.c.ts >= start,
            events.c.ts <= end, events.c.received <= end)
            .order_by(events.c.received, events.c.ts).limit(101)).all()
        for r in rows[:100]:
            q = r.payload
            if q.get('ts') == r.ts and 0 <= r.received-r.ts <= 5 and valid_quote(q):
                result = {'status': 'available', 'quote': {k:q[k] for k in ('ts', 'bid', 'ask', 'bid_size', 'ask_size')},
                          'source': 'original_archive', 'recorded_at': r.received}
                self.cache[key] = result
                return result
        if self.blocked or not self.key:
            return {'status': self.blocked or 'historical_quote_credentials_not_configured'}
        if self.requests >= 64 or time.monotonic() > self.deadline:
            return {'status': 'quote_budget_exhausted'}
        self.requests += 1
        response = self.client.get('https://api.massive.com/v3/quotes/'+symbol,
            params={'apiKey': self.key, 'timestamp.gte': str(int(start*1e9)),
                    'timestamp.lte': str(int(end*1e9)), 'sort': 'timestamp', 'order': 'asc', 'limit': 100})
        if response.status_code != 200:
            self.blocked = 'historical_quotes_http_'+str(response.status_code)
            return {'status': self.blocked}
        if len(response.content) > 500_000:
            return {'status': 'quote_response_exceeds_bound'}
        payload = response.json()
        for raw in payload.get('results', [])[:100]:
            q = {'ts': ts(raw.get('sip_timestamp')), 'bid': raw.get('bid_price'), 'ask': raw.get('ask_price'),
                 'bid_size': raw.get('bid_size'), 'ask_size': raw.get('ask_size')}
            if q['ts'] is not None and start <= q['ts'] <= end and valid_quote(q):
                result = {'status': 'available', 'quote': q, 'source': 'massive_historical_nbbo',
                          'retrieved_at': time.time(), 'original_local_receipt': 'unknown'}
                self.cache[key] = result
                time.sleep(.25)
                return result
        return {'status': 'no_eligible_quote', 'truncated': bool(payload.get('next_url'))}


def gamma_at(c, deadline):
    out = {'decision_at': deadline}
    raw = prior_event(c, 'matrix_raw', 'apex_SPY', deadline)
    if raw:
        apex = apex_levels(raw['payload']['data'], 'SPY', raw['received'])
        out['vendor'] = {**apex, 'event_id': raw['id'], 'recorded_at': raw['received'],
                         'source_age_seconds': deadline-apex['source_ts'] if apex.get('source_ts') else None}
    else:
        out['vendor'] = {'status': 'no_prior_apex'}
    chain, info = chain_at(c, deadline)
    out['chain'] = info
    if chain:
        spot, stamp = number(chain.get('selection_reference')), number(chain.get('selection_reference_ts'))
        if spot and stamp and 0 <= deadline-stamp <= 120:
            exposure = calculate(chain['contracts'], spot, chain['asof'], chain['source'], chain.get('complete', False))
            strikes = sorted(exposure.pop('strikes'), key=lambda r: abs(r['gex']), reverse=True)
            out['calculated_gex'] = {**exposure, 'top_strikes': strikes[:10], 'spot_source_ts': stamp,
                'scope_expiries': sorted({o['expiry'] for o in chain['contracts']}),
                'greek_source_clock': 'Snapshot receipt known; individual Greek publication timestamps unavailable'}
        else:
            out['calculated_gex'] = {'status': 'underlying_reference_not_fresh', 'spot': spot, 'spot_source_ts': stamp}
    return out


def run():
    db = Store(os.environ['DATABASE_URL'])
    quotes = Quotes(os.getenv('MASSIVE_API_KEY', ''))
    emit('protocol', POLICY)
    results = []
    try:
        with db.tx() as c:
            c.execute(text('SET TRANSACTION READ ONLY'))
            c.execute(text("SET LOCAL statement_timeout = '20s'"))
            latest = db.get(c, 'spy-opening-wait-v1:latest')
            observations = db.get(c, latest['key']+':observations') if latest else []
            for d in DATES:
                opened = session(d)[0]
                for minutes in (0, 15):
                    emit('gamma', {'date': d, 'opening_minutes': minutes, **gamma_at(c, opened+minutes*60)})
                signals = [r for r in observations if r['date'] == d and r['policy'] == 'candle_confirmation']
                for signal in signals:
                    if not signal['traded']:
                        result = {'date': d, 'wait': signal['wait'], 'status': 'no_signal'}
                        results.append(result)
                        emit('scenario', result)
                        continue
                    entry_at = opened+60*signal['entry_minute']
                    chain, info = chain_at(c, entry_at)
                    for expiry in (d, next_session(d)):
                        result = {'date': d, 'wait': signal['wait'], 'expiry': expiry,
                            'calendar_dte': (date.fromisoformat(expiry)-date.fromisoformat(d)).days,
                            'side': signal['side'], 'signal_at': entry_at, 'entry_reference': signal['entry_reference'],
                            'time_exit_at': opened+7200, 'chain': info,
                            'gamma_filter_applied': False, 'historical_signal_policy': 'frozen_premarket_breakout'}
                        contract = choose(chain, signal['entry_reference'], signal['side'], expiry) if chain else None
                        if not contract:
                            result['status'] = 'no_prior_eligible_contract'
                        else:
                            result['contract'] = {k:contract.get(k) for k in ('symbol', 'expiry', 'strike', 'type', 'multiplier')}
                            entry = quotes.get(c, contract['symbol'], entry_at)
                            exit_quote = quotes.get(c, contract['symbol'], opened+7200)
                            result.update(entry_evidence=entry, exit_evidence=exit_quote)
                            if entry['status'] == exit_quote['status'] == 'available':
                                result.update(status='calculated', **pnl(entry['quote'], exit_quote['quote']))
                            else:
                                result['status'] = 'missing_quote_evidence'
                        results.append(result)
                        emit('scenario', result)
        emit('complete', {'scenarios': len(results), 'calculated': sum(r['status']=='calculated' for r in results),
                          'provider_requests': quotes.requests, 'database_writes': 0})
    finally:
        quotes.client.close()
        db.engine.dispose()


if __name__ == '__main__':
    if os.getenv('RUN_SPY_OPTIONS_AUDIT') != 'true':
        emit('blocked', {'reason': 'explicit_audit_enable_required'})
    else:
        try:
            run()
        except Exception as error:
            emit('failed', {'error_type': type(error).__name__})
            raise SystemExit(1)
