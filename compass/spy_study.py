from . import observation_recovery as recovery
"""Prospective paper outcomes linked to the exact scheduled SPY report."""
import asyncio
import logging
import time
import uuid
from datetime import date,timedelta
from sqlalchemy import select,update
from .store import spy_checks as checks,spy_options as observations,spy_marks as marks,state,identity,events,discord_jobs
from .market import day,session,number
from .spy_confirmation import CHECKS,CONFIRMED,report_key
from .option_ideas import OptionIdeas,current,liquid,OPTION,FEE,SLIPPAGE,MAX_GAP

VERSION='spy-plan-0dte-v1'
KEY=VERSION
PHASES=('preopen',*(p for _,p in CHECKS))
PROTOCOL=dict(version=VERSION,quantity=1,expiry='SPY same-day standard 100-share contract only',
    entry='First eligible recorded quote after the plan has a confirmed Discord receipt, before the original 125-second confirmation deadline.',
    contract='Freeze the report watch contract when available; otherwise nearest observed whole-dollar strike to frozen report spot, ties lower. No switching after selection.',
    liquidity='Quote age <=5s; bid >=$0.10; positive sizes; uncrossed; spread <=10% of midpoint and $0.30.',
    continuity='Before entry: >=5 samples across >=20s of the last 30s, latest <=5s, gaps <=10s. After entry: each feed gap <=15s.',
    prices='Buy at ask + $0.01; sell at bid - $0.01 (minimum zero); $0.65 per contract each side.',
    exits='Frozen SPY stop/target; first observed stop/target or five minutes before the exchange close. No additional premium stop/target.',
    limitations='Sampled paired quotes, not tick-complete paths or actual fills. Underlying movement and option P&L are separate. Missing paths stay unresolved.',
    primary='Opening versus later confirmations under unchanged plan rules; coverage first, then completed net results. No automatic rule promotion.')


def saved(c,key,at):
    return c.execute(select(state.c.value).where(state.c.key==key,state.c.updated<=at)).scalar_one_or_none()


def observed_quote(c,symbol,at):
    row=c.execute(select(state.c.value,state.c.updated).where(state.c.key=='quote:'+symbol,state.c.updated<=at)).first()
    if not row:return None
    q=row.value;stamp=number(q.get('ts'));received=number(q.get('received',row.updated))
    if stamp is None or received is None or not stamp<=received<=row.updated or row.updated-stamp>5:return None
    return {**q,'recorded_at':row.updated}


