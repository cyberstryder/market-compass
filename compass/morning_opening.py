"""Forward-only opening review challenger. Never sends alerts or owns orders."""
import logging
from collections import Counter
from sqlalchemy import Table, Column, String, Float, JSON, Index, select, update
from .store import meta, identity
from .market import day, session, fresh, number

VERSION = 'morning-opening-v1'
rows = Table('morning_opening_reviews_v1', meta,
    Column('id', String(64), primary_key=True), Column('source_key', String(64), nullable=False),
    Column('decided_at', Float, nullable=False), Column('tracking', String(30), nullable=False),
    Column('payload', JSON, nullable=False))
Index('morning_opening_tracking',rows.c.tracking,rows.c.decided_at)


def context(window, now):
    hours = session(day(now))
    if not hours or not hours[0] <= now < hours[0]+1800:
        return {'ready': False, 'reason': 'Outside first 30 regular-session minutes'}
    start = hours[0]
    bars = {r[0]: r for r in window if start <= r[0] and r[0]+60 <= now}
    expected = list(range(int(start), int(now//60)*60, 60))
    missing = [t for t in expected if t not in bars]
    base = dict(ready=False, completed_minutes=len(expected), missing_minutes=missing,
        method='Five-minute opening range and observed session volume-weighted price; no ATR or EMA warm-up substitute')
    if len(expected)<5 or missing:
        return {**base, 'reason': 'Five completed opening minutes and every elapsed session minute required'}
    data = [bars[t] for t in expected]
    if any(len(r)<6 or any(number(v) is None for v in r[1:6]) or r[5]<0 or
           r[2]<max(r[1],r[3],r[4]) or r[3]>min(r[1],r[2],r[4]) for r in data):
        return {**base, 'reason': 'Invalid OHLCV'}
    volume=sum(r[5] for r in data)
    high=max(r[2] for r in data[:5]); low=min(r[3] for r in data[:5])
    if volume<=0 or high<=low:
        return {**base, 'reason':'Positive session volume and opening range required'}
    prices=[r[6] if len(r)>6 and number(r[6]) is not None else r[4] for r in data]
    return {**base, 'ready':True, 'reason':None, 'asof':expected[-1]+60,
        'range_high':high, 'range_low':low, 'range_width':high-low,
        'weighted_price':sum(p*r[5] for p,r in zip(prices,data))/volume,
        'weighted_price_basis':'Bar VWAP where available, otherwise bar close; volume weighted',
        'first_close':data[0][4], 'last_close':data[-1][4]}


def assess(candidate, inputs, feature, now):
    from .secondary import current
    available=candidate.get('available_at',candidate['source_ts'])
    timely=current(available,now,60) and candidate['source_ts']<=now
    q=inputs.get('quote'); missing=[]; rejected=[]; support=[]
    if not timely: missing.append('Original 60-second review window expired')
    if not feature['ready']: missing.append(feature['reason'])
    if candidate.get('side') not in ('long','short'): missing.append('Direction unavailable')
    if inputs.get('price_basis')!='same_underlying': missing.append('Same underlying required')
    quote_ok=fresh(q,now)
    if not quote_ok: missing.append('Independent bid/ask quote missing or older than five seconds')
    side=1 if candidate.get('side')=='long' else -1
    if candidate.get('resolved_at_review'): rejected.append('Original already resolved')
    if quote_ok:
        mid=(q['bid']+q['ask'])/2
        if q['ask']-q['bid']>max(.08,mid*.002): rejected.append('Spread exceeds original review limit')
        for key,label in [('stop','Original invalidation reached'),('target','Original target reached')]:
            value=number(candidate.get(key))
            if value is not None and (side*(mid-value)<=0 if key=='stop' else side*(value-mid)<=0): rejected.append(label)
        if feature['ready']:
            entry=number(candidate.get('entry'))
            if entry is None: missing.append('Original reference price required')
            elif abs(mid-entry)>.5*feature['range_width']: rejected.append('Price moved more than half the five-minute opening range from reference')
            if side*(mid-feature['weighted_price'])>0: support.append('Session weighted price agrees')
            boundary=feature['range_high'] if side==1 else feature['range_low']
            if side*(mid-boundary)>0: support.append('Five-minute opening range broken')
            if side*(feature['last_close']-feature['first_close'])>0: support.append('Completed opening price direction agrees')
    verdict='insufficient_data' if missing else 'rejected' if rejected else 'supported' if len(support)==3 else 'watch'
    return dict(rule_version=VERSION,verdict=verdict,timely=timely,accepted=verdict=='supported',
        reasons=missing or rejected or support,missing=missing,support=support,rejections=rejected,
        shadow_only=True,scope='Opening-context challenger; underlying measurements, not orders or option P&L')


def consider(db,c,candidate,inputs,baseline,now):
    if candidate['project']!='morning': return
    hours=session(day(candidate['source_ts']))
    if not hours or not hours[0]<=candidate['source_ts']<hours[0]+1800: return
    key=identity(VERSION,candidate['source_key'])
    if c.execute(select(rows.c.id).where(rows.c.id==key)).first(): return
    feature=context(db.get(c,'bar_window:'+inputs['market_symbol'],[]),now) if inputs.get('market_symbol') else {'ready':False,'reason':'Symbol unavailable'}
    decision=assess(candidate,inputs,feature,now)
    if (decision['missing'] and decision['timely'] and not decision['rejections']
            and baseline['missing'] and not baseline['rejections']
            and now<candidate.get('available_at',candidate['source_ts'])+60): return
    from .secondary import start_measurement
    payload=dict(version=VERSION,symbol=candidate['symbol'],side=candidate['side'],source_id=candidate['source_id'],
        source_key=candidate['source_key'],source_ts=candidate['source_ts'],decided_at=now,
        feature=feature,quote=inputs.get('quote'),decision=decision,baseline_at_capture=baseline,
        measurements=start_measurement(candidate,inputs,decision,now))
    c.execute(db.insert(rows).values(id=key,source_key=candidate['source_key'],decided_at=now,
        tracking=payload['measurements']['state'],payload=payload).on_conflict_do_nothing(index_elements=['id']))


def tick(db,c,now):
    from .secondary import advance_recorded_measurement
    active=c.execute(select(rows).where(rows.c.tracking=='tracking').order_by(rows.c.decided_at).limit(500)).mappings().all()
    for row in active:
        p=dict(row['payload']); m=p['measurements']
        p['measurements']=advance_recorded_measurement(db,c,m,p['side'],db.get(c,'quote:'+m['market_symbol']),now)
        c.execute(update(rows).where(rows.c.id==row['id']).values(payload=p,tracking=p['measurements']['state']))


def report(db,c,now):
    from .secondary import reviews
    selected=c.execute(select(rows).order_by(rows.c.decided_at.desc(),rows.c.id).limit(501)).mappings().all()
    keys=[r['source_key'] for r in selected[:500]]
    originals={r['source_key']:r['verdict'] for r in c.execute(select(reviews.c.source_key,reviews.c.verdict).where(
        reviews.c.project=='morning',reviews.c.source_key.in_(keys))).mappings()}
    items=[dict(r['payload'],baseline_final=originals.get(r['source_key'])) for r in selected[:500]]
    result=dict(version=VERSION,at=now,shadow_only=True,rows=items,truncated=len(selected)>500,
        counts=dict(Counter(p['decision']['verdict'] for p in items)),
        comparison_basis='Baseline-at-capture uses identical review time; baseline-final may decide later. Outcomes retain separate anchors.')
    logging.getLogger('uvicorn.error').info('Morning opening shadow: %s', {k:v for k,v in result.items() if k!='rows'})
    return result
