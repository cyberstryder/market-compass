"""Guarded Morning sender handoff; deployment never activates it automatically."""
import os
import re
from copy import deepcopy
from datetime import datetime
from sqlalchemy import select,func
from .store import identity
from .native_config import RevisionConflict
from .native_reports import morning
from .native_routing import routing_guard,KEY as ROUTE_KEY,day
from .native_outbox import outbox
from .market import session

KEY='native:morning:handoff'
OWNER='native:ownership:morning'


def snapshot(db,c):
    ownership=db.get(c,OWNER,{})
    return {'plan':db.get(c,KEY),'ownership':ownership,
        'sender_environment_enabled':os.getenv('NATIVE_PROGRAM_SEND_ENABLED','false').lower()=='true',
        'webhook_configured':bool(os.getenv('NATIVE_MORNING_DISCORD_WEBHOOK')),
        'in_flight':c.execute(select(func.count()).select_from(outbox).where(outbox.c.program=='morning',outbox.c.status=='sending')).scalar_one(),
        'unknown_delivery':c.execute(select(func.count()).select_from(outbox).where(outbox.c.program=='morning',outbox.c.status=='ambiguous')).scalar_one(),
        'basis':'This control does not pause or resume the original application. Resolve in-flight/unknown deliveries before resuming the original sender.'}


def review(db,c,review_session,now):
    r=morning(db,c,now,200,review_session,review_session)
    rows=r['signals']
    direct=[p for p in rows if any(x['origin']=='direct' and x.get('entry_packets',0)>0 for x in p['origins'])]
    blockers=[]
    if not rows or len(direct)!=len(rows):blockers.append('Every reviewed signal needs direct entry-intake provenance')
    if any(r['truncated'].values()):blockers.append('Review is truncated; acceptance cannot be inferred')
    if any(p['status'] in ('missing_native','awaiting_native') for p in r['source_only']):blockers.append('Original entry signals remain unmatched')
    if any(p['source_candles']['status']!='matched_source_inventory' or p['source_signal']['status']!='matched_fields' for p in rows):blockers.append('Native candle inventories or source signal fields do not reconcile')
    for p in rows:
        expected=60 if p['option'].get('contract') else 0
        samples=p['option_samples']
        same=p['source_option']['status']=='same_contract'
        both_unavailable=p['option'].get('status')=='unavailable' and p['source_option_status']=='unavailable' and not p['option'].get('contract') and not p['source_option'].get('source_contract')
        if (len(samples)!=expected or any(s['status']!='complete' or (s.get('quote') or {}).get('status') not in ('available','wide_spread','unavailable') for s in samples)
                or not (same or both_unavailable) or any(p['option_calculations']['sample_counts'].get(k,0) for k in ('different','missing_source_return'))):
            blockers.append('Option selection or scheduled observation accounting is incomplete')
            break
    if any(p['native_status']!='shadow' or p['source_status']!='delivered' for p in r['delivery']) or len(r['delivery'])!=2*len(rows):blockers.append('Expected entry/edit previews and original delivery evidence are incomplete')
    health=db.get(c,'health:project_morning',{})
    if health.get('status')!='connected' or health.get('checked_at',0)<session(review_session)[1] or now-health.get('checked_at',0)>120:blockers.append('Source feed needs a fresh post-session check')
    # Repeated imports refresh their timestamps without changing the evidence.
    evidence=deepcopy(r)
    for p in evidence['signals']:
        p.pop('source_checked_at',None)
        p['source_candles'].pop('snapshot_imported_at',None)
    return r,blockers,identity(evidence)


def prepare(db,review_session,effective_session,now):
    datetime.strptime(review_session,'%Y-%m-%d');datetime.strptime(effective_session,'%Y-%m-%d')
    reviewed=session(review_session);future=session(effective_session)
    if not reviewed or not future or now<reviewed[1]+3600 or effective_session<=day(now):raise ValueError('Review a completed session and choose a future market session')
    with routing_guard(db) as c:
        if db.get(c,OWNER,{}).get('owner')=='compass':raise RevisionConflict('Roll back the existing handoff before preparing another')
        r,blockers,checksum=review(db,c,review_session,now)
        plan={'id':identity('morning-handoff-v1',now,review_session,effective_session,r['summary']),
            'review_session':review_session,'effective_session':effective_session,'effective_from':future[0],
            'prepared_at':now,'review_checksum':checksum,'review_summary':r['summary'],'blockers':blockers,
            'state':'blocked' if blockers else 'prepared','external_changes_performed':False}
        db.put(c,KEY,plan)
        db.append(c,'native_handoff','morning','',now,plan,plan['id'])
        return plan


