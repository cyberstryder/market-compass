"""Complete daily aggregates; independent observations are never account returns."""
import json
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import select, func, case
from .market import CT, session
from .store import state, events, spy_checks, spy_options, tm_studies, tm_checkpoints, swing_trials, swing_checkpoints, discovery_trials, discovery_checks
from .setup_study import trials
from .operating_mode import policy
from .option_ideas import ideas
from .swing_ideas import swings
from .session_gaps import window
from .native_morning import store as morning
from .native_morning.expiration import net_exit, HORIZONS
from .native_smoothers import weekly
from .morning_history import history


def metrics(status, pnl):
    closed = status == 'closed'
    count = lambda condition: func.sum(case((condition, 1), else_=0))
    return dict(total=func.count(), closed=count(closed),
        wins=count(closed & (pnl > 0)), losses=count(closed & (pnl < 0)),
        breakeven=count(closed & (pnl == 0)), missing_pnl=count(closed & pnl.is_(None)),
        net_pnl=func.sum(case((closed, pnl), else_=0)),
        **{s: count(status == s) for s in ('open', 'pending', 'unresolved', 'excluded')})


def aggregate(c, table, conditions, dimensions, status=None, payload=None):
    p = table.c.payload if payload is None else payload
    status = table.c.status if status is None else status
    measures = metrics(status, p['pnl'].as_float())
    q = select(*(v.label(k) for k,v in {**dimensions, **measures}.items())).where(*conditions)
    if dimensions: q = q.group_by(*dimensions.values())
    rows = []
    for r in c.execute(q).mappings():
        row = dict(r)
        for key in measures: row[key] = row[key] or 0
        row['net_pnl'] = round(row['net_pnl'], 4) if row['closed'] > row['missing_pnl'] else None
        measured = row['wins'] + row['losses'] + row['breakeven']
        row['win_rate'] = row['wins']/measured if measured else None
        rows.append(row)
    return sorted(rows, key=lambda r: str([r[k] for k in dimensions]))


def reasons(c, table, conditions):
    p = table.c.payload
    reason = func.coalesce(p['exit_reason'].as_string(), p['waiting_reason'].as_string(), p['reason'].as_string(), 'Not recorded')
    return [dict(status=s, reason=r, count=n) for s,r,n in c.execute(
        select(table.c.status, reason, func.count()).where(*conditions,
            table.c.status.in_(('excluded', 'unresolved', 'pending'))).group_by(table.c.status, reason))]


def cohort(c, table, stamp, lower, upper, extra=()):
    conditions = [stamp >= lower, stamp < upper, *extra]
    p = table.c.payload
    return dict(totals=aggregate(c, table, conditions, {})[0],
        groups=aggregate(c, table, conditions, dict(symbol=p['underlying'].as_string() if table in (ideas, swings) else table.c.symbol,
            strategy=p['strategy'].as_string(), version=p['version'].as_string())),
        reasons=reasons(c, table, conditions), since=lower, through=upper,
        counts_complete=True, basis='Current outcomes of the complete creation cohort; independent overlapping experiments, not account P&L.')


def paper(c, lower, upper):
    p = state.c.value
    base = [state.c.key.like('trade:%')]
    dimensions = dict(asset=p['asset'].as_string(), strategy=p['strategy'].as_string())
    entered = p['entered_at'].as_float()
    exited = p['exited_at'].as_float()
    entry_scope = base + [entered >= lower, entered < upper]
    exit_scope = base + [exited >= lower, exited < upper]
    kwargs = dict(status=p['status'].as_string(), payload=p)
    return dict(entries=aggregate(c, state, entry_scope, dimensions, **kwargs),
        realized=aggregate(c, state, exit_scope, dimensions, **kwargs),
        by_symbol=aggregate(c, state, exit_scope, dict(asset=p['asset'].as_string(),symbol=p['symbol'].as_string()), **kwargs),
        since=lower, through=upper,
        basis='Entries grouped by entry time; realized P&L grouped by exit time. All paper accounts use the 17:00 CT risk-day rollover. No broker fills.')


