"""Explicit one-shot 0DTE entry timing research; no live strategy mutations."""
import base64
from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
import json
import math
import os
import random
import time
import uuid

import httpx
from sqlalchemy import select, update

from .market import session, ts
from .opening_wait_study import digest, normalize, prepare_days
from .store import Store, state, leases

VERSION = 'spy-0dte-timing-v1'
WAITS = (3, 5, 10, 15, 20, 30)
START, END, SPLIT = '2024-09-16', '2026-09-11', '2026-03-16'
PROTOCOL = {
    'version': VERSION, 'underlying': 'SPY', 'expiry': 'same date only', 'waits': WAITS,
    'start': START, 'end': END, 'validation_start': SPLIT,
    'signals': 'Frozen candle_confirmation premarket-breakout observations from the original study',
    'contract': 'Nearest whole-dollar strike to entry reference; exact half ties lower; standard 100-share contract verified as_of session date',
    'entry': 'First uncrossed positive-sized historical quote 1-6 seconds after signal; buy ask; entry spread <=20% midpoint',
    'primary_exit': 'First completed one-minute underlying close beyond fixed 1R stop or 2R target; otherwise 11:30 ET; first quote 1-6 seconds later',
    'R': 'Original fixed 0.20 times preceding 14-day mean true range',
    'secondary_exit': '11:30 ET fixed time; no intraday stops',
    'option_exit': 'Sell bid; valid zero bid marks a full premium write-off, not a verified fill',
    'fees_per_side': .65, 'fees_sensitivity': [0, .65, 1.15],
    'selection': 'Highest development mean net return on entry debit per common eligible session; >=50 trades; ties closest to 15',
    'comparison': 'Selected primary-exit window vs 15 on later validation; paired weekly bootstrap, 2000 draws, seed 17',
    'cohort': 'Every window needs evaluable evidence; missing evidence excludes the whole date; entry spread rejection is a known no-trade',
    'limits': '6000 requests and 20 minutes per run, maximum 10 request starts/second; missing inputs never fabricated',
    'notes': [
        'User requested 0DTE only. No 1DTE comparison or live parameter change.',
        'September 14-15 excluded: their option example was already inspected.',
        'Underlying outcomes were previously reviewed; this is new option pricing under a frozen follow-up protocol, not pristine unseen market data.',
        'No historical gamma filter; result applies to this specified premarket-breakout setup.',
        'Historical quotes are not verified executions; no market impact beyond bid/ask and fees.',
    ],
}


def emit(section, data):
    print('ZERO_DTE_TIMING '+json.dumps({'section': section, 'data': data}, allow_nan=False, separators=(',', ':')), flush=True)


def contract_spec(signal):
    price = signal['entry_reference']
    if not math.isfinite(price) or price <= 0:
        raise ValueError('invalid_entry_reference')
    low = math.floor(price)
    strike = low if price-low <= .5 else low+1
    kind = 'call' if signal['side'] == 1 else 'put'
    symbol = 'O:SPY'+signal['date'].replace('-', '')[2:]+('C' if kind == 'call' else 'P')+f'{strike*1000:08d}'
    return {'ticker': symbol, 'underlying_ticker': 'SPY', 'contract_type': kind,
            'expiration_date': signal['date'], 'strike_price': strike, 'shares_per_contract': 100}


def contract_valid(row, expected):
    return isinstance(row, dict) and all(row.get(k) == v for k, v in expected.items()) and not row.get('additional_underlyings')


def primary_exit(day, signal):
    entry, risk, side = signal['entry_reference'], signal['risk_dollars_per_share'], signal['side']
    for minute in range(signal['entry_minute'], 120):
        move = side*(day['rows'][minute][4]-entry)
        if move <= -risk:
            return day['open']+(minute+1)*60, 'underlying_close_stop'
        if move >= 2*risk:
            return day['open']+(minute+1)*60, 'underlying_close_target'
    return day['open']+7200, 'time'


