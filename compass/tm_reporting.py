"""Complete SQL denominators and stable pages for the TM receipt study."""
import base64
import json
import math
from sqlalchemy import select,func,case,true
from .store import tm_studies as studies,tm_checkpoints as checkpoints,flow_records
from .tm_scoring import VERSION,PROTOCOL,TM_THRESHOLD,COMPASS_THRESHOLD,dte_bucket

WINDOW_DAYS=30
FILTERS=('all','prospective','scored','insufficient_data','historical_inventory','tm_selected','compass_selected','both_selected')


def scope(at):
    return (studies.c.version==VERSION)&(studies.c.first_seen>=at-WINDOW_DAYS*86400)&\
        (studies.c.first_seen<=at)&(studies.c.assessed_at<=at)


def count(condition):return func.coalesce(func.sum(case((condition,1),else_=0)),0)


def measures(condition=true()):
    measured=condition&(checkpoints.c.status=='completed')&checkpoints.c.directional_return_pct.is_not(None)
    value=checkpoints.c.directional_return_pct
    return dict(records=count(condition),measured=count(measured),pending=count(condition&(checkpoints.c.status=='pending')),
        unavailable=count(condition&(checkpoints.c.status=='unavailable')),
        unsigned=count(condition&(checkpoints.c.status=='completed')&checkpoints.c.directional_return_pct.is_(None)),
        positive=count(measured&(value>0)),move_up_025=count(measured&(value>=.25)),
        move_down_025=count(measured&(value<=-.25)),
        sum_move=func.coalesce(func.sum(case((measured,value),else_=0)),0))


def finish(row):
    row=dict(row)
    n=row['measured']
    row['mean_directional_pct']=row.pop('sum_move')/n if n else None
    row['positive_fraction']=row['positive']/n if n else None
    row['coverage_fraction']=n/row['records'] if row['records'] else None
    return row


