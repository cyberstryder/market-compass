"""Bounded independent discovery research; never queues trades or Discord jobs."""
import asyncio
import logging
import time
import uuid
from datetime import date
from sqlalchemy import select, update, func
from .store import identity, flow_records, discovery_trials as trials, discovery_checks as checks
from .market import number, day, session, fresh
from .universe import symbols, EXCLUDED_STOCKS
from .swing_signals import daily_context, minute_context
from .swing_study import flow_snapshot, classify, targets

VERSION = 'discovery-v1'
SEEDS = ('PONY','FLY','ENPH','AAOI','DAL','S','TTWO','RIG','CARR','PGEN','SDGR','EWTX','ASX')
HORIZONS = ('60m', 'close', '1session', '5session')


def requested(db, c, cfg, at):
    """Current research pool plus pending endpoints, even when discovery is paused."""
    held = list(c.execute(select(trials.c.symbol).where(trials.c.status=='pending').distinct()).scalars())
    pool = db.get(c, VERSION+':universe', {})
    enabled=getattr(cfg,'discovery',False)
    active = pool.get('symbols', []) if enabled and 0 <= at-pool.get('at', 0) <= 300 else []
    seeds = cfg.discovery_seeds if enabled else ()
    return tuple(s for s in dict.fromkeys([*held,*seeds,*active]) if s not in EXCLUDED_STOCKS)


def flow_rows(c, at):
    # Receipt and revision times prevent using later corrections in this decision.
    return [dict(r) for r in c.execute(select(flow_records).where(
        flow_records.c.day==day(at),flow_records.c.source_ts>=at-1800, flow_records.c.source_ts<=at,
        flow_records.c.first_seen<=at, flow_records.c.last_seen<=at)
        .order_by(flow_records.c.source_ts.desc(),flow_records.c.vendor_id).limit(5001)).mappings()]


def refresh_universe(db, c, cfg, at, rows):
    prior=db.get(c,VERSION+':universe',{})
    retained={s:v for s,v in prior.get('dynamic',{}).items() if 0<=at-v['received_at']<=86400}
    for r in rows[:5000]:
        p=r['payload']
        try: symbol=symbols((p.get('symbol',''),))[0]
        except (ValueError,IndexError): continue
        if symbol in EXCLUDED_STOCKS or symbol in cfg.watch_symbols or symbol in cfg.discovery_seeds: continue
        if (number(p.get('premium')) or 0)<50000 or str(p.get('sentiment','')).lower() not in ('bullish','bearish'): continue
        stamp=number(p.get('source_ts'))
        if stamp is None or not 0<=at-stamp<=1800: continue
        if symbol not in retained or r['first_seen']>retained[symbol]['received_at']:
            retained[symbol]=dict(received_at=r['first_seen'],source_ts=stamp,vendor_id=p.get('vendor_id'))
    ranked=sorted(retained,key=lambda s:(-retained[s]['received_at'],s))
    room=min(cfg.discovery_limit,max(0,500-len((set(cfg.watch_symbols)|set(cfg.discovery_seeds))-EXCLUDED_STOCKS)))
    admitted=ranked[:room]
    base=[s for s in dict.fromkeys([*cfg.watch_symbols,*cfg.discovery_seeds]) if s not in EXCLUDED_STOCKS]
    result=dict(at=at,version=VERSION,symbols=(base+admitted)[:500],
        dynamic={s:retained[s] for s in admitted},
        excluded_capacity=ranked[room:]+base[500:],flow_query_truncated=len(rows)>5000,
        source='Configured seed coverage plus received classified unusual flow; no Obsidian input',
        liquidity='Discovery requests data first; technical admission separately requires price and dollar liquidity')
    db.put(c,VERSION+':universe',result)
    return result


def liquidity(rows, at):
    # Revisions deduplicated by session; incomplete current day never contributes.
    values={}
    for r in sorted(rows,key=lambda r:r.get('id',0)):
        if day(r['ts'])>=day(at): continue
        p=r['payload'];close=number(p.get('c'));volume=number(p.get('v'))
        if close is not None and volume is not None and close>0 and volume>=0:
            values[day(r['ts'])]=close*volume
    tail=[values[d] for d in sorted(values)[-20:]]
    return sum(tail)/20 if len(tail)==20 else None