def price_quote(payload, boundary, entry):
    """First valid market; wide entry is a no-trade, never pick a later narrow quote."""
    for raw in payload.get('results', [])[:100]:
        stamp = ts(raw.get('sip_timestamp'))
        values = [raw.get(k) for k in ('bid_price', 'ask_price', 'bid_size', 'ask_size')]
        if (stamp is None or not boundary+1 <= stamp <= boundary+6 or
                not all(isinstance(v, (float, int)) and math.isfinite(v) for v in values)):
            continue
        bid, ask, bs, ass = values
        if ask <= 0 or bid < 0 or bid > ask or ass < 1 or (bid > 0 and bs < 1):
            continue
        q = {'ts': stamp, 'bid': bid, 'ask': ask, 'bid_size': bs, 'ask_size': ass,
             'zero_bid_writeoff': bid == 0}
        if entry and (bid <= 0 or (ask-bid)/((ask+bid)/2) > .2):
            return {'status': 'entry_liquidity_filter', 'quote': q}
        return {'status': 'available', 'quote': q}
    return {'status': 'missing_quote', 'truncated': bool(payload.get('has_next_page'))}


class Evidence:
    def __init__(self, db, key):
        self.db, self.key = db, key
        self.requests, self.hits, self.last_start = 0, 0, 0.
        self.deadline = time.monotonic()+1200
        self.memory = {}
        self.client = httpx.Client(timeout=20, follow_redirects=False)

    def get(self, path, params):
        key = VERSION+':input:'+digest([path, params])
        if key in self.memory:
            return self.memory[key]
        with self.db.tx() as c:
            cached = self.db.get(c, key)
        if cached:
            self.hits += 1
            out = json.loads(gzip.decompress(base64.b64decode(cached['gzip_base64'])))
            self.memory[key] = out
            return out
        if self.requests >= 6000 or time.monotonic() >= self.deadline:
            raise ValueError('request_or_time_budget_exhausted')
        time.sleep(max(0, .1-(time.monotonic()-self.last_start)))
        self.last_start = time.monotonic()
        self.requests += 1
        response = self.client.get('https://api.massive.com'+path, params={**params, 'apiKey': self.key})
        if response.status_code == 404:
            out = {'status': 'not_found', 'results': None}
        elif response.status_code != 200:
            raise ValueError('massive_http_'+str(response.status_code))
        else:
            if len(response.content) > 2_000_000:
                raise ValueError('evidence_response_exceeds_bound')
            raw = response.json()
            out = {'results': raw.get('results'), 'status': raw.get('status'),
                   'has_next_page': bool(raw.get('next_url')), 'request_id': raw.get('request_id')}
        out['retrieved_at'] = time.time()
        out['query'] = {'path': path, 'params': params}
        packed = base64.b64encode(gzip.compress(json.dumps(out, separators=(',', ':')).encode())).decode()
        with self.db.tx() as c:
            self.db.put(c, key, {'sha256': digest(out), 'gzip_base64': packed})
        self.memory[key] = out
        return out

    def contract(self, signal):
        spec = contract_spec(signal)
        data = self.get('/v3/reference/options/contracts/'+spec['ticker'], {'as_of': signal['date']})
        return spec, contract_valid(data.get('results'), spec)

    def quote(self, symbol, boundary, entry=False):
        data = self.get('/v3/quotes/'+symbol, {'timestamp.gte': str(int((boundary+1)*1e9)),
            'timestamp.lte': str(int((boundary+6)*1e9)), 'sort': 'timestamp', 'order': 'asc', 'limit': 100})
        return price_quote({**data, 'results': data.get('results') or []}, boundary, entry)


def return_stats(rows, exit_name):
    trades = [r for r in rows if r['status'] == 'traded']
    def values(fee):
        return [((r[exit_name]['bid']-r['entry']['ask'])*100-2*fee)/(r['entry']['ask']*100+fee)
                if r['status'] == 'traded' else 0. for r in rows]
    returns = values(.65)
    dollars = [((r[exit_name]['bid']-r['entry']['ask'])*100-1.3) if r['status'] == 'traded' else 0. for r in rows]
    equity = peak = drawdown = 0.
    for v in returns:
        equity += v
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return {'sessions': len(rows), 'trades': len(trades), 'no_signal': sum(r['status']=='no_signal' for r in rows),
        'liquidity_skips': sum(r['status']=='entry_liquidity_filter' for r in rows),
        'mean_return_per_session': sum(returns)/len(rows) if rows else None,
        'mean_return_per_trade': sum(returns)/len(trades) if trades else None,
        'net_dollars_one_contract': sum(dollars), 'mean_dollars_per_trade': sum(dollars)/len(trades) if trades else None,
        'win_pct': 100*sum(v>0 for v in dollars)/len(trades) if trades else None,
        'max_drawdown_in_return_units': drawdown,
        'zero_bid_exits': sum(r[exit_name]['zero_bid_writeoff'] for r in trades),
        'mean_entry_debit': sum(r['entry']['ask']*100 for r in trades)/len(trades) if trades else None,
        'fee_sensitivity_return_per_session': {str(f): sum(values(f))/len(rows) if rows else None for f in (0, .65, 1.15)}}


