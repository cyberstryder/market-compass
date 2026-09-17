"""Full-window SPY report accounting and stable paper-path pages."""
import base64
import json
import math
from datetime import date,timedelta
from sqlalchemy import select,func,case,true
from .store import state,events,discord_jobs,spy_checks as checks,spy_options as observations,spy_marks as marks
from .spy_study import VERSION,KEY,PROTOCOL
from .market import day

COHORTS=('all','prospective','historical_inventory','schedule_gap','confirmed','waiting','no_entry')


def first_day(at):return (date.fromisoformat(day(at))-timedelta(days=29)).isoformat()
def scope(at):return (checks.c.version==VERSION)&(checks.c.day>=first_day(at))&(checks.c.day<=day(at))&(checks.c.created<=at)
def count(condition):return func.coalesce(func.sum(case((condition,1),else_=0)),0)


def report(db,c,at):
    original=(state.c.key.like('spy-brief:%'))&(state.c.key!='spy-brief:latest')&(state.c.updated<=at)
    link=state.outerjoin(checks,(checks.c.source_key==state.c.key)&(checks.c.version==VERSION)&(checks.c.created<=at))
    inv=dict(c.execute(select(func.count().label('saved_reports'),count(checks.c.id.is_not(None)).label('registered'),
        count(checks.c.id.is_(None)).label('unregistered')).select_from(link).where(original)).mappings().one())
    source=(checks.c.kind=='report');new=checks.c.origin=='prospective'
    totals=dict(c.execute(select(func.count().label('records'),count(source).label('reports'),
        count(source&new).label('prospective_reports'),count(checks.c.origin=='historical_inventory').label('historical_reports'),
        count(new&source&(checks.c.decision=='confirmed')).label('confirmations'),
        count(new&source&(checks.c.phase!='preopen')&(checks.c.decision=='waiting')).label('waits'),
        count(new&source&(checks.c.decision=='no_entry')).label('no_entry'),
        count(checks.c.kind=='schedule_gap').label('schedule_slots_without_report')).where(scope(at))).mappings().one())
    decisions=[dict(r) for r in c.execute(select(checks.c.origin,checks.c.kind,checks.c.phase,checks.c.decision,
        func.count().label('count')).where(scope(at)).group_by(checks.c.origin,checks.c.kind,checks.c.phase,checks.c.decision)).mappings()]
    joined=checks.join(observations,observations.c.id==checks.c.id)
    statuses=[dict(r) for r in c.execute(select(observations.c.status,func.count().label('count')).select_from(joined)
        .where(scope(at)).group_by(observations.c.status)).mappings()]
    closed=observations.c.status=='closed';p=observations.c.payload
    dimensions=dict(confirmation=checks.c.phase,side=p['underlying_side'].as_string(),
        exit_reason=p['exit_reason'].as_string(),collection=p['collection_version'].as_string())
    groups=[]
    for name,dimension in dimensions.items():
        rows=c.execute(select(dimension.label('bucket'),func.count().label('candidates'),count(closed).label('completed'),
            count(observations.c.status=='unresolved').label('unresolved'),count(observations.c.status=='excluded').label('excluded'),
            count(observations.c.status=='pending').label('pending'),count(observations.c.status=='open').label('open'),
            count(closed&(p['pnl'].as_float()>0)).label('wins'),
            func.sum(case((closed,p['pnl'].as_float()),else_=None)).label('net_pnl'),
            func.avg(case((closed,p['return_pct'].as_float()),else_=None)).label('mean_net_return_pct'),
            func.avg(case((closed,p['best_pct'].as_float()),else_=None)).label('mean_best_pct'),
            func.avg(case((closed,p['worst_pct'].as_float()),else_=None)).label('mean_worst_pct'))
            .select_from(joined).where(scope(at)).group_by(dimension)).mappings()
        groups.extend(dict(dimension=name,**r) for r in rows)
    active=c.execute(select(observations.c.payload).where(observations.c.status.in_(('pending','open')))).scalars().all()
    subscriptions=db.get(c,'options:subscriptions',{})
    available=set(subscriptions.get('symbols',[])) if 0<=at-subscriptions.get('at',0)<=15 else set()
    requested={p['contract']['symbol'] for p in active if p.get('contract')}
    return dict(at=at,version=VERSION,protocol=PROTOCOL,since_day=first_day(at),through_day=day(at),
        window_days=30,coverage='full_window',truncated=False,inventory=inv,counts=totals,decisions=decisions,
        statuses=statuses,groups=groups,activation=db.get(c,KEY+':activation',{}),
        collection=dict(requested=len(requested),in_subscription=len(requested&available),
            outside_subscription=len(requested-available),subscription_at=subscriptions.get('at')),
        note='All saved reports and audited schedule gaps in the 30-day session window. Historical reports have no reconstructed trades. Stock moves are separate from net option returns. Completed outcomes exclude missing paths; one-contract paper observations are not account performance.')