def assess(symbol, daily, minute, quote, dollar_volume, snapshot, at):
    """Frozen independent hypotheses, evaluated only on completed price evidence."""
    reasons=[];hours=session(day(at))
    if not hours or not hours[0]<=at<hours[1]-1800: reasons.append('outside_entry_window')
    if daily.get('status')!='ready' or daily.get('day')!=day(at): reasons.append('daily_history_not_ready')
    if minute.get('status')!='ready' or not 0<=at-minute.get('asof',0)<=90: reasons.append('minute_history_not_ready')
    if not fresh(quote,at) or (number(quote.get('ts')) or 0)>at: reasons.append('fresh_quote_required')
    if dollar_volume is None or dollar_volume<1000000: reasons.append('daily_dollar_volume_below_1m')
    if reasons: return dict(reasons=reasons,candidates=[],tests=[])
    price=number(minute['bar'].get('c'));atr=number(daily.get('atr14'))
    mid=(quote['bid']+quote['ask'])/2
    if any(number(minute['previous_bar'].get(k)) is None for k in ('c','h','l')): reasons.append('minute_ohlc_incomplete')
    if not price or price<2 or not atr or atr<=0: reasons.append('price_or_atr_ineligible')
    if (quote['ask']-quote['bid'])/mid>.01: reasons.append('underlying_spread_above_1pct')
    if atr and abs(mid-price)>.25*atr: reasons.append('quote_outside_signal_tolerance')
    if reasons: return dict(reasons=reasons,candidates=[],tests=[])
    candidates=[];tests=[]
    for side,sign in (('long',1),('short',-1)):
        f=classify(snapshot,symbol,side,at)
        b,p=minute['bar'],minute['previous_bar']
        trend=sign*(price-daily['sma20'])>0 and sign*(daily['sma20']-daily['sma50'])>=0
        extreme=daily['high20'] if sign==1 else daily['low20']
        near=abs(price-extreme)<=atr
        momentum=sign*(price-p['c'])>0
        reversal=(daily['close']<daily['sma20'] and 25<=daily['rsi14']<=50 if sign==1
                  else daily['close']>daily['sma20'] and 50<=daily['rsi14']<=75)
        bounce=price>p['h'] if sign==1 else price<p['l']
        near_low=abs(price-(daily['low5'] if sign==1 else daily['high5']))<=2*atr
        for rule,criteria in [('flow_continuation',dict(trend=trend,near_extreme=near,momentum=momentum)),
                              ('flow_bounce',dict(reversal_context=reversal,minute_reclaim=bounce,near_reversal_zone=near_low))]:
            failures=[k for k,v in criteria.items() if not v]
            if f['group'] not in ('fresh_flow','delayed_flow'): failures.append('flow_'+f['group'])
            tests.append(dict(side=side,rule=rule,criteria=criteria,failures=failures,flow=f))
            if not failures:
                candidates.append(dict(symbol=symbol,side=side,rule=rule,signal_time=minute['asof'],
                    signal_price=price,entry_mid=mid,quote=dict(quote),flow=f,daily=dict(daily),
                    minute=dict(minute),daily_dollar_volume=dollar_volume))
    return dict(reasons=[] if candidates else ['no_qualifying_hypothesis'],candidates=candidates,tests=tests)


def admit(db,c,item,at):
    # One record per symbol/direction/rule/session, persisted across restarts.
    key=identity(VERSION,item['symbol'],item['side'],item['rule'],day(at))
    frozen=dict(item,id=key,version=VERSION,assessed_at=at,mode='RESEARCH ONLY',
        note='Underlying midpoint observations; no selected option, trade, or notification')
    inserted=c.execute(db.insert(trials).values(id=key,symbol=item['symbol'],created=at,
        status='pending',payload=frozen).on_conflict_do_nothing(index_elements=['id']).returning(trials.c.id)).first()
    if not inserted:return False
    schedule=targets(at)
    for h in HORIZONS:
        target=schedule[h]
        hours=session(day(target))
        # Session endpoints use the final minute, not an after-hours quote.
        if hours and target>hours[1]-60: target=hours[1]-60
        c.execute(db.insert(checks).values(trial_id=key,horizon=h,target_at=target,status='pending',payload={}))
    return True


def observe(db,c,at):
    due=c.execute(select(checks,trials.c.payload.label('trial')).join(trials,trials.c.id==checks.c.trial_id)
        .where(checks.c.status=='pending',checks.c.target_at<=at)
        .order_by(checks.c.target_at).limit(500)).mappings().all()
    for row in due:
        p=row['trial'];q=db.get(c,'quote:'+p['symbol'],{});stamp=number(q.get('ts'))
        valid=bool(stamp is not None and row['target_at']<=stamp<=row['target_at']+60
                   and stamp<=at and fresh(q,at) and session(day(stamp))
                   and session(day(stamp))[0]<=stamp<session(day(stamp))[1])
        if not valid and at<=row['target_at']+60:continue
        payload=dict(checked_at=at,reason=None if valid else 'checkpoint_quote_unavailable')
        if valid:
            mid=(q['bid']+q['ask'])/2
            payload.update(midpoint=mid,quote=q,directional_pct=(mid/p['entry_mid']-1)*100*(1 if p['side']=='long' else -1))
        c.execute(update(checks).where(checks.c.trial_id==row['trial_id'],checks.c.horizon==row['horizon'])
            .values(status='measured' if valid else 'unavailable',payload=payload))
    for key in {r['trial_id'] for r in due}:
        statuses=list(c.execute(select(checks.c.status).where(checks.c.trial_id==key)).scalars())
        if 'pending' not in statuses:
            c.execute(update(trials).where(trials.c.id==key).values(status='complete' if all(s=='measured' for s in statuses) else 'incomplete'))


