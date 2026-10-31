"""Durable weekly observations; official delivery requires accepted ownership."""
import asyncio
from datetime import datetime,timedelta
import json
import logging
import math
import time
import uuid
import httpx
import pandas as pd
# Tickers dropped 2026-10-03: consistent money losers (<=40% WR, negative avg return over 5w)
DROPPED_TICKERS={'COST','WMT','BA','MSFT','TSLA','LLY','AMZN','QCOM','HOOD'}
# Best 10 by avg return % (5w): get individual detailed alerts, rest get compact format
# META added 2026-10-03 per Josh (fast 0.3d, 100% WR, benefits from 3 DTE)
# 2026-10-31 (Smoothers plan): BEST_10 no longer selects the detailed-alert
# set — the shadow board's top 10 by executable cost does (schedule()).
# Retained only as the historical record of the old selection rule.
BEST_10={'AMD','XLE','IWM','GOOGL','NFLX','COP','MCD','MAR','WFC','PLTR','META'}
# DTE overrides: 14 DTE for slow movers, 2-3 DTE for fast hitters with short expiries available
DTE_14={'SMCI','UBER'}  # 2.5-3.0d avg to target, need more time
DTE_SHORT={'IWM':2,'META':3,'GOOGL':3}  # IWM has dailies, META/GOOGL have Mon/Wed/Fri
# Human entry window for the Discord entry alert (was 120s, built for a bot;
# Josh chose 30 minutes 2026-10-05 so the alert stays actionable while read).
ENTRY_WINDOW_SECS=1800
from sqlalchemy import Table,Column,String,Float,JSON,select,func
from .store import meta,identity,events
from .chain_eod import event_key as chain_event_key
from .native_smoothers_data import Data,ET
from .smoothers_math import compute_smoother
from .smoothers_signals import classify_signal,get_week_start_indices
from .smoothers_quality import score_signal,rank_signals
from . import smoothers_cost
from .smoothers_factors import atr
from .smoothers_blackscholes import value_at_target
from .native_outbox import queue,owner as delivery_owner
from .native_config import bootstrap, effective
from . import smoothers_messages as messages

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
    if old.get('owner')=='compass':return
    if now-old.get('received_at',0)<3600:
        bootstrap(db,now)
        return
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
        current=db.locked_get(c,VERSION+':config',{})
        if current.get('owner')=='compass':return
        db.put(c,VERSION+':config',dict(data,received_at=now,revision=stamp))
        db.append(c,'native_config','smoothers','',now,data,identity(VERSION,'config',stamp))
    bootstrap(db,now)


def make_signal(config,bars,daily,first_session,now,week,*,daily_entry=False):
    if bars.empty or len(bars)<4*max(int(config[k]) for k in ('s1','s2','s3'))+10:
        raise ValueError('insufficient_hourly_history')
    if daily_entry:
        starts=[i for i,t in enumerate(bars.index) if t.timestamp()==first_session['open']]
    else:
        starts=get_week_start_indices(bars)
    if not len(starts):raise ValueError('missing_opening_hour' if daily_entry else 'missing_week_start')
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


def rolling_record(db,c,ticker,week,owned_since):
    """Use retained source history before ownership, then independent native weeks."""
    from .projects import records
    resolved={}
    for p in c.execute(select(records.c.payload).where(records.c.project=='smoothers',records.c.symbol==ticker)).scalars():
        original=p.get('original') or {};cohort=str(original.get('monday_date') or '')[:10]
        if cohort and cohort<week and p.get('source_ts',float('inf'))<owned_since and original.get('status') in ('WIN','LOSS'):
            resolved[cohort]=original['status']
    for p in c.execute(select(weekly.c.payload).where(weekly.c.ticker==ticker,weekly.c.week<week)).scalars():
        if p.get('created_at',0)>=owned_since and p['status'] in ('WIN','LOSS'):resolved[p['week']]=p['status']
    outcomes=[resolved[w] for w in sorted(resolved,reverse=True)[:4]]
    return {'wins':outcomes.count('WIN'),'losses':outcomes.count('LOSS'),'observed_weeks':len(outcomes)} if outcomes else None


