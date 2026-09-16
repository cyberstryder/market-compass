"""Presentation only: source identity stays separate from trade/fill status."""
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo('America/Chicago')
CATEGORIES = {
    'spy_morning': ('SPY MORNING PLAN', '0DTE morning'),
    'smoothers': ('SMOOTHERS', 'Weekly swing'),
    'futures': ('FUTURES', 'Intraday futures'),
    'morning': ('MORNING ALGO', 'Morning intraday'),
    'unusual_options': ('UNUSUAL OPTIONS', 'Intraday'),
    'end_of_day_algo': ('END OF DAY ALGO', 'End-of-day setup'),
    'swing': ('SWING', 'Multi-session swing'),
    'options_0dte': ('0DTE OPTIONS', 'Same-day expiry'),
    'options_ideas': ('OPTIONS IDEAS', 'Intraday options'),
    'swing_ideas': ('SWING IDEAS', 'Multi-session options'),
    'exposure': ('EXPOSURE LEVELS', 'Intraday'),
    'intraday': ('INTRADAY STOCKS', 'Intraday'),
    'system': ('SYSTEM', 'Service notice'),
}
SETUPS = {
    'orb15-breakout-v1': '15-minute opening-range breakout',
    'futures-orb15-v2': 'Session opening-range breakout (15m)',
    'orb_retest': 'Opening-range breakout / retest',
    'session_sweep_reclaim': 'Liquidity sweep / reclaim reversal',
    'trend_pullback': 'EMA / VWAP trend pullback',
    'volume_breakout': 'Volume-supported range breakout',
    'exposure_level_break': 'Exposure-level price break',
    'flow_price_breakout': 'Unusual options flow + price breakout',
    'daily_breakout': 'Daily range breakout',
    'pullback_reclaim': 'Daily pullback reclaim',
    'daily_reversal': 'Daily reversal',
}
EVENTS = {
    'entered': 'PAPER ENTRY', 'open': 'PAPER ENTRY', 'closed': 'PAPER EXIT',
    'setup_triggered': 'SETUP', 'setup_invalidated': 'INVALIDATED',
    'skipped': 'SKIPPED', 'options_skipped': 'OPTION SKIPPED',
    'management_blocked': 'DATA BLOCKED', 'notification_test': 'TEST — NO TRADE',
}
CONTEXT_FIELDS = ('strategy', 'track', 'side', 'underlying', 'underlying_side',
                  'asset', 'rule', 'matched_rules', 'alert_category')
OPTION = re.compile(r'^(?:O:)?([A-Z.]+)(\d{6})([CP])(\d{8})$')
FUTURE = re.compile(r'^(?:[A-Z_]+:)?(?:MES|MNQ|ES|NQ|MYM|YM|MGC|GC|SIL|SI|MCL|CL|HG)(?:[FGHJKMNQUVXZ]\d{1,4}|\d+!)(?:@\d+)?$')


def alert_context(payload):
    """Keep source identity on management/skip messages without copying a fill."""
    return {key: payload[key] for key in CONTEXT_FIELDS if key in payload}


def clean(value, limit=350):
    return ' '.join(str(value or '').split())[:limit]


def clock(stamp):
    return datetime.fromtimestamp(stamp, CT).strftime('%Y-%m-%d %H:%M:%S CT')


def number(value):
    if isinstance(value, (int, float)):
        return f'{value:,.4f}' if 0 < abs(value) < 1 else f'{value:,.2f}'
    return clean(value, 80)