def report(db,c,at,cfg=None):
    source=flow_records.outerjoin(studies,(studies.c.day==flow_records.c.day)&
        (studies.c.vendor_id==flow_records.c.vendor_id)&(studies.c.version==VERSION)&(studies.c.assessed_at<=at))
    inv=c.execute(select(func.count().label('received_records'),count(studies.c.id.is_not(None)).label('registered_records'),
        count(studies.c.id.is_(None)).label('unregistered_records')).select_from(source)
        .where(flow_records.c.first_seen<=at)).mappings().one()
    counts=c.execute(select(func.count().label('registered'),
        count(studies.c.origin=='prospective').label('prospective'),
        count(studies.c.origin=='historical_inventory').label('historical_inventory'),
        count(studies.c.score_status=='scored').label('scored'),
        count(studies.c.score_status=='insufficient_data').label('insufficient_data'),
        count(studies.c.status=='pending').label('pending_records'),
        count(studies.c.status=='complete').label('complete_records'),
        count(studies.c.status=='partial').label('partial_records'),
        count(studies.c.status=='unavailable').label('unavailable_records')).where(scope(at))).mappings().one()
    joined=studies.join(checkpoints,studies.c.id==checkpoints.c.study_id)
    outcome_counts=[dict(r) for r in c.execute(select(checkpoints.c.horizon,studies.c.origin,
        checkpoints.c.status,func.count().label('count')).select_from(joined).where(scope(at))
        .group_by(checkpoints.c.horizon,studies.c.origin,checkpoints.c.status)).mappings()]
    eligible=(studies.c.origin=='prospective')&studies.c.tm_score.is_not(None)&studies.c.compass_score.is_not(None)
    tm=studies.c.tm_score>=TM_THRESHOLD
    compass=studies.c.compass_score>=COMPASS_THRESHOLD
    arms=dict(all_matched=true(),tm_only=tm,compass_only=compass,both=tm&compass,
        tm_rejected=~tm,compass_rejected=~compass,neither=(~tm)&(~compass))
    columns={arm+'__'+key:value for arm,condition in arms.items() for key,value in measures(condition).items()}
    comparisons=[]
    for row in c.execute(select(checkpoints.c.horizon,*(v.label(k) for k,v in columns.items()))
            .select_from(joined).where(scope(at),eligible).group_by(checkpoints.c.horizon)).mappings():
        for arm in arms:
            comparisons.append(dict(horizon=row['horizon'],arm=arm,**finish({k.split('__')[1]:v for k,v in row.items() if k.startswith(arm+'__')})))
    tm_band=case((studies.c.tm_score.is_(None),'unknown'),(studies.c.tm_score<70,'<70'),
        (studies.c.tm_score<85,'70–84'),else_='85+')
    compass_band=case((studies.c.compass_score.is_(None),'unknown'),(studies.c.compass_score<50,'<50'),
        (studies.c.compass_score<70,'50–69'),else_='70+')
    dimensions=dict(tm_score=tm_band,compass_score=compass_band,delay=studies.c.age_bucket,
        dte=studies.c.dte_bucket,direction=studies.c.direction,
        flow_type=studies.c.payload['first_snapshot']['classification'].as_string(),
        option_type=studies.c.payload['first_snapshot']['option_type'].as_string(),
        oi_bucket=case((studies.c.payload['first_snapshot']['open_interest'].as_float().is_(None),'unknown'),
            (studies.c.payload['first_snapshot']['open_interest'].as_float()<100,'thin (<100)'),else_='100+'),
        receipt_session=studies.c.payload['assessment']['receipt_session'].as_string(),
        receipt_day=studies.c.payload['receipt_day'].as_string())
    buckets=[]
    for name,dimension in dimensions.items():
        for row in c.execute(select(dimension.label('bucket'),checkpoints.c.horizon,
            *(v.label(k) for k,v in measures().items())).select_from(joined)
            .where(scope(at),studies.c.origin=='prospective').group_by(dimension,checkpoints.c.horizon)).mappings():
            buckets.append(dict(dimension=name,**finish(row)))
    overdue=c.execute(select(func.count()).select_from(joined).where(scope(at),
        checkpoints.c.status=='pending',checkpoints.c.due_at<=at)).scalar_one()
    flow=db.get(c,'matrix:unusual_activity',{})
    collection={}
    if cfg is not None:
        from .universe import data_symbols
        collected=data_symbols(db,c,cfg,at)
        requested=(studies.c.origin=='prospective')&((studies.c.first_seen>=at-86400)|(studies.c.status=='pending'))
        total=c.execute(select(func.count(func.distinct(studies.c.symbol))).where(requested)).scalar_one()
        covered=c.execute(select(func.count(func.distinct(studies.c.symbol))).where(requested,studies.c.symbol.in_(collected))).scalar_one()
        collection=dict(requested_symbols=total,in_collection=covered,not_in_collection=total-covered,
            total_collection_symbols=len(collected),ceiling=500,
            note='Current membership, not historical readiness. Original monitors have priority; excluded/invalid symbols and capacity gaps remain visible.')
    return dict(at=at,since=at-WINDOW_DAYS*86400,window_days=WINDOW_DAYS,version=VERSION,protocol=PROTOCOL,
        coverage='full_window',truncated=False,inventory=dict(inv),counts=dict(counts),
        outcome_counts=outcome_counts,comparisons=comparisons,buckets=buckets,overdue_checkpoints=overdue,
        activation=db.get(c,'tm-study-v1:activation',{}),
        collection=collection,source_coverage=dict(day=flow.get('day'),unique_rows=flow.get('unique_rows'),vendor_total=flow.get('total'),
            estimated_gap=flow.get('recovery',{}).get('estimated_gap'),
            rejected_observations=flow.get('recovery',{}).get('rejected_observations'),
            note='Rejected observations can repeat on polls. Valid received IDs are the study denominator; the feed is vendor-filtered.'),
        note='Complete 30-day receipt-window summaries; lifetime inventory reconciled separately. Partial scores and missing checkpoints remain explicit. Matched comparisons require both scores. Underlying midpoint moves are not option profits; overlapping records are correlated.')


