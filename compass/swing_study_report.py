"""Uncapped SQL cohorts and stable individual Swing comparison pages."""
import base64
import json
import math
from sqlalchemy import select,func,case,true
from .store import swing_trials as trials,swing_checkpoints as checkpoints,events
from .swing_ideas import swings
from .swing_study import VERSION,KEY,PROTOCOL,GROUPS

WINDOW_DAYS=30
FILTERS=('all','prospective','historical_inventory','fresh_flow','delayed_flow',
    'fresh_vendor_blocked','no_confirming_flow','unavailable')


def scope(at):
    return (trials.c.version==VERSION)&(trials.c.first_seen>=at-WINDOW_DAYS*86400)&\
        (trials.c.first_seen<=at)&(trials.c.assessed_at<=at)


def count(condition):return func.coalesce(func.sum(case((condition,1),else_=0)),0)


def measures(condition=true()):
    completed=condition&(checkpoints.c.status=='completed')
    value=checkpoints.c.directional_return_pct
    return dict(records=count(condition),measured=count(completed),
        pending=count(condition&(checkpoints.c.status=='pending')),
        unavailable=count(condition&(checkpoints.c.status=='unavailable')),
        positive=count(completed&(value>0)),up_half=count(completed&(value>=.5)),
        down_half=count(completed&(value<=-.5)),
        sum_move=func.coalesce(func.sum(case((completed,value),else_=0)),0))


def finish(row):
    out=dict(row);n=out['measured']
    out['mean_directional_pct']=out.pop('sum_move')/n if n else None
    out['positive_fraction']=out['positive']/n if n else None
    return out


def report(db,c,at,cfg):
    source=events.outerjoin(trials,(trials.c.source_id==events.c.id)&(trials.c.version==VERSION)&(trials.c.assessed_at<=at))
    inv=c.execute(select(func.count().label('retained_candidates'),count(trials.c.id.is_not(None)).label('registered'),
        count(trials.c.id.is_(None)).label('unregistered')).select_from(source)
        .where(events.c.kind=='swing_candidate',events.c.received<=at)).mappings().one()
    prospective=trials.c.origin=='prospective'
    totals=c.execute(select(func.count().label('registered'),count(prospective).label('prospective'),
        count(trials.c.origin=='historical_inventory').label('historical_inventory'),
        count(prospective&(trials.c.flow_group!='unavailable')).label('classified'),
        count(prospective&(trials.c.flow_group=='unavailable')).label('flow_unavailable'),
        count(trials.c.status=='pending').label('pending_records'),
        count(trials.c.status=='complete').label('complete_records'),
        count(trials.c.status=='partial').label('partial_records'),
        count(trials.c.status=='unavailable').label('unavailable_records'),
        func.count(func.distinct(case((prospective,trials.c.payload['receipt_day'].as_string()),else_=None))).label('prospective_sessions'),
        func.count(func.distinct(case((prospective,trials.c.symbol),else_=None))).label('prospective_symbols')).where(scope(at))).mappings().one()
    joined=trials.join(checkpoints,trials.c.id==checkpoints.c.study_id)
    outcomes=[dict(r) for r in c.execute(select(trials.c.origin,checkpoints.c.horizon,checkpoints.c.status,
        checkpoints.c.reason,func.count().label('count')).select_from(joined).where(scope(at))
        .group_by(trials.c.origin,checkpoints.c.horizon,checkpoints.c.status,checkpoints.c.reason)).mappings()]
    g=trials.c.flow_group
    arms=dict(technical_only=true(),fresh_flow=g=='fresh_flow',delayed_flow=g=='delayed_flow',
        fresh_or_delayed=g.in_(('fresh_flow','delayed_flow')),
        any_support=g.in_(('fresh_flow','delayed_flow','fresh_vendor_blocked')),
        no_confirming_flow=g=='no_confirming_flow',rejected_by_fresh_flow=g!='fresh_flow')
    columns={arm+'__'+key:value for arm,condition in arms.items() for key,value in measures(condition).items()}
    comparisons=[]
    for r in c.execute(select(checkpoints.c.horizon,*(v.label(k) for k,v in columns.items())).select_from(joined)
            .where(scope(at),prospective,g!='unavailable').group_by(checkpoints.c.horizon)).mappings():
        for arm in arms:comparisons.append(dict(arm=arm,horizon=r['horizon'],
            **finish({k.split('__')[1]:v for k,v in r.items() if k.startswith(arm+'__')})))
    age=trials.c.payload['flow']['aligned_source_age'].as_float()
    dimensions=dict(flow_group=g,rule=trials.c.rule,side=trials.c.side,symbol=trials.c.symbol,
        flow_policy=trials.c.payload['flow']['dte_policy'].as_string(),
        receipt_day=trials.c.payload['receipt_day'].as_string(),
        source_age=case((age.is_(None),'no_aligned_print'),(age<=300,'0–5m'),(age<=900,'5–15m'),else_='15–30m'),
        price_tolerance=case((trials.c.payload['price_context']['within_entry_tolerance'].as_boolean().is_(True),'within 0.25 ATR'),
            (trials.c.payload['price_context']['within_entry_tolerance'].as_boolean().is_(False),'beyond 0.25 ATR'),else_='unavailable'))
    buckets=[]
    for name,dimension in dimensions.items():
        for r in c.execute(select(dimension.label('bucket'),checkpoints.c.horizon,
            *(v.label(k) for k,v in measures().items())).select_from(joined)
            .where(scope(at),prospective).group_by(dimension,checkpoints.c.horizon)).mappings():
            buckets.append(dict(dimension=name,**finish(r)))
    option_join=trials.join(swings,trials.c.payload['admission_id'].as_string()==swings.c.id)
    option_counts=[dict(r) for r in c.execute(select(trials.c.origin,swings.c.status,func.count().label('count'))
        .select_from(option_join).where(scope(at)).group_by(trials.c.origin,swings.c.status)).mappings()]
    from .universe import data_symbols
    collected=data_symbols(db,c,cfg,at)
    needed=(trials.c.origin=='prospective')&(trials.c.status=='pending')
    requested=c.execute(select(func.count(func.distinct(trials.c.symbol))).where(needed)).scalar_one()
    covered=c.execute(select(func.count(func.distinct(trials.c.symbol))).where(needed,trials.c.symbol.in_(collected))).scalar_one()
    return dict(at=at,since=at-WINDOW_DAYS*86400,window_days=WINDOW_DAYS,version=VERSION,protocol=PROTOCOL,
        inventory=dict(inv),counts=dict(totals),outcome_counts=outcomes,comparisons=comparisons,buckets=buckets,
        option_pipeline=option_counts,coverage='full_window',truncated=False,
        overdue_checkpoints=c.execute(select(func.count()).select_from(joined).where(scope(at),
            checkpoints.c.status=='pending',checkpoints.c.due_at<=at)).scalar_one(),
        activation=db.get(c,KEY+':activation',{}),
        collection=dict(pending_symbols=requested,in_collection=covered,outside_collection=requested-covered),
        note='All retained technical candidates in the full 30-day receipt window. Primary comparisons require known flow coverage; unknown-flow candidates retain their own outcomes. Midpoint moves are not option returns or stop/target paths. Historical inventory is separate; overlapping records are correlated.')


