"""Bounded morning confirmation schedule and durable per-session decisions."""
from datetime import datetime

from .market import CT, day, number

CHECKS = ((15, 'opening'), (30, 'followup_30'), (45, 'followup_45'), (60, 'followup_60'))
SCHEDULE_CT = ('08:20', '08:45:05', '09:00:05', '09:15:05', '09:30:05')
CONFIRMED = ('CALL SETUP CONFIRMED', 'PUT SETUP CONFIRMED')


def clock(value):
    return datetime.fromtimestamp(value, CT).strftime('%H:%M CT')


def report_key(session_day, phase):
    return 'spy-brief:' + session_day + ':' + phase


def reports_for_day(db, c, session_day):
    return {minute: db.get(c, report_key(session_day, phase)) for minute, phase in CHECKS}


def scheduled_phase(opening, now):
    if opening - 600 <= now < opening - 300:
        return 'preopen'
    return next((phase for minute, phase in CHECKS
                 if opening + minute*60 + 5 <= now < opening + minute*60 + 125), None)


def check_minute(opening, now, phase):
    explicit = next((minute for minute, name in CHECKS if name == phase), None)
    return explicit or min(60, max(15, int((now-opening)//900)*15))


def assess(context, reports, now, phase):
    """Read-only assessment; the caller persists the exact report atomically."""
    opening = context['session_open']
    candle = context['confirmation_candle']
    minute, boundary = candle['minute'], candle['end']
    first = reports.get(15)
    observed = dict(context['premarket'])
    frozen = None
    if first:
        frozen = first.get('frozen_premarket')
        if not frozen:  # Preserve levels from reports written before follow-ups existed.
            previous = first.get('context', {}).get('premarket', {})
            frozen = {'day': first.get('day'), 'high': previous.get('high'), 'low': previous.get('low'),
                      'frozen_at': first.get('generated_at'), 'source_asof': previous.get('asof')}
    elif now >= opening and minute == 15:
        frozen = {'day': day(now), 'high': observed['high'], 'low': observed['low'],
                  'frozen_at': now, 'source_asof': observed['asof']}
    if frozen:
        context['premarket'] = {**observed, 'high': frozen.get('high'), 'low': frozen.get('low')}
    context['premarket_observed'] = observed
    valid_range = bool(frozen and frozen.get('day') == day(now)
                       and number(frozen.get('low')) is not None and number(frozen.get('high')) is not None
                       and 0 < frozen['low'] < frozen['high'])
    same_range = bool(valid_range and all(number(observed.get(k)) is not None
                      and abs(observed[k]-frozen[k]) <= 1e-8 for k in ('high', 'low')))
    context['premarket_range_unchanged'] = same_range if now >= opening else None
    blocks = []
    if context['status'] != 'available':
        blocks.append('price evidence stale or missing')
    if not context['premarket_complete']:
        blocks.append('premarket coverage incomplete')
    if not (context.get('atr14_daily') or 0) > 0:
        blocks.append('prior daily ATR evidence incomplete')
    if now >= boundary:
        if not candle['complete']:
            blocks.append('confirmation candle incomplete')
        if not context['confirmation_history_complete']:
            blocks.append('earlier regular-session minutes incomplete')
    if minute > 15:
        if not first or not first.get('decision', '').startswith('WAIT'):
            blocks.append('recorded opening WAIT required')
        if any(not reports.get(m) for m, _ in CHECKS if m < minute):
            blocks.append('earlier scheduled check missing')
        if not valid_range:
            blocks.append('frozen opening premarket range unavailable')
        elif not same_range:
            blocks.append('recovered premarket range differs from frozen levels')
    elif now >= opening and not valid_range:
        blocks.append('premarket range unavailable')
    elif now >= opening and not same_range:
        blocks.append('recovered premarket range differs from frozen levels')

    prior_confirmation = next((p for p in reports.values() if p and p.get('decision') in CONFIRMED), None)
    final_no_entry = bool(reports.get(60) and reports[60].get('decision', '').startswith('NO ENTRY'))
    state = 'waiting'
    if prior_confirmation:
        state, decision = 'reference_only', 'REFERENCE ONLY — setup already confirmed; no second entry'
    elif final_no_entry:
        state, decision = 'reference_only', 'REFERENCE ONLY — morning checks finished with no entry'
    elif now >= boundary + 125:
        state, decision = 'reference_only', 'REFERENCE ONLY — confirmation window passed'
    elif now < boundary:
        decision = 'WAIT — first 15-minute candle closes at ' + clock(opening + 900)
        if blocks:
            decision = 'WAIT — ' + '; '.join(blocks)
    elif blocks:
        decision = 'WAIT — ' + '; '.join(blocks)
    else:
        pm, close = context['premarket'], candle['close']
        side = 'CALL' if close > pm['high'] else 'PUT' if close < pm['low'] else None
        state = 'confirmed' if side else 'waiting'
        decision = side + ' SETUP CONFIRMED' if side else 'WAIT — completed candle stayed inside premarket range'
    if minute == 60 and now >= boundary and state == 'waiting':
        state = 'no_entry'
        decision = 'NO ENTRY — ' + ('; '.join(blocks) if blocks else 'no confirmed breakout by ' + clock(boundary))

    next_check = next((opening + m*60 for m, _ in CHECKS if opening + m*60 > now), None)
    if state in ('confirmed', 'no_entry') or prior_confirmation or final_no_entry:
        next_check = None
    kind = None if now < opening else 'opening' if minute == 15 else 'later'
    label = ('PREOPEN' if phase == 'preopen' or now < opening else
             ('OPENING CHECK' if minute == 15 else 'LATER CHECK') + ' · ' + clock(boundary))
    return {'decision': decision, 'decision_state': state, 'phase_label': label,
            'check_minute': minute, 'confirmation_kind': kind, 'data_blocks': blocks,
            'frozen_premarket': frozen, 'next_check_at': next_check,
            'session_complete': bool(prior_confirmation or final_no_entry or state in ('confirmed', 'no_entry')
                                     or now >= opening + 60*60 + 125),
            'previous_confirmation_id': prior_confirmation.get('id') if prior_confirmation else None,
            'expires_at': opening if now < opening else boundary + 125}
