"""Immutable, optimistic revisions for Compass-owned Smoothers settings."""
import math
import re
from sqlalchemy import Table,Column,String,Float,JSON,select
from .store import meta,identity

KEY='native-smoothers-v1:config'
revisions=Table('native_smoothers_config_revisions_v1',meta,
    Column('id',String(64),primary_key=True),Column('created',Float,nullable=False),
    Column('parent',String(64)),Column('reason',String(300),nullable=False),Column('payload',JSON,nullable=False))

class RevisionConflict(ValueError):pass


def validate(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=500:raise ValueError('Provide 1–500 ticker configurations')
    result=[];seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Each configuration must be an object')
        ticker=row.get('ticker','')
        if not isinstance(ticker,str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}',ticker) or ticker in seen:raise ValueError('Invalid or duplicate ticker')
        seen.add(ticker)
        if any(type(row.get(k)) is not int or not 1<=row[k]<=500 for k in ('s1','s2','s3')):raise ValueError('Smoother lengths must be integers from 1 to 500')
        pm=row.get('pm');wr=row.get('backtest_wr')
        if type(pm) not in (int,float) or not math.isfinite(pm) or not 0<pm<1:raise ValueError('Target fraction must be between 0 and 1')
        if wr is not None and (type(wr) not in (int,float) or not math.isfinite(wr) or not 0<=wr<=1):raise ValueError('Backtest win rate must be a fraction or null')
        if type(row.get('enabled')) is not bool:raise ValueError('Enabled must be true or false')
        result.append({k:row.get(k) for k in ('ticker','s1','s2','s3','pm','enabled','backtest_wr')})
    return sorted(result,key=lambda x:x['ticker'])


def write_revision(db,c,previous,rows,now,reason,restored_from=None):
    parent=previous.get('revision');rid=identity('smoothers-config-v1',parent,rows,now,reason,restored_from)
    payload={**previous,'configs':rows,'revision':rid,'owner':'compass','received_at':now,
        'owned_since':previous.get('owned_since',now),'restored_from':restored_from}
    c.execute(revisions.insert().values(id=rid,created=now,parent=parent,reason=reason,payload=payload))
    db.put(c,KEY,payload)
    return payload


def bootstrap(db,now):
    with db.tx() as c:
        current=db.locked_get(c,KEY,{})
        if current.get('owner')=='compass':return current
        if not current.get('configs'):return None
        if now-current.get('received_at',0)>7200:raise ValueError('A fresh source snapshot is required for initial ownership')
        return write_revision(db,c,current,validate(current['configs']),now,'Initial Compass ownership; source settings preserved')


def revise(db,expected,rows,reason,now,restore=None):
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=300:raise ValueError('Provide a change reason, up to 300 characters')
    with db.tx() as c:
        current=db.locked_get(c,KEY,{})
        if current.get('owner')!='compass' or current.get('revision')!=expected:raise RevisionConflict('Configuration changed; reload before saving')
        if restore:
            previous=c.execute(select(revisions.c.payload).where(revisions.c.id==restore)).scalar_one_or_none()
            if previous is None:raise ValueError('Unknown revision')
            rows=previous['configs']
        return write_revision(db,c,current,validate(rows),now,reason.strip(),restore)


def effective(db,c,config):
    # Frozen source aggregate is a baseline, never re-imported after ownership.
    # Add only independent cohorts created after ownership, avoiding double count.
    from .native_smoothers import weekly
    stats={r['ticker']:{'ticker':r['ticker'],'wins':int(r.get('wins',0)),'losses':int(r.get('losses',0))} for r in config.get('alltime',[])}
    for p in c.execute(select(weekly.c.payload).where(weekly.c.status.in_(['WIN','LOSS']))).scalars():
        if p['created_at']<config.get('owned_since',float('inf')):continue
        r=stats.setdefault(p['ticker'],dict(ticker=p['ticker'],wins=0,losses=0))
        r['wins' if p['status']=='WIN' else 'losses']+=1
    return dict(config,alltime=list(stats.values()),statistics_basis='Frozen source baseline plus native resolved cohorts created after Compass ownership')


def snapshot(db,c):
    current=db.get(c,KEY,{})
    history=c.execute(select(revisions.c.id,revisions.c.parent,revisions.c.created,revisions.c.reason).order_by(revisions.c.created.desc()).limit(51)).mappings().all()
    return {'current':current,'history':[dict(r) for r in history[:50]],'history_truncated':len(history)>50,
        'basis':'Edits affect future weekly jobs; started cohorts retain their frozen revision. Rollback creates a new revision.'}
