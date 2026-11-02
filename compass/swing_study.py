"""Frozen technical candidates, pre-gate flow evidence and common price endpoints."""
import asyncio
import logging
import time
import uuid
from collections import Counter
from datetime import date,datetime,timedelta
from sqlalchemy import select,update,func
from .store import swing_trials as trials,swing_checkpoints as checkpoints,events,state,identity
from .market import day,session,fresh,number,NY
from .swing_signals import summarize_flow
from .flow_recovery import freshness
from .tm_study import quote_at,symbol_for

VERSION='swing-flow-study-v1'
KEY=VERSION
HORIZONS=('60m','close','1session','3session','5session','9session')
GROUPS=('fresh_flow','delayed_flow','fresh_vendor_blocked','no_confirming_flow','unavailable')
PROTOCOL=dict(version=VERSION,primary_horizon='5session',horizons=list(HORIZONS),
    population='Every newly retained Swing technical candidate, before flow/contract admission.',
    entry='First observed candidate processing; valid underlying midpoint within five seconds; signal within 90 seconds.',
    fresh_flow_seconds=300,flow_window_seconds=1800,
    outcome='Directional underlying midpoint change; no modeled option return, stop path, fees or executable fill.',
    comparison='Same candidate clock and endpoint for technical-only, fresh, delayed and relaxed flow selections.',
    review='Fixed before outcomes; review by symbol/session. Overlapping candidates are correlated; no automatic rule promotion.')


def flow_snapshot(rows,matrix,coverage,at,min_dte,max_dte):
    """Keep the actual pre-gate snapshots, not later vendor corrections."""
    by_symbol={};future=0
    for raw in rows:
        r=dict(raw);p=r['payload']
        if any(number(t) is not None and t>at for t in
                (r.get('first_seen'),r.get('last_seen'),p.get('received'),p.get('source_ts'))):
            future+=1;continue
        by_symbol.setdefault(p['symbol'],[]).append({k:r.get(k) for k in ('first_seen','last_seen','payload')})
    return dict(at=at,rows=by_symbol,matrix={k:matrix.get(k) for k in
        ('source_ts','received','cached','vendor_stale','stale','vendor_computed_ts','vendor_envelope_ts')},
        coverage=dict(coverage),future_snapshots=future,min_dte=min_dte,max_dte=max_dte)


def classify(snapshot,symbol,side,at):
    snap=snapshot or {};matrix=snap.get('matrix',{});coverage=snap.get('coverage',{})
    poll=number(matrix.get('received'));stamp=number(snap.get('at'))
    reasons=[]
    if stamp is None or not 0<=at-stamp<=30:reasons.append('flow_snapshot_missing_or_old')
    if poll is None or not 0<=at-poll<=60:reasons.append('vendor_poll_missing_or_old')
    if coverage.get('query_truncated'):reasons.append('flow_query_truncated')
    if coverage.get('vendor_limited') or coverage.get('recovery',{}).get('estimated_gap',0):reasons.append('vendor_recovery_incomplete')
    if snap.get('future_snapshots'):reasons.append('future_flow_snapshot_excluded')
    check=freshness(matrix,at,300)
    if check['status']=='clock_error':reasons.append('vendor_clock_error')
    rows=snap.get('rows',{}).get(symbol,[])
    summary=summarize_flow(rows,at,snap.get('min_dte',14),snap.get('max_dte',60)).get(symbol,{})
    aligned,opposite=('bullish','bearish') if side=='long' else ('bearish','bullish')
    premium=summary.get(aligned+'_premium',0);opposing=summary.get(opposite+'_premium',0)
    latest=number(summary.get(aligned+'_latest'));age=at-latest if latest is not None else None
    strength=bool(age is not None and 0<=age<=1800 and premium>=100000 and
        (len(summary.get(aligned+'_ids',[]))>=2 or premium>=250000) and premium>=2*opposing)
    group=('unavailable' if reasons else 'no_confirming_flow' if not strength else
        'delayed_flow' if age>300 else 'fresh_flow' if check['eligible_for_live_confirmation'] else 'fresh_vendor_blocked')
    return dict(group=group,reasons=reasons,summary=summary,aligned_source_age=age,
        strength_passed=strength,vendor_check=check,snapshot_at=stamp,source_rows=rows,
        coverage=coverage,min_dte=snap.get('min_dte'),max_dte=snap.get('max_dte'),
        dte_policy=str(snap.get('min_dte'))+'–'+str(snap.get('max_dte')),
        note='No confirming flow means none qualifies in the received filtered window; it is not proof of no market activity.')