def register(db,c,report,at,*,historical=False):
    """The exact report and registration decision are immutable after insertion."""
    phase=report.get('phase');label=report.get('day');source=report.get('id')
    if not isinstance(label,str) or phase not in PHASES or source!=report_key(label,phase):return False
    try:hours=session(label)
    except (ValueError,TypeError):return False
    if not hours:return False
    id=identity(VERSION,source)
    if c.execute(select(checks.c.id).where(checks.c.id==id)).first():return False
    ctx=report.get('context',{});generated=number(report.get('generated_at'))
    decision=report.get('decision','');confirmed=decision in CONFIRMED
    side='long' if decision==CONFIRMED[0] else 'short' if decision==CONFIRMED[1] else None
    origin='historical_inventory' if historical else 'prospective'
    reason='historical_inventory' if historical else 'not_confirmed' if not confirmed else None
    opening=hours[0]
    minute=dict((p,m) for m,p in CHECKS).get(phase)
    boundary=opening+minute*60 if opening is not None and minute else None
    expiry=number(report.get('expires_at'))
    plan=report.get('plans',{}).get('call' if side=='long' else 'put',{}) if confirmed else {}
    if not reason and (generated is None or not 0<=at-generated<=30 or boundary is None
            or not boundary+5<=generated<boundary+125 or expiry!=boundary+125 or at>=expiry):reason='report_clock_or_deadline'
    stop,target,reference=(number(plan.get(k)) for k in ('stop','target','entry_reference'))
    if not reason and (not all(v and v>0 for v in (stop,target,reference)) or
            not (stop<reference<target if side=='long' else target<reference<stop)):
        reason='invalid_frozen_bracket'
    existing=c.execute(select(observations.c.id).where(observations.c.version==VERSION,observations.c.day==label)).scalar_one_or_none()
    if not reason and existing:reason='session_observation_already_exists'
    p=dict(id=id,version=VERSION,source_key=source,day=label,phase=phase,origin=origin,registered_at=at,
        report=report,eligibility_reason=reason,preopen_key=report_key(label,'preopen'),
        opening_key=report_key(label,'opening'),observation_id=id if confirmed and not historical and not existing else None,
        prior_observation_id=existing,protocol=PROTOCOL)
    inserted=c.execute(db.insert(checks).values(id=id,version=VERSION,source_key=source,day=label,phase=phase,
        kind='report',origin=origin,decision='confirmed' if confirmed else report.get('decision_state','informational'),
        created=at,payload=p).on_conflict_do_nothing(index_elements=['id']).returning(checks.c.id)).first()
    if not inserted:return False
    if confirmed and not historical and not existing:
        closing=session(label)[1]
        opt=report.get('options',{}).get('call' if side=='long' else 'put',{})
        candidate=dict(id=id,version=VERSION,day=label,source_key=source,phase=phase,
            report_version=report.get('version','unversioned'),
            premarket_coverage_basis=ctx.get('premarket_coverage_basis','minute_clock_coverage'),
            check_minute=minute,confirmation_kind=report.get('confirmation_kind'),
            status='excluded' if reason else 'pending',created_at=at,updated_at=at,
            signal_time=generated,signal_price=reference,underlying='SPY',underlying_side=side,
            underlying_stop=stop,underlying_target=target,expires_at=expiry,
            flatten_at=closing-300,selection_reference=number(ctx.get('spot')) or reference,
            report_contract=opt.get('symbol'),reported_watch_contract=opt,
            contract=None,entry=None,exit=None,pnl=None,return_pct=None,
            samples=0,max_gap_seconds=0,best_pct=None,worst_pct=None,peak_at=None,
            underlying_best_points=None,underlying_worst_points=None,milestones=[],
            attempts=0,waiting_reason=reason or 'Waiting for fresh contract evidence and quotes',
            exit_reason=reason,finished_at=at if reason else None,protocol=PROTOCOL)
        c.execute(db.insert(observations).values(id=id,version=VERSION,day=label,status=candidate['status'],
            created=at,updated=at,payload=candidate).on_conflict_do_nothing(index_elements=['version','day']))
    return True


def inventory(db,c,at,limit=100):
    rows=c.execute(select(state.c.value).outerjoin(checks,(checks.c.source_key==state.c.key)&(checks.c.version==VERSION))
        .where(state.c.key.like('spy-brief:%'),state.c.key!='spy-brief:latest',checks.c.id.is_(None),state.c.updated<=at)
        .order_by(state.c.key).limit(limit)).scalars().all()
    return sum(register(db,c,r,at,historical=True) for r in rows)


