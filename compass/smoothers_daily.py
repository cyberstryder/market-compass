"""Quiet prospective daily formula comparison; no orders, alerts or weekly writes."""
import asyncio
import logging
import time
import uuid
from collections import Counter,defaultdict
from datetime import date,datetime,timedelta
import httpx
from sqlalchemy import select,update
from .store import smoothers_daily as trials,identity,events
from .market import day,session,fresh,NY,number
from .native_config import KEY as CONFIG_KEY
from .native_smoothers import make_signal
from .native_smoothers_data import Data
from .tm_study import quote_at

VERSION='smoothers-daily-comparison-v1'
HORIZONS=('60m','close','1session','3session','5session')
PROTOCOL=dict(version=VERSION,sessions=20,primary_horizon='close',
    schedule='Every exchange session; same completed opening-hour formula as weekly Smoothers. Process from open+65 to open+90 minutes; actual detection and quote time anchor entry.',
    population='Frozen enabled Smoothers tickers and parameter revision, excluding DJT. One first observed candidate per family, symbol, side and session.',
    families=['smoothers_daily','intraday_stock','scanner_0dte','options_ideas'],
    overlap='Same symbol, direction and exchange session. Confirmation available at detection is separated from later-only overlap; later signals cannot justify an earlier entry. Opposite directions are separate. No match observed is not proof of complete scanner coverage.',
    endpoints='Fresh underlying midpoint at actual first detection; 60 trading minutes and common session closes at 0/1/3/5 sessions. No stops, fills, fees or option-return inference.',
    timing='Compare actual persisted detection times, never backdate Smoothers to the opening-hour close.',
    interpretation='Compare Smoothers-only, scanner-only and overlap groups by weekday and family. Correlated experiments, unequal entry times and missing data prevent causal claims. No automatic promotion or new alerts.')


def quote_demand(db,c,now):
    """Use existing websocket/REST recovery, preserving five-second endpoints."""
    a=db.get(c,VERSION+':activation',{})
    wanted=[]
    if any(s['open']+3600<=now<=min(s['open']+5400,s['close']) for s in a.get('sessions',[])):
        wanted.extend(r['ticker'] for r in a.get('configs',[]))
    for p in c.execute(select(trials.c.payload).where(trials.c.status=='pending')).scalars():
        if any(q.get('status')=='pending' and q.get('target_at') is not None and
               q['target_at']-30<=now<=q['target_at']+5 for q in p['checkpoints'].values()):
            wanted.append(p['symbol'])
    return list(dict.fromkeys(wanted))


def retain_daily_basis(db,c,symbol,bars,observed):
    """Retain actual fetched completed bars, never backdate their receipt time."""
    if bars.empty:return
    today=date.fromisoformat(day(observed))
    completed=[(stamp,row) for stamp,row in bars.iterrows() if stamp.date()<today][-10:]
    basis=None
    for stamp,row in completed:
        at=datetime.combine(stamp.date(),datetime.min.time(),NY).timestamp()
        close=number(row.get('close'))
        if close and close>0:
            db.append(c,'daily','smoothers_daily',symbol,at,dict(c=close,
                basis='split_adjusted_provider_bar',fetched_at=observed))
            basis=dict(through=stamp.date().isoformat(),close=close,observed_at=observed)
    return basis


def activate(db,c,now):
    old=db.get(c,VERSION+':activation')
    if old:return old
    config=db.get(c,CONFIG_KEY,{})
    if not config.get('configs'):return None
    configs=[dict(r) for r in config['configs'] if r.get('enabled') and r['ticker']!='DJT']
    if not configs:return None
    sessions=[];start=date.fromisoformat(day(now))
    for n in range(60):
        d=(start+timedelta(days=n)).isoformat();hours=session(d)
        if hours and hours[0]>now:sessions.append(dict(day=d,open=hours[0],close=hours[1]))
        if len(sessions)==20:break
    if len(sessions)!=20:raise ValueError('Insufficient exchange calendar')
    value=dict(at=now,version=VERSION,protocol=PROTOCOL,configs=configs,revision=config.get('revision'),sessions=sessions,
        state='collecting',alerts=False,orders=False)
    db.put(c,VERSION+':activation',value)
    return value


