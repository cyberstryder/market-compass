"""Timestamped morning plan. Informational alerts only; no order or entry-rule writes."""
import asyncio
from datetime import datetime
import math
import time
import uuid

from .exposure import calculate
from .index_reference import reference_pair
from .market import CT, NY, day, dedup, fresh, number, session
from .spy_confirmation import (CONFIRMED, SCHEDULE_CT, assess, check_minute, report_key,
                               reports_for_day, scheduled_phase)
from .vendor_freshness import confirmation, context_check

VERSION = 'spy-morning-brief-v4'


def stamp(value):
    return datetime.fromtimestamp(value, CT).strftime('%b %d %H:%M:%S CT') if value is not None else 'unknown time'


def money(value):
    return f'{value:,.2f}' if number(value) is not None else 'unavailable'


def billions(value):
    return f'{value / 1e9:+.2f}B' if number(value) is not None else 'unavailable'


def bar_context(db, c, now, phase='preview'):
    hours = session(day(now))
    if not hours:
        return {'status': 'closed', 'reason': 'No regular equity session today'}
    opening, closing = hours
    raw = db.get(c, 'bar_window:SPY', [])
    rows = dedup([{'ts': r[0], 'payload': dict(zip(('o', 'h', 'l', 'c', 'v', 'vw'), r[1:]))}
                  for r in raw if len(r) >= 6], now)
    rows = [r for r in rows if all(number(r['payload'].get(k)) is not None for k in ('o', 'h', 'l', 'c', 'v'))
            and 0 < r['payload']['l'] <= min(r['payload']['o'], r['payload']['c'])
            <= max(r['payload']['o'], r['payload']['c']) <= r['payload']['h'] and r['payload']['v'] >= 0]
    pm_start = datetime.fromisoformat(day(now)).replace(hour=4, tzinfo=NY).timestamp()
    pm = [r for r in rows if pm_start <= r['ts'] < opening]
    rth = [r for r in rows if opening <= r['ts'] < closing]
    first = [r for r in rth if r['ts'] < opening + 900]
    expected_pm = max(0, int((min(now, opening) - pm_start) // 60))
    expected_minutes = {pm_start + i*60 for i in range(expected_pm)}
    observed_minutes = {r['ts'] for r in pm}
    missing_minutes = sorted(expected_minutes - observed_minutes)
    pm_complete = (expected_pm >= 60 and len(pm) >= .9 * expected_pm and pm
                   and min(now, opening) - (pm[-1]['ts'] + 60) <= 120)
    first_complete = {r['ts'] for r in first} == {opening + i * 60 for i in range(15)}
    minute = check_minute(opening, now, phase)
    boundary = opening + minute*60
    confirming = [r for r in rth if boundary-900 <= r['ts'] < boundary]
    candle_complete = {r['ts'] for r in confirming} == {boundary-900 + i*60 for i in range(15)}
    history_complete = {r['ts'] for r in rth if r['ts'] < boundary} == {opening + i*60 for i in range(minute)}
    candle = {'minute': minute, 'start': boundary-900, 'end': boundary,
              'bars': len(confirming), 'complete': candle_complete,
              'close': confirming[-1]['payload']['c'] if candle_complete else None}
    from .spy_daily import evidence as daily_evidence
    daily = daily_evidence(db, c, now)
    prior = daily['prior']
    atr = daily['atr14_daily']
    quote = db.get(c, 'quote:SPY')
    valid_quote = fresh(quote, now, 15) and quote['ts'] <= now
    latest = (rth or pm)[-1] if (rth or pm) else None
    spot = (quote['bid'] + quote['ask']) / 2 if valid_quote else latest['payload']['c'] if latest else None
    spot_ts = quote['ts'] if valid_quote else latest['ts'] + 60 if latest else None
    groups = {}
    for label, group in (('premarket', pm), ('regular', rth)):
        volume = sum(r['payload']['v'] for r in group)
        native = all(number(r['payload'].get('vw')) is not None for r in group)
        groups[label] = {'bars': len(group), 'high': max((r['payload']['h'] for r in group), default=None),
            'low': min((r['payload']['l'] for r in group), default=None),
            'open': group[0]['payload']['o'] if group else None,
            'vwap': sum((r['payload']['vw'] if number(r['payload'].get('vw')) is not None else r['payload']['c'])
                        * r['payload']['v'] for r in group) / volume if volume else None,
            'vwap_basis': 'source bar VWAP' if native else 'minute-close approximation',
            'asof': group[-1]['ts'] + 60 if group else None}
    recent = (rth or pm)[-4:]
    closes = [r['payload']['c'] for r in recent]
    structure = ('rising closes' if len(closes) == 4 and all(a < b for a, b in zip(closes, closes[1:])) else
                 'falling closes' if len(closes) == 4 and all(a > b for a, b in zip(closes, closes[1:])) else 'mixed closes')
    volume_rows = (rth or pm)[-21:]
    rv = None
    if len(volume_rows) == 21 and sum(r['payload']['v'] for r in volume_rows[:-1]) > 0:
        rv = volume_rows[-1]['payload']['v'] / (sum(r['payload']['v'] for r in volume_rows[:-1]) / 20)
    return {'status': 'available' if spot_ts is not None and 0 <= now - spot_ts <= 90 else 'stale_or_missing',
        'session_open': opening, 'session_close': closing, 'spot': spot, 'spot_ts': spot_ts,
        'spot_basis': 'quote midpoint' if valid_quote else 'completed minute close',
        'daily_readiness': daily,
        'prior': {k: number(prior.get(k)) for k in ('h', 'l', 'c')}, 'atr14_daily': atr,
        'premarket_complete': bool(pm_complete), 'premarket_expected_bars': expected_pm,
        'premarket_coverage_evidence': {
            'basis': 'Observed valid minute bars versus elapsed clock minutes; absence alone does not identify a feed failure',
            'required_bars': math.ceil(.9 * expected_pm), 'threshold_fraction': .9,
            'missing_minutes': missing_minutes, 'missing_count': len(missing_minutes),
            'first_observed': pm[0]['ts'] if pm else None,
            'last_observed': pm[-1]['ts'] if pm else None,
            'provider_omission_status': 'not_verified',
        },
        'first15_complete': first_complete, 'first15_close': first[-1]['payload']['c'] if first_complete else None,
        'first15_asof': opening + 900 if first_complete else None,
        'confirmation_candle': candle, 'confirmation_history_complete': history_complete,
        'structure': structure, 'recent_closes': closes, 'minute_volume_ratio': rv, **groups}


def _fresh_squeeze(latest, now):
    """SPY squeeze state only if scanned during the current session day.

    Prevents a stale prior-session squeeze (e.g. Friday's fire) from
    appearing as current in the morning brief.
    """
    at = (latest or {}).get('at')
    if not at or day(at) != day(now):
        return {}
    return (latest.get('states') or {}).get('SPY', {})


def gamma_context(db, c, now, spot):
    apex = db.get(c, 'apex:SPY', {})
    apex_check = context_check(apex, now)
    apex_ok = apex_check['eligible_for_context']
    ranked = sorted((r for r in apex.get('levels', []) if r.get('kind') == 'apex'),
                    key=lambda r: -(r.get('score') or 0))[:5] if apex_ok else []
    flip = next((r['price'] for r in apex.get('levels', []) if r.get('kind') == 'gamma_flip'), None) if apex_ok else None
    market = db.get(c, 'research:gamma_market', {})
    item = next((r for r in market.get('items', []) if r.get('symbol') == 'SPY'), {})
    check = confirmation({**market, 'source_ts': item.get('source_ts')}, now, 1800 if now < session(day(now))[0] else 180,
                         1800 if now < session(day(now))[0] else 180)
    vendor = item.get('data', {}) if check['eligible_for_live_confirmation'] else {}
    overview_flip = number(vendor.get('gammaFlipLevel'))
    apex_flip = flip
    if flip is None and overview_flip and overview_flip > 0:
        flip = overview_flip
    chain = db.get(c, 'chain:SPY', {})
    expiries = sorted({o.get('expiry') for o in chain.get('contracts', []) if isinstance(o.get('expiry'), str) and o['expiry'] >= day(now)})[:5]
    vendor_expiries = apex.get('expirations', []) if apex_ok else []
    aligned = bool(vendor_expiries and all(isinstance(d, str) and d in expiries for d in vendor_expiries))
    chosen = vendor_expiries if aligned else expiries
    contracts = [o for o in chain.get('contracts', []) if o.get('expiry') in chosen]
    recent_chain = 0 <= now - chain.get('asof', 0) <= 180
    proxy = calculate(contracts, spot, now, chain.get('source'), chain.get('complete', False)) if recent_chain and spot else {}
    calls = calculate([o for o in contracts if o.get('type') == 'call'], spot, now, chain.get('source')) if proxy else {}
    puts = calculate([o for o in contracts if o.get('type') == 'put'], spot, now, chain.get('source')) if proxy else {}
    local_gex = proxy.get('gex')
    vendor_gex = number(vendor.get('totalGEX'))
    disagreements = []
    if local_gex is not None and vendor_gex is not None and local_gex * vendor_gex < 0:
        disagreements.append('Vendor and Compass GEX signs disagree')
    if not aligned:
        disagreements.append('Apex/Compass expiry scope not verified equal')
    overview_scope = vendor.get('expirationsUsed')
    if not isinstance(overview_scope, list) or sorted(str(v) for v in overview_scope) != sorted(chosen):
        disagreements.append('Gamma overview/Compass expiry scope not verified equal')
    if apex_flip is not None and overview_flip is not None and abs(apex_flip - overview_flip) > .01:
        disagreements.append('Overview/Apex flips differ: ' + money(overview_flip) + '/' + money(apex_flip) + '; source clocks differ')
    if proxy.get('status') != 'available':
        disagreements.append('Compass GEX inputs partial or unavailable')
    today_proxy = calculate([o for o in contracts if o.get('expiry') == day(now)], spot, now, chain.get('source'), chain.get('complete', False)) if proxy else {}
    return {'apex_status': 'available' if apex_ok else apex_check['cache_status'], 'apex_asof': apex.get('source_ts'),
        'ranked': ranked, 'flip': flip, 'flip_asof': apex.get('source_ts') if apex_flip is not None else item.get('source_ts'),
        'flip_source': 'Apex' if apex_flip is not None else 'overview',
        'vendor_expiries': vendor_expiries, 'vendor_gex': vendor_gex,
        'vendor_call_gex': number(vendor.get('totalCallGEX')), 'vendor_put_gex': number(vendor.get('totalPutGEX')),
        'vendor_status': check['status'], 'vendor_asof': item.get('source_ts'),
        'local': {k: proxy.get(k) for k in ('gex', 'coverage', 'contracts', 'usable_gex', 'status')},
        'local_ranked': sorted(proxy.get('strikes', []), key=lambda r: -abs(r['gex']))[:5],
        'local_call_gex': calls.get('gex'), 'local_put_gex': puts.get('gex'),
        'put_call_ratio': abs(puts['gex'] / calls['gex']) if calls.get('gex') else None,
        'local_asof': chain.get('asof'), 'local_expiries': chosen, 'zero_dte_gex': today_proxy.get('gex'),
        'zero_dte_coverage': today_proxy.get('coverage'), 'discrepancies': disagreements,
        'dealer_levels': db.get(c, 'gamma_levels:SPY', {}),
        'squeeze': _fresh_squeeze(db.get(c, 'squeeze:latest', {}), now),
        'interpretation': 'Positioning context only. Call-positive/put-negative proxy is not measured dealer inventory; gamma sign alone does not select a trade.'}


def option_reference(db, c, now, kind, reference):
    chain = db.get(c, 'chain:SPY', {})
    out = {'side': kind, 'expiry': day(now), 'status': 'unavailable'}
    if reference is None or not 0 <= now - chain.get('asof', 0) <= 180:
        return {**out, 'reason': 'No recent same-day contract snapshot'}
    opts = [o for o in chain.get('contracts', []) if o.get('underlying') == 'SPY' and o.get('expiry') == day(now)
            and o.get('type') == kind and o.get('multiplier') == 100 and number(o.get('strike'))
            and o['strike'] == int(o['strike'])]
    if not opts:
        return {**out, 'reason': 'No verified standard same-day contract'}
    opt = min(opts, key=lambda o: (abs(o['strike'] - reference), o['strike']))
    out.update(symbol=opt['symbol'], strike=opt['strike'], chain_asof=chain['asof'])
    opening, closing = session(day(now))
    if not opening <= now < closing:
        return {**out, 'reason': 'SPY options regular session is closed; premium withheld'}
    quotes = [q for q in (db.get(c, 'quote:' + opt['symbol']), opt.get('quote'))
              if fresh(q, now, 10) and q['ts'] <= now and q['ts'] >= opening]
    if not quotes:
        return {**out, 'reason': 'No fresh option bid/ask'}
    q = max(quotes, key=lambda q: q['ts'])
    mid = (q['bid'] + q['ask']) / 2
    spread = (q['ask'] - q['bid']) / mid
    return {**out, 'status': 'available' if spread <= .20 else 'wide_spread', 'bid': q['bid'], 'ask': q['ask'],
        'mid': mid, 'quote_ts': q['ts'], 'spread_pct': spread * 100,
        'delta_snapshot': number(opt.get('delta')), 'iv_snapshot': number(opt.get('iv')),
        'premium_at_ask': q['ask'] * 100, 'max_debit_with_entry_fee': q['ask'] * 100 + .65,
        'reason': 'Watch contract; quote is not a fill. Premium can be lost in full.'}


def build(db, c, now, phase='preview'):
    context = bar_context(db, c, now, phase)
    from .spy_range import apply_verified_range
    apply_verified_range(db, c, context, now, day(now))
    out = {'version': VERSION, 'status': 'spy_morning_brief', 'alert_category': 'spy_morning',
           'phase': phase, 'day': day(now), 'generated_at': now, 'context': context, 'symbol': 'SPY'}
    if context['status'] == 'closed':
        return {**out, 'decision': 'MARKET CLOSED', 'expires_at': now}
    opening = context['session_open']
    assessment = assess(context, reports_for_day(db, c, day(now)), now, phase)
    gamma = gamma_context(db, c, now, context['spot'] if context['status'] == 'available' else None)
    pm = context['premarket']
    risk = (context.get('atr14_daily') or 0) * .2
    decision = assessment['decision']
    close = context['confirmation_candle']['close']
    plans = {}
    for side, sign, trigger in (('call', 1, pm.get('high')), ('put', -1, pm.get('low'))):
        if trigger is None or not risk:
            plans[side] = {'status': 'unavailable'}
            continue
        confirmed = decision == side.upper() + ' SETUP CONFIRMED'
        entry = close if confirmed else trigger
        levels = sorted({r['price'] for r in gamma['ranked'] if sign * (r['price'] - entry) > 0}, reverse=side == 'put')[:2]
        target_2r = entry + sign * 2 * risk
        # Wall-aware target (default since 2026-10-06): nearest Apex/GEX wall
        # between entry and the 2R target, minus a $0.15 buffer. Backtest:
        # 71% win, +0.36R when the wall sits at least 0.5R out; falls back
        # to blind 2R when no wall is in the way or it's too close to pay.
        wall = None
        walls = ({number(r.get('price')) for r in gamma.get('ranked', [])} |
                 {number(r.get('strike')) for r in gamma.get('local_ranked', [])})
        for px in sorted(walls, reverse=(side == 'put')):
            if px is None:
                continue
            d = sign * (px - entry)
            if 0 < d < sign * (target_2r - entry):
                wall = px
                break
        if wall is not None and sign * (wall - entry) >= 0.5 * risk:
            target, target_rule = wall - sign * 0.15, 'wall'
        else:
            target, target_rule = target_2r, '2r'
        plans[side] = {'status': 'confirmed' if confirmed else 'conditional', 'trigger': trigger,
            'entry_reference': entry, 'stop': entry - sign * risk, 'target': target,
            'target_2r': target_2r, 'target_rule': target_rule, 'wall': wall,
            'level_targets': levels, 'risk_R': risk,
            'rule': 'Completed 15-minute close above premarket high' if side == 'call' else 'Completed 15-minute close below premarket low'}
    ref = db.get(c, 'index_reference:SPY', {})
    mapping = reference_pair(ref.get('spy_close'), ref.get('spx_close'), ref.get('reference_day'), now, ref.get('source'))
    if mapping['status'] == 'available':
        mapping.update(received=ref.get('received'), computed_at=now)
    chart = []
    def add(label, value):
        if number(value) is None:
            return
        chart.append({'label': label, 'spy': value,
            'spx_estimate': value * mapping['ratio'] if mapping['status'] == 'available' else None,
            'xsp_estimate': value * mapping['ratio'] / 10 if mapping['status'] == 'available' else None})
    add('PM high', pm.get('high')); add('PM low', pm.get('low'))
    add('Prior high', context['prior'].get('h')); add('Prior low', context['prior'].get('l')); add('Prior close', context['prior'].get('c'))
    add('RTH VWAP' if now >= opening else 'PM VWAP', context['regular' if now >= opening else 'premarket'].get('vwap'))
    if context['first15_complete']:
        add('First 15m close', context['first15_close'])
    if assessment['check_minute'] > 15 and context['confirmation_candle']['complete']:
        add('Confirmation close', close)
    for side, plan in plans.items():
        add(side.title() + ' stop', plan.get('stop')); add(side.title() + ' target', plan.get('target'))
    out.update(**assessment, gamma=gamma, plans=plans, mapping=mapping, chart_levels=chart,
        options={side: option_reference(db, c, now, side, context['spot']) for side in ('call', 'put')},
        notes=['Conditional plan, not a broker order or an instruction to enter at a clock time.',
               '15-minute baseline retained; historical study did not prove it optimal.',
               'Stops/targets are SPY prices: 0.2 × prior 14-day mean true range risk, target 2R. Re-anchor to confirmed close.',
               'Apex levels are possible reaction areas, not guaranteed support, resistance, magnets or targets.'])
    return out


class BriefWorker:
    def __init__(self, db, cfg):
        self.db, self.cfg, self.owner = db, cfg, uuid.uuid4().hex

    def tick(self, now=None):
        now = time.time() if now is None else now
        if not self.cfg.spy_morning_brief:
            return
        hours = session(day(now))
        if not hours:
            return
        opening = hours[0]
        phase = scheduled_phase(opening, now)
        if not phase:
            return
        key = report_key(day(now), phase)
        with self.db.tx() as c:
            if not self.db.lease(c, 'spy-morning-brief', self.owner, 30) or self.db.get(c, key):
                return
            previous = reports_for_day(self.db, c, day(now))
            if any(p and p.get('decision') in CONFIRMED for p in previous.values()):
                return
            if phase.startswith('followup') and not (previous[15] and previous[15].get('decision', '').startswith('WAIT')):
                return
            report = build(self.db, c, now, phase)
            if phase != 'preopen':
                candle = report['context']['confirmation_candle']
                if not candle['complete'] and now < candle['end'] + 110:
                    return
            report['id'] = key
            from .spy_chart import companion, prepare_drawing
            send_drawing = prepare_drawing(self.db, c, report)
            self.db.append(c, 'alert', 'spy_brief', 'SPY', now, report, key)
            if send_drawing:
                self.db.append(c, 'alert', 'spy_brief', 'SPY', now, companion(report), key + ':chart')
            self.db.put(c, key, report)
            self.db.put(c, 'spy-brief:latest', report)
            from .spy_study import register
            register(self.db,c,report,now)
        self.db.health('spy_morning_brief', 'queued', phase + ' 0DTE morning plan', now)
        return report

    async def run(self):
        import logging
        self.db.health('spy_morning_brief', 'scheduled' if self.cfg.spy_morning_brief else 'disabled',
                       '08:20 preopen; 08:45 check; after WAIT, 09:00/09:15/09:30 Chicago; stop after confirmation')
        logging.getLogger('uvicorn.error').info('SPY morning brief enabled=%s schedule=%s_CT; follow-ups after WAIT only',
                                               self.cfg.spy_morning_brief, ','.join(SCHEDULE_CT))
        if not self.cfg.spy_morning_brief:
            return
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.db.health('spy_morning_brief', 'error', type(error).__name__)
            await asyncio.sleep(10)


def delivery_payload(row, now):
    """Single compact text message sized for copy-paste into TradingView AI; no embeds."""
    p = row['payload']; ctx = p['context']
    expired = now >= p.get('expires_at', 0)
    heading = 'DATED PLAN — refresh before use' if expired else p['decision']
    if not expired and p.get('data_blocks'):
        heading = 'DATA BLOCKED — ' + '; '.join(p['data_blocks'])
    content = '**SPY 0DTE · ' + p.get('phase_label', p['phase'].upper()) + '**\n' + heading
    if ctx['status'] == 'closed':
        return {'content': content, 'allowed_mentions': {'parse': []}}
    session_name = 'PREMARKET' if p['generated_at'] < ctx['session_open'] else 'REGULAR SESSION'
    group = ctx['premarket'] if session_name == 'PREMARKET' else ctx['regular']
    lines = ['SPY ' + money(ctx['spot']) + ' (' + ctx['spot_basis'] + ') · VWAP ' + money(group['vwap']),
        'Session O/H/L: ' + ' / '.join(money(group[k]) for k in ('open', 'high', 'low')),
        'Prior H/L/C: ' + ' / '.join(money(ctx['prior'][k]) for k in ('h', 'l', 'c')),
        'Premarket H/L: ' + money(ctx['premarket']['high']) + ' / ' + money(ctx['premarket']['low'])]
    for side, plan in p['plans'].items():
        if plan['status'] == 'unavailable':
            lines.append(side.upper() + ': plan unavailable')
        else:
            direction = 'close above' if side == 'call' else 'close below'
            tgt_label = 'target (wall)' if plan.get('target_rule') == 'wall' else '2R target'
            lines.append(side.upper() + ': trigger ' + direction + ' ' + money(plan['trigger']) +
                ' · entry ref ' + money(plan['entry_reference']) +
                ' · stop ' + money(plan['stop']) + ' · ' + tgt_label + ' ' + money(plan['target']))
            if plan.get('target_rule') == 'wall' and plan.get('wall'):
                lines.append('  wall at ' + money(plan['wall']) + ' · 2R would be ' + money(plan.get('target_2r')))
            if plan['level_targets']:
                lines.append('Next Apex reaction levels: ' + ', '.join(money(v) for v in plan['level_targets']))
    if p['decision'] not in CONFIRMED:
        lines.append('Inside the range: WAIT. A premarket break is not a confirmed entry.')
    return {'content': content + '\n' + '\n'.join(lines), 'allowed_mentions': {'parse': []},
            'username': 'Market Compass · SPY Morning'}


def main():
    """Preview never queues alerts; optional probe updates only its reference cache."""
    import json
    import os
    from .config import Config
    from .providers import Collectors
    from .store import Store
    cfg = Config(); db = Store(cfg.db)
    now = time.time()
    if os.environ.get('SPY_BRIEF_REFERENCE_PROBE') == 'true':
        async def probe():
            from .index_reference import collect
            collector = Collectors(db, cfg)
            try:
                return await collect(collector, now, force=True)
            finally:
                await collector.close()
        asyncio.run(probe())
    with db.tx() as c:
        result = build(db, c, now)
        vendor = db.get(c, 'research:gamma_market', {})
        spy = next((r for r in vendor.get('items', []) if r.get('symbol') == 'SPY'), {})
        reference = db.get(c, 'index_reference:SPY', {})
        print('SPY_BRIEF_INPUTS ' + json.dumps({'index_reference': reference,
            'gamma_row_fields': sorted(spy.get('data', {})),
            'chain_present': bool(db.get(c, 'chain:SPY', {}).get('contracts')),
            'apex_present': bool(db.get(c, 'apex:SPY', {}).get('levels')),
            'bars_retained': len(db.get(c, 'bar_window:SPY', []))}, allow_nan=False), flush=True)
        print('SPY_BRIEF_PREVIEW ' + json.dumps(result, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()