def skips(c, lower, upper):
    p = events.c.payload
    conditions = [events.c.ts >= lower, events.c.ts < upper,
        events.c.source == 'engine', events.c.kind.in_(('alert','paper_decision')),
        p['status'].as_string().in_(('skipped','options_skipped'))]
    dims = [events.c.symbol, p['status'].as_string(), p['reason'].as_string()]
    return [dict(symbol=s,status=t,reason=r,count=n,first_at=a,last_at=b)
        for s,t,r,n,a,b in c.execute(select(*dims,func.count(),func.min(events.c.ts),func.max(events.c.ts))
            .where(*conditions).group_by(*dims))]


def morning_daily(c, lower, upper, now):
    """Read every selected signal in batches; option horizons are separate cohorts."""
    s = morning.signals
    scope = [s.c.signal_at_ms >= lower*1000, s.c.signal_at_ms < upper*1000, s.c.is_test.is_(False)]
    coverage = Counter()
    counts = Counter(); groups = defaultdict(lambda:dict(measured=0,wins=0,losses=0,breakeven=0,net_pnl=0))
    cursor = ''; total = 0
    while True:
        rows = c.execute(select(s.c.signal_id).where(*scope, s.c.signal_id > cursor)
            .order_by(s.c.signal_id).limit(200)).scalars().all()
        if not rows: break
        total += len(rows); cursor = rows[-1]
        from .morning_schema.outcomes import stock_coverage
        bars=defaultdict(list)
        for b in c.execute(select(morning.stock_bars).where(morning.stock_bars.c.signal_id.in_(rows))).mappings():
            if b['open_at_ms']+60000<=now*1000:bars[b['signal_id']].append(json.loads(b['bar_json']))
        for sid,raw in c.execute(select(s.c.signal_id,s.c.signal_json).where(s.c.signal_id.in_(rows))):
            coverage[stock_coverage(json.loads(raw),sorted(bars[sid],key=lambda b:b['open_at_ms']),True,int(now*1000))['status']]+=1
        refs = {r.signal_id:json.loads(r.quote_json) if r.quote_json else {} for r in c.execute(
            select(morning.option_jobs.c.signal_id,morning.option_jobs.c.quote_json).where(morning.option_jobs.c.signal_id.in_(rows)))}
        for variant, table in (('current_contract',morning.option_samples),('zero_dte',morning.expiration_samples)):
            for r in c.execute(select(table).where(table.c.signal_id.in_(rows),table.c.minute.in_(HORIZONS))).mappings():
                if r['due_at_ms'] > now*1000: continue
                ref = refs.get(r['signal_id'],{})
                if variant=='zero_dte': ref=ref.get('expiration_comparison',{}).get('variants',{}).get('zero_dte',{})
                q=json.loads(r['quote_json']) if r['quote_json'] else {}
                if q.get('sampled_at_ms',now*1000+1)>now*1000: continue
                value=net_exit(ref,q) if (ref.get('contract') or {}).get('symbol') else None
                if value is None:
                    counts[variant+': '+str(r['minute'])+'m: '+q.get('reason',r['status'])]+=1
                    continue
                g=groups[(variant,r['minute'])];v=value['net_pnl']
                g['measured']+=1;g['wins']+=v>0;g['losses']+=v<0;g['breakeven']+=v==0;g['net_pnl']+=v
    original_stamp=history.c.payload['signal_at_ms'].as_float()
    originals=c.execute(select(func.count()).select_from(history).where(history.c.kind=='signal',original_stamp>=lower*1000,original_stamp<upper*1000)).scalar_one()
    missing=c.execute(select(func.count()).select_from(s).where(*scope,~select(history.c.source_id).where(history.c.kind=='signal',history.c.source_id==s.c.signal_id).exists())).scalar_one()
    from .native_routing import receipts
    direct=c.execute(select(func.count()).select_from(s).where(*scope,select(receipts.c.id).where(
        receipts.c.signal_id==s.c.signal_id,receipts.c.origin=='direct',
        receipts.c.payload['entry'].as_boolean().is_(True),
        receipts.c.payload['status'].as_string().in_(['accepted','duplicate'])).exists())).scalar_one()
    return dict(direct_received_signals=direct,original_comparison_available=missing==0 and total>0,
        intake_basis='Direct accepted entry receipts verify native intake, not original-source parity. Missing original records remain unavailable.',
        signals=total,stock_coverage=dict(coverage),original_signals=originals,native_without_original=missing,
        source_status='original_source_gap' if missing else 'no_native_signals' if not total else 'matched_signal_ids',
        options=[dict(variant=v,minutes=h,**groups[(v,h)],unmeasured=total-groups[(v,h)]['measured'])
                 for v in ('current_contract','zero_dte') for h in HORIZONS],
        quote_gaps=dict(counts), counts_complete=True,
        basis='Native signal census; original mirror is comparison evidence. Each option horizon is a separate available-quote cohort, not a paired strategy comparison or account P&L. Unmeasured includes pending, unscheduled and unavailable quotes.')