def queue_signal(db,c,p,event,payload,now,event_time):
    q=p.get('quote' if event=='entry' else 'exit_quote') or {}
    live=p.get('live_underlying') or {}
    publication=dict(id=p['id'],project='smoothers',contract=p.get('contract'),track='swing',
        status='native_option_entry' if event=='entry' else 'native_option_exit',
        entry=p.get('entry_premium'),underlying_target=p.get('target_price'),
        entry_price=p.get('entry_price'),target_price=p.get('target_price'),
        model_entry_time=p.get('model_entry_time'),
        live_underlying=dict(status=live.get('status'),price=live.get('price'),
            ts=(live.get('trade_at_ms') or 0)/1000),
        est_return_pct=p.get('est_return_pct'),week=p.get('week'),
        signal_status=p.get('status'),exit_underlying=p.get('exit_underlying'),
        quote=dict(bid=q.get('bid'),ask=q.get('ask'),ts=(q.get('quote_at_ms') or 0)/1000),
        # Shadow-board fields (2026-10-31): the delivery renderer shows
        # board cost on entries and the Wednesday-horizon outcome on
        # exits, driven by these publication values.
        cost_rank=p.get('cost_rank'),cost_key=p.get('cost_key'),
        entry_spread_pct=p.get('entry_spread_pct'),entry_rent_pct_day=p.get('entry_rent_pct_day'),
        friction=bool(p.get('friction')),friction_reason=p.get('friction_reason'),
        horizon=p.get('horizon'),
        reason='Weekly Smoothers underlying target / Wednesday-close horizon',
        exit_reason=event if event!='entry' else None,expires_at=now+ENTRY_WINDOW_SECS,
        exit_rule='Underlying target, otherwise Wednesday-close horizon scoring; no premium stop configured',
        outcome='unresolved' if event!='entry' else None)
    return queue(db,c,'smoothers',p['id']+':'+event,payload,now,
                 event_time=event_time,cohort_time=p.get('model_entry_time'),publication=publication)


def refresh_entry_quotes(signals,data,now):
    """Freeze the final cohort against quotes fetched together at publication."""
    # Live underlying for the print, fetched in the same publication batch
    # as the option quotes. A failed or stale read leaves live_underlying
    # unavailable and the alerts fall back to the labeled entry reference
    # alone; it never blocks or delays the batch.
    tickers=sorted({p['ticker'] for p in signals if p.get('ticker')})
    if tickers:
        try:live=data.stock_snapshots(tickers)
        except Exception as exc:
            live={t:{'status':'unavailable','reason':type(exc).__name__} for t in tickers}
        for p in signals:
            if p.get('ticker') in live:p['live_underlying']=live[p['ticker']]
    contracts=[p['contract'] for p in signals if p.get('contract')]
    if not contracts:return signals
    try:quotes=data.quotes(contracts)
    except Exception as exc:
        quotes={c['symbol']:{'status':'unavailable','reason':type(exc).__name__} for c in contracts}
    for p in signals:
        if not p.get('contract'):continue
        p.setdefault('selection_option_observation',dict(quote=p.get('quote'),entry_premium=p.get('entry_premium'),est_return_pct=p.get('est_return_pct')))
        q=quotes.get(p['contract']['symbol'],{'status':'unavailable','reason':'missing_batch_quote'})
        p.update(quote=q,entry_premium=None,est_return_pct=None,entry_quote_refreshed_at=now)
        if q.get('status') in ('available','wide_spread') and q.get('midpoint',0)>0:
            p['entry_premium']=q['midpoint']
            expiry=datetime.fromisoformat(p['contract']['expiration']+'T16:00:00').replace(tzinfo=ET).timestamp()
            model=value_at_target(q['midpoint'],q['midpoint'],p['entry_price'],p['contract']['strike'],p['target_price'],max(expiry-now,0)/(365*86400),p['direction'])
            p['est_return_pct']=(model['value_at_target']/q['midpoint']-1)*100
        stats=p.get('stats_at_entry') or {}
        p.update(score_signal(target_atr_mult=p.get('target_atr_mult'),alltime_wins=stats.get('wins',0),alltime_losses=stats.get('losses',0),
            backtest_wr=p['config'].get('backtest_wr'),entry_premium=p['entry_premium'],est_return_pct=p['est_return_pct']))
    return signals


