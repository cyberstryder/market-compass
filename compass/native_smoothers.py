"""Durable weekly selector, target monitor and premium worker, all shadow-only."""
import asyncio
from datetime import datetime,timedelta
import json
import logging
import math
import time
import uuid
import httpx
import pandas as pd
from sqlalchemy import Table,Column,String,Float,JSON,select,func
from .store import meta,identity
from .native_smoothers_data import Data,ET
from .smoothers_math import compute_smoother
from .smoothers_signals import classify_signal,get_week_start_indices
from .smoothers_quality import score_signal,rank_signals
from .smoothers_factors import atr
from .smoothers_blackscholes import value_at_target
from .native_outbox import queue

VERSION='native-smoothers-v1'
weekly=Table('native_smoothers_signals_v1',meta,Column('id',String(64),primary_key=True),
    Column('week',String(10),nullable=False),Column('ticker',String(16),nullable=False),
    Column('status',String(24),nullable=False),Column('payload',JSON,nullable=False))


def monday_for(now):
    d=datetime.fromtimestamp(now,ET).date();return d-timedelta(days=d.weekday())


def refresh_config(db,cfg,client,now):
    if not cfg.smoothers_url or not cfg.smoothers_token:return
    with db.tx() as c:
        old=db.get(c,VERSION+':config',{})
    if now-old.get('received_at',0)<3600:return
    r=client.get(cfg.smoothers_url.rstrip('/')+'/api/compass/config',headers={'Authorization':'Bearer '+cfg.smoothers_token})
    if r.status_code!=200:raise ValueError('Source config HTTP '+str(r.status_code))
    if len(r.content)>2*1024*1024:raise ValueError('Source config too large')
    data=r.json()
    if data.get('schema_version')!=1 or data.get('project')!='smoothers' or data.get('mode')!='observe_only':raise ValueError('Invalid config source')
    configs=data.get('configs')
    if not isinstance(configs,list) or not configs or len(configs)>500:raise ValueError('Invalid config inventory')
    import re
    tickers=set()
    for row in configs:
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}',row['ticker']) or row['ticker'] in tickers:raise ValueError('Invalid/duplicate ticker')
        tickers.add(row['ticker'])
        if any(not 1<=int(row[k])<=500 for k in ('s1','s2','s3')) or not 0<float(row['pm'])<1:raise ValueError('Invalid smoother inputs')
        if type(row['enabled']) is not bool:raise ValueError('Invalid enabled flag')
    stamp=identity(data['configs'],data.get('alltime',[]))
    with db.tx() as c:
        db.put(c,VERSION+':config',dict(data,received_at=now,revision=stamp))
        db.append(c,'native_config','smoothers','',now,data,identity(VERSION,'config',stamp))


def make_signal(config,bars,daily,first_session,now,week):
    if bars.empty or len(bars)<4*max(int(config[k]) for k in ('s1','s2','s3'))+10:
        raise ValueError('insufficient_hourly_history')
    starts=get_week_start_indices(bars)
    if not len(starts):raise ValueError('missing_week_start')
    idx=int(starts[-1]);stamp=bars.index[idx]
    if stamp.tz_convert(ET).date().isoformat()!=first_session['date']:raise ValueError('wrong_entry_session')
    if stamp.timestamp()!=first_session['open']:raise ValueError('missing_opening_hour')
    if 'parts' in bars and bars['parts'].iloc[idx]!=2:raise ValueError('incomplete_opening_hour')
    dirs=[compute_smoother(bars,int(config[k])).iloc[idx] for k in ('s1','s2','s3')]
    if any(pd.isna(v) for v in dirs):raise ValueError('smoother_warmup')
    kind,direction=classify_signal(*[int(v) for v in dirs])
    if kind is None:return None
    entry=float(bars['close'].iloc[idx]);pm=float(config['pm'])
    target=entry*(1+pm if direction=='CALL' else 1-pm)
    history=daily[daily.index<pd.Timestamp(first_session['date'])] if not daily.empty else daily
    av=atr(history).iloc[-1] if not history.empty else None
    av=None if av is None or pd.isna(av) else float(av)
    return dict(id=identity(VERSION,week,config['ticker']),ticker=config['ticker'],week=week,
        direction=direction,signal_type=kind,entry_price=entry,target_price=round(target,4),
        pm=pm,model_entry_time=first_session['open']+3600,created_at=now,
        config=config,atr_14d=av,target_atr_mult=round(abs(target-entry)/av,3) if av else None,
        entry_bar={k:float(bars[k].iloc[idx]) for k in ('open','high','low','close','volume')},
        smoother_directions=[int(v) for v in dirs],hourly_input_count=len(bars),
        hourly_input_checksum=identity(bars.to_json(date_format='iso')),
        status='OPEN',last_seen_at=None,last_underlying=None,contract=None,entry_premium=None,
        quote=None,est_return_pct=None,version=VERSION,mode='shadow')


