"""Operational acceptance and frozen correction boundary; no outcome rewriting."""
import asyncio
import json
import logging
import time
from collections import Counter
from sqlalchemy import select
from .option_ideas import ideas as option_ideas
from .setup_study import trials
from .secondary import reviews
from .market import day
from .swing_ideas import swings

KEY='forward-acceptance-v1'
LOG=logging.getLogger('uvicorn.error')


def counts(rows):
    return dict(total=len(rows),states=dict(Counter(r.get('status','unknown') for r in rows)),
        reasons=dict(Counter(r.get('exit_reason','none') for r in rows)))


def capture(db,cfg,now):
    with db.tx() as c:
        marker=db.locked_get(c,KEY+':activation',{'at':now,'option_model':'paired-recorded-quotes-v1'})
        since=marker['at']
        options=c.execute(select(option_ideas.c.payload).where(option_ideas.c.created>=now-86400)
            .order_by(option_ideas.c.created.desc()).limit(5001)).scalars().all()
        options_truncated=len(options)>5000
        options=options[:5000]
        new=[r for r in options if r['created_at']>=since]
        old=[r for r in options if r['created_at']<since]
        scans=list(db.prefix(c,'swing_scan:').values())
        technical=db.recent(c,'swing_candidate',limit=5001,since=since)
        status=dict(at=now,since=since,options_truncated=options_truncated,options_before=counts(old),options_after=counts(new),
            options_replay_models=dict(Counter(r.get('observation_model','legacy_latest') for r in new)),
            option_subscription=db.get(c,'options:subscriptions',{}),
            swing_scan=dict(Counter(r.get('status','unknown') for r in scans)),
            swing_daily_ready=sum(r.get('daily_ready',False) for r in scans),swing_symbols=len(scans),
            swing_technical_candidates=len(technical),swing_candidates_truncated=len(technical)>5000,
            swing_without_flow=sum(not r['payload'].get('flow_confirmed') for r in technical),
            futures_persistence=list(db.prefix(c,'futures_persistence:').values()),
            note='Before/after creation cohorts, not retroactive reclassification or evidence of improvement.')
        # Bounded failure details expose which stream went missing.
        status['option_failures']=[{k:r.get(k) for k in ('id','underlying','contract','created_at','finished_at','exit_reason','gap_detail','observation_model')}
            for r in options if r['status']=='unresolved'][:12]
        future_rows=c.execute(select(trials.c.payload).where(trials.c.status=='unresolved')
            .order_by(trials.c.started.desc()).limit(20)).scalars().all()
        status['futures_gaps']=[{k:r.get(k) for k in ('symbol','started','finished','gap_detail','observation_model')}
            for r in future_rows]
        recent=c.execute(select(reviews.c.verdict).where(reviews.c.decided_at>=since)).scalars().all()
        status['secondary_after']=dict(Counter(recent))
        db.put(c,KEY+':report',status)
    # Subscription symbols and failure IDs stay in the authenticated report.
    brief={k:v for k,v in status.items() if k not in ('option_subscription','option_failures','futures_gaps')}
    brief['option_subscription_count']=len(status['option_subscription'].get('symbols',[]))
    LOG.info('Forward acceptance: %s',json.dumps(brief,sort_keys=True))
    for row in status['option_failures']:LOG.info('Option failure audit: %s',json.dumps(row,sort_keys=True))
    return status


async def run(db,cfg):
    while True:
        try:await asyncio.to_thread(capture,db,cfg,time.time())
        except asyncio.CancelledError:raise
        except Exception as error:LOG.warning('Forward acceptance failed: %s',type(error).__name__)
        await asyncio.sleep(60)