def targets(at):
    sessions=[];start=date.fromisoformat(day(at))
    for offset in range(45):
        hours=session((start+timedelta(days=offset)).isoformat())
        if hours and hours[1]>at:sessions.append(hours)
        if len(sessions)==10:break
    if len(sessions)<10:raise ValueError('Insufficient exchange calendar coverage')
    remaining=3600
    for opening,close in sessions:
        available=close-max(opening,at)
        if remaining<=available:
            minute=min(max(opening,at)+remaining,close-.001);break
        remaining-=available
    return dict(zip(HORIZONS,[minute,*[sessions[i][1]-.001 for i in (0,1,3,5,9)]]))


def register(db,c,event,at,*,snapshot=None,historical=False):
    key=identity(VERSION,event['id'])
    if c.execute(select(trials.c.id).where(trials.c.id==key)).first():return False
    original=event['payload'];signal=original.get('signal',{})
    symbol,blocked=symbol_for({'symbol':signal.get('symbol',event['symbol'])})
    side=signal.get('side','unknown');first=event['received']
    old=historical or snapshot is None
    flow=classify(snapshot,symbol,side,at) if not old else dict(group='historical_inventory',
        reasons=['Original pre-gate flow context was not captured'],source_rows=[],summary={})
    q=c.execute(select(state.c.value).where(state.c.key=='quote:'+symbol,state.c.updated<=at)).scalar_one_or_none() if not old and not blocked else None
    signal_at=number(signal.get('signal_time'))
    reason='historical_inventory' if old else blocked
    if not reason and side not in ('long','short'):reason='unknown_direction'
    if not reason and (number(signal.get('signal_price')) or 0)<=0:reason='invalid_signal_reference'
    if not reason and (not 0<=at-first<=30 or signal_at is None or not 0<=at-signal_at<=90):reason='candidate_processing_clock_or_age'
    if not reason and not (fresh(q,at) and signal_at<=q['ts']<=at and 0<=at-q['ts']<=5 and q.get('received',at)<=at):reason='missing_fresh_candidate_quote'
    entry=None if reason else (q['bid']+q['ask'])/2
    scheduled=targets(at) if entry is not None else {}
    risk=abs((number(signal.get('signal_price')) or 0)-(number(signal.get('stop')) or 0))
    atr=number(signal.get('daily',{}).get('atr14'))
    price_context=dict(signal_to_reference_pct=(entry/signal['signal_price']-1)*100,
        within_entry_tolerance=bool(atr and abs(entry-signal['signal_price'])<=.25*atr),
        initial_risk=risk) if entry is not None else {}
    payload=dict(id=key,version=VERSION,source_id=event['id'],source_key=event['key'],
        first_seen=first,candidate_at=event['ts'],assessed_at=at,signal=signal,
        origin='historical_inventory' if old else 'prospective',flow=flow,
        original_candidate=original,original_flow_confirmed=original.get('flow_confirmed'),
        entry_mid=entry,entry_quote=dict(q) if entry is not None else None,entry_reason=reason,
        price_context=price_context,checkpoint_targets=scheduled,receipt_day=day(first),
        admission_id=identity('swing-ideas-v1',symbol,side,signal.get('rule'),signal.get('signal_time')),
        note='Common candidate-time price observations; admitted option simulations remain in their original ledger.')
    inserted=c.execute(db.insert(trials).values(id=key,version=VERSION,source_id=event['id'],symbol=symbol,
        side=side,rule=signal.get('rule','unknown'),first_seen=first,assessed_at=at,origin=payload['origin'],
        flow_group=flow['group'],status='pending' if entry is not None else 'unavailable',payload=payload)
        .on_conflict_do_nothing(index_elements=['id']).returning(trials.c.id)).first()
    if not inserted:return False
    c.execute(db.insert(checkpoints).values([dict(study_id=key,horizon=h,target_at=scheduled.get(h),
        due_at=scheduled[h]+10 if h in scheduled else None,status='pending' if h in scheduled else 'unavailable',
        reason=reason) for h in HORIZONS]).on_conflict_do_nothing(index_elements=['study_id','horizon']))
    return True


