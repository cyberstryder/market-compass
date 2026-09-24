"""Pre-entry research eligibility from observations available at decision time."""
from collections import Counter
from .quote_path import recorded_path

VERSION='quote-continuity-v1'
SUSTAINED_VERSION='quote-continuity-v2'
LOOKBACK=300
MIN_SPAN=90
MIN_SAMPLES=15
MAX_HISTORY_GAP=15


def summary(rows,now):
    stamps=sorted({q['ts'] for q in rows})
    gaps=[b-a for a,b in zip(stamps,stamps[1:])]
    return dict(samples=len(stamps),span_seconds=stamps[-1]-stamps[0] if stamps else 0,
        latest_age_seconds=now-stamps[-1] if stamps else None,max_gap_seconds=max(gaps,default=0))


def short_ready(value):
    return (value['samples']>=5 and value['span_seconds']>=20
        and value['latest_age_seconds'] is not None and value['latest_age_seconds']<=5
        and value['max_gap_seconds']<=10)


def assess(db,c,symbol,now):
    rows,truncated,_=recorded_path(db,c,symbol,now-30,now)
    quality=summary(rows,now)
    return dict(version=VERSION,at=now,**quality,truncated=truncated,
        ready=not truncated and short_ready(quality))


def assess_sustained(db,c,symbol,now):
    # One bounded archive read; only original fresh-at-storage evidence is used.
    rows,truncated,_=recorded_path(db,c,symbol,now-LOOKBACK,now)
    recent=summary([q for q in rows if q['ts']>now-30],now)
    history=summary(rows,now)
    stamps=sorted({q['ts'] for q in rows})
    droughts=[b for a,b in zip(stamps,stamps[1:]) if b-a>MAX_HISTORY_GAP]
    history.update(lookback_seconds=LOOKBACK,minimum_span_seconds=MIN_SPAN,
        minimum_samples=MIN_SAMPLES,gap_limit_seconds=MAX_HISTORY_GAP,
        droughts=len(droughts),last_drought_at=droughts[-1] if droughts else None)
    reason=('archive_truncated' if truncated else 'recent_quote_drought' if droughts
        else 'insufficient_sustained_history' if history['samples']<MIN_SAMPLES or history['span_seconds']<MIN_SPAN
        else 'short_window_not_ready' if not short_ready(recent) else 'ready')
    return dict(version=SUSTAINED_VERSION,at=now,**recent,truncated=truncated,
        short_window_ready=not truncated and short_ready(recent),sustained=history,
        ready=reason=='ready',reason=reason)


def rank(quality):
    history=quality['sustained']
    return (history['max_gap_seconds'],-history['span_seconds'],-history['samples'],
        quality['max_gap_seconds'],-quality['samples'])


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
    policies=[]
    for policy in sorted({r['continuity_policy'] for r in rows if r.get('continuity_policy')}):
        selected=[r for r in rows if r.get('continuity_policy')==policy]
        policies.append(dict(policy=policy,candidates=len(selected),
            opened=sum(r.get('opened_at') is not None for r in selected),
            open=sum(r['status']=='open' for r in selected),
            closed=sum(r['status']=='closed' for r in selected),
            unresolved=sum(r['status']=='unresolved' for r in selected),
            excluded=sum(r['status']=='excluded' for r in selected)))
    qualified=[r for r in rows if r.get('continuity_policy')==SUSTAINED_VERSION]
    # Counts describe the last saved candidate checks, not independent failures.
    reasons=Counter(q.get('reason','unknown') for r in qualified if r['status'] in ('pending','excluded')
        for q in r.get('continuity_checks',{}).values() if not q.get('ready'))
    return dict(cohorts=out,policy=SUSTAINED_VERSION,continuity_cohorts=policies,
        continuity_rejections=dict(reasons),candidates=len(qualified),
        waiting=sum(r['status']=='pending' for r in qualified),
        excluded=sum(r['status']=='excluded' for r in qualified),
        continuity_excluded=sum(r['status']=='excluded' and r.get('exit_reason')=='Waiting for option quote continuity' for r in qualified))