def endpoints(at):
    from .swing_study import targets
    return {k:v for k,v in targets(at).items() if k in HORIZONS}


def add(db,c,activation,family,symbol,side,at,now,evidence,entry_quote=None,reason=None,reference_basis=None):
    today=day(at)
    if today not in {s['day'] for s in activation['sessions']} or symbol not in {r['ticker'] for r in activation['configs']}:
        return False
    if side not in ('long','short'):return False
    key=identity(VERSION,today,family,symbol,side)
    if c.execute(select(trials.c.id).where(trials.c.id==key)).first():return False
    if entry_quote is None and not reason:
        entry_quote,reason=quote_at(c,symbol,at)
    targets=endpoints(at) if entry_quote else {}
    historical=db.recent(c,'daily',symbol,limit=10)
    ref=next((r for r in historical if day(r['ts'])<today and r['received']<=now and number(r['payload'].get('c'))),None)
    price_basis=reference_basis or (dict(through=day(ref['ts']),close=ref['payload']['c']) if ref else None)
    p=dict(price_basis=price_basis,id=key,version=VERSION,day=today,family=family,symbol=symbol,side=side,
        detected_at=at,registered_at=now,config_revision=activation['revision'],evidence=evidence,
        entry=entry_quote,reason=reason,status='pending' if entry_quote else 'unavailable',
        checkpoints={h:dict(target_at=targets.get(h),status='pending' if entry_quote else 'unavailable',reason=reason) for h in HORIZONS})
    c.execute(db.insert(trials).values(id=key,day=today,family=family,symbol=symbol,side=side,
        created=at,status=p['status'],next_due=min(targets.values())+10 if targets else None,payload=p).on_conflict_do_nothing(index_elements=['id']))
    return True


def schedule(db,data,now,clock=time.time):
    with db.tx() as c:
        activation=activate(db,c,now)
        if not activation:return
        sess=next((s for s in activation['sessions'] if s['day']==day(now)),None)
        if not sess:return
        key=VERSION+':day:'+sess['day'];job=db.get(c,key,dict(day=sess['day'],index=0,rows=[],state='waiting'))
    cutoff=sess['open']+3600
    if now<cutoff+300 or job['state'] in ('complete','missed'):return
    configs=activation['configs']
    if now>min(cutoff+1800,sess['close']):
        job.update(state='missed',reason='daily_processing_window_missed',unprocessed=[r['ticker'] for r in configs[job['index']:]])
    elif job['index']<len(configs):
        config=configs[job['index']];result=dict(symbol=config['ticker'],checked_at=now)
        try:
            bars=data.hourly(config['ticker'],cutoff)
            daily=data.daily(config['ticker'],cutoff)
            signal=make_signal(config,bars,daily,dict(date=sess['day'],open=sess['open']),now,sess['day'],daily_entry=True)
            observed=clock()
            if observed>min(cutoff+1800,sess['close']):raise ValueError('daily_processing_window_missed')
            if signal:
                with db.tx() as c:
                    basis=retain_daily_basis(db,c,config['ticker'],daily,observed)
                    q=db.get(c,'quote:'+config['ticker'])
                    valid=fresh(q,observed) and 0<=observed-q['ts']<=5 and q.get('received',observed)<=observed
                    price=dict(price=(q['bid']+q['ask'])/2,source_ts=q['ts'],received_at=q.get('received',observed)) if valid else None
                    add(db,c,activation,'smoothers_daily',config['ticker'],'long' if signal['direction']=='CALL' else 'short',
                        observed,observed,dict(formula=signal,cutoff=cutoff,processing_delay_seconds=observed-cutoff),price,
                        None if valid else 'no_fresh_detection_quote',reference_basis=basis)
                result.update(status='candidate',direction=signal['direction'],detected_at=observed,entry_available=valid)
            else:result['status']='no_signal'
        except Exception as error:
            result.update(status='unavailable',reason=str(error) if isinstance(error,ValueError) else type(error).__name__)
        job['rows'].append(result);job['index']+=1;job['state']='running'
        if job['index']==len(configs):job.update(state='complete',finished_at=clock())
    with db.tx() as c:db.put(c,key,job)


