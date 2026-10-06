"""Bounded gap adjudication and separate post-gap measurements, never repaired fills."""
from sqlalchemy import select, update, func
from .store import gap_followups, identity
from .market import fresh, session, day

VERSION = 'observation-recovery-v1'
# NBIS 2026-10-06: 30s grace was killing good trades on feed blips.
# 90s covers routine latency without trusting truly dead feeds.
GRACE = 30


def enable(p):
    p['gap_policy'] = VERSION


def is_gap(reason):
    return 'observation_gap' in reason or 'quote_missing' in reason


def blocked(p):
    return p.get('gap_pending',{}).get('last_detected_at') == p.get('evidence_checked_at', -1)


def clear(p):
    if p.get('gap_pending') and not blocked(p):
        p['recovered_pending_gaps'] = p.get('recovered_pending_gaps',0)+1
        p.pop('gap_pending',None)
        p.pop('gap_detail',None)


def defer(db,c,p,now,reason):
    if p.get('gap_policy') != VERSION or not is_gap(reason):
        return False
    checked=p.get('evidence_checked_at',now)
    pending=p.setdefault('gap_pending',dict(first_detected_at=checked,reason=reason))
    pending.update(last_detected_at=checked,detail=dict(p.get('gap_detail',{})))
    # Bounded grace lets already-received, fresh-at-storage quotes become visible.
    # It does not increase the permitted source-time gap or admit stale history.
    return checked-pending['first_detected_at'] < GRACE


def register(db,c,p,now,reason):
    if p.get('gap_policy') != VERSION or not is_gap(reason):return
    checked=p.get('evidence_checked_at',now)
    deadline=p.get('flatten_at',p.get('exit_deadline',checked))
    symbol=(p.get('contract') or {}).get('symbol') or p.get('symbol')
    if not symbol:return
    under=p.get('underlying')
    symbols=list(dict.fromkeys(s for s in (symbol,under) if s))
    archive=p.get('archive_check',{})
    checks=list(archive.values()) if 'option' in archive else [archive]
    classification='missing_usable_quotes'
    if any(x.get('late_storage',0) for x in checks if isinstance(x,dict)):
        classification='storage_delay'
    elif any(x.get('stale_or_invalid_when_recorded',0) for x in checks if isinstance(x,dict)):
        classification='stale_or_invalid_at_receipt'
    streams={}
    for s in symbols:
        h=db.get(c,'health:option_stream' if s.startswith('O:') else 'health:alpaca_stocks',{})
        row=h.get('symbols',{}).get(s,{})
        streams[s]=dict(health_at=h.get('at'),**{k:row.get(k) for k in
            ('last_source_ts','last_socket_read_at','last_commit_at','source_to_commit_seconds','silence_seconds')})
    if classification=='missing_usable_quotes' and any(
            r.get('last_source_ts') is not None and r.get('last_commit_at') is not None
            and r.get('last_socket_read_at',0)>r['last_commit_at']
            and 0<=checked-(r.get('health_at') or 0)<=35 for r in streams.values()):
        classification='receipt_ahead_of_storage'
    key=identity(VERSION,p['id'])
    value=dict(id=key,source_id=p['id'],version=VERSION,collection_version=p.get('collection_version','unversioned'),
        study_version=p.get('version'),symbols=symbols,symbol=symbol,underlying=under,
        created_at=checked,deadline=deadline,reason=reason,gap_detail=p.get('gap_detail'),
        classification=classification,receipt_evidence=streams,status='active' if checked<deadline else 'complete',
        samples=0,last_sample_at=None,latest=None,
        basis='Post-gap observed quotes only; original bracket remains unresolved. No repaired entry, exit, P&L or win rate.')
    c.execute(db.insert(gap_followups).values(id=key,status=value['status'],created=checked,
        deadline=deadline,payload=value).on_conflict_do_nothing(index_elements=['id']))
    p['gap_followup_id']=key
    p['gap_classification']=classification


def active(c,now):
    return c.execute(select(gap_followups.c.payload).where(gap_followups.c.status=='active',
        gap_followups.c.deadline>now).order_by(gap_followups.c.created)).scalars().all()


def tick(db,c,now):
    rows=c.execute(select(gap_followups.c.payload).where(gap_followups.c.status=='active')).scalars().all()
    for p in rows:
        if now>=p['deadline']:
            p.update(status='complete',completed_at=now)
        elif now-(p['last_sample_at'] or 0)>=60:
            hours=session(day(now))
            if '@' not in p['symbol'] and (not hours or not hours[0]<=now<hours[1]):continue
            quotes={s:db.get(c,'quote:'+s) for s in p['symbols']}
            usable={s:q for s,q in quotes.items() if fresh(q,now) and 0<=now-q['ts']<=5 and q['ts']>=p['created_at']}
            p.update(last_sample_at=now,checks=p.get('checks',0)+1)
            if usable:
                sample=dict(at=now,quotes={s:{k:q.get(k) for k in ('ts','bid','ask','source','received','socket_read_at')} for s,q in usable.items()},
                    missing_symbols=sorted(set(p['symbols'])-set(usable)),original_outcome='unresolved')
                p.update(samples=p['samples']+1,latest=sample)
                db.append(c,'gap_measurement','research',p['symbol'],now,sample,'gap:'+p['id']+':'+str(int(now//60)))
        c.execute(update(gap_followups).where(gap_followups.c.id==p['id']).values(status=p['status'],payload=p))
    db.put(c,'observation_recovery:worker',dict(at=now,version=VERSION,active=sum(p['status']=='active' for p in rows)))