def token(values):return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def parse(cursor,kind,now,asof,cohort):
    anchor=None
    if cursor:
        try:
            version,tag,at,prior,stamp,id=json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (version!=VERSION or tag!=kind or prior!=cohort or not isinstance(id,str) or len(id)!=64
                    or not math.isfinite(at) or not math.isfinite(stamp) or not 0<stamp<=at
                    or (asof is not None and asof!=at)):raise ValueError()
            anchor=(stamp,id)
        except (ValueError,TypeError,UnicodeError,OverflowError) as e:raise ValueError('Invalid SPY study cursor') from e
    else:at=now if asof is None else asof
    if not math.isfinite(at) or not 0<at<=now:raise ValueError('Invalid report time')
    return at,anchor


def record_page(c,now,*,cohort='all',limit=100,asof=None,cursor=''):
    if cohort not in COHORTS or not 1<=limit<=100:raise ValueError('Invalid SPY study cohort or page size')
    at,anchor=parse(cursor,'records',now,asof,cohort)
    condition=(true() if cohort=='all' else checks.c.origin==cohort if cohort in ('prospective','historical_inventory')
        else checks.c.kind==cohort if cohort=='schedule_gap' else checks.c.decision==cohort if cohort in ('confirmed','waiting','no_entry')
        else observations.c.status==cohort)&scope(at)
    joined=checks.outerjoin(observations,observations.c.id==checks.c.id)
    total=c.execute(select(func.count()).select_from(joined).where(condition)).scalar_one()
    # Page the small research ledger first. Joining the entire quote/event
    # archive before LIMIT lets the planner scan millions of unrelated events.
    q=select(checks,observations.c.payload.label('option')).select_from(joined).where(condition)
    if anchor:
        stamp,id=anchor;q=q.where((checks.c.created<stamp)|((checks.c.created==stamp)&(checks.c.id<id)))
    rows=c.execute(q.order_by(checks.c.created.desc(),checks.c.id.desc()).limit(limit+1)).mappings().all()
    more=len(rows)>limit;rows=rows[:limit]
    keys=[r['source_key'] for r in rows if r['source_key']]
    deliveries={}
    if keys:
        event_rows=c.execute(select(events.c.id,events.c.key)
            .where(events.c.key.in_(keys),events.c.kind=='alert')).all()
        by_id={r.id:r.key for r in event_rows}
        if by_id:
            jobs=c.execute(select(discord_jobs.c.event_id,discord_jobs.c.status,discord_jobs.c.confirmation)
                .where(discord_jobs.c.event_id.in_(by_id))).all()
            deliveries={by_id[r.event_id]:(r.status,r.confirmation) for r in jobs}
    records=[dict(r['payload'],record_kind=r['kind'],decision_state=r['decision'],option=r['option'],
        delivery_status=deliveries.get(r['source_key'],(None,None))[0],
        delivery=deliveries.get(r['source_key'],(None,None))[1]) for r in rows]
    return dict(records=records,total=total,asof=at,cohort=cohort,limit=limit,has_more=more,
        next_cursor=token([VERSION,'records',at,cohort,rows[-1]['created'],rows[-1]['id']]) if more else None,
        since_day=first_day(at),through_day=day(at))


def mark_page(c,now,id,*,limit=100,asof=None,cursor=''):
    if len(id)!=64 or any(ch not in '0123456789abcdef' for ch in id):raise ValueError('Invalid SPY observation ID')
    if not 1<=limit<=100:raise ValueError('Invalid SPY mark page size')
    at,anchor=parse(cursor,'marks',now,asof,id)
    if not c.execute(select(observations.c.id).where(observations.c.id==id)).first():raise LookupError('SPY observation not found')
    condition=(marks.c.study_id==id)&(marks.c.processed_at<=at)&(marks.c.at<=at)
    total=c.execute(select(func.count()).select_from(marks).where(condition)).scalar_one()
    q=select(marks).where(condition)
    if anchor:
        stamp,key=anchor;q=q.where((marks.c.at>stamp)|((marks.c.at==stamp)&(marks.c.id>key)))
    rows=c.execute(q.order_by(marks.c.at,marks.c.id).limit(limit+1)).mappings().all()
    more=len(rows)>limit;rows=rows[:limit]
    return dict(records=[dict(r) for r in rows],total=total,asof=at,has_more=more,
        next_cursor=token([VERSION,'marks',at,id,rows[-1]['at'],rows[-1]['id']]) if more else None)