def save(db,c,p):
    c.execute(db.insert(weekly).values(id=p['id'],week=p['week'],ticker=p['ticker'],status=p['status'],payload=p)
              .on_conflict_do_update(index_elements=['id'],set_={'status':p['status'],'payload':p}))


def schedule(db,data,now):
    monday=monday_for(now);key=VERSION+':week:'+monday.isoformat()
    with db.tx() as c:
        state=db.get(c,key)
    if state is None:
        sessions=data.calendar(monday)
        state={'week':monday.isoformat(),'sessions':sessions,'state':'waiting','index':0,'errors':[]}
        with db.tx() as c:db.put(c,key,state)
    if not state['sessions']:return
    first=state['sessions'][0]
    if now<first['open']+3900:return
    if state['state'] in ('complete','partial','missed'):return
    if now>first['open']+7200:
        state['state']='missed'
        with db.tx() as c:db.put(c,key,state)
        return
    if 'config' not in state:
        with db.tx() as c: config=db.get(c,VERSION+':config',{})
        if now-config.get('received_at',0)>7200:return
        state['config']=config;state['started']=now;state['state']='running'
        with db.tx() as c:db.put(c,key,state)
    configs=[r for r in state['config']['configs'] if r['enabled']]
    if state['index']<len(configs):
        config=configs[state['index']]
        try:
            # Freeze price inputs at the first completed session hour, independent
            # of how long the job takes to process the universe.
            cutoff=first['open']+3600
            bars=data.hourly(config['ticker'],cutoff)
            daily=data.daily(config['ticker'],cutoff)
            signal=make_signal(config,bars,daily,first,now,monday.isoformat())
            if signal:
                try:
                    contract=data.contract(config['ticker'],monday+timedelta(days=4),signal['direction'],signal['entry_price'])
                    q=data.quote(contract) if contract else None
                    signal.update(contract=contract,quote=q)
                    if q and q.get('status') in ('available','wide_spread'):
                        signal['entry_premium']=q['midpoint']
                        expiry=datetime.fromisoformat(contract['expiration']+'T16:00:00').replace(tzinfo=ET).timestamp()
                        model=value_at_target(q['midpoint'],q['midpoint'],signal['entry_price'],contract['strike'],signal['target_price'],max(expiry-now,0)/(365*86400),signal['direction'])
                        signal['est_return_pct']=(model['value_at_target']/q['midpoint']-1)*100
                except Exception as e:signal['option_error']=type(e).__name__
                stats=next((x for x in state['config'].get('alltime',[]) if x['ticker']==config['ticker']),{})
                signal.update(score_signal(target_atr_mult=signal['target_atr_mult'],alltime_wins=stats.get('wins',0),alltime_losses=stats.get('losses',0),
                    backtest_wr=config.get('backtest_wr'),entry_premium=signal['entry_premium'],est_return_pct=signal['est_return_pct']))
                signal['stats_at_entry']=stats;signal['config_revision']=state['config']['revision']
                with db.tx() as c:save(db,c,signal)
        except Exception as e:state['errors'].append({'ticker':config['ticker'],'reason':type(e).__name__})
        state['index']+=1
        with db.tx() as c:db.put(c,key,state)
        return
    with db.tx() as c:
        signals=list(c.execute(select(weekly.c.payload).where(weekly.c.week==monday.isoformat()).with_for_update()).scalars())
        ranked=rank_signals(signals)
        for p in ranked:
            save(db,c,p)
            if p['is_featured']:queue(db,c,'smoothers',p['id']+':entry',{'content':f"SMOOTHERS | {p['ticker']} {p['direction']} | reference {p['entry_price']:.4f} target {p['target_price']:.4f}",'allowed_mentions':{'parse':[]}},now)
        queue(db,c,'smoothers',monday.isoformat()+':summary',{'content':f"Weekly cohort: {len(ranked)} signals; {len(state['errors'])} errors; shadow only",'allowed_mentions':{'parse':[]}},now)
        state.update(state='partial' if state['errors'] else 'complete',finished=now,signals=len(ranked))
        db.put(c,key,state)