def compact_line(p):
    """One-liner for the compact batch: same labeled reference + live
    underlying segment as the roster (messages.stock_text), plus the
    shadow-board cost segment (spread / rent / cost + friction flag)
    in place of the retired quality score."""
    return (f"{p['ticker']} {p['direction']} {messages.stock_text(p)}"
            f" → ${p['target_price']:.2f} · {messages.cost_text(p)}{messages.friction_text(p)}")


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
        with db.tx() as c:
            config=db.get(c,VERSION+':config',{})
            if config.get('owner')=='compass':config=effective(db,c,config,monday.isoformat())
        if not config.get('configs') or (config.get('owner')!='compass' and now-config.get('received_at',0)>7200):return
        state['config']=config;state['started']=now;state['state']='running';state['message_version']=messages.VERSION
        with db.tx() as c:db.put(c,key,state)
    configs=[r for r in state['config']['configs'] if r['enabled'] and r['ticker'] not in DROPPED_TICKERS]
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
                    # DTE selection: 14 DTE for slow movers, 2-3 DTE for fast hitters, else standard Friday (4d)
                    ticker=config['ticker']
                    if ticker in DTE_14:
                        expiry_date=monday+timedelta(days=11)  # Next Friday (14 DTE)
                    elif ticker in DTE_SHORT:
                        expiry_date=monday+timedelta(days=DTE_SHORT[ticker])  # 2-3 DTE
                    else:
                        expiry_date=monday+timedelta(days=4)  # Standard Friday (5-7 DTE)
                    contract=data.contract(ticker,expiry_date,signal['direction'],signal['entry_price'])
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
                signal['message_version']=messages.VERSION
                # Attach historical conviction stats for alert display
                try:
                    from .smoothers_history import get_ticker_stats
                    signal['historical_stats']=get_ticker_stats(db, config['ticker'], monday.isoformat(), state['config'].get('owned_since', float('inf')))
                except Exception:
                    signal['historical_stats']={}
                with db.tx() as c:
                    signal['rolling_at_entry']=rolling_record(db,c,config['ticker'],monday.isoformat(),state['config'].get('owned_since',float('inf')))
                    save(db,c,signal)
        except Exception as e:state['errors'].append({'ticker':config['ticker'],'reason':type(e).__name__})
        state['index']+=1
        with db.tx() as c:db.put(c,key,state)
        return
    with db.tx() as c:
        signals=list(c.execute(select(weekly.c.payload).where(weekly.c.week==monday.isoformat())).scalars())
    signals=refresh_entry_quotes(signals,data,now)
    with db.tx() as c:
        # The monitor may resolve a signal while the batch quotes are fetched.
        # Merge only entry fields, preserving its target/premium observations.
        entry_fields=('quote','entry_premium','est_return_pct','entry_quote_refreshed_at','selection_option_observation','live_underlying',
                      'quality_version','quality_score','quality_atr_score','quality_reliability_score','quality_option_adjustment')
        for p in signals:
            current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
            current.update({k:p[k] for k in entry_fields if k in p});p.update(current)
        ranked=rank_signals(signals)
        # Shadow board (2026-10-31): annotate executable cost from the
        # publication-batch entry quotes and order the board by it. The
        # quality tiers rank_signals assigned stay stored for audit; the
        # board, the detailed-alert set, and the compact batches all
        # follow cost order (ranked first, quote-missing last).
        ranked=smoothers_cost.annotate(ranked)
        for p in ranked:
            save(db,c,p)
            # Top 10 by cost get individual detailed alerts; rest compact.
            if p.get('cost_rank') and p['cost_rank']<=10:
                queue_signal(db,c,p,'entry',messages.entry(p),now,p['model_entry_time'])
        # Compact format for the rest of the board (split for Discord
        # 2000-char limit; batches of 20 since lines grew with the
        # ref/live stock segment)
        compact=[p for p in ranked if not (p.get('cost_rank') and p['cost_rank']<=10)]
        for i in range(0, len(compact), 20):
            batch=compact[i:i+20]
            lines=[compact_line(p) for p in batch]
            msg=f"SMOOTHERS — SHADOW BOARD (compact {i//20+1}/{(len(compact)+19)//20}) | {monday.isoformat()} · scored only · not a buy list\n" + " | ".join(lines)
            queue(db,c,'smoothers',f"{monday.isoformat()}:compact:{i//20}",{'content':msg},now,event_time=first['open']+3600,cohort_time=first['open']+3600)
        previews=messages.roster(ranked,monday.isoformat(),now,len(state['errors']))
        for i,payload in enumerate(previews):
            event_key=monday.isoformat()+':summary'+(':'+str(i+1) if i else '')
            queue(db,c,'smoothers',event_key,payload,now,event_time=first['open']+3600,cohort_time=first['open']+3600)
        state['summary_messages']=len(previews)
        state.update(state='partial' if state['errors'] else 'complete',finished=now,signals=len(ranked))
        db.put(c,key,state)


