"""Receipt-time TM assessments and independently scheduled price checkpoints."""
import asyncio
import logging
import time
import uuid
from collections import Counter
from sqlalchemy import select, func, update
from .store import tm_studies as studies, tm_checkpoints as checkpoints, flow_records, state, events, identity
from .market import fresh,day
from .tm_scoring import VERSION, PROTOCOL, HORIZONS, score, age_bucket, dte_bucket, checkpoint_times

LOG=logging.getLogger('uvicorn.error')
KEY='tm-study-v1'
# These option underlyings do not use the Alpaca stock quote stream. Retain
# their records, but never substitute an ETF proxy for an index outcome.
from .universe import INDEX_UNDERLYINGS


def symbol_for(row):
    from .universe import symbols, EXCLUDED_STOCKS
    try:
        symbol=symbols((row['symbol'],))[0]
        if symbol in INDEX_UNDERLYINGS:return symbol,'unsupported_index_underlying'
        return symbol,None if symbol not in EXCLUDED_STOCKS else 'collection_excluded_symbol'
    except (ValueError,IndexError,KeyError):
        return str(row.get('symbol',''))[:100],'invalid_underlying_symbol'


def contexts(c,symbols,at):
    keys=[prefix+s for s in symbols for prefix in ('quote:','scanner_features:','bar_window:')]
    saved={r.key:r.value for r in c.execute(select(state.c.key,state.c.value)
        .where(state.c.key.in_(keys),state.c.updated<=at))} if keys else {}
    result={}
    from .scanner import features,bars_from_window
    for symbol in symbols:
        f=saved.get('scanner_features:'+symbol,{})
        if f.get('status')!='ready' or not 0<=at-f.get('asof',0)<=90:
            f=features(symbol,bars_from_window(saved.get('bar_window:'+symbol,[])),at)
        result[symbol]=(saved.get('quote:'+symbol),f)
    return result


def register(db,c,label,rows,receipt,*,existing=None,envelope=None,at=None,historical=False):
    """One insert per ID/version. Corrections cannot mutate frozen assessments."""
    at=time.time() if at is None else at
    existing=existing or {}
    unique={}
    for row in rows:unique.setdefault(row['vendor_id'],row)
    ids={vendor:identity(VERSION,label,vendor) for vendor in unique}
    present=set(c.execute(select(studies.c.id).where(studies.c.id.in_(list(ids.values())))).scalars())
    wanted=[row for vendor,row in unique.items() if ids[vendor] not in present]
    if not wanted:return 0
    db.locked_get(c,KEY+':activation',dict(at=at,protocol=PROTOCOL))
    live=[row for row in wanted if not historical and row['vendor_id'] not in existing]
    available=contexts(c,{symbol_for(row)[0] for row in live if not symbol_for(row)[1]},at)
    inserts=[];observations=[]
    for row in wanted:
        vendor=row['vendor_id'];id=ids[vendor]
        old=historical or vendor in existing
        first_seen=existing.get(vendor,receipt)
        symbol,blocked=symbol_for(row)
        if old:
            assessment=dict(direction='unknown',tm_score=None,compass_score=None,score_status='historical_inventory',
                components=[],score_range=None,entry_mid=None,quote_ready=False,
                reason='Not captured at first receipt; latest inventory is not the original score')
        else:
            q,f=available.get(symbol,(None,{}))
            assessment=score(row,q,f,at)
            blocked=blocked or ('late_receipt_processing' if not 0<=at-receipt<=30 else
                'vendor_clock_error' if row['source_ts']>receipt else None)
            if blocked:
                assessment.update(entry_mid=None,entry_quote=None,quote_ready=False,compass_score=None,
                    compass_selected=None,score_status='insufficient_data',score_range=None,components=[])
            assessment['reason']=blocked or (None if assessment['quote_ready'] else 'No fresh stored underlying quote at receipt processing')
        entry=assessment.get('entry_mid')
        targets=checkpoint_times(at) if entry is not None else {}
        origin='historical_inventory' if old else 'prospective'
        payload=dict(id=id,version=VERSION,day=label,vendor_id=vendor,symbol=symbol,first_seen=first_seen,
            assessed_at=at,processing_seconds=at-first_seen,origin=origin,receipt_day=day(first_seen),
            first_snapshot=None if old else dict(row),inventory_snapshot=dict(row) if old else None,
            vendor_envelope={k:(envelope or {}).get(k) for k in
                ('cached','vendor_stale','vendor_envelope_ts','vendor_computed_ts','vendor_session_state')},
            source_ts=row['source_ts'],source_age_at_receipt=first_seen-row['source_ts'],
            assessment=assessment,entry_mid=entry,checkpoint_targets=targets)
        inserts.append(dict(id=id,day=label,vendor_id=vendor,version=VERSION,symbol=symbol,first_seen=first_seen,
            assessed_at=at,origin=origin,status='pending' if entry is not None else 'unavailable',
            score_status=assessment['score_status'],direction=assessment['direction'],
            age_bucket=age_bucket(row['source_ts'],first_seen),dte_bucket=dte_bucket(row.get('expiry'),first_seen),
            tm_score=assessment['tm_score'],compass_score=assessment['compass_score'],payload=payload))
        for horizon in HORIZONS:
            target=targets.get(horizon)
            observations.append(dict(study_id=id,horizon=horizon,target_at=target,
                due_at=target+10 if target is not None else None,status='pending' if target is not None else 'unavailable',
                reason=None if target is not None else 'historical_inventory' if old else blocked or 'missing_receipt_quote'))
    created=set(c.execute(db.insert(studies).values(inserts).on_conflict_do_nothing(index_elements=['id'])
        .returning(studies.c.id)).scalars())
    observations=[r for r in observations if r['study_id'] in created]
    for offset in range(0,len(observations),500):
        c.execute(db.insert(checkpoints).values(observations[offset:offset+500])
            .on_conflict_do_nothing(index_elements=['study_id','horizon']))
    return len(created)


