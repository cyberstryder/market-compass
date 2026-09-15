"""Causal entry cohorts over the existing independent setup trials.

Only newly observed, fresh completed bars advance durable state. No orders,
historical backfill, or alternative fills are created by these cohorts.
"""
from collections import defaultdict
import logging
from .futures import risk_day
from .market import number
from .store import identity

VERSION = 'futures-entry-variants-v1'
NAMES = ('repeated', 'first_per_trend', 'pullback_reset')
PREFIX = VERSION + ':'


def observe_bar(db, c, symbol, f, now):
    from .futures_assessment import observe
    observe(db, c, symbol, f, now)
    key = PREFIX + symbol
    old = db.get(c, key, {})
    stamp = number(f.get('asof'))
    if stamp is None or stamp > now or stamp <= old.get('bar_at', 0):
        return
    current = f.get('status') == 'ready' and 0 <= now-stamp <= 90
    bias = f.get('htf15_bias') if current else None
    bias = bias if bias in (-1, 0, 1) else None
    new_day = old.get('day') != risk_day(stamp)
    contiguous = stamp-old.get('bar_at', stamp-60) == 60
    # After a gap, the first entry in a trend is unknowable until an observed
    # contiguous transition. Do not manufacture a new trend after a restart.
    transition = contiguous and bias is not None and old.get('bias') is not None and bias != old['bias']
    reset = not old or new_day or transition
    known = current and bias is not None and (reset or (contiguous and old.get('known', False)))
    s = dict(old, day=risk_day(stamp), bar_at=stamp, observed_at=now,
             bias=bias, known=known)
    if reset:
        s.update(epoch=identity(VERSION, symbol, risk_day(stamp), stamp),
                 epoch_at=stamp, touch_at=None, reclaim_at=None)
    if not known:
        s.update(touch_at=None, reclaim_at=None)
    bar = f.get('bar', {})
    low, high, close, e9, e21 = (number(v) for v in
        (bar.get('l'), bar.get('h'), bar.get('c'), f.get('ema9'), f.get('ema21')))
    if known and bias in (-1, 1) and all(v is not None for v in (low, high, close, e9, e21)):
        # A completed bar spanning EMA21 is a touch. Reclaim must be a later
        # completed bar; its close must clear EMA9 in the trend direction.
        touched = low <= e21 <= high
        reclaimed = close > e9 if bias == 1 else close < e9
        if s.get('touch_at') and stamp > s['touch_at'] and reclaimed:
            s['reclaim_at'] = stamp
        if touched and not s.get('reclaim_at'):
            s['touch_at'] = s.get('touch_at') or stamp
        # Retain a completed reset until a trial consumes it. Subsequent
        # touches begin another reset, preserving completed-bar ordering.
        if touched and old.get('reclaim_at') and stamp > old['reclaim_at']:
            s.update(touch_at=stamp, reclaim_at=None)
    db.put(c, key, s)
    if db.get(c, PREFIX+'activation') is None:
        db.put(c, PREFIX+'activation', {'at':now, 'version':VERSION})
        logging.getLogger('uvicorn.error').info('Futures entry variants activated: version=%s at=%s', VERSION, now)


def classify(db, c, signal, now, admitted):
    result = {'version':VERSION, 'at':now, 'repeated':'selected' if admitted else 'excluded',
              'first_per_trend':'unknown', 'pullback_reset':'unknown'}
    if not admitted:
        return dict(result, first_per_trend='excluded', pullback_reset='excluded', reason='entry_excluded')
    s = db.get(c, PREFIX+signal['symbol'], {})
    stamp = number(signal.get('signal_time'))
    if not s.get('known') or stamp != s.get('bar_at') or not 0 <= now-s['bar_at'] <= 90:
        return dict(result, reason='missing_fresh_contiguous_trend_context')
    result.update(epoch=s['epoch'], trend_at=s['epoch_at'], bias=s['bias'],
                  bar_at=s['bar_at'], touch_at=s.get('touch_at'), reclaim_at=s.get('reclaim_at'))
    direction = 1 if signal['side'] == 'long' else -1
    if s['bias'] != direction:
        return dict(result, first_per_trend='skipped', pullback_reset='skipped', reason='outside_directional_trend')
    key = PREFIX+'rule:'+identity(signal['symbol'], signal['strategy'], signal['side'])
    rule = db.get(c, key, {})
    first = rule.get('epoch') != s['epoch']
    reset = bool(s.get('touch_at') and s.get('reclaim_at') and
                 s['touch_at'] > rule.get('last_pullback_entry', now) and
                 s['reclaim_at'] > s['touch_at'])
    result.update(first_per_trend='selected' if first else 'skipped',
                  pullback_reset='selected' if first or reset else 'skipped',
                  reason='first_observed_entry' if first else 'pullback_reset' if reset else 'repeat_without_reset')
    rule.update(epoch=s['epoch'])
    if first or reset:
        rule['last_pullback_entry'] = now
    db.put(c, key, rule)
    return result


def report(rows):
    grouped = defaultdict(list)
    legacy = 0
    for p in rows:
        if p.get('asset') != 'future':
            continue
        if p.get('entry_variants', {}).get('version') != VERSION:
            legacy += 1
            continue
        grouped[(p['symbol'], p['strategy'], p['side'], p['version'], p.get('fill_version'), p['alerted'])].append(p)
    groups = []
    for (symbol, strategy, side, model, fill, alerted), members in grouped.items():
        for variant in NAMES:
            selected = [p for p in members if p['entry_variants'][variant] == 'selected']
            closed = [p for p in selected if p['status'] == 'closed']
            wins = sum(p['pnl'] > 0 for p in closed)
            groups.append(dict(symbol=symbol, strategy=strategy, side=side, model=model, fill_version=fill,
                alerted=alerted, variant=variant, total=len(members), selected=len(selected),
                skipped=sum(p['entry_variants'][variant] == 'skipped' for p in members),
                unknown=sum(p['entry_variants'][variant] == 'unknown' for p in members),
                excluded=sum(p['entry_variants'][variant] == 'excluded' for p in members),
                open=sum(p['status'] == 'open' for p in selected),
                unresolved=sum(p['status'] == 'unresolved' for p in selected),
                closed=len(closed), wins=wins, losses=sum(p['pnl'] < 0 for p in closed),
                breakeven=sum(p['pnl'] == 0 for p in closed),
                win_rate=wins/len(closed) if closed else None,
                mean_r=sum(p['r_multiple'] for p in closed)/len(closed) if closed else None))
    return {'version':VERSION, 'groups':groups, 'pre_activation_trials':legacy,
            'basis':'Frozen entry selections share the original one-unit quote trial; overlapping results are not portfolio returns'}