def other_research(c, lower, upper):
    output=[]
    for name,table,stamp,checks,join in (
        ('TraderMatrix',tm_studies,tm_studies.c.first_seen,tm_checkpoints,tm_checkpoints.c.study_id==tm_studies.c.id),
        ('Swing flow comparison',swing_trials,swing_trials.c.first_seen,swing_checkpoints,swing_checkpoints.c.study_id==swing_trials.c.id),
        ('Discovery',discovery_trials,discovery_trials.c.created,discovery_checks,discovery_checks.c.trial_id==discovery_trials.c.id)):
        scope=[stamp>=lower,stamp<upper]
        states=dict(c.execute(select(table.c.status,func.count()).where(*scope).group_by(table.c.status)).all())
        checkpoints=[dict(horizon=h,status=s,count=n) for h,s,n in c.execute(
            select(checks.c.horizon,checks.c.status,func.count()).select_from(checks.join(table,join)).where(*scope).group_by(checks.c.horizon,checks.c.status))]
        output.append(dict(program=name,states=states,total=sum(states.values()),checkpoints=checkpoints,
            basis='Daily received research cohort and checkpoint completeness; counts are not trade wins or dollar returns.'))
    return output


def verification(db,c,day,now):
    checks=list(c.execute(select(spy_checks.c.payload).where(spy_checks.c.day==day)).scalars())
    p=events.c.payload
    plans=list(c.execute(select(p).where(events.c.source=='spy_brief',events.c.kind=='alert',p['day'].as_string()==day)).scalars())
    plans=[p for p in plans if p.get('status')!='spy_chart_prompt']
    draws=list(c.execute(select(events.c.ts,p).where(events.c.source=='spy_brief',events.c.kind=='alert',p['day'].as_string()==day,p['status'].as_string()=='spy_chart_prompt')).all())
    return dict(spy_daily=db.get(c,'spy-daily-readiness:'+day,{'status':'awaiting_session_evidence'}),
        spy_checks=len(checks), spy_decisions=dict(Counter(p.get('report',{}).get('decision','unknown') for p in checks)),
        spy_details=[dict(phase=p.get('phase'),at=p.get('registered_at'),decision=p.get('report',{}).get('decision'),data_blocks=p.get('report',{}).get('data_blocks',[]),eligibility_reason=p.get('eligibility_reason')) for p in checks],
        drawing_messages=len(draws), drawing_reasons=dict(Counter((p.get('drawing_update') or {}).get('reason','legacy_policy') for _,p in draws)),
        drawing_suppressions=sum(p.get('drawing_update',{}).get('send') is False for p in plans),
        last_plan_at=max((p.get('generated_at',0) for p in plans),default=None),
        ask_options=list(db.prefix(c,'ask-evidence:'+day+':').values()),
        basis='Observed evidence only. Readiness is not an entry signal; queued drawing messages are not confirmed Discord delivery. A new session remains unverified until its own observations arrive.')


