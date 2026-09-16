"""Timestamped morning plan. Informational alerts only; no order or entry-rule writes."""
import asyncio
from datetime import datetime
import time
import uuid

from .exposure import calculate
from .index_reference import prior_session, reference_pair
from .market import CT, NY, day, dedup, fresh, number, session
from .vendor_freshness import confirmation, context_check

VERSION = 'spy-morning-brief-v1'


def stamp(value):
    return datetime.fromtimestamp(value, CT).strftime('%b %d %H:%M:%S CT') if value is not None else 'unknown time'


def money(value):
    return f'{value:,.2f}' if number(value) is not None else 'unavailable'


def billions(value):
    return f'{value / 1e9:+.2f}B' if number(value) is not None else 'unavailable'


def bar_context(db, c, now):
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
    pm_complete = (expected_pm >= 60 and len(pm) >= .9 * expected_pm and pm
                   and min(now, opening) - (pm[-1]['ts'] + 60) <= 120)
    first_complete = {r['ts'] for r in first} == {opening + i * 60 for i in range(15)}
    daily = {}
    for row in db.recent(c, 'daily', 'SPY', limit=90):
        d = day(row['ts'])
        if d < day(now) and d not in daily and session(d):
            daily[d] = row['payload']
    prior = daily.get(prior_session(now), {})
    ordered = [daily[d] for d in sorted(daily)][-15:]
    atr = None
    if len(ordered) == 15 and all(all(number(r.get(k)) is not None for k in ('h', 'l', 'c')) for r in ordered):
        atr = sum(max(b['h'] - b['l'], abs(b['h'] - a['c']), abs(b['l'] - a['c']))
                  for a, b in zip(ordered, ordered[1:])) / 14
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
        'prior': {k: number(prior.get(k)) for k in ('h', 'l', 'c')}, 'atr14_daily': atr,
        'premarket_complete': bool(pm_complete), 'premarket_expected_bars': expected_pm,
        'first15_complete': first_complete, 'first15_close': first[-1]['payload']['c'] if first_complete else None,
        'first15_asof': opening + 900 if first_complete else None,
        'structure': structure, 'recent_closes': closes, 'minute_volume_ratio': rv, **groups}


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
        disagreements.append('Vendor/Compass expiry scope not verified equal')
    if proxy.get('status') != 'available':
        disagreements.append('Compass GEX inputs partial or unavailable')
    today_proxy = calculate([o for o in contracts if o.get('expiry') == day(now)], spot, now, chain.get('source'), chain.get('complete', False)) if proxy else {}
    return {'apex_status': 'available' if apex_ok else apex_check['cache_status'], 'apex_asof': apex.get('source_ts'),
        'ranked': ranked, 'flip': flip, 'vendor_expiries': vendor_expiries, 'vendor_gex': vendor_gex,
        'vendor_call_gex': number(vendor.get('callGEX')), 'vendor_put_gex': number(vendor.get('putGEX')),
        'vendor_status': check['status'], 'vendor_asof': item.get('source_ts'),
        'local': {k: proxy.get(k) for k in ('gex', 'coverage', 'contracts', 'usable_gex', 'status')},
        'local_ranked': sorted(proxy.get('strikes', []), key=lambda r: -abs(r['gex']))[:5],
        'local_call_gex': calls.get('gex'), 'local_put_gex': puts.get('gex'),
        'put_call_ratio': abs(puts['gex'] / calls['gex']) if calls.get('gex') else None,
        'local_asof': chain.get('asof'), 'local_expiries': chosen, 'zero_dte_gex': today_proxy.get('gex'),
        'zero_dte_coverage': today_proxy.get('coverage'), 'discrepancies': disagreements,
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
    context = bar_context(db, c, now)
    out = {'version': VERSION, 'status': 'spy_morning_brief', 'alert_category': 'spy_morning',
           'phase': phase, 'day': day(now), 'generated_at': now, 'context': context, 'symbol': 'SPY'}
    if context['status'] == 'closed':
        return {**out, 'decision': 'MARKET CLOSED', 'expires_at': now}
    opening = context['session_open']
    gamma = gamma_context(db, c, now, context['spot'] if context['status'] == 'available' else None)
    pm = context['premarket']
    risk = (context.get('atr14_daily') or 0) * .2
    decision = 'WAIT — first 15-minute candle closes at 08:45 CT'
    close = context['first15_close']
    eligible = context['status'] == 'available' and context['premarket_complete'] and risk > 0
    if now >= opening + 900:
        decision = 'WAIT — opening data incomplete'
        if eligible and context['first15_complete']:
            decision = ('CALL SETUP CONFIRMED' if close > pm['high'] else
                        'PUT SETUP CONFIRMED' if close < pm['low'] else 'WAIT — first candle stayed inside premarket range')
    if now > opening + 1025:
        decision = 'REFERENCE ONLY — opening confirmation window passed'
    if not eligible:
        decision = 'WAIT — price/range/ATR evidence incomplete'
    plans = {}
    for side, sign, trigger in (('call', 1, pm.get('high')), ('put', -1, pm.get('low'))):
        if trigger is None or not risk:
            plans[side] = {'status': 'unavailable'}
            continue
        confirmed = decision == side.upper() + ' SETUP CONFIRMED'
        entry = close if confirmed else trigger
        levels = sorted({r['price'] for r in gamma['ranked'] if sign * (r['price'] - entry) > 0}, reverse=side == 'put')[:2]
        plans[side] = {'status': 'confirmed' if confirmed else 'conditional', 'trigger': trigger,
            'entry_reference': entry, 'stop': entry - sign * risk, 'target': entry + sign * 2 * risk,
            'level_targets': levels, 'risk_R': risk,
            'rule': 'Completed 15-minute close above premarket high' if side == 'call' else 'Completed 15-minute close below premarket low'}
    ref = db.get(c, 'index_reference:SPY', {})
    mapping = reference_pair(ref.get('spy_close'), ref.get('spx_close'), ref.get('reference_day'), now, ref.get('source'))
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
    add('Gamma flip', gamma['flip'])
    for i, level in enumerate(gamma['ranked']):
        add('Apex #' + str(i + 1), level['price'])
    for i, level in enumerate(gamma['local_ranked']):
        if not any(abs(row['spy'] - level['strike']) < .00001 for row in chart):
            add('GEX #' + str(i + 1), level['strike'])
    for side, plan in plans.items():
        add(side.title() + ' stop', plan.get('stop')); add(side.title() + ' target', plan.get('target'))
    out.update(decision=decision, gamma=gamma, plans=plans, mapping=mapping, chart_levels=chart,
        options={side: option_reference(db, c, now, side, context['spot']) for side in ('call', 'put')},
        expires_at=opening if now < opening else opening + 1025,
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
        phase = ('preopen' if opening - 600 <= now < opening - 300 else
                 'opening' if opening + 905 <= now < opening + 1025 else None)
        if not phase:
            return
        key = 'spy-brief:' + day(now) + ':' + phase
        with self.db.tx() as c:
            if not self.db.lease(c, 'spy-morning-brief', self.owner, 30) or self.db.get(c, key):
                return
            report = build(self.db, c, now, phase)
            if phase == 'opening' and not report['context']['first15_complete'] and now < opening + 1010:
                return
            report['id'] = key
            self.db.append(c, 'alert', 'spy_brief', 'SPY', now, report, key)
            self.db.put(c, key, report)
            self.db.put(c, 'spy-brief:latest', report)
        self.db.health('spy_morning_brief', 'queued', phase + ' 0DTE morning plan', now)
        return report

    async def run(self):
        import logging
        self.db.health('spy_morning_brief', 'scheduled' if self.cfg.spy_morning_brief else 'disabled',
                       '08:20 and 08:45 Chicago on equity session days; 0DTE conditional plans')
        logging.getLogger('uvicorn.error').info('SPY morning brief enabled=%s schedule=08:20,08:45:05_CT', self.cfg.spy_morning_brief)
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
    """One Discord message with bounded rich cards; no lossy 2,000-character truncation."""
    p = row['payload']; ctx = p['context']
    expired = now >= p.get('expires_at', 0)
    heading = 'DATED PLAN — refresh before use' if expired else p['decision']
    content = '**SPY 0DTE MORNING PLAN · ' + p['phase'].upper() + '**\n' + heading
    if ctx['status'] == 'closed':
        return {'content': content, 'allowed_mentions': {'parse': []}}
    session_name = 'PREMARKET' if p['generated_at'] < ctx['session_open'] else 'REGULAR SESSION'
    group = ctx['premarket'] if session_name == 'PREMARKET' else ctx['regular']
    lines = [session_name + ' · ' + stamp(p['generated_at']),
        'SPY ' + money(ctx['spot']) + ' (' + ctx['spot_basis'] + ', ' + stamp(ctx['spot_ts']) + ')',
        'Session O/H/L: ' + ' / '.join(money(group[k]) for k in ('open', 'high', 'low')),
        'VWAP ' + money(group['vwap']) + ' · ' + group['vwap_basis'],
        'Prior H/L/C: ' + ' / '.join(money(ctx['prior'][k]) for k in ('h', 'l', 'c')),
        ctx['structure'] + '; latest minute volume / prior 20: ' + (f"{ctx['minute_volume_ratio']:.2f}×" if ctx['minute_volume_ratio'] is not None else 'unavailable'),
        f"Premarket coverage {ctx['premarket']['bars']}/{ctx['premarket_expected_bars']} minutes; " + ('usable' if ctx['premarket_complete'] else 'incomplete'),
        'First 15m close: ' + money(ctx['first15_close']) + ' · ' + stamp(ctx['first15_asof'])]
    if ctx['spot'] is not None and ctx['prior'].get('c'):
        lines.append('Versus prior close: ' + f"{ctx['spot']-ctx['prior']['c']:+.2f} ({(ctx['spot']/ctx['prior']['c']-1)*100:+.2f}%)")
    if ctx['spot'] is not None and group.get('vwap') is not None:
        lines.append('Price bias: ' + ('above VWAP' if ctx['spot'] > group['vwap'] else 'below VWAP' if ctx['spot'] < group['vwap'] else 'at VWAP') + '; entry still needs the candle close.')
    for side, plan in p['plans'].items():
        if plan['status'] == 'unavailable':
            lines.append(side.upper() + ': plan unavailable')
        else:
            lines.append(side.upper() + ': ' + plan['rule'] + ' ' + money(plan['trigger']) +
                '; reference ' + money(plan['entry_reference']) + '; stop ' + money(plan['stop']) + '; 2R target ' + money(plan['target']))
            if plan['level_targets']:
                lines.append('Next Apex reaction levels: ' + ', '.join(money(v) for v in plan['level_targets']))
    lines += ['Inside the range: WAIT. A premarket break is not a confirmed entry.',
              'Stops/targets use SPY prices, not option premiums. Provisional plans re-anchor to the confirmed close.']
    embeds = [{'title': 'Price structure and conditional entries', 'description': '\n'.join(lines)}]
    g = p['gamma']; local = g['local']
    lines = ['Vendor GEX ' + billions(g['vendor_gex']) + ' · ' + g['vendor_status'] + ' · ' + stamp(g['vendor_asof']),
        'Vendor call/put GEX: ' + billions(g['vendor_call_gex']) + ' / ' + billions(g['vendor_put_gex']),
        'Vendor flip ' + money(g['flip']) + ' · Apex ' + g['apex_status'] + ' · ' + stamp(g['apex_asof']),
        'Compass proxy ' + billions(local.get('gex')) + '; call/put ' + billions(g['local_call_gex']) + ' / ' + billions(g['local_put_gex']),
        'P/C proxy ' + (f"{g['put_call_ratio']:.2f}" if g['put_call_ratio'] is not None else 'unavailable') +
        '; coverage ' + (f"{local['coverage']:.1%}" if local.get('coverage') is not None else 'unavailable') + ' · ' + stamp(g['local_asof']),
        'Compass expiries: ' + (', '.join(g['local_expiries']) or 'unavailable'),
        '0DTE-only proxy ' + billions(g['zero_dte_gex']) + '; coverage ' + (f"{g['zero_dte_coverage']:.1%}" if g['zero_dte_coverage'] is not None else 'unavailable')]
    if g['vendor_gex'] is not None:
        lines.insert(0, 'Vendor regime: ' + ('negative gamma' if g['vendor_gex'] < 0 else 'positive gamma' if g['vendor_gex'] > 0 else 'neutral gamma'))
    if ctx['spot'] is not None and g['flip'] is not None:
        lines.append('Spot minus flip: ' + f"{ctx['spot']-g['flip']:+.2f}")
    for i, r in enumerate(g['ranked']):
        lines.append(f"#{i+1} SPY {r['price']:.2f} · score {r['score']:g} · GEX {billions(r.get('net_gex'))} · OI {money(r.get('oi'))}")
    if not g['ranked']:
        lines.append('Current Apex rankings unavailable; stale rankings withheld.')
    if g['local_ranked']:
        lines.append('Compass largest |GEX|: ' + '; '.join(money(r['strike']) + ' ' + billions(r['gex']) for r in g['local_ranked']))
    lines += g['discrepancies'] + [g['interpretation']]
    embeds.append({'title': 'Gamma, ranked levels and source comparison', 'description': '\n'.join(lines)})
    m = p['mapping']
    if m['status'] == 'available':
        lines = ['SPY | estimated SPX | estimated XSP',
            *[r['label'] + ': ' + ' | '.join(money(r[k]) for k in ('spy', 'spx_estimate', 'xsp_estimate')) for r in p['chart_levels']],
            'Basis: ' + m['reference_day'] + ' closes; ratio ' + f"{m['ratio']:.6f}" + '; ' + m['source'],
            'XSP = SPX / 10. Estimates are chart references, not live index prices or option strikes. SPY/index basis can change.']
    else:
        lines = ['SPX/XSP estimates unavailable: no matched previous-session reference.',
                 'SPY levels above remain separately available.']
    embeds.append({'title': 'Levels to copy to your charts', 'description': '\n'.join(lines)})
    lines = []
    for side, o in p['options'].items():
        label = side.upper() + ' ' + money(o.get('strike')) + ' · expiry ' + o['expiry']
        if o['status'] in ('available', 'wide_spread'):
            lines.append(label + '\nBid/ask/mid ' + '/'.join(money(o[k]) for k in ('bid', 'ask', 'mid')) +
                '; spread ' + f"{o['spread_pct']:.1f}%" + (' — TOO WIDE' if o['status'] == 'wide_spread' else '') +
                '\nSnapshot delta ' + money(o['delta_snapshot']) + '; IV ' + (f"{o['iv_snapshot']:.1%}" if o['iv_snapshot'] is not None else 'unavailable') +
                '; ask debit + entry fee $' + money(o['max_debit_with_entry_fee']) + '\nQuote ' + stamp(o['quote_ts']))
        else:
            lines.append(label + '\nPremium unavailable: ' + o['reason'])
    lines += ['0DTE watch contracts only. No entry without confirmation and a fresh usable quote. The full premium can be lost.',
              '15-minute baseline retained; the historical study did not prove an optimal window. No position sizing or broker order.']
    embeds.append({'title': 'Same-day options', 'description': '\n'.join(lines),
                   'footer': {'text': p.get('id', VERSION) + ' · Event ' + str(row['id'])}})
    # Bounded field counts and numbers above keep the payload well below Discord's total embed cap.
    return {'content': content, 'embeds': embeds, 'allowed_mentions': {'parse': []}, 'username': 'Market Compass · SPY Morning'}


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