def alert_identity(row, now=None):
    now = time.time() if now is None else now
    p = row['payload']
    status = p.get('status', '')
    symbol = row['symbol']
    option = OPTION.fullmatch(symbol)
    project = p.get('project') or str(row.get('source', '')).removeprefix('project:')
    rule = p.get('rule') or str(p.get('strategy', '')).rsplit(':', 1)[-1]
    future = p.get('asset') == 'future' or FUTURE.fullmatch(symbol)
    # A supporting flow print never renames a technical or futures strategy.
    if status == 'notification_test':
        category = 'system'
    elif project in ('morning', 'smoothers', 'futures'):
        category = project
    elif p.get('alert_category') in CATEGORIES:
        category = p['alert_category']
    elif future:
        category = 'futures'
    elif p.get('track') == 'swing' or str(p.get('strategy', '')).startswith('swing20-'):
        category = 'swing'
    elif rule == 'flow_price_breakout':
        category = 'unusual_options'
    elif rule == 'exposure_level_break':
        category = 'exposure'
    elif option or status == 'options_skipped':
        category = 'options_0dte'
    else:
        category = 'intraday'
    label, horizon = CATEGORIES[category]
    event = EVENTS.get(status, clean(status.replace('_', ' ')).upper() or 'OBSERVATION')
    mode = '[SIMULATED] — no broker order'
    origin = 'Compass scanner' if 'scanner' in str(p.get('strategy', '')) or row.get('source') == 'scanner' else 'Compass engine'
    if status == 'project_observation':
        event = {'entry_alert': 'SOURCE ENTRY', 'exit_alert': 'SOURCE EXIT',
                 'flatten_alert': 'SESSION EXIT', 'cutoff_alert': 'SESSION EXIT',
                 'emulator_fill': 'STRATEGY FILL'}.get(p.get('source_event'), 'SOURCE OBSERVATION')
        mode = '[SOURCE ALERT] — broker fill unconfirmed'
        origin = 'TradingView mirror' if category == 'futures' else label.title() + ' mirror'
    elif status == 'notification_test':
        mode, origin = 'TEST — no trade', 'Market Compass'
    elif status == 'spy_morning_brief':
        event = 'DATED PLAN' if now >= p.get('expires_at', 0) else 'MORNING PLAN'
        mode, origin = '[CONDITIONAL PLAN]', 'Compass SPY morning brief'
    elif status == 'setup_triggered':
        event = 'EXPIRED SETUP' if now >= p.get('expires_at', 0) else 'SETUP'
        mode = '[SIMULATED SETUP] — no broker order'
    elif status == 'setup_result':
        event = 'SETUP RESULT · ' + clean(p.get('outcome','unresolved')).upper()
        mode = '[INDEPENDENT SETUP TEST] — one unit; no broker order'
        origin = 'Compass setup study'
    elif status.startswith('swing_idea_'):
        suffix=status.removeprefix('swing_idea_')
        event=('NEW SWING' if suffix=='new' else 'SWING UPDATE' if suffix.startswith('update_')
               else 'SESSION REOPEN' if suffix.startswith('reopen_') else 'SWING EXIT' if suffix=='closed' else 'DATA GAP')
        if suffix=='new' and now-p.get('opened_at',0)>120:
            event='HISTORICAL SWING ENTRY'
        elif suffix!='new' and now-row['ts']>120:
            event='DELAYED '+event
        mode='[SIMULATED SWING IDEA] — one contract; carries between stock sessions'
        origin='Compass swing scanner'
    elif status.startswith('option_idea_'):
        suffix=status.removeprefix('option_idea_')
        event=('NEW IDEA' if suffix=='new' else 'OPTION UPDATE' if suffix.startswith('update_')
               else 'OPTION EXIT' if suffix=='closed' else 'DATA GAP')
        if suffix=='new' and now-p.get('opened_at',0)>120:
            event='EXPIRED IDEA'
        elif suffix.startswith('update_') and now-row['ts']>120:
            event='DELAYED OPTION UPDATE'
        mode='[SIMULATED OPTION IDEA] — one contract; intraday'
        origin='Compass all-day options scanner'
    elif status == 'secondary_review':
        event = 'SECONDARY ' + clean(p.get('verdict', 'review')).replace('_', ' ').upper()
        if now >= p.get('expires_at', 0):
            event = 'SECONDARY HISTORICAL · ' + clean(p.get('verdict', 'review')).replace('_', ' ').upper()
        mode = '[SECONDARY REVIEW] — underlying context; no broker order'
        origin = 'Compass review of ' + clean(p.get('review_source'), 80)
    direction = clean(p.get('side', '')).upper()
    if direction in ('EXIT', 'FLAT'):
        direction = ''
    instrument = 'Futures contract' if future or category == 'futures' else 'Underlying stock / ETF'
    display_symbol = symbol.split('@')[0] if future else symbol
    if option:
        underlying, expiry, kind, strike = option.groups()
        name = 'CALL' if kind == 'C' else 'PUT'
        direction = ('BUY ' if p.get('side') == 'long' else 'SELL ' if p.get('side') == 'short' else '') + name
        display_symbol = f'{underlying} {int(strike) / 1000:g}{kind}'
        instrument = f'{name} option · expiry 20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]} · {symbol}'
    setup = SETUPS.get(rule) or ('20-day closing breakout' if str(p.get('strategy', '')).startswith('swing20-') else clean(p.get('strategy', ''), 150))
    return {'format_version': 1, 'category': category, 'label': label, 'event': event,
            'mode': mode, 'origin': origin, 'horizon': horizon, 'instrument': instrument,
            'symbol': display_symbol, 'direction': direction, 'setup': setup,
            'title': f'{label} | {event} | {display_symbol}' + (f' · {direction}' if direction else '')}