def activate(db,plan_id,previous_sender_paused,operator_reviewed,now):
    if previous_sender_paused is not True or operator_reviewed is not True:raise ValueError('Confirm the original sender is paused and the session/preview review is accepted')
    hook=os.getenv('NATIVE_MORNING_DISCORD_WEBHOOK','')
    if os.getenv('NATIVE_PROGRAM_SEND_ENABLED','false').lower()!='true' or not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',hook):raise ValueError('Dedicated sender environment is not enabled/configured')
    with routing_guard(db) as c:
        if db.get(c,OWNER,{}).get('owner')=='compass':raise RevisionConflict('Roll back the existing handoff before arming another')
        plan=db.locked_get(c,KEY,{})
        if plan.get('id')!=plan_id or plan.get('state')!='prepared':raise RevisionConflict('Prepare and review the current handoff plan first')
        if now>=plan['effective_from'] or now-plan['prepared_at']>86400:raise ValueError('Plan expired or session started; prepare a new clean-session handoff')
        # Do not arm across intervening active market sessions after the reviewed session.
        from datetime import timedelta
        d=datetime.strptime(plan['review_session'],'%Y-%m-%d').date()+timedelta(days=1)
        effective=datetime.strptime(plan['effective_session'],'%Y-%m-%d').date()
        while d<effective:
            if session(d.isoformat()):raise ValueError('Handoff must follow the reviewed market session')
            d+=timedelta(days=1)
        _,blockers,checksum=review(db,c,plan['review_session'],now)
        if blockers:raise RevisionConflict('Session evidence no longer qualifies: '+'; '.join(blockers))
        if checksum!=plan.get('review_checksum'):raise RevisionConflict('Session evidence changed; prepare and review a new handoff plan')
        unsettled=c.execute(select(func.count()).select_from(outbox).where(outbox.c.program=='morning',outbox.c.status.in_(['pending','sending','ambiguous']))).scalar_one()
        if unsettled:raise ValueError('Resolve pending or uncertain native deliveries before handoff')
        epoch=identity(plan_id,now)
        db.put(c,OWNER,{'owner':'compass','previous_sender_paused':True,'accepted_at':now,'epoch':epoch,'effective_from':plan['effective_from'],'plan_id':plan_id})
        route=db.locked_get(c,ROUTE_KEY,{'revision':None,'schedule':[],'prepared':None})
        plan['routing_before']=deepcopy(route)
        # Supersede future rehearsal plans so none can disable the official path.
        route['schedule']=[p for p in route['schedule'] if p['effective_session']<plan['effective_session']]+[{'mode':'direct_shadow','effective_session':plan['effective_session'],'reason':'Accepted official sender handoff'}]
        route['prepared']=None
        route['revision']=identity(route,epoch);db.put(c,ROUTE_KEY,route)
        plan.update(state='armed',accepted_at=now,epoch=epoch);db.put(c,KEY,plan)
        db.append(c,'native_handoff','morning','',now,{'action':'armed','plan_id':plan_id,'epoch':epoch})
        return snapshot(db,c)


def rollback(db,now):
    with routing_guard(db) as c:
        # Serialize with sender claim/finalization. A network request already in
        # flight cannot be unsent; it remains explicitly unresolved until checked.
        rows=c.execute(select(outbox).where(outbox.c.program=='morning',outbox.c.status.in_(['pending','sending'])).with_for_update()).mappings().all()
        previous=db.get(c,OWNER,{})
        db.put(c,OWNER,{'owner':'original','revoked_at':now,'previous_epoch':previous.get('epoch')})
        for r in rows:
            c.execute(outbox.update().where(outbox.c.id==r['id']).values(status='cancelled' if r['status']=='pending' else 'ambiguous',delivery=dict(r['delivery'],error='handoff_rollback_requires_delivery_reconciliation' if r['status']=='sending' else 'handoff_cancelled')))
        plan=db.get(c,KEY)
        if previous.get('owner')=='compass' and plan and plan.get('state')=='armed' and 'routing_before' in plan:
            route=deepcopy(plan['routing_before'])
            route['revision']=identity('handoff-route-rollback',route,previous.get('epoch'),now)
            db.put(c,ROUTE_KEY,route)
        if plan:db.put(c,KEY,dict(plan,state='rolled_back',rolled_back_at=now))
        db.append(c,'native_handoff','morning','',now,{'action':'rollback','previous_epoch':previous.get('epoch')})
        return snapshot(db,c)
