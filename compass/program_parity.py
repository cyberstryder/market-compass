"""Bounded read-only migration evidence. Never sends an official notification."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta
import logging
import time
import uuid
from sqlalchemy import select
from .market import number, session, CT, dedup
from .projects import records
from .smoothers_quality import score_signal, QUALITY_VERSION

VERSION='program-parity-v1'


def morning_options(p):
    ref=p.get('option') or {}
    results=[]
    for sample in p.get('option_samples',[]):
        q=sample.get('quote') or {}
        item=dict(minute=sample.get('minute'),status='unavailable',source_status=q.get('status',sample.get('status')))
        values=[number(x) for x in (ref.get('ask'),ref.get('midpoint'),q.get('bid'),q.get('midpoint'))]
        contract=(ref.get('contract') or {}).get('symbol')
        other=(q.get('contract') or {}).get('symbol')
        if ref.get('status') not in ('available','wide_spread') or q.get('status') not in ('available','wide_spread'):
            item['reason']='source_quote_unavailable'
        elif not contract or contract!=other:
            item['reason']='contract_identity_unverified'
        elif any(v is None for v in values) or values[0]<=0 or values[1]<=0 or min(values[2:])<0:
            item['reason']='invalid_prices'
        elif not isinstance(q.get('quote_at_ms'),(int,float)) or not isinstance(ref.get('quote_at_ms'),(int,float)) or q['quote_at_ms']<=ref['quote_at_ms']:
            item['reason']='quote_order_unverified'
        else:
            ret=(values[2]/values[0]-1)*100
            mid=(values[3]/values[1]-1)*100
            expected=number(q.get('ask_to_bid_return_pct'))
            expected_mid=number(q.get('midpoint_return_pct'))
            item.update(status='matched' if expected is not None and expected_mid is not None and abs(expected-ret)<1e-6 and abs(expected_mid-mid)<1e-6 else 'missing_source_return' if expected is None or expected_mid is None else 'different',
                ask_to_bid_return_pct=ret,midpoint_return_pct=mid,
                source_ask_to_bid_return_pct=expected,source_midpoint_return_pct=expected_mid)
        results.append(item)
    delivery=p.get('delivery') or {}
    return dict(source_id=p['id'],symbol=p['symbol'],source_ts=p['source_ts'],samples=[r for r in results if r['minute'] in (5,15,30,60)],
        sample_counts=dict(Counter(r['status'] for r in results)),
        delivery_status='exported' if delivery.get('schema_version')==1 else 'not_exported',
        delivery_events=delivery.get('events',[]),
        basis='Arithmetic reconciliation of original sampled quotes, not independent fills or continuous option peaks')


def smoother_quality(p):
    original=p.get('original') or {}
    result=dict(source_id=p['id'],symbol=p['symbol'],source_ts=p['source_ts'],status='unavailable')
    required=('alltime_wins_at_entry','alltime_losses_at_entry','target_atr_mult')
    if original.get('quality_version')!=QUALITY_VERSION or any(original.get(k) is None for k in required):
        return dict(result,reason='missing_supported_source_quality_inputs')
    score=score_signal(target_atr_mult=original['target_atr_mult'],
        alltime_wins=original['alltime_wins_at_entry'],alltime_losses=original['alltime_losses_at_entry'],
        backtest_wr=original.get('backtest_wr_at_entry'),entry_premium=original.get('entry_premium'),
        est_return_pct=original.get('est_return_pct_at_entry'))
    differences={k:dict(source=original.get(k),compass=v) for k,v in score.items() if k!='quality_version'
                 and (number(original.get(k)) is None or abs(float(original[k])-v)>.011)}
    return dict(result,status='different' if differences else 'matched',computed=score,differences=differences,
                basis='Source-equivalent calculation on imported entry inputs; independent weekly selection not yet verified')


def week_schedule(monday):
    """Research schedule using Compass exchange calendar, not fixed weekdays."""
    sessions=[]
    for offset in range(5):
        date=monday+timedelta(days=offset)
        hours=session(date.isoformat())
        if hours: sessions.append((date.isoformat(),*hours))
    if not sessions: return None
    return dict(first_session=sessions[0][0],entry_at=sessions[0][1]+3900,
                last_session=sessions[-1][0],close_at=sessions[-1][2]+300,
                basis='Compass XNYS calendar; compare with source Alpaca calendar before cutover')


def target_observation(db,c,p,now):
    start=number(p.get('source_ts'))
    target=number(p.get('target'))
    if start is None or target is None or not 0<=now-start<=7*86400:
        return dict(status='outside_recent_week_or_missing_input')
    source_rows=db.recent(c,'bar',p['symbol'],limit=4001,since=start)
    truncated=len(source_rows)>4000
    bars=dedup(source_rows[:4000],now)
    expected=set()
    date=datetime.fromtimestamp(start,CT).date()
    while date<=datetime.fromtimestamp(now,CT).date():
        hours=session(date.isoformat())
        if hours:
            begin=max(start,hours[0]); end=min(now,hours[1])
            expected.update(range(int((begin+59)//60*60),int(end//60)*60,60))
        date+=timedelta(days=1)
    bars=[r for r in bars if int(r['ts']) in expected]
    present={int(r['ts']) for r in bars}
    missing=len(expected-present)
    hits=[r for r in bars if (r['payload']['h']>=target if p.get('side')=='long' else r['payload']['l']<=target)]
    first=hits[0] if hits else None
    earlier_missing=bool(first and any(t<first['ts'] for t in expected-present))
    status='observed_touch' if first else 'no_touch_in_complete_observed_window' if not missing and expected and not truncated else 'incomplete_observation'
    return dict(status=status,observed_minutes=len(present),expected_minutes=len(expected),
        missing_minutes=missing,truncated=truncated,first_observed_touch=first['ts'] if first else None,
        first_touch_verified=bool(first and not earlier_missing and not truncated),
        touch_bar=first['payload'] if first else None,source_status=p.get('status'),
        basis='Retained completed stock bars; observed target touch is not an option exit or fill')


def tick(db,owner,now,morning_enabled=True):
    with db.tx() as c:
        if not db.lease(c,VERSION,owner,90): return
        results={}
        for project in ('morning','smoothers'):
            if project=='morning' and not morning_enabled:
                old=db.get(c,VERSION+':report',{}).get('projects',{}).get('morning',{})
                results['morning']={**old,'retired':True,'checked':old.get('checked',0),'samples':old.get('samples',{}),'delivery':old.get('delivery',{})}
                continue
            found=c.execute(select(records.c.payload).where(records.c.project==project)
                .order_by(records.c.source_ts.desc()).limit(501)).scalars().all()
            calc=morning_options if project=='morning' else smoother_quality
            rows=[]
            for p in found[:500]:
                try:
                    row=calc(p)
                    if project=='smoothers': row['target_observation']=target_observation(db,c,p,now)
                    rows.append(row)
                except (KeyError,TypeError,ValueError,OverflowError):
                    rows.append(dict(source_id=p.get('id'),symbol=p.get('symbol'),status='unavailable',reason='invalid_source_fields'))
            results[project]=dict(rows=rows[:100],checked=len(rows),truncated=len(found)>500,
                counts=dict(Counter(r.get('status','observed') for r in rows)))
            if project=='morning':
                results[project]['samples']=dict(sum((Counter(r.get('sample_counts',{})) for r in rows),Counter()))
                results[project]['delivery']=dict(Counter(r.get('delivery_status','unavailable') for r in rows))
        local=datetime.fromtimestamp(now,CT).date()
        results['week_schedule']=week_schedule(local-timedelta(days=local.weekday()))
        report=dict(version=VERSION,at=now,projects=results,cutover_ready=False,
                    notifications_enabled=False,orders_enabled=False)
        db.put(c,VERSION+':report',report)
        logging.getLogger('uvicorn.error').info('Program parity: morning=%s options=%s delivery=%s smoothers=%s cutover_ready=false',
            results['morning']['checked'],results['morning']['samples'],results['morning']['delivery'],results['smoothers']['counts'])


async def run(db,cfg=None):
    owner=uuid.uuid4().hex
    while True:
        try: await asyncio.to_thread(tick,db,owner,time.time(),cfg.morning_enabled if cfg is not None else True)
        except Exception as e:
            db.health('program_parity','error',type(e).__name__)
        await asyncio.sleep(60)
