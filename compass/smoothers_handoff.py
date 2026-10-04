"""Accept a complete weekly shadow cycle before arming a future cohort."""
import os
import re
from copy import deepcopy
from datetime import datetime,timedelta
from sqlalchemy import select,func
from .market import session,ts
from .native_config import KEY as CONFIG,RevisionConflict
from .native_outbox import outbox,delivery_guard
from .native_smoothers import VERSION,weekly
from .native_reports import smoothers
from .smoothers_messages import VERSION as MESSAGE_VERSION
from .store import identity

KEY='native:smoothers:handoff'
OWNER='native:ownership:smoothers'


def sessions(week):
    monday=datetime.strptime(week,'%Y-%m-%d').date()
    if monday.weekday()!=0:raise ValueError('Use the Monday date identifying the weekly cohort')
    result=[]
    for offset in range(5):
        day=(monday+timedelta(days=offset)).isoformat();hours=session(day)
        if hours:result.append(dict(date=day,open=hours[0],close=hours[1]))
    if not result:raise ValueError('Week has no equity sessions')
    return result


def snapshot(db,c):
    counts=dict(c.execute(select(outbox.c.status,func.count()).where(outbox.c.program=='smoothers').group_by(outbox.c.status)).all())
    return {'plan':db.get(c,KEY),'ownership':db.get(c,OWNER,{'owner':'original'}),
        'sender_environment_enabled':os.getenv('NATIVE_PROGRAM_SEND_ENABLED','false').lower()=='true',
        'webhook_configured':bool(re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',os.getenv('NATIVE_SMOOTHERS_DISCORD_WEBHOOK',''))),
        'webhook_secondary_configured':bool(re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',os.getenv('NATIVE_SMOOTHERS_DISCORD_WEBHOOK_SECONDARY',''))),
        'in_flight':counts.get('sending',0),'unknown_delivery':counts.get('ambiguous',0),
        'basis':'Prepare checks a complete native week. Activation also requires review of original channel messages: the original feed has no Discord receipts. This control does not pause or resume the original app. After rollback, reconcile in-flight/unknown deliveries before resuming it.'}


def review(db,c,week,now):
    calendar=sessions(week);r=smoothers(db,c,now,200,week)
    state=db.get(c,VERSION+':week:'+week,{})
    config=db.get(c,CONFIG,{})
    frozen=state.get('config') or {};enabled=[x for x in frozen.get('configs',[]) if x.get('enabled')]
    blockers=[]
    if now<calendar[-1]['close']+3600:blockers.append('Wait until the complete market week and post-close reconciliation finish')
    if state.get('sessions')!=calendar:blockers.append('Native calendar does not cover every actual session of the week')
    if state.get('state')!='complete' or state.get('errors') or not enabled or state.get('index')!=len(enabled):blockers.append('Every enabled configuration needs a completed weekly selection without processing errors')
    if not calendar[0]['open']+3900<=state.get('started',0)<=state.get('finished',0)<=calendar[0]['open']+7200:blockers.append('Weekly selection did not complete in its first-session entry window')
    if config.get('owner')!='compass' or frozen.get('revision')!=config.get('revision'):blockers.append('Current Compass configuration must match the reviewed frozen revision')
    if state.get('message_version')!=MESSAGE_VERSION:blockers.append('Review a week produced with the current official alert format')
    if any(r['truncated'].values()):blockers.append('Truncated evidence cannot establish acceptance')
    rows=r['rows']
    if not rows or state.get('signals')!=len(rows) or r['source_only']:blockers.append('Native and original weekly signal inventories must reconcile')
    if any(x['comparison']['status']!='matched_fields' or x['configuration_comparison']['status']!='matched_fields' or x['option_comparison']['status']!='matched_fields' for x in rows):blockers.append('Signal, quality/rank, configuration or contract differences remain')
    for x in rows:
        p=x['native'];source=x['source_observations'] or {}
        if (p.get('status') not in ('WIN','LOSS') or p.get('coverage_missing') or p.get('config_revision')!=frozen.get('revision')
            or p.get('message_version')!=MESSAGE_VERSION or p.get('model_entry_time')!=calendar[0]['open']+3600):
            blockers.append('Every signal needs complete native observations and the reviewed cohort provenance');break
        if p['status']=='WIN':
            bar=p.get('touch_bar') or {};hit=bar.get('high') if p.get('direction')=='CALL' else bar.get('low')
            stamp=p.get('resolution_time')
            try:source_stamp=ts(source.get('resolution_time'))
            except (ValueError,TypeError):source_stamp=None
            if (not stamp or hit is None or (hit<p['target_price'] if p.get('direction')=='CALL' else hit>p['target_price'])
                or source_stamp!=stamp or not p.get('exit_quote') or not p.get('touch_detected_at',0)>=stamp+60):
                blockers.append('Target minutes and completed touch-bar/quote observations need reconciliation');break
        elif p.get('last_seen_at')!=calendar[-1]['close']-60 or p.get('exit_underlying') is None:
            blockers.append('Weekly closures need the final completed regular-session minute');break
        if (p.get('contract') and p.get('entry_premium') and p.get('resolution_time',0)-p['model_entry_time']>=3600
                and (p.get('premium_attempt_at',0)<p['model_entry_time'] or not p.get('premium_last_attempt_quote'))):
            blockers.append('Longer-lived priced contracts need retained premium observation attempts');break
    if (not r['delivery'] or r['unexpected_intents'] or any(x['native_status']!='shadow' or not x['current_format_matches']
            or not x['delivery'].get('event_time') or not x['delivery'].get('cohort_time') for x in r['delivery'])):
        blockers.append('All expected entry, roster and terminal previews must match the current format and remain shadow')
    if c.execute(select(func.count()).select_from(weekly).where(weekly.c.status=='OPEN')).scalar_one():blockers.append('Resolve open native cohorts before a new weekly sender boundary')
    health=db.get(c,'health:project_smoothers',{})
    if health.get('status')!='connected' or health.get('checked_at',0)<calendar[-1]['close'] or not 0<=now-health.get('checked_at',0)<=120:blockers.append('Source feed needs a fresh post-week check')
    evidence=deepcopy(r)
    for x in evidence['rows']:x.pop('source_checked_at',None)
    evidence['config_revision']=config.get('revision')
    return r,blockers,identity(evidence)


def prepare(db,review_week,effective_week,now):
    reviewed=sessions(review_week);future=sessions(effective_week)
    if datetime.strptime(effective_week,'%Y-%m-%d')!=datetime.strptime(review_week,'%Y-%m-%d')+timedelta(days=7):raise ValueError('Handoff must start the immediately following weekly cohort')
    if now<reviewed[-1]['close']+3600 or now>=future[0]['open']:raise ValueError('Review after the full week closes and before the next week opens')
    with delivery_guard(db) as c:
        if db.get(c,OWNER,{}).get('owner')=='compass':raise RevisionConflict('Roll back the existing Smoothers handoff before preparing another')
        r,blockers,checksum=review(db,c,review_week,now)
        plan={'id':identity('smoothers-handoff-v1',now,review_week,effective_week,checksum),
            'review_week':review_week,'effective_week':effective_week,'effective_session':future[0]['date'],'effective_from':future[0]['open'],
            'prepared_at':now,'review_checksum':checksum,'review_summary':r['summary'],'blockers':blockers,
            'state':'blocked' if blockers else 'prepared','external_changes_performed':False,
            'original_delivery_evidence':'unrecorded; manual original-channel review required'}
        db.put(c,KEY,plan);db.append(c,'native_handoff','smoothers','',now,plan,plan['id'])
        return plan


def activate(db,plan_id,previous_sender_paused,operator_reviewed,original_alerts_reviewed,now):
    if previous_sender_paused is not True or operator_reviewed is not True or original_alerts_reviewed is not True:raise ValueError('Confirm original sender paused, weekly evidence/previews accepted, and original channel messages reviewed')
    hook=os.getenv('NATIVE_SMOOTHERS_DISCORD_WEBHOOK','')
    if os.getenv('NATIVE_PROGRAM_SEND_ENABLED','false').lower()!='true' or not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',hook):raise ValueError('Dedicated Smoothers sender environment is not enabled/configured')
    with delivery_guard(db) as c:
        if db.get(c,OWNER,{}).get('owner')=='compass':raise RevisionConflict('Roll back the existing Smoothers handoff before arming another')
        plan=db.locked_get(c,KEY,{})
        if plan.get('id')!=plan_id or plan.get('state')!='prepared':raise RevisionConflict('Prepare and review the current weekly handoff plan first')
        if now>=plan['effective_from'] or not 0<=now-plan['prepared_at']<=4*86400:raise ValueError('Plan expired or the next week has started')
        _,blockers,checksum=review(db,c,plan['review_week'],now)
        if blockers:raise RevisionConflict('Weekly evidence no longer qualifies: '+'; '.join(blockers))
        if checksum!=plan['review_checksum']:raise RevisionConflict('Weekly evidence changed; prepare and review again')
        if c.execute(select(func.count()).select_from(outbox).where(outbox.c.program.in_(('smoothers','smoothers_shared')),outbox.c.status.in_(['pending','sending','ambiguous']))).scalar_one():raise ValueError('Reconcile pending or uncertain Smoothers deliveries first')
        epoch=identity(plan_id,now)
        db.put(c,OWNER,{'owner':'compass','previous_sender_paused':True,'accepted_at':now,'epoch':epoch,
                       'effective_from':plan['effective_from'],'effective_week':plan['effective_week'],'plan_id':plan_id})
        plan.update(state='armed',accepted_at=now,epoch=epoch,original_alerts_reviewed=True)
        db.put(c,KEY,plan);db.append(c,'native_handoff','smoothers','',now,{'action':'armed','plan_id':plan_id,'epoch':epoch,'original_alerts_reviewed':True})
        return snapshot(db,c)


def rollback(db,now):
    with delivery_guard(db) as c:
        rows=c.execute(select(outbox).where(outbox.c.program.in_(('smoothers','smoothers_shared')),outbox.c.status.in_(['pending','sending'])).with_for_update()).mappings().all()
        previous=db.get(c,OWNER,{})
        db.put(c,OWNER,{'owner':'original','revoked_at':now,'previous_epoch':previous.get('epoch')})
        for r in rows:
            c.execute(outbox.update().where(outbox.c.id==r['id']).values(status='cancelled' if r['status']=='pending' else 'ambiguous',
                delivery=dict(r['delivery'],error='handoff_cancelled' if r['status']=='pending' else 'handoff_rollback_requires_delivery_reconciliation')))
        plan=db.get(c,KEY)
        if plan:db.put(c,KEY,dict(plan,state='rolled_back',rolled_back_at=now))
        db.append(c,'native_handoff','smoothers','',now,{'action':'rollback','previous_epoch':previous.get('epoch')})
        return snapshot(db,c)