def wednesday_session(sessions):
    """The week's Wednesday session: the session dated Monday+2 when the
    calendar has one, else the third session (holiday-shortened weeks),
    else None — a week with no third session gets no horizon scoring."""
    if not sessions:return None
    try:monday=datetime.fromisoformat(sessions[0]['date']).date()
    except (KeyError,TypeError,ValueError):return None
    wed=(monday+timedelta(days=2)).isoformat()
    for s in sessions:
        if s.get('date')==wed:return s
    return sessions[2] if len(sessions)>=3 else None


def horizon_score(c,p,wed,now):
    """Premium-score one still-open pick out at Wednesday's chain close.

    Reads the chain_eod event for (Wednesday, ticker) from the events
    table — server-side code reads the archive natively; no HTTP surface
    is involved. Returns the horizon dict to store on the payload, or
    None when the Wednesday snapshot is not stored yet and the caller
    should retry on a later pass. A price is never fabricated: a stored
    snapshot without the pick's contract, a missing bid, or an unusable
    entry ask records an explicit 'unavailable' outcome with its reason
    (the pick remains tracked for touches either way)."""
    from .smoothers_scorecard import FEE_PER_CONTRACT
    base={'date':wed['date'],'scored_at':now}
    contract=p.get('contract') or {}
    if not contract.get('symbol'):
        return {**base,'status':'unavailable','reason':'no_contract_recorded'}
    row=c.execute(select(events.c.payload).where(events.c.key==chain_event_key(wed['date'],p['ticker']))).first()
    if row is None:
        # The snapshot job fires after Wednesday's close; give it a day
        # before concluding the archive will never have this symbol.
        if now<wed['close']+36*3600:return None
        return {**base,'status':'unavailable','reason':'wednesday_chain_missing'}
    payload=row[0] or {}
    match=next((r for r in payload.get('contracts',[]) if r.get('symbol')==contract['symbol']),None)
    if match is None:
        return {**base,'status':'unavailable','reason':'contract_not_in_wednesday_chain'}
    bid=(match.get('quote') or {}).get('bid')
    ask=(p.get('quote') or {}).get('ask')
    try:bid=float(bid)
    except (TypeError,ValueError):bid=None
    try:ask=float(ask)
    except (TypeError,ValueError):ask=None
    if bid is None or bid<0:
        return {**base,'status':'unavailable','reason':'chain_bid_missing'}
    if ask is None or ask<=0:
        return {**base,'status':'unavailable','reason':'entry_ask_missing'}
    if contract.get('multiplier')!=100:
        return {**base,'status':'unavailable','reason':'unsupported_contract_multiplier'}
    net=round((bid-ask)*100-FEE_PER_CONTRACT,2)
    return {**base,'status':'scored','chain_bid':bid,'entry_ask':ask,
            'premium_net':net,'premium_pct':round((bid/ask-1)*100,2),'fee':FEE_PER_CONTRACT}


def score_horizon(db,p,wed,now):
    """Persist one pick's horizon outcome (own transaction, row-locked)
    and queue its WEDNESDAY HORIZON notice. Returns True when scored."""
    with db.tx() as c:
        current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
        if current['status']!='OPEN' or current.get('horizon'):return False
        h=horizon_score(c,current,wed,now)
        if h is None:return False
        current['horizon']=h;save(db,c,current);p.update(current)
        queue(db,c,'smoothers',p['id']+':horizon',messages.horizon(p),now,
              event_time=wed['close'],cohort_time=p.get('model_entry_time'))
    return True