def capture(db,c,activation,now):
    """Retained prospective candidates; original option outcomes remain untouched."""
    from .setup_study import trials as setups
    from .option_ideas import ideas
    today=day(now)
    sess=next((s for s in activation['sessions'] if s['day']==today),None)
    if not sess:return
    allowed=[r['ticker'] for r in activation['configs']]
    for table,stamp,label in ((setups,setups.c.started,'setup'),(ideas,ideas.c.created,'options_ideas')):
        # Re-read a bounded session census; deduplication freezes the earliest record.
        # SQL prefilter avoids treating a display cap as complete coverage.
        conditions=[stamp>=sess['open'],stamp<min(now-5,sess['close'])]
        if label=='setup':conditions.append(table.c.payload['asset'].as_string().in_(('stock','option')))
        cursor_key=VERSION+':cursor:'+today+':'+label
        cursor=db.get(c,cursor_key,dict(at=sess['open'],id=''))
        from sqlalchemy import or_,and_
        conditions.append(or_(stamp>cursor['at'],and_(stamp==cursor['at'],table.c.id>cursor['id'])))
        rows=c.execute(select(table.c.id,stamp.label('stamp'),table.c.payload).where(*conditions)
            .order_by(stamp,table.c.id).limit(500)).mappings().all()
        for row in rows:
            p=row['payload']
            family=('scanner_0dte' if p.get('asset')=='option' else 'intraday_stock') if label=='setup' else label
            symbol=p.get('underlying') or p.get('symbol')
            side=p.get('underlying_side',p.get('side')) if label=='setup' else p.get('underlying_side')
            if symbol in allowed:
                add(db,c,activation,family,symbol,side,row['stamp'],now,
                    dict(source_id=row['id'],strategy=p.get('strategy'),source_state=p.get('status'),
                        basis='Underlying candidate observation; original option result remains separate'))
        if rows:db.put(c,cursor_key,dict(at=rows[-1]['stamp'],id=rows[-1]['id']))
        db.put(c,VERSION+':coverage:'+today+':'+label,dict(at=now,loaded=len(rows),catching_up=len(rows)==500))


def observe(db,c,now):
    rows=c.execute(select(trials.c.payload).where(trials.c.status=='pending',trials.c.next_due<=now).order_by(trials.c.next_due).limit(500)).scalars().all()
    cache={}
    for p in rows:
        changed=False
        for check in p['checkpoints'].values():
            target=check['target_at']
            if check['status']!='pending' or now<target+10:continue
            key=(p['symbol'],target)
            if key not in cache:cache[key]=quote_at(c,*key)
            q,reason=cache[key]
            from .swing_study import basis_reason
            reason=reason or basis_reason(c,p['symbol'],{'signal':{'daily':p.get('price_basis') or {}}},target)
            if reason:q=None
            check.update(status='completed' if q else 'unavailable',reason=reason,checked_at=now,quote=q)
            if q:check['directional_return_pct']=(q['price']/p['entry']['price']-1)*100*(1 if p['side']=='long' else -1)
            changed=True
        if changed:
            states=[r['status'] for r in p['checkpoints'].values()]
            p['status']='pending' if 'pending' in states else 'complete' if all(x=='completed' for x in states) else 'partial' if 'completed' in states else 'unavailable'
            c.execute(update(trials).where(trials.c.id==p['id']).values(status=p['status'],next_due=min((v['target_at']+10 for v in p['checkpoints'].values() if v['status']=='pending'),default=None),payload=p))