def build(db,c,cfg,now,session_day=None):
    w=window(now,session_day,asset='future',scope='full');day=w['day']
    date=datetime.fromisoformat(day).date()
    risk_start=datetime.combine(date-timedelta(days=1),time(17),CT).timestamp()
    risk_end=datetime.combine(date,time(17),CT).timestamp()
    if risk_start>now: raise ValueError('Cannot report a session that has not opened')
    upper=min(now,risk_end)
    cash=session(day)
    lower_cash=cash[0] if cash else datetime.combine(date,time(),CT).timestamp()
    upper_cash=min(now,cash[1] if cash else lower_cash)
    skips_=skips(c,risk_start,upper)
    from .engine import spec
    locked=[r['first_at'] for r in skips_ if r['reason']=='Daily simulated loss limit' and spec(r['symbol'])['asset']=='future']
    first=min(locked) if locked else None
    futures=cohort(c,trials,trials.c.started,w['since'],w['through'],[trials.c.payload['asset'].as_string()=='future'])
    post=cohort(c,trials,trials.c.started,first,w['through'],[trials.c.payload['asset'].as_string()=='future']) if first else None
    monday=(date-timedelta(days=date.weekday())).isoformat()
    smoother_states=dict(c.execute(select(weekly.c.status,func.count()).where(weekly.c.week==monday).group_by(weekly.c.status)).all())
    spy_scope=[spy_options.c.day==day]
    option_selections=[p for p in db.prefix(c,'pending_research_options:').values()
        if lower_cash<=p.get('created_at',0)<upper_cash]
    from .reliability_report import report as reliability_report
    return dict(smoothers_daily=db.get(c,'smoothers-daily-comparison-v1:report',{'state':'awaiting_activation'}),quote_reliability=reliability_report(db,c,risk_start,upper),day=day,asof=now,operating_policy=policy(cfg),counts_complete=True,paper=paper(c,risk_start,upper),skips=skips_,
        setup_context=aggregate(c,trials,[trials.c.started>=w['since'],trials.c.started<w['through']],
            dict(symbol=trials.c.symbol,strategy=trials.c.strategy,version=trials.c.version,
                session=func.coalesce(trials.c.payload['research_context']['session'].as_string(),'not_recorded'),
                market_state=func.coalesce(trials.c.payload['research_context']['market_state'].as_string(),'not_recorded'))),
        futures=futures,post_lock=dict(first_recorded_block_at=first,cohort=post,
            status='observed_after_loss_block' if post and post['totals']['total'] else 'no_trials_after_recorded_block' if first else 'no_loss_block_recorded',
            basis='Trials created after the first recorded loss-limit rejection; does not establish the exact time the account locked or continuous quote coverage.'),
        zero_dte_selection=dict(last_attempt_reasons=dict(Counter(p.get('last_selection',{}).get('reason','No saved attempt diagnostic') for p in option_selections)),
            contract_rejections=dict(Counter(r.get('reason','Unknown') for p in option_selections for r in p.get('last_selection',{}).get('rejections',[]))),
            total=len(option_selections),states=dict(Counter(p['status'] for p in option_selections)),
            reasons=dict(Counter(p.get('reason') or p.get('last_selection',{}).get('reason') or 'Awaiting selection'
                for p in option_selections if p['status']!='observed'))),
        zero_dte_setups=cohort(c,trials,trials.c.started,lower_cash,upper_cash,[trials.c.payload['asset'].as_string()=='option']),
        options=cohort(c,ideas,ideas.c.created,lower_cash,upper_cash),
        swing_options=cohort(c,swings,swings.c.created,lower_cash,upper_cash),
        stock_setups=cohort(c,trials,trials.c.started,lower_cash,upper_cash,[trials.c.payload['asset'].as_string()=='stock']),
        morning=morning_daily(c,lower_cash,upper_cash,now),
        smoothers=dict(week=monday,states=smoother_states,total=sum(smoother_states.values()),basis='Current weekly underlying-target outcomes; not daily realized option returns.'),
        spy_options=dict(totals=aggregate(c,spy_options,spy_scope,{})[0],basis='Scheduled SPY option observations; separate from the 0DTE paper scanner.'),
        research=other_research(c,lower_cash,upper_cash),
        verification=verification(db,c,day,now),
        note='Saved outcomes as of report generation, not a historical status snapshot. Independent studies overlap and must not be added to paper account P&L. Independent evaluations are unrestricted by paper-account limits. Paper records are separate historical benchmarks when paper trading is paused.')