def inventory(db,c,at,limit=500):
    joined=flow_records.outerjoin(studies,(studies.c.day==flow_records.c.day)&
        (studies.c.vendor_id==flow_records.c.vendor_id)&(studies.c.version==VERSION))
    rows=c.execute(select(flow_records).select_from(joined).where(studies.c.id.is_(None))
        .order_by(flow_records.c.day,flow_records.c.vendor_id).limit(limit)).mappings().all()
    groups={}
    for row in rows:groups.setdefault(row['day'],[]).append(row)
    count=0
    for label,items in groups.items():
        count+=register(db,c,label,[r['payload'] for r in items],at,
            existing={r['vendor_id']:r['first_seen'] for r in items},at=at,historical=True)
    return count


def quote_at(c,symbol,target):
    # Five-second source window and a fixed recording deadline, not worker time.
    rows=c.execute(select(events.c.ts,events.c.received,events.c.payload).where(events.c.kind=='quote',
        events.c.symbol==symbol,events.c.ts>=target-5,events.c.ts<=target,events.c.received<=target+5)
        .order_by(events.c.ts.desc(),events.c.id.desc()).limit(201)).all()
    if len(rows)>200:return None,'quote_window_overflow'
    for row in rows:
        q=row.payload
        if q.get('ts')==row.ts and row.ts<=row.received and fresh(q,row.received) and fresh(q,target):
            return dict(price=(q['bid']+q['ask'])/2,source_ts=row.ts,received_at=row.received),None
    return None,'no_timely_usable_checkpoint_quote'


def observe(db,c,now,limit=300):
    rows=c.execute(select(checkpoints,studies.c.symbol,studies.c.direction,studies.c.payload)
        .join(studies,studies.c.id==checkpoints.c.study_id)
        .where(checkpoints.c.status=='pending',checkpoints.c.due_at<=now)
        .order_by(checkpoints.c.due_at,checkpoints.c.study_id,checkpoints.c.horizon).limit(limit)).mappings().all()
    cache={};changed=set()
    for row in rows:
        key=(row['symbol'],row['target_at'])
        if key not in cache:cache[key]=quote_at(c,*key)
        quote,reason=cache[key]
        values=dict(status='completed' if quote else 'unavailable',reason=reason,checked_at=now)
        if quote:
            move=(quote['price']/row['payload']['entry_mid']-1)*100
            values.update(**quote,raw_return_pct=move,directional_return_pct=
                move if row['direction']=='long' else -move if row['direction']=='short' else None)
        c.execute(update(checkpoints).where(checkpoints.c.study_id==row['study_id'],
            checkpoints.c.horizon==row['horizon'],checkpoints.c.status=='pending').values(**values))
        changed.add(row['study_id'])
    for id in changed:
        counts=Counter(c.execute(select(checkpoints.c.status).where(checkpoints.c.study_id==id)).scalars())
        status='pending' if counts['pending'] else 'complete' if counts['completed']==len(HORIZONS) else 'partial' if counts['completed'] else 'unavailable'
        c.execute(update(studies).where(studies.c.id==id).values(status=status))
    return len(rows)


def requested_symbols(c,now):
    return c.execute(select(studies.c.symbol,func.max(studies.c.first_seen).label('seen'))
        .where(studies.c.origin=='prospective',
            (studies.c.first_seen>=now-86400)|(studies.c.status=='pending'))
        .group_by(studies.c.symbol).order_by(func.max(studies.c.first_seen).desc(),studies.c.symbol)
        .limit(501)).all()


class Study:
    def __init__(self,db,cfg):self.db,self.cfg,self.owner=db,cfg,uuid.uuid4().hex

    def tick(self,now):
        from .tm_reporting import report
        with self.db.tx() as c:
            if not self.db.lease(c,KEY,self.owner,120):return
            self.db.locked_get(c,KEY+':activation',dict(at=now,protocol=PROTOCOL))
            checked=observe(self.db,c,now)
            added=inventory(self.db,c,now)
            self.db.put(c,KEY+':worker',dict(at=now,version=VERSION,checkpoints_processed=checked,historical_registered=added))
            previous=self.db.get(c,KEY+':report',{})
            if now-previous.get('at',0)>=60:
                result=report(self.db,c,now,cfg=self.cfg)
                self.db.put(c,KEY+':report',result)
                LOG.info('TM study coverage: %s',__import__('json').dumps({k:result[k] for k in ('at','inventory','counts','outcome_counts')},sort_keys=True))
        self.db.health('tm_scoring_study','running','Frozen receipt assessments and forward underlying checkpoints',
            version=VERSION,checkpoints_processed=checked,historical_registered=added)

    async def run(self):
        while True:
            try:await asyncio.to_thread(self.tick,time.time())
            except asyncio.CancelledError:raise
            except Exception as error:
                LOG.exception('TM study worker failed')
                await asyncio.to_thread(self.db.health,'tm_scoring_study','error',type(error).__name__)
            await asyncio.sleep(5)