def message_for(row, now=None):
    now = time.time() if now is None else now
    p = row['payload']
    if p.get('status') == 'spy_morning_brief':
        from .spy_brief import delivery_payload
        body=delivery_payload(row,now)
        return (body['content']+'\n'+ '\n\n'.join(e['title']+'\n'+e['description'] for e in body.get('embeds',[])))[:1900]
    identity = alert_identity(row, now)
    if p.get('status','').startswith('swing_idea_'):
        return swing_idea_message(row, identity, now)
    if p.get('status','').startswith('option_idea_'):
        return option_idea_message(row, identity, now)
    if p.get('status') == 'secondary_review':
        return secondary_message(row, identity, now)
    lines = [f"**{identity['title']}**", f"{identity['mode']} · {identity['horizon']}"]
    if p.get('status') == 'notification_test':
        lines[0] = '[TEST — NO TRADE] **SYSTEM | DELIVERY CHECK**'
        lines.append('This verifies the alert channel. No position was opened.')
    if identity['setup']:
        lines.append('Setup: ' + identity['setup'])
    if identity['category'] != 'system':
        lines.append('Instrument: ' + identity['instrument'])
    prices = []
    entry_label = 'Entry reference' if p.get('status') == 'setup_triggered' else 'Simulated entry'
    for key, label in (('source_price', 'Source reference'), ('fill_price', 'Strategy-emulator fill'),
                       ('entry', entry_label), ('stop', 'Stop'), ('target', 'Target'),
                       ('exit', 'Simulated exit')):
        if p.get(key) is not None:
            amount = number(p[key])
            if OPTION.fullmatch(row['symbol']):
                label += ' premium'
                amount = '$' + amount
            prices.append(f'{label}: {amount}')
    if prices:
        lines.append(' | '.join(prices))
    if p.get('qty') is not None:
        lines.append('Quantity: ' + clean(p['qty'], 40))
    if p.get('initial_risk') is not None:
        lines.append('Modeled initial risk: $' + number(p['initial_risk']))
    if p.get('pnl') is not None:
        lines.append(('Trial P&L: $' if p.get('status')=='setup_result' else 'Simulated P&L: $') + number(p['pnl']) + ' (after modeled fees)')
    if p.get('status')=='setup_result':
        lines.append('Independent experiment; overlaps other trials. Not account performance.')
        if p.get('r_multiple') is not None: lines.append('Result: '+number(p['r_multiple'])+'R | Duration: '+number(p.get('elapsed_seconds',0)/60)+' minutes')
    if p.get('status') == 'setup_triggered':
        if identity['event'] == 'EXPIRED SETUP':
            lines.append('EXPIRED SETUP — DELAYED DELIVERY. The entry window has ended. This is a historical notification.')
        else:
            lines.append('Entry window ends: ' + clock(p['expires_at']))
        lines.append('Portfolio simulation: ' + clean(p.get('paper_status', 'not entered')).replace('_', ' ') + '.')
        if p.get('setup_trial_id'): lines.append('Independent setup outcome tracking active; portfolio limits do not stop measurement.')
        if identity['category']!='futures': lines.append('Option selection is reported separately.')
    # An exit reason must take precedence over the original entry thesis.
    reason = p.get('exit_reason') or p.get('reason')
    if reason:
        lines.append('Reason: ' + clean(reason).replace('_', ' '))
    matches = [SETUPS.get(rule, clean(rule).replace('_', ' ')) for rule in p.get('matched_rules', [])]
    if len(matches) > 1:
        lines.append('Also matched: ' + '; '.join(dict.fromkeys(matches))[:250])
    evidence_lines = []
    for evidence in p.get('evidence', []):
        label = clean(evidence.get('source'), 40) + ' / ' + clean(evidence.get('kind'), 60).replace('_', ' ')
        for key in ('symbol', 'agreement'):
            if evidence.get(key):
                label += ' ' + clean(evidence[key], 35)
        stamp = evidence.get('source_ts')
        label += (' as of ' + clock(stamp)) if stamp is not None else ' / source time unavailable'
        if evidence.get('usage') in ('dated_context', 'context_only_not_current_confirmation'):
            label += ' (context only)'
        if label not in evidence_lines:
            evidence_lines.append(label)
    footer = '\nSource: ' + identity['origin']
    ref = p.get('trade_id') or p.get('setup_id') or p.get('parent_signal') or p.get('source_record_id') or p.get('id')
    if ref:
        footer += '\nRef: ' + clean(ref, 100)
    footer += f"\nEvent #{row['id']} | " + clock(row['ts'])
    if now - row['ts'] > 30:
        footer += f" | Delivery age: {int(now - row['ts'])} seconds"
    content = '\n'.join(lines)
    if evidence_lines:
        content += '\nEvidence: ' + '; '.join(evidence_lines[:5])
    return content[:1900 - len(footer)] + footer