def report(db,c,at):
    recent=list(c.execute(select(trials).order_by(trials.c.created.desc()).limit(101)).mappings())
    records=[]
    for r in recent[:100]:
        outcomes=[dict(horizon=x.horizon,status=x.status,target_at=x.target_at,**x.payload) for x in
                  c.execute(select(checks).where(checks.c.trial_id==r['id'])).mappings()]
        records.append(dict(r['payload'],status=r['status'],outcomes=outcomes))
    return dict(at=at,version=VERSION,universe=db.get(c,VERSION+':universe',{}),
        worker=db.get(c,VERSION+':worker',{}),coverage=list(db.prefix(c,VERSION+':scan:').values()),
        total=c.execute(select(func.count()).select_from(trials)).scalar_one(),records=records,
        records_truncated=len(recent)>100,
        note='Research only. Fresh and delayed flow are separate hypotheses. No option selection or Discord sending. Returns are underlying midpoint changes before costs.')


class Discovery:
    def __init__(self,db,cfg):self.db,self.cfg,self.owner,self.cursor=db,cfg,uuid.uuid4().hex,0

    def tick(self,at):
        from .universe import data_symbols
        with self.db.tx() as c:
            if not self.db.lease(c,VERSION,self.owner,30):return
            observe(self.db,c,at)
            if self.cfg.discovery:
                rows=flow_rows(c,at);pool=refresh_universe(self.db,c,self.cfg,at,rows)
                matrix=self.db.get(c,'matrix:unusual_activity',{})
                snapshot=flow_snapshot(rows[:5000],matrix,dict(query_truncated=len(rows)>5000,
                    vendor_limited=matrix.get('limited'),recovery=self.db.get(c,'recovery:flow:'+day(at),{})),at,0,180)
                universe=pool['symbols'];collected=set(data_symbols(self.db,c,self.cfg,at))
                chunk=(universe[self.cursor:]+universe[:self.cursor])[:12]
                self.cursor=(self.cursor+len(chunk))%max(1,len(universe))
                hours=session(day(at));entry_open=bool(hours and hours[0]<=at<hours[1]-1800)
                for symbol in chunk:
                    if not entry_open:
                        self.db.put(c,VERSION+':scan:'+symbol,dict(symbol=symbol,at=at,reasons=['outside_entry_window']))
                        continue
                    cached=self.db.get(c,VERSION+':daily:'+symbol,{})
                    if cached.get('day')!=day(at) or at-cached.get('at',0)>=300:
                        raw=self.db.recent(c,'daily',symbol,limit=180)
                        cached=dict(at=at,day=day(at),daily=daily_context(raw,at),liquidity=liquidity(raw,at))
                        self.db.put(c,VERSION+':daily:'+symbol,cached)
                    daily=cached['daily'];minute=minute_context(self.db.recent(c,'bar',symbol,limit=8),at)
                    result=assess(symbol,daily,minute,self.db.get(c,'quote:'+symbol,{}),cached['liquidity'],snapshot,at)
                    if symbol not in collected:result=dict(reasons=['collection_capacity'],candidates=[],tests=[])
                    saved=dict(symbol=symbol,at=at,bar_at=minute.get('asof'),daily_ready=daily.get('status')=='ready',
                        reasons=result['reasons'],tests=result['tests'])
                    self.db.put(c,VERSION+':scan:'+symbol,dict(saved,tests=[dict(side=t['side'],rule=t['rule'],criteria=t['criteria'],failures=t['failures'],flow_group=t['flow']['group']) for t in saved['tests']]))
                    # Compact durable per-bar reasons; full flow evidence is frozen on candidates.
                    diagnostic=dict(saved,tests=[dict(side=t['side'],rule=t['rule'],criteria=t['criteria'],
                        failures=t['failures'],flow_group=t['flow']['group']) for t in saved['tests']])
                    self.db.append(c,'discovery_decision',VERSION,symbol,at,diagnostic,
                        identity(VERSION,'decision',symbol,minute.get('asof') or int(at//300)*300))
                    for item in result['candidates']:
                        item['origin']='dynamic_flow' if symbol in pool['dynamic'] else 'audit_seed' if symbol in self.cfg.discovery_seeds else 'base_watchlist'
                        admit(self.db,c,item,at)
            self.db.put(c,VERSION+':worker',dict(at=at,enabled=self.cfg.discovery,mode='RESEARCH ONLY'))
        self.db.health('discovery','running','Independent discovery research; no execution',version=VERSION)

    async def run(self):
        while True:
            try: await asyncio.to_thread(self.tick,time.time())
            except asyncio.CancelledError:raise
            except Exception as error:
                logging.getLogger('uvicorn.error').exception('Discovery research worker failed')
                await asyncio.to_thread(self.db.health,'discovery','error',type(error).__name__)
            await asyncio.sleep(5)
