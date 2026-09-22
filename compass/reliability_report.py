"""Complete version cohorts and current collection diagnostics; no causal claims."""
from sqlalchemy import select, func, case
from .store import gap_followups, spy_options
from .quote_collection import VERSION


def report(db,c,since,until):
    from .setup_study import trials
    from .option_ideas import ideas
    from .swing_ideas import swings
    from .spy_timeframes import arms
    cohorts=[]
    for name,t,stamp in [('setups',trials,trials.c.started),('options',ideas,ideas.c.created),
            ('swing',swings,swings.c.created),('SPY',spy_options,spy_options.c.created),('SPY timing',arms,arms.c.created)]:
        p=t.c.payload;version=func.coalesce(p['collection_version'].as_string(),'unversioned')
        gap=p['exit_reason'].as_string().like('%observation_gap%') | p['exit_reason'].as_string().like('%quote_missing%')
        count=lambda expr:func.sum(case((expr,1),else_=0))
        q=select(version.label('version'),func.count().label('records'),
            count(t.c.status=='closed').label('completed'),count(t.c.status=='open').label('open'),
            count(t.c.status=='unresolved').label('unresolved'),count(gap).label('gaps'),
            count((t.c.status=='open') & p['gap_pending']['first_detected_at'].as_float().is_not(None)).label('pending_gaps'),
            count(p['entry'].as_float().is_not(None)).label('opened'),
            func.sum(func.coalesce(p['recovered_pending_gaps'].as_integer(),0)).label('recovered_processing_gaps'))
        for row in c.execute(q.where(stamp>=since,stamp<until).group_by(version)).mappings():
            r={k:v or 0 for k,v in row.items()};r.update(study=name)
            r['gap_pct']=round(100*r['gaps']/r['opened'],2) if r['opened'] else None
            r['completion_pct']=round(100*r['completed']/r['opened'],2) if r['opened'] else None
            cohorts.append(r)
    follow=list(c.execute(select(gap_followups.c.status,func.count(),
        func.sum(gap_followups.c.payload['samples'].as_integer())).where(gap_followups.c.created>=since,
        gap_followups.c.created<until).group_by(gap_followups.c.status)).all())
    latency=[]
    for key in ('alpaca_stocks','option_stream'):
        h=db.get(c,'health:'+key,{})
        rows=list(h.get('symbols',{}).values())
        samples=[r['socket_to_commit_seconds'] for r in rows if r.get('socket_to_commit_seconds') is not None]
        latency.append(dict(feed=key,health_at=h.get('at'),measured_symbols=len(samples),
            max_latest_socket_to_commit_seconds=max(samples,default=None),quote_queue=h.get('quote_queue')))
    subscriptions=db.get(c,'options:subscriptions',{})
    stream=db.get(c,'health:option_stream',{})
    ack_current=0<=until-stream.get('at',0)<=35
    subscriptions=dict(subscriptions,acknowledgements_current=ack_current,
        active_unacknowledged=[s for s in subscriptions.get('active_symbols',[])
            if not stream.get('symbols',{}).get(s,{}).get('subscription',{}).get('Q',{}).get('acknowledged_at')] if ack_current else None)
    latest=list(c.execute(select(gap_followups.c.payload).where(gap_followups.c.created>=since,
        gap_followups.c.created<until).order_by(gap_followups.c.created.desc()).limit(25)).scalars())
    return dict(latest_followups=latest,latest_followups_limit=25,version=VERSION,since=since,through=until,cohorts=cohorts,
        followups=[dict(status=s,records=n,samples=m or 0) for s,n,m in follow],
        subscriptions=subscriptions,latency=latency,
        worker=db.get(c,'observation_recovery:worker'),
        basis='Complete creation cohorts grouped by collection version; open observations are immature. Differences are descriptive, not proof of improvement. Latency is the latest stored sample per symbol, not a historical percentile. Post-gap samples never repair original outcomes.')