def row_return(r, exit_name):
    if r['status'] != 'traded':
        return 0.
    return ((r[exit_name]['bid']-r['entry']['ask'])*100-1.3)/(r['entry']['ask']*100+.65)


def interval(rows, baseline, exit_name):
    groups = defaultdict(list)
    for r in rows:
        d = date.fromisoformat(r['date'])
        groups[(d-timedelta(days=d.weekday())).isoformat()].append(row_return(r, exit_name)-row_return(baseline[r['date']], exit_name))
    blocks = list(groups.values())
    if len(blocks) < 12:
        return {'status': 'insufficient_weeks', 'weeks': len(blocks)}
    rng, samples = random.Random(17), []
    for _ in range(2000):
        chosen = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        samples.append(sum(map(sum, chosen))/sum(map(len, chosen)))
    samples.sort()
    return {'status': 'estimated', 'weeks': len(blocks), 'draws': 2000,
        'mean_difference': sum(map(sum, blocks))/sum(map(len, blocks)),
        'lower95': samples[50], 'upper95': samples[1950]}


def analyze(records):
    bad_dates = {r['date'] for r in records if r['status'] not in ('traded', 'no_signal', 'entry_liquidity_filter')}
    cohort = [r for r in records if r['date'] not in bad_dates]
    dev = [r for r in cohort if r['date'] < SPLIT]
    val = [r for r in cohort if r['date'] >= SPLIT]
    development = {n: return_stats([r for r in dev if r['wait']==n], 'primary_exit') for n in WAITS}
    eligible = [n for n in WAITS if development[n]['trades'] >= 50]
    selected = max(eligible, key=lambda n:(development[n]['mean_return_per_session'], -abs(n-15))) if eligible else None
    validation = {n: return_stats([r for r in val if r['wait']==n], 'primary_exit') for n in WAITS}
    fixed = {n: return_stats([r for r in val if r['wait']==n], 'fixed_exit') for n in WAITS}
    base = {r['date']: r for r in val if r['wait']==15}
    chosen = [r for r in val if r['wait']==selected]
    return {'development': development, 'validation': validation, 'validation_fixed_exit': fixed,
        'selected_on_development': selected, 'selected_vs_15': interval(chosen, base, 'primary_exit'),
        'selected_vs_15_fixed_exit': interval(chosen, base, 'fixed_exit'),
        'included_dates': len({r['date'] for r in cohort}), 'excluded_dates': sorted(bad_dates),
        'exclusion_reasons': dict(Counter(r['status'] for r in records if r['date'] in bad_dates and r['status'] not in ('traded', 'no_signal', 'entry_liquidity_filter')))}


def cached_days(db):
    rows, daily = {}, None
    with db.tx() as c:
        cached = c.execute(select(state.c.value).where(state.c.key.startswith('spy-opening-wait-v1:input:'))).scalars().all()
    for value in cached:
        query = value['query']
        raw = json.loads(gzip.decompress(base64.b64decode(value['gzip_base64'])))
        if digest(raw) != value['sha256']:
            raise ValueError('cached_underlying_hash_mismatch')
        if query['timeframe'] == '1Day':
            daily = raw
        elif query['timeframe'] == '1Min':
            for row in normalize(raw):
                if row[0] in rows and rows[row[0]] != row:
                    raise ValueError('conflicting_underlying_cache')
                rows[row[0]] = row
    if not daily:
        raise ValueError('missing_daily_cache')
    days, missing = prepare_days(list(rows.values()), daily, START, END)
    if missing:
        raise ValueError('original_common_underlying_cohort_changed')
    return {d['date']: d for d in days}


