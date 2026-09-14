"""Presentation only: source identity stays separate from trade/fill status."""
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo('America/Chicago')
CATEGORIES = {
    'smoothers': ('SMOOTHERS', 'Weekly swing'),
    'futures': ('FUTURES', 'Intraday futures'),
    'morning': ('MORNING ALGO', 'Morning intraday'),
    'unusual_options': ('UNUSUAL OPTIONS', 'Intraday'),
    'end_of_day_algo': ('END OF DAY ALGO', 'End-of-day setup'),
    'swing': ('SWING', 'Multi-session swing'),
    'options_0dte': ('0DTE OPTIONS', 'Same-day expiry'),
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
FUTURE = re.compile(r'^(?:[A-Z_]+:)?(?:MES|MNQ|ES|NQ|MGC|GC|SIL|SI|MCL|CL|HG)(?:[FGHJKMNQUVXZ]\d{1,4}|\d+!)(?:@\d+)?$')


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
    elif status == 'setup_triggered':
        event = 'EXPIRED SETUP' if now >= p.get('expires_at', 0) else 'SETUP'
        mode = '[SIMULATED SETUP] — no broker order'
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
    identity = alert_identity(row, now)
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
        lines.append('Simulated P&L: $' + number(p['pnl']) + ' (after modeled fees)')
    if p.get('status') == 'setup_triggered':
        if identity['event'] == 'EXPIRED SETUP':
            lines.append('EXPIRED SETUP — DELAYED DELIVERY. The entry window has ended. This is a historical notification.')
        else:
            lines.append('Entry window ends: ' + clock(p['expires_at']))
        lines.append('Paper position: ' + clean(p.get('paper_status', 'not entered')).replace('_', ' ') + '. Option selection is reported separately.')
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