def report(db,c,now):
    a=db.get(c,VERSION+':activation')
    if not a:return dict(state='awaiting_config',protocol=PROTOCOL,groups=[],overlap=[])
    rows=c.execute(select(trials.c.payload).where(trials.c.day.in_([s['day'] for s in a['sessions']]))).scalars().all()
    days={s['day']:db.get(c,VERSION+':day:'+s['day'],{}) for s in a['sessions']}
    indexed={(p['day'],p['symbol'],p['side'],p['family']):p for p in rows}
    grouped=defaultdict(dict)
    for p in rows:grouped[(p['day'],p['symbol'],p['side'])][p['family']]=p
    groups=defaultdict(list);overlap=defaultdict(list)
    for p in rows:
        peers=[r for f,r in grouped[(p['day'],p['symbol'],p['side'])].items() if f!=p['family']]
        if p['family']=='smoothers_daily':kind='overlap' if peers else 'no_match_observed'
        else:
            smooth=indexed.get((p['day'],p['symbol'],p['side'],'smoothers_daily'))
            kind='overlap' if smooth else 'no_smoothers_match_observed'
            census=days.get(p['day'],{})
            coverage=next((r for r in census.get('rows',[]) if r['symbol']==p['symbol']),{})
            if not smooth and (census.get('state')!='complete' or coverage.get('status') not in ('candidate','no_signal')):
                kind='smoothers_census_incomplete'
            if smooth:overlap[p['family']].append(p['detected_at']-smooth['detected_at'])
        confirmation=('available_at_detection' if any(r['detected_at']<=p['detected_at'] for r in peers) else 'later_only' if peers else 'none_observed') if p['family']=='smoothers_daily' else ('available_at_detection' if smooth and smooth['detected_at']<=p['detected_at'] else 'later_only' if smooth else 'none_observed')
        for horizon,check in p['checkpoints'].items():
            groups[(p['family'],kind,confirmation,date.fromisoformat(p['day']).strftime('%A'),horizon)].append(check)
    out=[]
    for (family,kind,confirmation,weekday,horizon),checks in sorted(groups.items()):
        values=[v['directional_return_pct'] for v in checks if v['status']=='completed']
        out.append(dict(family=family,group=kind,confirmation=confirmation,weekday=weekday,horizon=horizon,records=len(checks),
            measured=len(values),pending=sum(x['status']=='pending' for x in checks),unavailable=sum(x['status']=='unavailable' for x in checks),
            mean_directional_pct=sum(values)/len(values) if values else None,positive=sum(v>0 for v in values),
            unavailable_reasons=dict(Counter(v.get('reason') or 'unspecified' for v in checks if v['status']=='unavailable'))))
    return dict(state='awaiting_first_session' if now<a['sessions'][0]['open'] else 'collecting' if now<a['sessions'][-1]['close'] else 'entry_collection_complete',next_formula_at=next((s['open']+3900 for s in a['sessions'] if s['open']+3900>now),None),activation=a,protocol=PROTOCOL,groups=out,
        records=len(rows),days=list(days.values()),overlap=[dict(family=f,matched=len(v),smoothers_earlier=sum(x>0 for x in v),
        scanner_earlier=sum(x<0 for x in v),same_time=sum(x==0 for x in v),mean_lead_seconds=sum(v)/len(v)) for f,v in sorted(overlap.items())],
        coverage={k:v for k,v in db.prefix(c,VERSION+':coverage:').items()},
        note='Underlying observations only, not option P&L or executable fills. No match observed is conditional on captured coverage. First 20 full sessions; five-session outcomes mature later. No alerts or automatic strategy promotion.')


async def run(db,cfg):
    owner=uuid.uuid4().hex
    with httpx.Client(timeout=8,follow_redirects=False) as client:
        data=Data(cfg,client)
        while True:
            now=time.time()
            try:
                with db.tx() as c:
                    leased=db.lease(c,VERSION,owner,120)
                    a=activate(db,c,now) if leased else None
                if a:
                    data.deadline=time.monotonic()+40
                    await asyncio.to_thread(schedule,db,data,now)
                    def tick():
                        at=time.time()
                        with db.tx() as c:
                            capture(db,c,a,at);observe(db,c,at)
                            result=db.get(c,VERSION+':report',{})
                            if at-result.get('asof',0)>=60:
                                result=report(db,c,at);result['asof']=at;db.put(c,VERSION+':report',result)
                            db.put(c,VERSION+':worker',dict(at=at,state=result['state'],records=result['records'],alerts=False,orders=False))
                        db.health(VERSION,'running','Quiet daily Smoothers comparison; underlying endpoints, no alerts or orders')
                    await asyncio.to_thread(tick)
                elif leased:await asyncio.to_thread(db.health,VERSION,'waiting','Awaiting Smoothers configuration')
            except asyncio.CancelledError:raise
            except Exception:
                logging.getLogger('uvicorn.error').exception('Daily Smoothers comparison failed')
                await asyncio.to_thread(db.health,VERSION,'error','Research worker failed; see runtime logs')
            await asyncio.sleep(10)