def audit_schedule(db,c,cfg,at):
    """Record missed/unused slots after their deadline, never replay a decision."""
    activation=db.get(c,KEY+':activation',{}).get('at',at)
    cursor=db.get(c,KEY+':schedule_cursor',day(activation));today=day(at);added=0
    for _ in range(7):
        if cursor>today:break
        hours=session(cursor);complete=True
        if hours:
            opening=hours[0]
            for phase in PHASES:
                minute=dict((p,m) for m,p in CHECKS).get(phase)
                start=opening-600 if phase=='preopen' else opening+minute*60+5
                deadline=opening-300 if phase=='preopen' else opening+minute*60+125
                if start<activation:continue
                if at<deadline+5:complete=False;continue
                source=report_key(cursor,phase)
                if db.get(c,source):continue
                prior=[db.get(c,report_key(cursor,p)) for m,p in CHECKS if minute and m<minute]
                confirmed=next((r for r in prior if r and r.get('decision') in CONFIRMED),None)
                reason=('skipped_after_confirmation' if confirmed else
                    'brief_disabled_at_audit' if not cfg.spy_morning_brief else
                    'blocked_missing_opening_wait' if phase.startswith('followup') and
                    not ((db.get(c,report_key(cursor,'opening')) or {}).get('decision','').startswith('WAIT'))
                    else 'missed_expected_check')
                source+=':schedule-gap';id=identity(VERSION,source)
                p=dict(id=id,version=VERSION,day=cursor,phase=phase,source_key=source,
                    expected_report_key=report_key(cursor,phase),window_start=start,deadline=deadline,
                    registered_at=at,origin='prospective',reason=reason,
                    earlier_confirmation_key=(confirmed or {}).get('id'),enabled_at_audit=cfg.spy_morning_brief,
                    note='A missing slot is not a WAIT verdict or a simulated trade. Late reports, if any, remain separate.')
                added+=int(bool(c.execute(db.insert(checks).values(id=id,version=VERSION,source_key=source,day=cursor,
                    phase=phase,kind='schedule_gap',origin='prospective',decision=reason,created=at,payload=p)
                    .on_conflict_do_nothing(index_elements=['id']).returning(checks.c.id)).first()))
        if not complete:break
        cursor=(date.fromisoformat(cursor)+timedelta(days=1)).isoformat()
        db.put(c,KEY+':schedule_cursor',cursor)
    return added


def contract_for(chain,p,now):
    if not chain or not 0<=now-chain.get('asof',0)<=180:return None,'chain_missing_or_old'
    if chain.get('complete') is not True:return None,'chain_pagination_incomplete'
    kind='call' if p['underlying_side']=='long' else 'put';choices=[]
    for o in chain.get('contracts',[]):
        m=OPTION.fullmatch(str(o.get('symbol','')));strike=number(o.get('strike'))
        if (not m or m[1]!='SPY' or o.get('underlying')!='SPY' or o.get('expiry')!=p['day']
                or m[2]!=date.fromisoformat(p['day']).strftime('%y%m%d') or o.get('type')!=kind
                or m[3]!=('C' if kind=='call' else 'P') or o.get('multiplier')!=100
                or strike is None or strike<=0 or strike!=int(strike) or abs(int(m[4])/1000-strike)>1e-8):continue
        frozen=p.get('report_contract') or (p.get('contract') or {}).get('symbol')
        if frozen and o['symbol']!=frozen:continue
        choices.append(o)
    if not choices:return None,'reported_contract_not_verified' if p.get('report_contract') else 'no_verified_same_day_contract'
    chosen=min(choices,key=lambda o:(abs(o['strike']-p['selection_reference']),o['strike'],o['symbol']))
    return {k:chosen.get(k) for k in ('symbol','underlying','expiry','type','strike','multiplier','delta','iv','oi','volume')},None


def stream_requests(c,now,status):
    rows=c.execute(select(observations.c.payload).where(observations.c.status==status).order_by(observations.c.created)).scalars()
    return [p['contract']['symbol'] for p in rows if p.get('contract') and (status=='open' or now<p['expires_at'])]