def monitor(db,data,now):
    monday=monday_for(now).isoformat()
    with db.tx() as c:
        week=db.get(c,VERSION+':week:'+monday,{})
        rows=list(c.execute(select(weekly.c.payload).where(weekly.c.week==monday,weekly.c.status=='OPEN')).scalars())
    sessions=week.get('sessions',[])
    active=next((s for s in sessions if s['open']+60<=now<=s['close']+300),None)
    end=now//60*60
    rows=sorted(rows,key=lambda p:p.get('target_checked_at',0))
    starts={p['id']:max(p['model_entry_time'],
        p.get('coverage_missing_since',p['model_entry_time']) if p.get('coverage_missing')
        else (p.get('last_seen_at') or p['model_entry_time'])-120) for p in rows}
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
                current.update(target_checked_at=now,coverage_missing=True,
                    coverage_missing_since=min(start,current.get('coverage_missing_since') or start),
                    target_error=errors.get(p['ticker'],'empty_completed_bars'))
                save(db,c,current)
            continue
        bars=bars[bars.index+pd.Timedelta(minutes=1)<=pd.Timestamp(end,unit='s',tz='UTC')]
        bars=bars[[any(s['open']<=ts.timestamp()<s['close'] for s in sessions) for ts in bars.index]]
        if bars.empty:continue
        expected=set()
        for session in sessions:
            expected.update(range(int(max(start,session['open'])//60)*60,int(min(end,session['close'])//60)*60,60))
        missing=expected-{int(t.timestamp()) for t in bars.index}
        p['coverage_missing']=bool(missing)
        p['coverage_missing_since']=min(missing) if missing else None
        hits=bars[bars['high']>=p['target_price']] if p['direction']=='CALL' else bars[bars['low']<=p['target_price']]
        p.update(target_error=None,last_seen_at=bars.index[-1].timestamp(),last_underlying=float(bars['close'].iloc[-1]),target_checked_at=now)
        if not hits.empty:
            hit=hits.iloc[0];p.update(status='WIN',resolution_time=hits.index[0].timestamp(),exit_underlying=p['target_price'],
                touch_bar={k:float(hit[k]) for k in ('open','high','low','close')},touch_detected_at=now)
            # Capture at observation time, never substitute an old hourly premium.
            try:p['exit_quote']=data.quote(p['contract']) if p.get('contract') else {'status':'unavailable','reason':'no_contract'}
            except Exception as e:p['exit_quote']={'status':'unavailable','reason':type(e).__name__}
        with db.tx() as c:
            current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
            if current['status']!='OPEN':continue
            current.update({k:p[k] for k in ('status','resolution_time','exit_underlying','touch_bar','touch_detected_at','exit_quote','last_seen_at','last_underlying','target_checked_at','coverage_missing','coverage_missing_since','target_error') if k in p})
            p=current
            save(db,c,p)
            db.append(c,'native_target_check','smoothers',p['ticker'],now,{'id':p['id'],'from':start,'through':end,'missing_minutes':len(missing),'last_seen_at':p['last_seen_at'],'status':p['status']})
            if p['status']=='WIN':queue_signal(db,c,p,'target',messages.target(p),now,p['resolution_time']+60)
    # Wednesday horizon (2026-10-31): picks still OPEN after Wednesday's
    # close are premium-scored out at Wednesday's chain bid. The pick
    # itself stays OPEN — Thu/Fri target touches keep resolving as
    # statistics — only the premium outcome is fixed at the horizon.
    wed=wednesday_session(sessions)
    if wed and now>wed['close']+300:
        with db.tx() as c:
            open_rows=list(c.execute(select(weekly.c.payload).where(weekly.c.week==monday,weekly.c.status=='OPEN')).scalars())
        for p in open_rows:
            if not p.get('horizon'):
                try:score_horizon(db,p,wed,now)
                except Exception:logging.getLogger('uvicorn.error').exception('native_smoothers horizon scoring failed for %s',p.get('id'))
    # Recover overdue closures after a restart even when the calendar week changed.
    overdue=[]
    with db.tx() as c:
        open_weeks=c.execute(select(weekly.c.week).where(weekly.c.status=='OPEN').distinct()).scalars().all()
        for open_week in open_weeks:
            closing=db.get(c,VERSION+':week:'+open_week,{}).get('sessions',[])
            if not closing or now<closing[-1]['close']+300:continue
            for p in c.execute(select(weekly.c.payload).where(weekly.c.week==open_week,weekly.c.status=='OPEN')).scalars().all():
                overdue.append((closing,p))
    for closing,p in overdue:
        # A pick that never got its Wednesday horizon scored while its
        # week was current (engine down Wed–Fri) is scored now, before
        # the closure writes its final state.
        wed=wednesday_session(closing)
        if wed and not p.get('horizon'):
            try:score_horizon(db,p,wed,now)
            except Exception:logging.getLogger('uvicorn.error').exception('native_smoothers horizon scoring failed for %s',p.get('id'))
        # Capture at resolution time, never substitute an old hourly premium:
        # the same quote capture the target-touch path uses, so a LOSS or
        # UNRESOLVED week is measurable (or visibly unmeasurable) instead of
        # silently carrying no exit quote and no option P&L.
        try:p['exit_quote']=data.quote(p['contract']) if p.get('contract') else {'status':'unavailable','reason':'no_contract'}
        except Exception as e:p['exit_quote']={'status':'unavailable','reason':type(e).__name__}
        # An observed target is not an option fill or option profit.
        final_seen=(p.get('last_seen_at') or 0)==closing[-1]['close']-60
        p.update(status='UNRESOLVED' if p.get('coverage_missing') or not final_seen else 'LOSS',resolution_time=closing[-1]['close']+300,
                 exit_underlying=p.get('last_underlying') if final_seen else None,closed_detected_at=now,
                 closure_basis='Completed final regular-session minute close; option return unverified')
        with db.tx() as c:
            current=c.execute(select(weekly.c.payload).where(weekly.c.id==p['id']).with_for_update()).scalar_one()
            if current['status']!='OPEN':continue
            current.update({k:p[k] for k in ('status','resolution_time','exit_underlying','closed_detected_at','closure_basis','exit_quote') if k in p})
            p=current
            save(db,c,p)
            queue_signal(db,c,p,'close',messages.close(p),now,p['resolution_time'])


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
            current['premium_last_attempt_quote']=q
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
                        with db.tx() as c:owned=db.get(c,VERSION+':config',{}).get('owner')=='compass'
                        if not owned:await asyncio.to_thread(refresh_config,db,cfg,client,now)
                        await asyncio.to_thread(schedule,db,data,now)
                    elif role=='target':await asyncio.to_thread(monitor,db,data,now)
                    else:await asyncio.to_thread(premium,db,data,now)
                    with db.tx() as c:
                        ownership=delivery_owner(db,c,'smoothers',time.time())
                        mode='official' if ownership else 'shadow'
                        db.put(c,VERSION+':worker:'+role,{'at':time.time(),'status':mode,'ownership_epoch':ownership['epoch'] if ownership else None})
                    if now-last_log>=60:
                        with db.tx() as c:
                            config=db.get(c,VERSION+':config',{})
                            job=db.get(c,VERSION+':week:'+monday_for(now).isoformat(),{})
                        logging.getLogger('uvicorn.error').info('Native Smoothers: role=%s configs=%s week=%s state=%s processed=%s errors=%s mode=%s',role,len(config.get('configs',[])),job.get('week'),job.get('state'),job.get('index',0),len(job.get('errors',[])),mode)
                        db.health(VERSION+'_'+role,mode,'Native worker active; Discord delivery requires sender environment and accepted ownership')
                        last_log=now
            except Exception as e:
                failed=True
                try: db.health(VERSION+'_'+role,'error',type(e).__name__)
                except Exception: logging.getLogger('uvicorn.error').exception('native_smoothers health write failed')
                logging.getLogger('uvicorn.error').warning('Native Smoothers %s: %s',role,type(e).__name__)
            await asyncio.sleep(30 if failed else 2 if role=='schedule' else max(1,60-(time.time()-15)%60) if role=='target' else max(1,300-(time.time()-35)%300))
