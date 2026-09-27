"""Tape Confirmed: our setup triggers joined with same-direction options flow.

A setup is tape-confirmed when qualifying vendor flow arrives within +/-15
minutes of the trigger, in the same direction, with minimum conviction.
Evidence only: the confirmed flag is attached to setup-study trials so the
confirmed-vs-unconfirmed comparison can be measured. Never alerts, never
trades, never admits.
"""
from sqlalchemy import select

from .market import number
from .setup_study import trials

WINDOW = 900            # seconds each side of the trigger
MIN_PREMIUM = 250000    # minimum same-direction premium in the window
MIN_SCORE = 85          # minimum vendor score on one print in the window
MIX_RATIO = 0.5         # opposing premium >= this fraction of directional -> mixed
SCAN_THROTTLE = 120
LOOKBACK = 86400        # only consider trials started within the last day


def _direction(option_type):
    t = str(option_type or '').lower()
    if t in ('call', 'c'):
        return 'call'
    if t in ('put', 'p'):
        return 'put'
    return None


def confirm_trial(symbol, side, signal_ts, flow_rows, invalidated_ts=None):
    """Join one setup trigger with vendor flow. Pure function.

    side: 'long' | 'short'. Returns a result dict; never raises on bad rows.
    """
    result = {'symbol': symbol, 'side': side, 'signal_ts': signal_ts,
              'window_s': WINDOW, 'confirmed': False, 'call_premium': 0.0,
              'put_premium': 0.0, 'max_score': None, 'flow_count': 0,
              'void_reason': None}
    if invalidated_ts is not None and invalidated_ts <= signal_ts + WINDOW:
        result['void_reason'] = 'setup_invalidated_before_window_close'
        return result
    call_premium = put_premium = 0.0
    max_score, count = None, 0
    for row in flow_rows or []:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            continue
        stamp = number(row.get('source_ts'))
        if stamp is None or abs(stamp - signal_ts) > WINDOW:
            continue
        premium = number(row.get('premium')) or 0
        direction = _direction(row.get('option_type'))
        if direction == 'call':
            call_premium += premium
        elif direction == 'put':
            put_premium += premium
        else:
            continue
        count += 1
        score = number(row.get('score'))
        if score is not None and (max_score is None or score > max_score):
            max_score = score
    result.update(call_premium=call_premium, put_premium=put_premium,
                  max_score=max_score, flow_count=count)
    directional = call_premium if side == 'long' else put_premium
    opposing = put_premium if side == 'long' else call_premium
    if directional >= MIN_PREMIUM and opposing >= MIX_RATIO * directional:
        result['void_reason'] = 'direction_mixed'
        return result
    if directional >= MIN_PREMIUM and max_score is not None and max_score >= MIN_SCORE:
        result['confirmed'] = True
    return result


def recent_invalidations(db, c, source_ids, limit=1000):
    """Find setup_invalidated alert events for the given signal ids."""
    found = {}
    for event in db.recent(c, 'alert', limit=limit):
        payload = event.get('payload') or {}
        if payload.get('status') == 'setup_invalidated' and payload.get('setup_id') in source_ids:
            ts = number(event.get('ts'))
            if ts is not None:
                sid = payload['setup_id']
                if sid not in found or ts < found[sid]:
                    found[sid] = ts
    return found


