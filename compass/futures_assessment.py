"""Forward-only market context; hypotheses, not a trained strategy selector."""
from collections import defaultdict
from .market import number, fresh

VERSION = 'futures-market-assessment-v1'
PREFIX = VERSION + ':'


def assess(f, now):
    result = dict(version=VERSION, observed_at=now, bar_at=f.get('asof'),
                  state='unknown', direction=0, reason='missing_fresh_features')
    stamp = number(f.get('asof'))
    if f.get('status') != 'ready' or stamp is None or not 0 <= now-stamp <= 90:
        return result
    names = ('ema9', 'ema21', 'ema21_previous', 'atr14', 'vwap', 'rvol20')
    values = {k:number(f.get(k)) for k in names}
    close = number(f.get('bar', {}).get('c'))
    if any(v is None for v in values.values()) or close is None or values['atr14'] <= 0:
        return dict(result, reason='missing_indicators')
    result['features'] = dict(values, close=close, htf15_bias=f.get('htf15_bias'))
    if f.get('htf15_bias') not in (-1, 0, 1):
        return dict(result, reason='missing_completed_higher_timeframe')
    atr = values['atr14']
    separation = (values['ema9']-values['ema21'])/atr
    slope = (values['ema21']-values['ema21_previous'])/atr
    result.update(ema_separation_atr=separation, ema_slope_atr=slope,
                  range_atr=(f['bar']['h']-f['bar']['l'])/atr)
    up = separation >= .15 and slope > 0 and close > values['vwap'] and f['htf15_bias'] == 1
    down = separation <= -.15 and slope < 0 and close < values['vwap'] and f['htf15_bias'] == -1
    if result['range_atr'] >= 2 and values['rvol20'] >= 1.5:
        return dict(result, state='expansion', direction=1 if close > values['ema21'] else -1,
                    reason='large_bar_with_elevated_volume')
    if up or down:
        return dict(result, state='trend', direction=1 if up else -1, reason='ema_vwap_higher_timeframe_agree')
    if abs(separation) <= .15 and abs(slope) <= .05:
        return dict(result, state='range_candidate', reason='compressed_flat_emas')
    return dict(result, state='mixed', reason='directional_evidence_disagrees')


def observe(db, c, symbol, f, now):
    old = db.get(c, PREFIX+symbol, {})
    stamp = number(f.get('asof'))
    if stamp is None or stamp <= (old.get('bar_at') or 0) or stamp > now:
        return
    db.put(c, PREFIX+symbol, assess(f, now))


def freeze(db, c, signal, now, q, spec):
    saved = db.get(c, PREFIX+signal['symbol'], {})
    valid = (saved.get('version') == VERSION and saved.get('bar_at') == signal.get('signal_time')
             and saved.get('observed_at', now+1) <= now and 0 <= now-saved['bar_at'] <= 90)
    state = dict(saved) if valid else dict(version=VERSION, state='unknown', direction=0,
                                          reason='no_matching_forward_assessment')
    stamp = number(signal.get('signal_time'))
    liquid = bool(stamp is not None and fresh(q, now) and q['ts'] >= stamp and
                  q['ask']-q['bid'] <= max(spec['tick']*8, (q['ask']+q['bid'])/2*.002))
    rule = signal.get('rule', '')
    direction = 1 if signal['side'] == 'long' else -1
    suitable = ((state['state'] == 'trend' and state['direction'] == direction and rule in ('trend_pullback', 'orb_retest'))
        or (state['state'] == 'expansion' and state['direction'] == direction and rule == 'volume_breakout')
        or (state['state'] == 'range_candidate' and rule == 'session_sweep_reclaim'))
    return dict(state, frozen_at=now, quote_fresh_and_liquid=liquid,
                proposed_selection='candidate' if suitable and liquid else 'abstain',
                selection_basis='Untested fixed hypothesis; all existing trials continue', execution_eligible=False)


def report(rows):
    grouped = defaultdict(list)
    legacy = 0
    for p in rows:
        if p.get('asset') != 'future':
            continue
        a = p.get('market_assessment', {})
        if a.get('version') != VERSION:
            legacy += 1
            continue
        key = (p['symbol'], p['strategy'], p['side'], p['version'], p.get('fill_version'),
               p['alerted'], a['state'], a['proposed_selection'])
        grouped[key].append(p)
    groups = []
    for key, members in sorted(grouped.items(), key=lambda item:str(item[0])):
        closed = [p for p in members if p['status'] == 'closed']
        groups.append(dict(zip(('symbol','strategy','side','model','fill_version','alerted','state','selection'),key),
            total=len(members), closed=len(closed),
            open=sum(p['status']=='open' for p in members),
            unresolved=sum(p['status']=='unresolved' for p in members),
            excluded=sum(p['status']=='excluded' for p in members),
            mean_r=sum(p['r_multiple'] for p in closed)/len(closed) if closed else None,
            win_rate=sum(p['pnl']>0 for p in closed)/len(closed) if closed else None))
    return dict(version=VERSION, groups=groups, pre_activation_trials=legacy, execution_eligible=False,
                basis='Forward frozen hypotheses; illustrative costs; overlapping trials are not portfolio returns or holdout validation')
