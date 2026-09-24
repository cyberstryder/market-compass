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

# Conservative boundary after Railway marked PR47 collector healthy (15:42:33.300Z).
FEED_FIX_AT=1789486954.0

KEY='forward-acceptance-v1'
LOG=logging.getLogger('uvicorn.error')


def counts(rows):
    return dict(total=len(rows),states=dict(Counter(r.get('status','unknown') for r in rows)),
        reasons=dict(Counter(r.get('exit_reason','none') for r in rows)))


def option_validation(rows, model='paired-recorded-quotes-v1'):
    paired=[r for r in rows if r.get('observation_model')==model]
    closed=sum(r['status']=='closed' for r in paired)
    unresolved=sum(r['status']=='unresolved' for r in paired)
    return dict(status='gaps_detected' if unresolved else 'completed_cycle_observed' if closed else 'awaiting_completed_cycle',
        opened=len(paired),closed=closed,unresolved=unresolved,
        note='Observation-path validation only; excluded candidates do not validate replay or profitability.')


def feed_validation(rows):
    return {'since':FEED_FIX_AT,'basis':'opened_at after PR47 collector healthy; preexisting observations excluded',
        **option_validation([r for r in rows if r.get('opened_at',0)>=FEED_FIX_AT],'paired-recorded-quotes-v2')}


def stream_summary(health):
    compact={k:v for k,v in health.items() if k!='symbols'}
    for field in ('source_age_at_read','silence_seconds','socket_to_commit_seconds','source_to_commit_seconds'):
        values=[r[field] for r in health.get('symbols',{}).values() if r.get(field) is not None]
        compact['max_'+field]=max(values) if values else None
    return compact


def failure_evidence(rows):
    result=Counter()
    for row in rows:
        if row.get('status')!='unresolved': continue
        archive=row.get('archive_check') or {}
        for stream in ('option','underlying'):
            check=archive.get(stream) or {}
            if not check:
                result[stream+': no archive diagnostic']+=1
            elif check.get('stale_or_invalid_when_recorded',0):
                result[stream+': stale or invalid archived samples']+=1
            elif not check.get('usable_rows',0):
                result[stream+': no usable archived samples']+=1
            else:
                result[stream+': usable samples present; inspect gap timing']+=1
    return dict(result)


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
            options_replay_models=dict(Counter(r.get('observation_model','not_opened') for r in new)),
            options_observation_validation=option_validation(new),
            options_v2_validation=option_validation(new,'paired-recorded-quotes-v2'),
            feed_fix_validation=feed_validation(options),
            options_reliability_validation=option_validation([r for r in options if r.get('collection_version')=='option-reliability-v1'],'paired-recorded-quotes-v2'),
            options_reliability_v2_validation=option_validation([r for r in options if r.get('collection_version')=='option-reliability-v2'],'paired-recorded-quotes-v2'),
            option_subscription=db.get(c,'options:subscriptions',{}),
            swing_scan=dict(Counter(r.get('status','unknown') for r in scans)),
            swing_daily_ready=sum(r.get('daily_ready',False) for r in scans),swing_symbols=len(scans),
            swing_history=dict(Counter(r.get('daily_history',{}).get('status','not_diagnosed') for r in scans)),
            swing_history_blocked=[dict(symbol=r['symbol'],reason=r.get('reason'),
                status=r.get('daily_history',{}).get('status'),
                missing_sessions=len(r.get('daily_history',{}).get('missing_sessions',[])))
                for r in scans if not r.get('daily_ready')][:144],
            swing_technical_candidates=len(technical),swing_candidates_truncated=len(technical)>5000,
            swing_without_flow=sum(not r['payload'].get('flow_confirmed') for r in technical),
            futures_persistence=list(db.prefix(c,'futures_persistence:').values()),
            note='Before/after creation cohorts, not retroactive reclassification or evidence of improvement.')
        from .option_continuity import cohorts
        status['reliability']=dict(at=now,since=now-86400,truncated=options_truncated,**cohorts(options),
            option_stream=stream_summary(db.get(c,'health:option_stream',{})),
            stock_stream=stream_summary(db.get(c,'health:alpaca_stocks',{})),
            failure_evidence=failure_evidence(options),
            recovery=db.get(c,'health:option_recovery',{}))
        from .option_selection_audit import snapshot as selection_audit
        status['zero_dte']=selection_audit(db,c,cfg,now)
        from .session_trace import tgt_trace, futures_session_audit
        status['tgt_quote_trace']=tgt_trace(db,c,now)
        status['futures_session_audit']=futures_session_audit(db,c,now)
        status['futures_full_session_audit']=futures_session_audit(db,c,now,scope='full')
        # Bounded failure details expose which stream went missing.
        status['option_failures']=[{k:r.get(k) for k in ('id','underlying','contract','created_at','finished_at','exit_reason','gap_detail','archive_check','observation_model','collection_version','underlying_collection_version')}
            for r in options if r['status']=='unresolved'][:12]
        future_rows=c.execute(select(trials.c.payload).where(trials.c.status=='unresolved')
            .order_by(trials.c.started.desc()).limit(20)).scalars().all()
        status['futures_gaps']=[{k:r.get(k) for k in ('symbol','started','finished','gap_detail','archive_check','observation_model')}
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