def scan(db, c, cfg, now):
    """Throttled pass: confirm trials whose flow window has closed."""
    if not getattr(cfg, 'tape_confirmed', True):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'tape_confirmed:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    flow = db.get(c, 'matrix:unusual_activity', {})
    flow_rows = flow.get('rows') or []
    cutoff = now - LOOKBACK
    rows = c.execute(select(trials.c.id, trials.c.symbol, trials.c.side,
                            trials.c.status, trials.c.payload)
                     .where(trials.c.started >= cutoff)).all()
    evaluated = confirmed = 0
    # Collect trials whose flow window has closed and which lack a verdict.
    wanted = {}
    for r in rows:
        payload = r.payload if isinstance(r.payload, dict) else {}
        signal_ts = number(payload.get('signal_time')) or 0
        if signal_ts + WINDOW > now:
            continue  # window still open; evaluate on a later pass
        if db.get(c, 'tape_confirm:' + r.id) is not None:
            continue  # already evaluated
        wanted[r.id] = (r.symbol, r.side, r.status, payload, signal_ts)
    invalid = recent_invalidations(db, c,
                                   {p.get('source_id') for _, _, _, p, _ in wanted.values()
                                    if p.get('source_id')})
    for trial_id, (symbol, side, status, payload, signal_ts) in wanted.items():
        if status == 'excluded':
            result = {'symbol': symbol, 'side': side, 'signal_ts': signal_ts,
                      'window_s': WINDOW, 'confirmed': False,
                      'void_reason': 'trial_excluded_no_observation'}
        else:
            result = confirm_trial(symbol, side, signal_ts, flow_rows,
                                   invalid.get(payload.get('source_id')))
        result.update(trial_id=trial_id, source_id=payload.get('source_id'),
                      strategy=payload.get('strategy'), rule=payload.get('rule'),
                      evaluated_at=now)
        db.put(c, 'tape_confirm:' + trial_id, result)
        evaluated += 1
        if result['confirmed']:
            confirmed += 1
            db.append(c, 'tape_confirmation', 'tape_confirmed', symbol, now,
                      {'trial_id': trial_id, 'signal_ts': signal_ts, 'side': side,
                       'call_premium': result['call_premium'],
                       'put_premium': result['put_premium'],
                       'max_score': result['max_score']},
                      key='tapecfm:%s' % trial_id)
    db.put(c, 'tape_confirmed:scanned_at', now)
    return {'ran': True, 'evaluated': evaluated, 'confirmed': confirmed}


def display(db, c, now, days=14):
    """Recent confirmations plus the confirmed-vs-unconfirmed scorecard."""
    since = now - days * 86400
    confirms = {key[13:]: value for key, value in db.prefix(c, 'tape_confirm:').items()
                if (value.get('evaluated_at') or 0) >= since}
    cards = {}
    for trial_id, flag in confirms.items():
        if not flag.get('confirmed'):
            continue
        key = (flag.get('side'), 'confirmed')
        cards.setdefault(key, []).append(trial_id)
    # Join trial outcomes.
    outcomes = {'confirmed': {'n': 0, 'wins': 0, 'sum_r': 0.0},
                'unconfirmed': {'n': 0, 'wins': 0, 'sum_r': 0.0}}
    rows = c.execute(select(trials.c.id, trials.c.side, trials.c.status, trials.c.payload)
                     .where(trials.c.started >= since)).all()
    for r in rows:
        payload = r.payload if isinstance(r.payload, dict) else {}
        if r.status != 'closed':
            continue
        r_mult = number(payload.get('r_multiple'))
        if r_mult is None:
            continue
        bucket = 'confirmed' if confirms.get(r.id, {}).get('confirmed') else 'unconfirmed'
        outcomes[bucket]['n'] += 1
        outcomes[bucket]['wins'] += 1 if r_mult > 0 else 0
        outcomes[bucket]['sum_r'] += r_mult
    for bucket in outcomes.values():
        bucket['win_rate'] = bucket['wins'] / bucket['n'] if bucket['n'] else None
        bucket['mean_r'] = bucket['sum_r'] / bucket['n'] if bucket['n'] else None
    recent = sorted(
        ({'trial_id': tid, **flag} for tid, flag in confirms.items() if flag.get('confirmed')),
        key=lambda r: r['evaluated_at'], reverse=True)[:50]
    return {'asof': now, 'window_days': days,
            'scorecard': outcomes, 'recent_confirmed': recent,
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