def run():
    db = Store(os.environ['DATABASE_URL'])
    key = os.environ['MASSIVE_API_KEY']
    with db.tx() as c:
        original = db.get(c, 'spy-opening-wait-v1:latest')
        if not original:
            raise ValueError('original_study_required')
        signals = db.get(c, original['key']+':observations')
    run_id = digest([PROTOCOL, original['id']])
    run_key = VERSION+':run:'+run_id
    owner = uuid.uuid4().hex
    with db.tx() as c:
        old = db.get(c, run_key)
        if old and old.get('status') == 'complete':
            emit('report', old)
            return
        if not db.lease(c, VERSION, owner, 1300):
            raise ValueError('study_already_running')
    evidence = Evidence(db, key)
    manifest = {'status': 'running', 'id': run_id, 'protocol': PROTOCOL, 'original_study_id': original['id'],
                'started_at': time.time(), 'code_commit': os.getenv('RAILWAY_GIT_COMMIT_SHA')}
    records = []
    try:
        days = cached_days(db)
        by_day = defaultdict(list)
        for s in signals:
            if START <= s['date'] <= END and s['policy']=='candle_confirmation':
                by_day[s['date']].append(s)
        if set(by_day) != set(days) or any(sorted(s['wait'] for s in group) != list(WAITS) for group in by_day.values()):
            raise ValueError('incomplete_frozen_signal_cohort')
        emit('started', {**manifest, 'sessions': len(days)})
        for index, (d, group) in enumerate(sorted(by_day.items()), 1):
            day_key = run_key+':day:'+d
            with db.tx() as c:
                done = db.get(c, day_key)
            if done:
                records.extend(done)
                continue
            today = []
            for signal in group:
                r = {'date': d, 'wait': signal['wait']}
                if not signal['traded']:
                    today.append({**r, 'status': 'no_signal'})
                    continue
                spec, valid = evidence.contract(signal)
                r.update(contract=spec, side=signal['side'], entry_at=days[d]['open']+signal['entry_minute']*60)
                if not valid:
                    today.append({**r, 'status': 'contract_not_verified'})
                    continue
                entry = evidence.quote(spec['ticker'], r['entry_at'], True)
                if entry['status'] != 'available':
                    today.append({**r, **entry})
                    continue
                exit_at, reason = primary_exit(days[d], signal)
                primary = evidence.quote(spec['ticker'], exit_at)
                fixed = evidence.quote(spec['ticker'], days[d]['open']+7200)
                if primary['status'] != 'available' or fixed['status'] != 'available':
                    today.append({**r, 'status': 'missing_exit_quote', 'primary_status': primary['status'], 'fixed_status': fixed['status']})
                    continue
                today.append({**r, 'status': 'traded', 'entry': entry['quote'], 'primary_exit': primary['quote'],
                    'fixed_exit': fixed['quote'], 'primary_exit_at': exit_at, 'exit_reason': reason})
            records.extend(today)
            with db.tx() as c:
                db.put(c, day_key, today)
            if index % 20 == 0 or index == len(days):
                emit('progress', {'through': d, 'sessions': index, 'requests': evidence.requests, 'cache_hits': evidence.hits,
                                  'missing_records': sum(r['status'] in ('contract_not_verified','missing_quote','missing_exit_quote') for r in records)})
        results = analyze(records)
        report = {**manifest, 'status': 'complete', 'finished_at': time.time(), 'requests': evidence.requests,
                  'cache_hits': evidence.hits, 'results': results, 'records_key': run_key+':records'}
        with db.tx() as c:
            db.put(c, run_key+':records', records)
            db.put(c, run_key, report)
            db.put(c, VERSION+':latest', {'key': run_key, 'id': run_id})
        emit('results', results)
        emit('complete', {k:v for k,v in report.items() if k not in ('results','protocol')})
    except Exception as e:
        error = str(e) if isinstance(e, ValueError) else type(e).__name__
        with db.tx() as c:
            db.put(c, run_key, {**manifest, 'status': 'blocked', 'error': error, 'requests': evidence.requests})
        emit('blocked', {'error': error, 'requests': evidence.requests, 'completed_records': len(records)})
        raise
    finally:
        evidence.client.close()
        with db.tx() as c:
            c.execute(update(leases).where(leases.c.key==VERSION, leases.c.owner==owner).values(until=0))
        db.engine.dispose()


if __name__ == '__main__':
    if os.getenv('RUN_ZERO_DTE_TIMING') != 'true':
        emit('blocked', {'error': 'explicit_study_enable_required'})
    else:
        try:
            run()
        except Exception as error:
            emit('failed', {'error_type': type(error).__name__})
            raise SystemExit(1)
