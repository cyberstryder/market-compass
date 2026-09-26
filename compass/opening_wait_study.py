"""Offline SPY timing research. No imports from live strategy or notification code.

Run explicitly with RUN_OPENING_WAIT_STUDY=true. Historical evidence and results
use isolated state keys, never the live quote/bar journal or strategy settings.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, time as clock_time, timedelta, timezone
import base64
import gzip
import hashlib
import json
import math
import os
import random
import time
import uuid

import httpx

from .market import NY, session, ts
from .store import Store, events
from sqlalchemy import select, func

VERSION = 'spy-opening-wait-v1'
WAITS = (3, 5, 10, 15, 20, 30)
POLICIES = ('minimum_wait', 'candle_confirmation')
PROTOCOL = {
    'version': VERSION, 'symbol': 'SPY', 'waits': list(WAITS),
    'policies': list(POLICIES), 'feed': 'sip', 'adjustment': 'raw',
    'levels': 'Observed 04:00-09:29 New York premarket high and low, frozen at 09:30',
    'minimum_wait': 'After N opening minutes, evaluate each completed one-minute close',
    'candle_confirmation': 'Evaluate only completed N-minute candles aligned to 09:30',
    'trigger': 'First eligible close strictly above premarket high or below premarket low',
    'entry': 'Following one-minute bar open; one entry per session; no retrospective entry',
    'last_signal_minutes': 60, 'time_exit_minutes': 120,
    'risk': '0.20 times the mean true range of the previous 14 completed daily bars',
    'target_R': 2, 'cost_bps_each_side': 1, 'cost_sensitivity_bps_each_side': [0.5, 1, 2],
    'range_return': 'Any completed minute closes back across the trigger level in the next 10 minutes',
    'same_bar': 'Report stop-first/target-first outcome bounds when ordering is unknown',
    'selection': 'Highest development mean conservative net R per eligible session; ties prefer 15',
    'validation': 'Last six months untouched by selection; weekly block bootstrap of paired daily returns',
    'limitations': [
        'Underlying SPY price study, not historical option returns or the full gamma-based plan',
        'Historical OHLC includes vendor corrections; original real-time publication latency is unavailable',
        'Premarket levels use observed trades; missing trade minutes are not fabricated',
        'Next-bar opens and fixed costs are execution models, not verified fills',
        'First pass fixes stops/targets and tests timing; it does not optimize a trading strategy',
    ],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalize(raw):
    """Return unique ordered [timestamp, open, high, low, close, volume] rows."""
    found = {}
    for r in raw:
        row = [ts(r['t']), *[float(r[k]) for k in ('o', 'h', 'l', 'c', 'v')]]
        stamp, o, h, l, close, v = row
        if (not all(math.isfinite(x) for x in row) or stamp % 60 or
                min(o, h, l, close) <= 0 or v < 0 or l > min(o, close) or h < max(o, close) or h < l):
            raise ValueError('invalid_historical_bar')
        if stamp in found and found[stamp] != row:
            raise ValueError('conflicting_historical_bar')
        found[stamp] = row
    return [found[k] for k in sorted(found)]


def daily_risks(raw):
    """Snapshot ATR only from daily bars strictly preceding each session."""
    previous = None
    ranges = []
    dates = []
    out = {}
    for row in sorted(raw, key=lambda r: ts(r['t'])):
        d = datetime.fromtimestamp(ts(row['t']), NY).date().isoformat()
        o, h, l, close = (float(row[k]) for k in ('o', 'h', 'l', 'c'))
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, close)) or h < max(o, close) or l > min(o, close):
            raise ValueError('invalid_daily_bar')
        if d in dates:
            raise ValueError('duplicate_daily_bar')
        expected = []
        prior_date = date.fromisoformat(d)-timedelta(days=1)
        while len(expected) < 15:
            if session(prior_date.isoformat()):
                expected.append(prior_date.isoformat())
            prior_date -= timedelta(days=1)
        if len(ranges) >= 15 and dates[-15:] == list(reversed(expected)):
            out[d] = {'risk': 0.2 * sum(ranges[-14:]) / 14, 'prior_close': previous}
        ranges.append(max(h-l, abs(h-previous), abs(l-previous)) if previous is not None else h-l)
        dates.append(d)
        previous = close
    return out


def prepare_days(rows, daily, start, end):
    groups = defaultdict(dict)
    for row in rows:
        d = datetime.fromtimestamp(row[0], NY).date().isoformat()
        groups[d][row[0]] = row
    risks = daily_risks(daily)
    days, exclusions = [], []
    current = date.fromisoformat(start)
    while current <= date.fromisoformat(end):
        d = current.isoformat()
        current += timedelta(days=1)
        hours = session(d)
        if not hours:
            continue
        opened, closed = hours
        observed = groups.get(d, {})
        # Same complete morning is required by every timing variant.
        needed = [opened + i*60 for i in range(121)]
        missing = sum(stamp not in observed for stamp in needed)
        pre_start = datetime.combine(date.fromisoformat(d), clock_time(4), NY).timestamp()
        pre = [r for t, r in observed.items() if pre_start <= t < opened]
        reason = ('incomplete_regular_morning' if missing else
                  'insufficient_premarket' if len(pre) < 30 or max((r[0] for r in pre), default=0) < opened-120 else
                  'missing_prior_daily_history' if d not in risks else None)
        if reason:
            exclusions.append({'date': d, 'reason': reason, 'missing_regular_minutes': missing, 'premarket_bars': len(pre)})
            continue
        risk = risks[d]['risk']
        high, low = max(r[2] for r in pre), min(r[3] for r in pre)
        if risk <= 0 or high <= low:
            exclusions.append({'date': d, 'reason': 'invalid_range_or_risk'})
            continue
        rth = [observed[t] for t in needed]
        gap_atr = abs(rth[0][1]-risks[d]['prior_close']) / (risk/0.2)
        days.append({'date': d, 'open': opened, 'close': closed, 'rows': rth,
                     'high': high, 'low': low, 'risk': risk, 'premarket_bars': len(pre),
                     'gap_bucket': 'small' if gap_atr < 0.25 else 'medium' if gap_atr < 0.75 else 'large',
                     'gap_atr': gap_atr})
    return days, exclusions


def simulate(day, wait, policy):
    rows, risk = day['rows'], day['risk']
    base = {'date': day['date'], 'wait': wait, 'policy': policy, 'gap_bucket': day['gap_bucket'],
            'traded': False, 'net_R': 0., 'upper_R': 0., 'cost_R': {str(v): 0. for v in (0.5, 1, 2)}}
    for minute in range(wait, 61):
        if policy == 'candle_confirmation' and minute % wait:
            continue
        close = rows[minute-1][4]
        side = 1 if close > day['high'] else -1 if close < day['low'] else 0
        if side:
            break
    else:
        return base
    entry = rows[minute][1]
    trigger = day['high'] if side == 1 else day['low']
    stop, target = entry-side*risk, entry+side*2*risk
    lower = upper = None
    exit_minute, exit_reason = 120, 'time'
    for j in range(minute, 120):
        _, o, h, l, close, _ = rows[j]
        stop_open = side*(o-stop) <= 0
        target_open = side*(o-target) >= 0
        stop_hit = l <= stop if side == 1 else h >= stop
        target_hit = h >= target if side == 1 else l <= target
        if stop_open:
            lower = upper = o
            exit_reason = 'stop_gap'
        elif target_open:
            lower = upper = target
            exit_reason = 'target_gap_limit'
        elif stop_hit and target_hit:
            lower, upper = stop, target
            exit_reason = 'same_bar_ambiguous'
        elif stop_hit:
            lower = upper = stop
            exit_reason = 'stop'
        elif target_hit:
            lower = upper = target
            exit_reason = 'target'
        if lower is not None:
            exit_minute = j
            break
    if lower is None:
        lower = upper = rows[120][1]
    path = rows[minute:120]
    favorable = [(r[2]-entry if side == 1 else entry-r[3])/risk for r in path]
    adverse = [(r[3]-entry if side == 1 else entry-r[2])/risk for r in path]
    one_R = next((minute+i for i, value in enumerate(favorable) if value >= 1), None)
    costs = {str(bps): (side*(lower-entry)-bps/10000*(entry+lower))/risk for bps in (0.5, 1, 2)}
    return {**base, 'traded': True, 'side': side, 'entry_minute': minute,
            'entry_reference': entry, 'risk_dollars_per_share': risk, 'trigger': trigger,
            'exit_minute': exit_minute, 'exit_reason': exit_reason, 'net_R': costs['1'],
            'upper_R': (side*(upper-entry)-(entry+upper)/10000)/risk,
            'cost_R': costs, 'range_return_10m': any(side*(r[4]-trigger) <= 0 for r in rows[minute:minute+10]),
            'mfe_R_to_1130': max(favorable), 'mae_R_to_1130': min(adverse),
            'one_R_minute': one_R,
            'entry_distance_from_level_R': side*(entry-trigger)/risk}


def mean(values):
    return sum(values)/len(values) if values else None


def summary(rows, early=None):
    trades = [r for r in rows if r['traded']]
    returns = [r['net_R'] for r in rows]
    equity = peak = drawdown = 0.
    for value in returns:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    benchmark, missed = 0, 0
    if early is not None:
        for r in rows:
            b = early[r['date']]
            if b['traded'] and b['one_R_minute'] is not None:
                benchmark += 1
                missed += (not r['traded'] or r['side'] != b['side'] or r['entry_minute'] > b['one_R_minute'])
    return {
        'sessions': len(rows), 'trades': len(trades), 'no_trade': len(rows)-len(trades),
        'mean_net_R_per_session': mean(returns), 'mean_net_R_per_trade': mean([r['net_R'] for r in trades]),
        'win_pct': 100*sum(r['net_R'] > 0 for r in trades)/len(trades) if trades else None,
        'mean_upper_R_per_session': mean([r['upper_R'] for r in rows]),
        'total_net_R': sum(returns), 'max_drawdown_R': drawdown,
        'range_return_10m_pct': 100*sum(r['range_return_10m'] for r in trades)/len(trades) if trades else None,
        'median_entry_minute': sorted(r['entry_minute'] for r in trades)[len(trades)//2] if trades else None,
        'mean_mfe_R': mean([r['mfe_R_to_1130'] for r in trades]),
        'mean_mae_R': mean([r['mae_R_to_1130'] for r in trades]),
        'same_bar_ambiguous': sum(r['exit_reason'] == 'same_bar_ambiguous' for r in trades),
        'early_move_sessions': benchmark, 'missed_early_moves': missed,
        'missed_early_move_pct': 100*missed/benchmark if benchmark else None,
        'cost_sensitivity_R_per_session': {str(v): mean([r['cost_R'][str(v)] for r in rows]) for v in (0.5, 1, 2)},
    }


def paired_interval(rows, baseline, seed=17, draws=2000):
    groups = defaultdict(list)
    for r in rows:
        d = date.fromisoformat(r['date'])
        week = (d-timedelta(days=d.weekday())).isoformat()
        groups[week].append(r['net_R']-baseline[r['date']]['net_R'])
    blocks = list(groups.values())
    if len(blocks) < 12:
        return {'status': 'insufficient_weeks', 'weeks': len(blocks)}
    rng, samples = random.Random(seed), []
    for _ in range(draws):
        chosen = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        samples.append(sum(sum(b) for b in chosen)/sum(len(b) for b in chosen))
    samples.sort()
    return {'status': 'estimated', 'weeks': len(blocks), 'method': 'paired weekly block bootstrap',
            'draws': draws, 'mean_difference_R_per_session': mean([v for b in blocks for v in b]),
            'lower95': samples[int(draws*0.025)], 'upper95': samples[int(draws*0.975)]}


def analyze(days, split):
    all_rows = [simulate(d, n, p) for d in days for p in POLICIES for n in WAITS]
    report = {}
    for policy in POLICIES:
        policy_rows = [r for r in all_rows if r['policy'] == policy]
        development = [r for r in policy_rows if r['date'] < split]
        validation = [r for r in policy_rows if r['date'] >= split]
        dev = {n: summary([r for r in development if r['wait'] == n]) for n in WAITS}
        eligible = [n for n in WAITS if dev[n]['sessions'] and dev[n]['trades'] >= 30]
        selected = max(eligible, key=lambda n: (dev[n]['mean_net_R_per_session'], -abs(n-15))) if eligible else None
        early = {r['date']: r for r in validation if r['wait'] == 3}
        base = {r['date']: r for r in validation if r['wait'] == 15}
        val = {n: summary([r for r in validation if r['wait'] == n], early) for n in WAITS}
        chosen = [r for r in validation if r['wait'] == selected]
        interval = paired_interval(chosen, base) if selected is not None else {'status': 'insufficient_development_trades'}
        quarters = {}
        for r in validation:
            quarter = r['date'][:4]+'-Q'+str((int(r['date'][5:7])-1)//3+1)
            quarters.setdefault(quarter, []).append(r)
        report[policy] = {'development': dev, 'validation': val, 'selected_on_development': selected,
                          'selected_vs_15_validation': interval,
                          'validation_quarters': {q: {n: summary([r for r in v if r['wait'] == n]) for n in (selected, 15) if n is not None} for q, v in quarters.items()},
                          'validation_gap_buckets': {g: {n: summary([r for r in validation if r['wait'] == n and r['gap_bucket'] == g]) for n in (selected, 15) if n is not None} for g in ('small', 'medium', 'large')}}
    return report, all_rows


class History:
    def __init__(self, db, key, secret):
        self.db, self.requests, self.deadline = db, 0, time.monotonic()+1800
        self.client = httpx.Client(timeout=30, follow_redirects=False,
                                   headers={'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret})

    def fetch(self, timeframe, start, end):
        params = {'symbols': 'SPY', 'timeframe': timeframe, 'start': start, 'end': end,
                  'limit': 10000, 'feed': 'sip', 'adjustment': 'raw', 'sort': 'asc'}
        key = VERSION+':input:'+digest(params)
        with self.db.tx() as c:
            cached = self.db.get(c, key)
        if cached:
            return json.loads(gzip.decompress(base64.b64decode(cached['gzip_base64'])))
        rows, seen = [], set()
        while True:
            if self.requests >= 140 or time.monotonic() > self.deadline:
                raise ValueError('history_request_or_time_budget_exhausted')
            self.requests += 1
            response = self.client.get('https://data.alpaca.markets/v2/stocks/bars', params=params)
            if response.status_code != 200:
                raise ValueError('alpaca_http_'+str(response.status_code))
            if len(response.content) > 8_000_000:
                raise ValueError('history_response_too_large')
            payload = response.json()
            if not isinstance(payload.get('bars'), dict) or set(payload['bars'])-{'SPY'}:
                raise ValueError('invalid_history_envelope')
            rows.extend(payload['bars'].get('SPY', []))
            token = payload.get('next_page_token')
            if not token:
                break
            if token in seen:
                raise ValueError('history_pagination_loop')
            seen.add(token)
            params['page_token'] = token
            time.sleep(1)
        value = {'gzip_base64': base64.b64encode(gzip.compress(json.dumps(rows, separators=(',', ':')).encode())).decode(),
                 'received_at': time.time(), 'bars': len(rows), 'sha256': digest(rows), 'query': {k:v for k,v in params.items() if k != 'page_token'}}
        with self.db.tx() as c:
            self.db.put(c, key, value)
        time.sleep(1)
        return rows


def emit(kind, value):
    print('OPENING_STUDY '+json.dumps({'section': kind, 'data': value}, separators=(',', ':'), allow_nan=False), flush=True)


def run(start='2024-09-16', end='2026-09-15', split='2026-03-16'):
    now = datetime.now(timezone.utc)
    last = session(end)
    if not date.fromisoformat(start) < date.fromisoformat(split) < date.fromisoformat(end) or not last or last[1] >= now.timestamp():
        raise ValueError('study_dates_require_completed_sessions_and_held_out_period')
    if (date.fromisoformat(end)-date.fromisoformat(start)).days > 1096:
        raise ValueError('study_exceeds_three_year_bound')
    required = [os.getenv(k, '').strip() for k in ('DATABASE_URL', 'APCA_API_KEY_ID', 'APCA_API_SECRET_KEY')]
    if not all(required):
        raise ValueError('study_credentials_not_configured')
    db = Store(required[0])
    run_id = digest([PROTOCOL, start, end, split])
    key = VERSION+':run:'+run_id
    with db.tx() as c:
        previous = db.get(c, key)
        if previous and previous.get('status') == 'complete':
            emit('report', previous)
            return previous
        if not db.lease(c, VERSION, uuid.uuid4().hex, 1900):
            raise ValueError('study_already_running')
        audit = c.execute(select(func.count(), func.min(events.c.ts), func.max(events.c.ts)).where(
            events.c.source == 'tradermatrix', events.c.symbol.in_(('SPY', 'apex_SPY')),
            events.c.kind.in_(('matrix_raw', 'research')))).one()
        state = {'status': 'fetching', 'id': run_id, 'protocol': PROTOCOL, 'start': start, 'end': end,
                 'validation_start': split, 'started_at': time.time(),
                 'gamma_archive': {'rows': audit[0], 'first_received': audit[1], 'last_received': audit[2]}}
        db.put(c, key, state)
    emit('started', {k:state[k] for k in ('id', 'start', 'end', 'validation_start', 'gamma_archive')})
    history = History(db, required[1], required[2])
    try:
        daily_start = (date.fromisoformat(start)-timedelta(days=60)).isoformat()
        finish = (datetime.combine(date.fromisoformat(end)+timedelta(days=1), clock_time(), NY)-timedelta(seconds=1)).isoformat()
        daily = history.fetch('1Day', datetime.combine(date.fromisoformat(daily_start), clock_time(), NY).isoformat(), finish)
        collected = {}
        raw_count = 0
        cursor = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        while cursor <= end_date:
            next_month = (cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
            until = min(next_month, end_date+timedelta(days=1))
            part = history.fetch('1Min', datetime.combine(cursor, clock_time(), NY).isoformat(),
                                 (datetime.combine(until, clock_time(), NY)-timedelta(seconds=1)).isoformat())
            raw_count += len(part)
            for row in normalize(part):
                if row[0] in collected and collected[row[0]] != row:
                    raise ValueError('conflicting_month_boundary_bar')
                collected[row[0]] = row
            state.update(fetched_through=(until-timedelta(days=1)).isoformat(), raw_bars=raw_count, requests=history.requests)
            with db.tx() as c:
                db.put(c, key, state)
            emit('progress', {k:state[k] for k in ('fetched_through', 'raw_bars', 'requests')})
            cursor = until
        rows = [collected[k] for k in sorted(collected)]
        days, exclusions = prepare_days(rows, daily, start, end)
        if len(days) < 100:
            raise ValueError('fewer_than_100_complete_sessions')
        policies, observations = analyze(days, split)
        report = {**state, 'status': 'complete', 'finished_at': time.time(), 'input_sha256': digest(rows),
                  'normalized_bars': len(rows), 'eligible_sessions': len(days), 'excluded_sessions': exclusions,
                  'exclusion_counts': dict(Counter(e['reason'] for e in exclusions)), 'policies': policies,
                  'observations_key': key+':observations'}
        with db.tx() as c:
            db.put(c, key+':observations', observations)
            db.put(c, key, report)
            db.put(c, VERSION+':latest', {'key': key, 'id': run_id})
        emit('quality', {k:v for k,v in report.items() if k not in ('protocol', 'policies', 'excluded_sessions')})
        for p, value in policies.items():
            emit(p, value)
        emit('complete', {'id': run_id, 'state_key': key, 'observations': len(observations)})
        return report
    except Exception as e:
        state.update(status='blocked', error=str(e) if isinstance(e, ValueError) else type(e).__name__)
        with db.tx() as c:
            db.put(c, key, state)
        emit('blocked', {'id': run_id, 'error': state['error']})
        raise
    finally:
        history.client.close()
        db.engine.dispose()


if __name__ == '__main__':
    if os.getenv('RUN_OPENING_WAIT_STUDY') != 'true':
        emit('blocked', {'error': 'explicit_study_enable_required'})
    else:
        try:
            run()
        except Exception as error:
            # Never print request headers, connection strings or exception repr.
            emit('failed', {'error_type': type(error).__name__})
            raise SystemExit(1)
