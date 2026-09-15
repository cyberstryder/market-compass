"""Pre-entry research eligibility from observations available at decision time."""
from .quote_path import recorded_path

VERSION='quote-continuity-v1'


def assess(db,c,symbol,now):
    rows,truncated,_=recorded_path(db,c,symbol,now-30,now)
    stamps=sorted({q['ts'] for q in rows})
    gaps=[b-a for a,b in zip(stamps,stamps[1:])]
    span=stamps[-1]-stamps[0] if stamps else 0
    age=now-stamps[-1] if stamps else None
    maximum=max(gaps,default=0)
    ready=not truncated and len(stamps)>=5 and span>=20 and age is not None and age<=5 and maximum<=10
    return dict(version=VERSION,at=now,samples=len(stamps),span_seconds=span,
        latest_age_seconds=age,max_gap_seconds=maximum,truncated=truncated,ready=ready)


def cohorts(rows):
    versions=sorted({r.get('collection_version','unversioned') for r in rows})
    out=[]
    for version in versions:
        selected=[r for r in rows if r.get('collection_version','unversioned')==version]
        opened=[r for r in selected if r.get('opened_at') is not None]
        closed=sum(r['status']=='closed' for r in opened)
        unresolved=sum(r['status']=='unresolved' for r in opened)
        out.append(dict(version=version,opened=len(opened),closed=closed,unresolved=unresolved,
            open=sum(r['status']=='open' for r in opened),
            completion_pct=100*closed/len(opened) if opened else None,
            unresolved_pct=100*unresolved/len(opened) if opened else None))
    qualified=[r for r in rows if r.get('continuity_policy')==VERSION]
    return dict(cohorts=out,policy=VERSION,candidates=len(qualified),
        waiting=sum(r['status']=='pending' for r in qualified),
        excluded=sum(r['status']=='excluded' for r in qualified),
        continuity_excluded=sum(r['status']=='excluded' and r.get('exit_reason')=='Waiting for option quote continuity' for r in qualified))