class Paper(OptionIdeas):
    """Reuse bounded paired replay; keep this ledger's frozen brackets and fees."""
    marks_table = marks

    def release(self,c,p,now):
        """Official observations require a real plan-delivery receipt."""
        if not p.get('delivery'):
            delivery=c.execute(select(discord_jobs.c.confirmation).join(events,events.c.id==discord_jobs.c.event_id)
                .where(events.c.key==p['source_key'],events.c.kind=='alert',discord_jobs.c.status=='sent',
                    discord_jobs.c.route=='spy_morning')).scalar_one_or_none()
            at=number((delivery or {}).get('at'))
            if at is None or not p['signal_time']<=at<=now or not (delivery or {}).get('message_id'):
                return None
            p['delivery']=delivery
        return p['delivery']['at']

    def save(self,c,p,now):
        p['updated_at']=now
        c.execute(update(observations).where(observations.c.id==p['id']).values(status=p['status'],updated=now,payload=p))

    def notify(self,*args):pass

    def finish(self,c,p,now,reason,price=None):
        if price is None and recovery.defer(self.db,c,p,now,reason):
            self.save(c,p,now)
            return
        if price is None:recovery.register(self.db,c,p,now,reason)
        else:recovery.clear(p)
        p.update(status='closed' if price is not None else 'unresolved',exit=price,
            finished_at=now,exit_reason=reason,pnl=None,return_pct=None,outcome='unresolved')
        if price is not None:
            p['pnl']=round((price-p['entry'])*100-2*FEE,4)
            p['return_pct']=p['pnl']/(p['entry']*100+FEE)*100
            p['outcome']='win' if p['pnl']>0 else 'loss' if p['pnl']<0 else 'breakeven'
        self.save(c,p,now)

    def pending(self,c,p,now):
        def wait(reason,**detail):
            p.update(waiting_reason=reason,last_attempt=dict(at=now,reason=reason,**detail))
            self.save(c,p,now)
        p['attempts']+=1
        if now>=p['expires_at']:
            p.update(status='excluded',finished_at=now,exit_reason=p['waiting_reason'])
            self.save(c,p,now);return
        uq=observed_quote(c,'SPY',now)
        if not current(uq,now) or uq['ts']<p['signal_time']:
            return wait('fresh_underlying_after_report_required',underlying_quote=uq)
        long=p['underlying_side']=='long'
        px=uq['bid'] if long else uq['ask']
        if (px<=p['underlying_stop'] or px>=p['underlying_target'] if long else
                px>=p['underlying_stop'] or px<=p['underlying_target']):
            p.update(status='excluded',finished_at=now,exit_reason='frozen_underlying_bracket_already_crossed')
            return wait(p['exit_reason'],underlying_quote=uq)
        chain=saved(c,'chain:SPY',now)
        candidate,reason=contract_for(chain,p,now)
        if reason:return wait(reason)
        if p.get('contract') and candidate['symbol']!=p['contract']['symbol']:
            return wait('frozen_contract_missing_from_latest_chain')
        if not p.get('contract'):
            p.update(contract=candidate,contract_selected_at=now,chain_at_selection={k:chain.get(k) for k in ('asof','source','complete')})
        released_at=self.release(c,p,now)
        if released_at is None:return wait('confirmed_plan_delivery_required')
        oq=observed_quote(c,candidate['symbol'],now)
        if not liquid(oq,now) or oq['ts']<released_at:
            return wait('fresh_liquid_option_after_delivery_required',option_quote=oq)
        if uq['ts']<released_at:return wait('fresh_underlying_after_delivery_required',underlying_quote=uq)
        from .option_continuity import assess
        quality=assess(self.db,c,candidate['symbol'],now);underlying_quality=assess(self.db,c,'SPY',now)
        if not quality['ready'] or not underlying_quality['ready']:
            return wait('pre_entry_continuity_required',option=quality,underlying=underlying_quality)
        from .quote_path import recorded_path
        path,truncated,diagnostic=recorded_path(self.db,c,'SPY',p['signal_time']-.000001,now)
        stamps=[r['ts'] for r in path]
        gaps=[b-a for a,b in zip(stamps,stamps[1:])]
        audit=dict(from_report=p['signal_time'],through=now,samples=len(path),truncated=truncated,
            first=stamps[0] if stamps else None,last=stamps[-1] if stamps else None,
            max_gap=max(gaps,default=0),archive=diagnostic)
        p['pre_entry_stock_path']=audit
        touched=next((r for r in path if (r['bid']<=p['underlying_stop'] or r['bid']>=p['underlying_target']
            if long else r['ask']>=p['underlying_stop'] or r['ask']<=p['underlying_target'])),None)
        if touched:
            audit['first_bracket_touch']=touched
            p.update(status='excluded',finished_at=now,exit_reason='pre_entry_frozen_bracket_touched')
            return wait(p['exit_reason'],stock_path=audit)
        if (truncated or not stamps or stamps[0]-p['signal_time']>5 or now-stamps[-1]>5 or audit['max_gap']>MAX_GAP):
            return wait('pre_entry_stock_path_incomplete',stock_path=audit)
        entry=round(oq['ask']+SLIPPAGE,2)
        p.update(status='open',opened_at=now,entry=entry,entry_debit=entry*100+FEE,samples=1,
            entry_quote=dict(oq),entry_underlying_quote=dict(uq),entry_underlying_mid=(uq['bid']+uq['ask'])/2,
            entry_contract_metadata=candidate,entry_chain={k:chain.get(k) for k in ('asof','source','complete')},
            entry_spread=oq['ask']-oq['bid'],entry_spread_pct=100*(oq['ask']-oq['bid'])/((oq['ask']+oq['bid'])/2),
            entry_continuity=quality,underlying_entry_continuity=underlying_quality,
            entry_delay_seconds=now-p['signal_time'],entry_delay_after_delivery=now-p['delivery']['at'] if p.get('delivery') else None,
            entry_delay_after_release=now-released_at,option_source=oq.get('source'),
            last_option_ts=oq['ts'],last_underlying_ts=uq['ts'],last_quote=dict(oq),last_underlying_quote=dict(uq),
            observation_model='paired-recorded-quotes-v2',collection_version=oq.get('collection_version','unversioned'),
            underlying_collection_version=uq.get('collection_version','unversioned'),waiting_reason=None)
        if p['collection_version'] in ('option-reliability-v5','option-reliability-v6','option-reliability-v7','option-reliability-v8'):recovery.enable(p)
        self.mark(c,p,now,oq,uq,'entry')
        self.save(c,p,now)

    def mark(self,c,p,now,oq,uq,event):
        price=round(max(0,oq['bid']-SLIPPAGE),2);net=(price-p['entry'])*100-2*FEE
        pct=net/p['entry_debit']*100;underlying=(uq['bid']+uq['ask'])/2
        points=(underlying-p['entry_underlying_mid'])*(1 if p['underlying_side']=='long' else -1)
        p.update(mark=price,mark_at=now,unrealized=round(net,4),mark_pct=pct,underlying_mark=underlying,
            underlying_directional_pct=points/p['entry_underlying_mid']*100)
        if p['best_pct'] is None or pct>p['best_pct']:p.update(best_pct=pct,peak_at=now)
        p['worst_pct']=min(p['worst_pct'],pct) if p['worst_pct'] is not None else pct
        p['underlying_best_points']=max(p['underlying_best_points'],points) if p['underlying_best_points'] is not None else points
        p['underlying_worst_points']=min(p['underlying_worst_points'],points) if p['underlying_worst_points'] is not None else points
        for threshold in (10,25,50):
            if pct>=threshold and not any(x['threshold']==threshold for x in p['milestones']):
                p['milestones'].append(dict(threshold=threshold,at=now,seconds_from_entry=now-p['opened_at']))
        id=identity(p['id'],event,oq['ts'],uq['ts'])
        payload=dict(event=event,source_at=now,option_quote=dict(oq),underlying_quote=dict(uq),
            liquidation_price=price,net_pnl=round(net,4),net_return_pct=pct,
            underlying_mid=underlying,underlying_directional_pct=p['underlying_directional_pct'])
        c.execute(self.db.insert(self.marks_table).values(id=id,study_id=p['id'],at=now,processed_at=self.clock() if self.clock else now,
            payload=payload).on_conflict_do_nothing(index_elements=['id']))
        return price

    def observe_pair(self,c,p,now,oq=None,uq=None):
        oq=oq if oq is not None else observed_quote(c,p['contract']['symbol'],now)
        uq=uq if uq is not None else observed_quote(c,'SPY',now)
        ot=p.get('seen_option_ts',p['last_option_ts']);ut=p.get('seen_underlying_ts',p['last_underlying_ts'])
        if (now-ot>MAX_GAP and (not current(oq,now) or oq['ts']-ot>MAX_GAP)
                or now-ut>MAX_GAP and (not current(uq,now) or uq['ts']-ut>MAX_GAP)):
            p['gap_detail']=dict(at=now,option_last=ot,underlying_last=ut,
                option_next=(oq or {}).get('ts'),underlying_next=(uq or {}).get('ts'))
            return self.finish(c,p,now,'observation_gap')
        if now>p['flatten_at']+5:return self.finish(c,p,now,'session_exit_quote_missing')
        if current(oq,now) and oq['ts']>=ot:p.update(seen_option_ts=oq['ts'],seen_option_quote=oq)
        if current(uq,now) and uq['ts']>=ut:p.update(seen_underlying_ts=uq['ts'],seen_underlying_quote=uq)
        if not current(oq,now) or not current(uq,now):return
        if oq['ts']<p['last_option_ts'] or uq['ts']<p['last_underlying_ts']:return
        changed=oq['ts']>p['last_option_ts'] or uq['ts']>p['last_underlying_ts']
        if not changed and now<p['flatten_at']:return
        p['max_gap_seconds']=max(p['max_gap_seconds'],oq['ts']-ot,uq['ts']-ut)
        p.update(last_option_ts=oq['ts'],last_underlying_ts=uq['ts'],last_quote=oq,last_underlying_quote=uq)
        p['samples']+=1;long=p['underlying_side']=='long'
        px=uq['bid'] if long else uq['ask']
        invalid=px<=p['underlying_stop'] if long else px>=p['underlying_stop']
        target=px>=p['underlying_target'] if long else px<=p['underlying_target']
        reason='underlying_stop' if invalid else 'underlying_target' if target else 'session_flatten' if now>=p['flatten_at'] else None
        price=self.mark(c,p,now,oq,uq,reason or 'mark')
        if reason:
            p['exit_quote']=dict(oq);p['exit_underlying_quote']=dict(uq)
            return self.finish(c,p,now,reason,price)
        self.save(c,p,now)