def inventory(db,c,at,limit=300):
    rows=c.execute(select(events).outerjoin(trials,(trials.c.source_id==events.c.id)&(trials.c.version==VERSION))
        .where(events.c.kind=='swing_candidate',trials.c.id.is_(None)).order_by(events.c.id).limit(limit)).mappings().all()
    return sum(register(db,c,r,at,historical=True) for r in rows)


def basis_reason(c,symbol,payload,target):
    """Exclude a known change in the split-adjusted reference available by target."""
    daily=payload.get('signal',{}).get('daily',{})
    try:
        start=datetime.fromisoformat(daily['through']).replace(tzinfo=NY)
        stop=start+timedelta(days=1)
    except (KeyError,TypeError,ValueError):return 'missing_reference_price_basis'
    original=number(daily.get('close'))
    p=c.execute(select(events.c.payload).where(events.c.kind=='daily',events.c.symbol==symbol,
        events.c.ts>=start.timestamp(),events.c.ts<stop.timestamp(),events.c.received<=target+5)
        .order_by(events.c.received.desc(),events.c.id.desc()).limit(1)).scalar_one_or_none()
    current=number((p or {}).get('c'))
    if not original or not current:return 'missing_reference_price_basis'
    return 'historical_price_basis_changed' if abs(current/original-1)>.01 else None


def observe(db,c,at,limit=300):
    rows=c.execute(select(checkpoints,trials.c.symbol,trials.c.side,trials.c.payload)
        .join(trials,trials.c.id==checkpoints.c.study_id)
        .where(checkpoints.c.status=='pending',checkpoints.c.due_at<=at)
        .order_by(checkpoints.c.due_at,checkpoints.c.study_id).limit(limit)).mappings().all()
    quotes={};changed=set()
    for r in rows:
        k=(r['symbol'],r['target_at'])
        if k not in quotes:quotes[k]=quote_at(c,*k)
        q,reason=quotes[k]
        reason=reason or basis_reason(c,r['symbol'],r['payload'],r['target_at'])
        values=dict(status='unavailable' if reason else 'completed',reason=reason,checked_at=at)
        if not reason:
            move=(q['price']/r['payload']['entry_mid']-1)*100
            values.update(**q,raw_return_pct=move,directional_return_pct=move if r['side']=='long' else -move)
        c.execute(update(checkpoints).where(checkpoints.c.study_id==r['study_id'],
            checkpoints.c.horizon==r['horizon'],checkpoints.c.status=='pending').values(**values))
        changed.add(r['study_id'])
    for id in changed:
        counts=Counter(c.execute(select(checkpoints.c.status).where(checkpoints.c.study_id==id)).scalars())
        status='pending' if counts['pending'] else 'complete' if counts['completed']==len(HORIZONS) else 'partial' if counts['completed'] else 'unavailable'
        c.execute(update(trials).where(trials.c.id==id).values(status=status))
    return len(rows)


def requested_symbols(c):
    return c.execute(select(trials.c.symbol).where(trials.c.origin=='prospective',trials.c.status=='pending')
        .distinct().order_by(trials.c.symbol).limit(501)).scalars().all()


class Study:
    def __init__(self,db,cfg):self.db,self.cfg,self.owner=db,cfg,uuid.uuid4().hex

    def tick(self,at):
        from .swing_study_report import report
        with self.db.tx() as c:
            if not self.db.lease(c,KEY,self.owner,120):return
            self.db.locked_get(c,KEY+':activation',dict(at=at,protocol=PROTOCOL))
            checked=observe(self.db,c,at);added=inventory(self.db,c,at)
            self.db.put(c,KEY+':worker',dict(at=at,version=VERSION,checked=checked,historical_registered=added))
            if at-self.db.get(c,KEY+':report',{}).get('at',0)>=60:
                result=report(self.db,c,at,self.cfg);self.db.put(c,KEY+':report',result)
                logging.getLogger('uvicorn.error').info('Swing comparison coverage: %s',
                    {k:result[k] for k in ('at','inventory','counts')})
        self.db.health('swing_flow_study','running','Frozen technical candidates with common forward price checkpoints',version=VERSION)

    async def run(self):
        while True:
            try:await asyncio.to_thread(self.tick,time.time())
            except asyncio.CancelledError:raise
            except Exception as error:
                logging.getLogger('uvicorn.error').exception('Swing comparison worker failed')
                await asyncio.to_thread(self.db.health,'swing_flow_study','error',type(error).__name__)
            await asyncio.sleep(5)