def filtered(cohort):
    if cohort not in FILTERS:raise ValueError('Invalid Swing study cohort')
    return true() if cohort=='all' else trials.c.origin==cohort if cohort in ('prospective','historical_inventory') else trials.c.flow_group==cohort


def record_page(c,now,*,cohort='all',limit=100,asof=None,cursor=''):
    condition=filtered(cohort);anchor=None
    if not 1<=limit<=100:raise ValueError('Invalid page size')
    if cursor:
        try:
            version,at,prior,stamp,id=json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (version!=VERSION or prior!=cohort or not isinstance(id,str) or len(id)!=64
                    or not math.isfinite(at) or not math.isfinite(stamp) or not at-WINDOW_DAYS*86400<=stamp<=at
                    or (asof is not None and asof!=at)):raise ValueError()
            anchor=(stamp,id)
        except (ValueError,TypeError,UnicodeError,OverflowError) as e:raise ValueError('Invalid Swing record cursor') from e
    else:at=now if asof is None else asof
    if not math.isfinite(at) or not 0<at<=now:raise ValueError('Invalid report time')
    condition &= scope(at)
    total=c.execute(select(func.count()).select_from(trials).where(condition)).scalar_one()
    q=select(trials,swings.c.status.label('option_status')).outerjoin(swings,trials.c.payload['admission_id'].as_string()==swings.c.id).where(condition)
    if anchor:
        stamp,id=anchor;q=q.where((trials.c.first_seen<stamp)|((trials.c.first_seen==stamp)&(trials.c.id<id)))
    rows=c.execute(q.order_by(trials.c.first_seen.desc(),trials.c.id.desc()).limit(limit+1)).mappings().all()
    more=len(rows)>limit;rows=rows[:limit];outcomes={}
    if rows:
        for r in c.execute(select(checkpoints).where(checkpoints.c.study_id.in_([p['id'] for p in rows]))).mappings():
            outcomes.setdefault(r['study_id'],[]).append(dict(r))
    records=[dict(r['payload'],symbol=r['symbol'],side=r['side'],rule=r['rule'],flow_group=r['flow_group'],
        status=r['status'],option_status=r['option_status'],checkpoints=outcomes.get(r['id'],[])) for r in rows]
    token=base64.urlsafe_b64encode(json.dumps([VERSION,at,cohort,rows[-1]['first_seen'],rows[-1]['id']]).encode()).decode() if more else None
    return dict(records=records,total=total,asof=at,since=at-WINDOW_DAYS*86400,window_days=WINDOW_DAYS,
        cohort=cohort,limit=limit,has_more=more,next_cursor=token)