def monitor(db,data,now):
    monday=monday_for(now).isoformat()
    with db.tx() as c:
        week=db.get(c,VERSION+':week:'+monday,{})
        rows=list(c.execute(select(weekly.c.payload).where(weekly.c.week==monday,weekly.c.status=='OPEN')).scalars())
    sessions=week.get('sessions',[])
    active=next((s for s in sessions if s['open']+60<=now<=s['close']+300),None)
    end=now//60*60
    rows=sorted(rows,key=lambda p:p.get('target_checked_at',0))
    starts={p['id']:max(p['model_entry_time'],(p.get('last_seen_at') or p['model_entry_time'])-120) for p in rows}
    batches={};errors={}
    if active:
        for offset in range(0,len(rows),20):
            group=rows[offset:offset+20]
            try:
                batches.update(data.bar_batch([p['ticker'] for p in group],'1Min',min(starts[p['id']] for p in group),end))
            except Exception as e:
                errors.update({p['ticker']:type(e).__name__ for p in group})
    for p in rows:
        if not active:continue
        start=starts[p['id']]
        bars=batches.get(p['ticker'],pd.DataFrame())
        if not bars.empty:bars=bars[bars.index>=pd.Timestamp(start,unit='s',tz='UTC')]
        if bars.empty:
            with db.tx() as c:
                current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
                current.update(target_checked_at=now,coverage_missing=True,target_error=errors.get(p['ticker'],'empty_completed_bars'))
                save(db,c,current)
            continue
        bars=bars[bars.index+pd.Timedelta(minutes=1)<=pd.Timestamp(end,unit='s',tz='UTC')]
        bars=bars[[any(s['open']<=ts.timestamp()<s['close'] for s in sessions) for ts in bars.index]]
        if bars.empty:continue
        expected=set()
        for session in sessions:
            expected.update(range(int(max(start,session['open'])//60)*60,int(min(end,session['close'])//60)*60,60))
        missing=expected-{int(t.timestamp()) for t in bars.index}
        p['coverage_missing']=p.get('coverage_missing',False) or bool(missing)
        hits=bars[bars['high']>=p['target_price']] if p['direction']=='CALL' else bars[bars['low']<=p['target_price']]
        p.update(target_error=None,last_seen_at=bars.index[-1].timestamp(),last_underlying=float(bars['close'].iloc[-1]),target_checked_at=now)
        if not hits.empty:
            hit=hits.iloc[0];p.update(status='WIN',resolution_time=hits.index[0].timestamp(),exit_underlying=p['target_price'],
                touch_bar={k:float(hit[k]) for k in ('open','high','low','close')},touch_detected_at=now)
        with db.tx() as c:
            current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
            if current['status']!='OPEN':continue
            current.update({k:p[k] for k in ('status','resolution_time','exit_underlying','touch_bar','touch_detected_at','last_seen_at','last_underlying','target_checked_at','coverage_missing','target_error') if k in p})
            p=current
            save(db,c,p)
            db.append(c,'native_target_check','smoothers',p['ticker'],now,{'id':p['id'],'from':start,'through':end,'missing_minutes':len(missing),'last_seen_at':p['last_seen_at'],'status':p['status']})
            if p['status']=='WIN':queue(db,c,'smoothers',p['id']+':target',{'content':f"SMOOTHERS | TARGET HIT | {p['ticker']} | trigger {p['target_price']} | observed 1m close {p['touch_bar']['close']}",'allowed_mentions':{'parse':[]}},now)
    # Recover overdue closures after a restart even when the calendar week changed.
    with db.tx() as c:
        open_weeks=c.execute(select(weekly.c.week).where(weekly.c.status=='OPEN').distinct()).scalars().all()
        for open_week in open_weeks:
            closing=db.get(c,VERSION+':week:'+open_week,{}).get('sessions',[])
            if not closing or now<closing[-1]['close']+300:continue
            for p in c.execute(select(weekly.c.payload).where(weekly.c.week==open_week,weekly.c.status=='OPEN').with_for_update()).scalars().all():
                # An observed target is not an option fill or option profit.
                p.update(status='UNRESOLVED' if p.get('coverage_missing') or (p.get('last_seen_at') or 0)<closing[-1]['close']-60 else 'LOSS',resolution_time=now,exit_underlying=None,closure_basis='weekly target not observed; closing price unavailable')
                save(db,c,p)
                queue(db,c,'smoothers',p['id']+':close',{'content':f"SMOOTHERS | WEEK CLOSED | {p['ticker']} | target not observed; option return unverified",'allowed_mentions':{'parse':[]}},now)


def premium(db,data,now):
    monday=monday_for(now).isoformat()
    with db.tx() as c:
        week=db.get(c,VERSION+':week:'+monday,{})
        rows=list(c.execute(select(weekly.c.payload).where(weekly.c.week==monday,weekly.c.status=='OPEN')).scalars())
    if not any(s['open']<=now<s['close'] for s in week.get('sessions',[])):return
    rows=sorted((p for p in rows if p.get('contract') and p.get('entry_premium') and p.get('last_underlying') and now-p.get('premium_checked_at',0)>=3300),key=lambda p:p.get('premium_attempt_at',0))[:100]
    quotes=data.options.snapshots([p['contract'] for p in rows]) if rows else {}
    for p in rows:
        try:q=quotes.get(p['contract']['symbol'],{'status':'unavailable'})
        except Exception:q={'status':'unavailable'}
        with db.tx() as c:
            current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
            current['premium_attempt_at']=now
            save(db,c,current)
        if q.get('status') not in ('available','wide_spread'):continue
        expiration=p['contract']['expiration']
        expiry=next((s['close'] for s in week['sessions'] if s['date']==expiration),None)
        if expiry is None:continue
        model=value_at_target(p['entry_premium'],q['midpoint'],p['last_underlying'],p['contract']['strike'],p['target_price'],max(0,expiry-now)/(365*86400),p['direction'])
        # Merge only premium fields so target monitor cannot be overwritten by a stale read.
        with db.tx() as c:
            current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
            current.update(premium_checked_at=now,last_quote=q,premium_model=model)
            save(db,c,current)
            db.append(c,'native_premium','smoothers',p['ticker'],now,{'id':p['id'],'quote':q,'model':model})


async def run(db,cfg,role='schedule'):
    owner=uuid.uuid4().hex;last_log=0
    with httpx.Client(timeout=8,follow_redirects=False) as client:
        data=Data(cfg,client)
        while True:
            now=time.time();failed=False
            try:
                with db.tx() as c:active=db.lease(c,VERSION+':'+role,owner,180)
                if active and cfg.alpaca_key and cfg.alpaca_secret:
                    data.deadline=time.monotonic()+60
                    if role=='schedule':
                        await asyncio.to_thread(refresh_config,db,cfg,client,now)
                        await asyncio.to_thread(schedule,db,data,now)
                    elif role=='target':await asyncio.to_thread(monitor,db,data,now)
                    else:await asyncio.to_thread(premium,db,data,now)
                    with db.tx() as c:db.put(c,VERSION+':worker:'+role,{'at':time.time(),'status':'shadow','sending_enabled':False})
                    if now-last_log>=60:
                        with db.tx() as c:
                            config=db.get(c,VERSION+':config',{})
                            job=db.get(c,VERSION+':week:'+monday_for(now).isoformat(),{})
                        logging.getLogger('uvicorn.error').info('Native Smoothers: role=%s configs=%s week=%s state=%s processed=%s errors=%s sending=false',role,len(config.get('configs',[])),job.get('week'),job.get('state'),job.get('index',0),len(job.get('errors',[])))
                        db.health(VERSION+'_'+role,'shadow','Native worker active; official sender unchanged')
                        last_log=now
            except Exception as e:
                failed=True
                db.health(VERSION+'_'+role,'error',type(e).__name__)
                logging.getLogger('uvicorn.error').warning('Native Smoothers %s: %s',role,type(e).__name__)
            await asyncio.sleep(30 if failed else 2 if role=='schedule' else max(1,60-(time.time()-15)%60) if role=='target' else max(1,300-(time.time()-35)%300))