def secondary_message(row, identity, now):
    p = row['payload']
    lines = [f"**{identity['title']}**", identity['mode'],
             'Original strategy continues independently.', 'Setup: ' + identity['setup'],
             'Assessment: ' + clean(p.get('reason'), 600)]
    if p.get('reference_price') is not None:
        lines.append('Review reference: ' + number(p['reference_price']) + ' · ' + clean(p.get('market_symbol'), 80))
    if p.get('price_basis') == 'linked_contract_context':
        lines.append('Dated-contract context; original continuous-chart stop/target are not validated.')
    lines.append('Option selection and account risk are not approved by this review.')
    if p.get('optional_missing'):
        lines.append('Context limits: ' + '; '.join(clean(v, 100) for v in p['optional_missing']))
    if now >= p.get('expires_at', 0):
        lines.append('HISTORICAL — delivery exceeded the review window. Do not treat as a current entry.')
    if p.get('source_time') is not None:
        lines.append('Original: ' + clock(p['source_time']))
    if p.get('available_at') is not None and p['available_at'] != p.get('source_time'):
        lines.append('Source candidate created: ' + clock(p['available_at']))
    if p.get('quote_ts') is not None:
        lines.append('Quote: ' + clock(p['quote_ts']))
    footer = ('\nReview: ' + clock(row['ts']) + '\nSource: ' + identity['origin'] +
              '\nOriginal ref: ' + clean(p.get('source_record_id'), 120) +
              '\nReview ref: ' + clean(p.get('review_id'), 64) +
              '\nRule: ' + clean(p.get('rule_version'), 60) + ' | Event #' + str(row['id']))
    return '\n'.join(lines)[:1900 - len(footer)] + footer


