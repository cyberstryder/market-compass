"""Finite-period coverage and paired daily comparisons, with evaluation lock."""
from collections import Counter, defaultdict
from statistics import mean
from random import Random
from sqlalchemy import select, func
from .spy_timeframes import VERSION, TIMEFRAMES, PROTOCOL, arms, checks, days


def metrics(rows):
    closed = sorted((p for p in rows if p['status']=='closed'), key=lambda p:(p['day'],p['id']))
    pnl = [p['pnl'] for p in closed]
    gains = sum(max(0,x) for x in pnl)
    losses = -sum(min(0,x) for x in pnl)
    equity = peak = drawdown = 0
    for x in pnl:
        equity += x
        peak = max(peak,equity)
        drawdown = max(drawdown,peak-equity)
    avg = lambda key:mean(p[key] for p in closed if p.get(key) is not None) if any(p.get(key) is not None for p in closed) else None
    return dict(completed=len(closed), net_pnl=sum(pnl) if rows else None,
        wins=sum(x>0 for x in pnl), win_rate=sum(x>0 for x in pnl)/len(pnl) if pnl else None,
        profit_factor=gains/losses if losses else None,
        profit_factor_reason=None if losses else 'no_losses' if pnl else 'not_measured',
        closed_equity_drawdown=drawdown if rows else None,
        mean_return_pct=avg('return_pct'), mean_best_pct=avg('best_pct'), mean_worst_pct=avg('worst_pct'),
        mean_entry_delay_seconds=avg('entry_delay_seconds'),
        mean_hold_seconds=mean(p['finished_at']-p['opened_at'] for p in closed) if closed else None,
        mean_giveback_pct_points=mean(max(0,p['best_pct']-p['return_pct']) for p in closed) if closed else None)


def differences(rows, common, unlocked):
    bykey={(p['day'],p['timeframe']):p.get('pnl') or 0 for p in rows}
    result=[]
    for tf in TIMEFRAMES[:-1]:
        item=dict(timeframe=tf,reference=15,matched_days=len(common),mean_difference=None,interval=None,
            status='held_out' if not unlocked else 'insufficient_matched_sessions',
            method='3,000 paired session bootstrap resamples; 98.333% percentile interval per contrast (three planned comparisons; Bonferroni family alpha 0.05).')
        if unlocked and len(common)>=10:
            delta=[bykey[(d,tf)]-bykey[(d,15)] for d in sorted(common)]
            rng=Random(20260918+tf)
            samples=sorted(mean(rng.choices(delta,k=len(delta))) for _ in range(3000))
            tail=.05/(2*3)
            item.update(status='estimated_not_promoted',mean_difference=mean(delta),
                interval=[samples[int(tail*len(samples))],samples[min(len(samples)-1,int((1-tail)*len(samples)))]])
        result.append(item)
    return result


def report(db,c,now,activation=None):
    activation = activation or db.get(c,VERSION+':activation',{})
    rows = c.execute(select(arms.c.payload).order_by(arms.c.day,arms.c.timeframe)).scalars().all()
    # The frozen study has at most 21 days x 4 arms. No preview limit defines totals.
    unlocked = bool(activation and now >= activation['evaluation_end'])
    groups, paired, comparisons = [], [], []
    coverage = dict(Counter(p['status'] for p in rows))
    for cohort in ('warmup','evaluation'):
        selected = [p for p in rows if p['cohort']==cohort]
        byday = defaultdict(list)
        for p in selected:
            byday[p['day']].append(p)
        common = {label for label, ps in byday.items() if len(ps)==len(TIMEFRAMES)
            and all(p['status'] in ('closed','no_signal') and not p['missed_checks'] and not p['blocked_checks'] for p in ps)}
        if cohort=='evaluation':comparisons=differences(selected,common,unlocked)
        visible = cohort=='warmup' or unlocked
        for tf in TIMEFRAMES:
            arm = [p for p in selected if p['timeframe']==tf]
            groups.append(dict(cohort=cohort,timeframe=tf,days=len(arm),statuses=dict(Counter(p['status'] for p in arm)),
                blocked_checks=sum(p['blocked_checks'] for p in arm),missed_checks=sum(p['missed_checks'] for p in arm),
                results=metrics([p for p in arm if p['status']=='closed']) if visible else None))
            matched = [p for p in arm if p['day'] in common]
            paired.append(dict(cohort=cohort,timeframe=tf,matched_days=len(common),
                no_signal_days=sum(p['status']=='no_signal' for p in matched),
                mean_daily_net=sum(p.get('pnl') or 0 for p in matched)/len(common) if visible and common else None,
                results=metrics(matched) if visible else None))
    cuts=[]
    for dimension in ('time_bucket','gap_regime'):
        buckets=defaultdict(list)
        for p in rows:
            if p['status']=='closed' and (p['cohort']=='warmup' or unlocked):
                buckets[(p['cohort'],p['timeframe'],p.get(dimension,'unknown'))].append(p)
        cuts.extend(dict(dimension=dimension,cohort=k[0],timeframe=k[1],bucket=k[2],results=metrics(ps)) for k,ps in sorted(buckets.items()))
    due = sum(s['close']<=now for s in activation.get('sessions',[]))
    return dict(at=now,version=VERSION,protocol=PROTOCOL,activation=activation,coverage=coverage,
        evaluation_locked=not unlocked,completed_calendar_sessions=due,required_sessions=20,
        groups=groups,paired=paired,cuts=cuts,comparisons=comparisons,winner=None,
        note='Compare only matched fully observed days. Missing option paths remain unresolved; they are not zero returns. Warmup is separate. Evaluation results unlock after the fixed final close; no automatic best-timeframe claim. Continuous automated exits do not measure human screen time.')


def records(c,now,*,timeframe=0,offset=0,limit=100):
    if timeframe not in (0,*TIMEFRAMES) or offset<0 or not 1<=limit<=100:
        raise ValueError('Invalid timeframe or page')
    where = checks.c.created<=now
    if timeframe:
        where &= checks.c.arm_id.in_(select(arms.c.id).where(arms.c.timeframe==timeframe))
    total = c.execute(select(func.count()).select_from(checks).where(where)).scalar_one()
    rows = c.execute(select(checks.c.payload).where(where).order_by(checks.c.boundary.desc(),checks.c.id)
        .offset(offset).limit(limit)).scalars().all()
    # Decision evidence has no evaluation trade returns or option paths.
    contexts = c.execute(select(days.c.payload)).scalars().all()
    return dict(records=rows,total=total,offset=offset,limit=limit,has_more=offset+len(rows)<total,
        contexts=contexts,asof=now,note='Live audit paging; newer checks may move page boundaries. Full totals are independent of page size.')