class Study:
    def __init__(self,db,cfg):self.db,self.cfg,self.owner=db,cfg,uuid.uuid4().hex

    def tick(self,now):
        from .spy_study_report import report
        with self.db.tx() as c:
            if not self.db.lease(c,KEY,self.owner,120):return
            self.db.locked_get(c,KEY+':activation',dict(at=now,protocol=PROTOCOL))
            registered=inventory(self.db,c,now);gaps=audit_schedule(self.db,c,self.cfg,now)
            paper=Paper(self.db,self.cfg,clock=lambda:now)
            rows=c.execute(select(observations.c.payload).where(observations.c.status.in_(('pending','open')))
                .order_by(observations.c.created)).scalars().all()
            for p in rows:
                if p['status']=='pending':paper.pending(c,p,now)
                else:paper.observe(c,p,now)
            self.db.put(c,KEY+':worker',dict(at=now,registered=registered,schedule_gaps=gaps,active=len(rows),version=VERSION))
            if now-self.db.get(c,KEY+':report',{}).get('at',0)>=60:
                result=report(self.db,c,now);self.db.put(c,KEY+':report',result)
                logging.getLogger('uvicorn.error').info('SPY plan study coverage: %s',{k:result[k] for k in ('at','inventory','counts')})
        self.db.health('spy_plan_study','running','Linked SPY confirmation and 0DTE paper outcomes',version=VERSION)

    async def run(self):
        while True:
            try:await asyncio.to_thread(self.tick,time.time())
            except asyncio.CancelledError:raise
            except Exception as error:
                logging.getLogger('uvicorn.error').exception('SPY plan study failed')
                await asyncio.to_thread(self.db.health,'spy_plan_study','error',type(error).__name__)
            await asyncio.sleep(2)