def option_idea_message(row, identity, now):
    p=row['payload']
    contract=p['contract']
    lines=[f"**{identity['title']}**", identity['mode'],
        clean(p['underlying'])+' '+number(contract['strike'])+' '+clean(contract['type']).upper()
        +' · expires '+clean(contract['expiry']),
        'Setup: '+clean(p.get('reason') or p.get('strategy')),
        'Option entry $'+number(p['entry'])+' · premium stop $'+number(p['premium_stop'])
        +' · premium target $'+number(p['premium_target']),
        'Underlying invalidation '+number(p['underlying_stop'])+' · target '+number(p['underlying_target'])]
    q=p.get('last_quote',{})
    if q:
        lines.append('Option bid / ask $'+number(q['bid'])+' / $'+number(q['ask'])
                     +' · '+clock(q['ts'])+' · '+clean(p.get('option_source')))
    if p.get('idea_status')=='closed':
        lines.append('Simulated exit $'+number(p['exit'])+' · net $'+number(p['pnl'])
                     +' ('+number(p['return_pct'])+'%) · '+clean(p['exit_reason']).replace('_',' '))
    elif p.get('idea_status')=='unresolved':
        lines.append('Observation gap: final option P&L and win/loss are unresolved.')
    elif p.get('mark_pct') is not None:
        lines.append('Modeled net return '+number(p['mark_pct'])+'% · current bid less exit allowance and fees')
    if p.get('best_pct') is not None:
        lines.append('Best observed net return '+number(p['best_pct'])+'% · '+clock(p['peak_at']))
    lines.append('Intraday close deadline '+clock(p['flatten_at'])+'; expiry is not the holding period.')
    lines.append('One-contract experiment. Includes $0.65 per side and $0.01 price allowances; not account P&L.')
    if identity['event'] in ('EXPIRED IDEA','DELAYED OPTION UPDATE'):
        lines.append('Delayed historical notification; not a current entry.')
    lines.append('Source setup '+clean(p['source_id'],16)+' · Event #'+str(row['id'])+' · '+clock(row['ts']))
    return '\n'.join(lines)[:1900]


def swing_idea_message(row, identity, now):
    p=row['payload']
    o,d,f=p['contract'],p['daily'],p['flow']
    lines=[f"**{identity['title']}**",identity['mode'],
        clean(p['underlying'])+' $'+number(o['strike'])+' '+clean(o['type']).upper()+' · expires '+clean(o['expiry']),
        'Setup: '+identity['setup']+' · '+('bullish' if p['underlying_side']=='long' else 'bearish'),
        'Tech: SMA20 '+number(d['sma20'])+' / SMA50 '+number(d['sma50'])+' · RSI '+number(d['rsi14'])
        +' · completed daily through '+clean(d['through'])+'; weekly '+clean(d['weekly_bias']),
        'Observed filtered flow (30m): calls $'+number(f.get('call_premium'))+' / puts $'+number(f.get('put_premium')),
        'Vendor-classified swing flow: bullish $'+number(f.get('bullish_premium'))+' / bearish $'+number(f.get('bearish_premium')),
        'Flow source time '+clock(f['latest'])+' · unusual feed only; not total market buying',
        'Option entry $'+number(p['entry'])+' · premium stop $'+number(p['premium_stop'])+' · target $'+number(p['premium_target']),
        'Underlying invalidation '+number(p['underlying_stop'])+' · target '+number(p['underlying_target'])]
    if p.get('idea_status')=='closed':
        lines.append('Simulated exit $'+number(p['exit'])+' · net $'+number(p['pnl'])+' ('+number(p['return_pct'])+'%) · '+clean(p['exit_reason']).replace('_',' '))
    elif p.get('idea_status')=='unresolved':
        lines.append('UNRESOLVED: '+clean(p['exit_reason']).replace('_',' ')+'; no final win/loss.')
    elif p.get('mark_pct') is not None:
        lines.append('Last observed net return '+number(p['mark_pct'])+'% · option quote '+clock(p['last_option_ts']))
    if p.get('overnight_gaps'):
        gap=p['overnight_gaps'][-1]
        lines.append('Latest reopen: option bid change $'+number(gap['option_bid_change'])+' · underlying change $'+number(gap['underlying_mid_change']))
    lines.append('Final exit deadline '+clock(p['exit_deadline'])+'; overnight gaps can exceed stops.')
    lines.append('One-contract experiment; $0.65 per side and $0.01 price allowances. Pending orders are not broker trades.')
    if identity['event']=='HISTORICAL SWING ENTRY' or identity['event'].startswith('DELAYED'):
        lines.append('Delayed historical notification; not a current entry.')
    footer='\nRef '+clean(p['id'],16)+' · Event #'+str(row['id'])+' · '+clock(row['ts'])
    return '\n'.join(lines)[:1900-len(footer)]+footer
