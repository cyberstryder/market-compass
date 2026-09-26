"""Operational acceptance evidence without inspecting protected return cohorts."""
import os
from collections import Counter
from datetime import datetime, timedelta
from sqlalchemy import select, func
from .store import state, events, discord_jobs
from .market import CT
from .weekday_candidates import PREFIX
from .setup_study import trials
from .smoothers_handoff import review, snapshot as sender_snapshot

KEY = 'strategy-readiness-v1'


def report(db,c,now):
    monday=(datetime.fromtimestamp(now,CT).date()-timedelta(days=datetime.fromtimestamp(now,CT).weekday())).isoformat()
    candidates=c.execute(select(func.count()).select_from(state).where(state.c.key.like(PREFIX+'%'))).scalar_one()
    counts=c.execute(select(trials.c.symbol,trials.c.strategy,trials.c.version,trials.c.status,func.count())
        .where(trials.c.started>=now-30*86400,trials.c.payload['asset'].as_string()=='future',
               ~func.lower(trials.c.strategy).like('%orb%'))
        .group_by(trials.c.symbol,trials.c.strategy,trials.c.version,trials.c.status)).all()
    futures=[dict(symbol=s,rule=r,version=v,status=st,count=n) for s,r,v,st,n in counts]
    weekly,blockers,_=review(db,c,monday,now)
    differences=Counter(k for r in weekly['rows'] for k in r['comparison'].get('differences',{}))
    comparison=Counter(r['comparison']['status'] for r in weekly['rows'])
    source_missing=Counter(k for r in weekly['rows'] for k in r['comparison'].get('missing_source_fields',[]))
    latest=db.get(c,'spy-brief:latest',{})
    latest_day=latest.get('day')
    receipts=[]
    if latest_day:
        for e,status,confirmation in c.execute(select(events.c.payload,discord_jobs.c.status,discord_jobs.c.confirmation)
                .select_from(events.outerjoin(discord_jobs,discord_jobs.c.event_id==events.c.id))
                .where(events.c.kind=='alert',events.c.payload['day'].as_string()==latest_day,
                       events.c.payload['status'].as_string().in_(('spy_morning_brief','spy_chart_prompt')))):
            receipts.append(dict(kind=e.get('status'),phase=e.get('phase'),status=status or 'unregistered',
                                 receipt={k:v for k,v in (confirmation or {}).items() if k in ('message_id','at','reason')}))
    return dict(version=KEY,at=now,
        weekday=dict(candidates=candidates,alerts_enabled=False,
            status='awaiting_forward_cohort' if not candidates else 'research_review_pending',
            reason='New board deployed after September 25 close. No backfilled entries or automatic strategy promotion.'),
        smoothers=dict(week=monday,job=weekly['job'].get('state'),signals=len(weekly['rows']),
            comparison=dict(comparison),field_differences=dict(differences),missing_source_fields=dict(source_missing),
            configuration=dict(Counter(r['configuration_comparison']['status'] for r in weekly['rows'])),
            contracts=dict(Counter(r['option_comparison']['status'] for r in weekly['rows'])),
            blockers=list(dict.fromkeys(blockers)),truncated=weekly['truncated'],
            previews=len(weekly['delivery']),preview_mismatches=sum(not x['current_format_matches'] for x in weekly['delivery']),
            sender=sender_snapshot(db,c),shared_configured=bool(os.getenv('NATIVE_SMOOTHERS_SHARED_DISCORD_WEBHOOK')),
            original_delivery='unrecorded; original channel review required'),
        futures=dict(rows=futures,counts_complete=True,returns_reviewed=False,
            reason='Non-ORB methods only; versions separate. Frozen ten-session evaluation is not yet complete.'),
        spy=dict(day=latest_day,version=latest.get('version'),decision=latest.get('decision'),
            data_status=latest.get('data_status'),data_blocks=latest.get('data_blocks'),
            atr14_daily=latest.get('context',{}).get('atr14_daily'),receipts=receipts,
            live_entry_acceptance='Pending a naturally qualifying confirmation and complete option path'))