def filtered(cohort):
    if cohort not in FILTERS:raise ValueError('Invalid TM study cohort')
    return {'all':true(),'prospective':studies.c.origin=='prospective',
        'historical_inventory':studies.c.origin=='historical_inventory',
        'scored':studies.c.score_status=='scored','insufficient_data':studies.c.score_status=='insufficient_data',
        'tm_selected':studies.c.tm_score>=TM_THRESHOLD,'compass_selected':studies.c.compass_score>=COMPASS_THRESHOLD,
        'both_selected':(studies.c.tm_score>=TM_THRESHOLD)&(studies.c.compass_score>=COMPASS_THRESHOLD)}[cohort]


def record_page(c,now,*,cohort='all',limit=100,asof=None,cursor=''):
    condition=filtered(cohort)
    if not 1<=limit<=100:raise ValueError('Invalid page size')
    anchor=None
    if cursor:
        try:
            version,at,prior,stamp,id=json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (version!=VERSION or prior!=cohort or not isinstance(id,str) or len(id)!=64
                    or not math.isfinite(stamp) or not math.isfinite(at) or not at-WINDOW_DAYS*86400<=stamp<=at
                    or (asof is not None and asof!=at)):raise ValueError()
            anchor=(stamp,id)
        except (ValueError,TypeError,UnicodeError,OverflowError) as error:raise ValueError('Invalid TM record cursor') from error
    else:at=now if asof is None else asof
    if not math.isfinite(at) or not 0<at<=now:raise ValueError('Invalid report time')
    condition &= scope(at)
    total=c.execute(select(func.count()).select_from(studies).where(condition)).scalar_one()
    q=select(studies).where(condition)
    if anchor:
        stamp,id=anchor
        q=q.where((studies.c.first_seen<stamp)|((studies.c.first_seen==stamp)&(studies.c.id<id)))
    rows=c.execute(q.order_by(studies.c.first_seen.desc(),studies.c.id.desc()).limit(limit+1)).mappings().all()
    more=len(rows)>limit;rows=rows[:limit]
    outcomes={}
    if rows:
        for r in c.execute(select(checkpoints).where(checkpoints.c.study_id.in_([p['id'] for p in rows]))).mappings():
            outcomes.setdefault(r['study_id'],[]).append(dict(r))
    records=[dict(r['payload'],status=r['status'],tm_score=r['tm_score'],compass_score=r['compass_score'],
        score_status=r['score_status'],age_bucket=r['age_bucket'],
        dte_bucket=dte_bucket((r['payload'].get('inventory_snapshot') or {}).get('expiry'),r['first_seen'])
            if r['origin']=='historical_inventory' else r['dte_bucket'],
        checkpoints=outcomes.get(r['id'],[])) for r in rows]
    next_cursor=base64.urlsafe_b64encode(json.dumps([VERSION,at,cohort,rows[-1]['first_seen'],rows[-1]['id']]).encode()).decode() if more else None
    return dict(records=records,total=total,asof=at,since=at-WINDOW_DAYS*86400,window_days=WINDOW_DAYS,
        cohort=cohort,limit=limit,has_more=more,next_cursor=next_cursor)


def detail(c,id):
    row=c.execute(select(studies,flow_records.c.payload.label('latest_snapshot'),flow_records.c.last_seen)
        .outerjoin(flow_records,(flow_records.c.day==studies.c.day)&(flow_records.c.vendor_id==studies.c.vendor_id))
        .where(studies.c.id==id)).mappings().first()
    if not row:return None
    original=row['payload'].get('first_snapshot')
    clean=lambda p:{k:v for k,v in (p or {}).items() if k!='received'}
    changed=clean(original)!=clean(row['latest_snapshot']) if original else None
    return dict(row['payload'],status=row['status'],latest_vendor_snapshot=row['latest_snapshot'],
        latest_vendor_seen=row['last_seen'],vendor_changed_since_assessment=changed,
        checkpoints=[dict(r) for r in c.execute(select(checkpoints).where(checkpoints.c.study_id==id)
            .order_by(checkpoints.c.target_at,checkpoints.c.horizon)).mappings()])
