from . import observation_recovery as recovery
"""Independent multi-session option observations. No broker or portfolio mutations."""
import asyncio
import logging
import time
import uuid
from types import SimpleNamespace
from collections import Counter
from sqlalchemy import Table, Column, String, Float, JSON, Index, select, update, func
from .store import meta, identity, flow_records,events
from .market import day, session, number
from .option_ideas import current, liquid, eligible_contracts, FEE, SLIPPAGE
from .swing_signals import stored_daily_context, minute_context, technical_setups, summarize_flow, confirms, trading_seconds, hold_deadline
from .daily_history import RECOVERY_CACHE, current_context
from .flow_recovery import freshness as flow_freshness

VERSION = 'swing-ideas-v1'
MAX_GAP = 60  # Seconds of open exchange time, excluding scheduled closures.
swings = Table('swing_ideas_v1', meta,
    Column('id', String(64), primary_key=True),
    Column('underlying', String(100), nullable=False),
    Column('status', String(24), nullable=False),
    Column('created', Float, nullable=False),
    Column('updated', Float, nullable=False),
    Column('payload', JSON, nullable=False))
Index('swing_ideas_active', swings.c.status, swings.c.created)


def active_underlyings(c):
    return list(c.execute(select(swings.c.underlying).where(swings.c.status=='open').distinct()).scalars())


def stream_requests(c, now, status=None):
    rows = c.execute(select(swings.c.payload).where(swings.c.status.in_((status,) if status else ('pending','open')))
                     .order_by(swings.c.created)).scalars().all()
    wanted = [p['contract']['symbol'] for p in rows if p['status']=='open']
    pending = [p for p in rows if p['status']=='pending' and now < p['expires_at']]
    for index in range(4):
        wanted.extend(p['candidates'][index]['symbol'] for p in pending if len(p.get('candidates',[]))>index)
    return list(dict.fromkeys(wanted))


class SwingIdeas:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner, self.cursor = uuid.uuid4().hex, 0
        self.flow_cache, self.flow_at, self.flow_coverage = {}, 0, {}
        self.study_flow = None

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    def save(self, c, p, now):
        p['updated_at'] = now
        c.execute(update(swings).where(swings.c.id==p['id']).values(status=p['status'], updated=now, payload=p))

    def notify(self, c, p, now, event):
        if self.cfg.swing_alerts:
            self.db.append(c,'alert','swing_ideas',p['contract']['symbol'],now,
                {**p, 'status':'swing_idea_'+event, 'idea_status':p['status'], 'asset':'option',
                 'side':'long', 'alert_category':'swing_ideas', 'track':'swing', 'mode':'SIMULATED'},
                'swing-idea:'+p['id']+':'+event)

    def queue(self, c, signal, flow, now):
        hours = session(day(now))
        if (not self.cfg.swing_ideas or signal['symbol'] not in self.cfg.watch_symbols
                or not hours or not hours[0] <= now < hours[1]-1800
                or not 0 <= now-signal['signal_time'] <= 90 or not confirms(flow,signal['side'],now)
                or not flow_freshness(self.db.get(c,'matrix:unusual_activity',{}),now,300)['eligible_for_live_confirmation']):
            return
        key = identity(VERSION, signal['symbol'], signal['side'], signal['rule'], signal['signal_time'])
        p = dict(id=key,version=VERSION,underlying=signal['symbol'],underlying_side=signal['side'],
            strategy=signal['strategy'],rule=signal['rule'],reason=signal['reason'],
            signal_time=signal['signal_time'],signal_price=signal['signal_price'],
            underlying_stop=signal['stop'],underlying_target=signal['target'],
            daily=signal['daily'],weekly_aligned=signal['weekly_aligned'],flow=flow,
            flow_coverage=self.flow_coverage,atr=signal['daily']['atr14'],
            status='pending',created_at=now,updated_at=now,expires_at=min(now+120,hours[1]-1800),
            candidates=[],contract=None,entry=None,exit=None,pnl=None,return_pct=None,
            waiting_reason='Refreshing chain and requesting liquid option quotes',
            samples=0,max_gap_seconds=0,milestones=[],best_pct=None,worst_pct=None,
            overnight_gaps=[],sessions_observed=[],
            basis='Independent one-contract swing simulation; ask + $0.01 entry, bid - $0.01 exit, $0.65 per side; not account P&L')
        inserted = c.execute(self.db.insert(swings).values(id=key,underlying=p['underlying'],
            status=p['status'],created=now,updated=now,payload=p).on_conflict_do_nothing(index_elements=['id'])
            .returning(swings.c.id)).first()
        if inserted:
            self.db.put(c,'focus:'+p['underlying'],dict(symbol=p['underlying'],priority=115,at=now,
                reason='Swing idea: select a dated contract and verify live quote'))

    def exclude(self, c, p, now, reason):
        p.update(status='excluded',finished_at=now,exit_reason=reason)
        self.save(c,p,now)

    def pending(self, c, p, now):
        q = self.db.get(c,'quote:'+p['underlying'])
        chain = self.db.get(c,'chain:'+p['underlying'],{})
        stream = self.db.get(c,'options:subscriptions',{})
        now = self.now(now)
        if not self.cfg.swing_ideas or now >= p['expires_at']:
            return self.exclude(c,p,now,'New swings disabled' if not self.cfg.swing_ideas else p['waiting_reason'])
        if not confirms(p['flow'],p['underlying_side'],now):
            return self.exclude(c,p,now,'Supporting flow exceeded its five-minute source-age limit')
        check = flow_freshness(self.db.get(c,'matrix:unusual_activity',{}),now,300)
        if not check['eligible_for_live_confirmation']:
            p['waiting_reason'] = 'Supporting flow collection is not current: '+check['status']
            return self.save(c,p,now)
        if not current(q,now) or q['ts'] < p['signal_time']:
            p['waiting_reason'] = 'Fresh underlying quote required'
            return self.save(c,p,now)
        long = p['underlying_side']=='long'
        spot = (q['bid']+q['ask'])/2
        invalid = q['bid']<=p['underlying_stop'] if long else q['ask']>=p['underlying_stop']
        target = q['bid']>=p['underlying_target'] if long else q['ask']<=p['underlying_target']
        if invalid or target or abs(spot-p['signal_price']) > p['atr']*.25:
            return self.exclude(c,p,now,'Underlying invalidated, reached target, or exceeded 0.25 daily ATR entry tolerance')
        selection_cfg = SimpleNamespace(ideas_min_dte=self.cfg.swing_min_dte,
            ideas_max_dte=self.cfg.swing_max_dte,ideas_target_dte=self.cfg.swing_target_dte)
        p['candidates'] = eligible_contracts(chain,p['underlying'],p['underlying_side'],spot,now,selection_cfg) if chain.get('complete') else []
        p['waiting_reason'] = 'No recent complete chain with an eligible contract' if not p['candidates'] else 'Waiting for a fresh, liquid streamed option quote'
        selected = set(stream.get('symbols',[])) if 0 <= now-stream.get('at',0) <= 15 else set()
        for contract in p['candidates']:
            oq = self.db.get(c,'quote:'+contract['symbol'])
            now = self.now(now)
            if (contract['symbol'] not in selected or not liquid(oq,now) or oq['ts']<p['signal_time']
                    or not current(q,now) or now >= p['expires_at']):
                continue
            deadline = hold_deadline(now,contract['expiry'],self.cfg.swing_hold_sessions)
            if deadline is None or deadline <= now:
                continue
            entry = round(oq['ask']+SLIPPAGE,2)
            p.update(status='open',contract=contract,opened_at=now,entry=entry,
                entry_quote_ts=oq['ts'],entry_underlying=spot,option_source=oq.get('source'),
                premium_stop=round(entry*.65,2),premium_target=round(entry*1.75,2),exit_deadline=deadline,
                last_option_ts=oq['ts'],last_underlying_ts=q['ts'],last_quote=oq,last_underlying_quote=q,
                chain_asof=chain['asof'],chain_source=chain.get('source'),metadata_day=day(now),
                sessions_observed=[day(now)],waiting_reason=None,observation_state='market_open',
                holding_rule=f'Maximum {self.cfg.swing_hold_sessions} trading sessions, including entry; exit before expiry')
            p['collection_version']=oq.get('collection_version','unversioned')
            if p['collection_version'] in ('option-reliability-v5','option-reliability-v6','option-reliability-v7','option-reliability-v8'):recovery.enable(p)
            self.notify(c,p,now,'new')
            break
        self.save(c,p,now)

    def finish(self, c, p, now, reason, price=None):
        if price is None:recovery.register(self.db,c,p,now,reason)
        p.update(status='closed' if price is not None else 'unresolved',exit=price,finished_at=now,exit_reason=reason)
        if price is not None:
            p['pnl'] = round((price-p['entry'])*100-2*FEE,4)
            p['return_pct'] = p['pnl']/(p['entry']*100+FEE)*100
            p['outcome'] = 'win' if p['pnl']>0 else 'loss' if p['pnl']<0 else 'breakeven'
        else:
            p.update(pnl=None,return_pct=None,outcome='unresolved')
        self.save(c,p,now)
        self.notify(c,p,now,'closed' if price is not None else 'unresolved')

    def metadata(self, c, p, now):
        if p.get('metadata_day') == day(now):
            return 'ready'
        chain = self.db.get(c,'chain:'+p['underlying'],{})
        if (not chain.get('complete') or not 0 <= now-chain.get('asof',0) <= 1800
                or day(chain['asof']) != day(now)):
            return 'waiting'
        contract = next((o for o in chain.get('contracts',[]) if o.get('symbol')==p['contract']['symbol']),None)
        if not contract or any(contract.get(k)!=p['contract'].get(k) for k in ('underlying','strike','expiry','type','multiplier')):
            return 'contract_metadata_changed'
        # Split-adjusted history can change old reference levels after a split.
        rows = self.db.recent(c,'daily',p['underlying'],limit=160)
        ref = next((r for r in rows if day(r['ts'])==p['daily']['through']),None)
        if not ref or number(ref['payload'].get('c')) is None:
            return 'waiting'
        if abs(ref['payload']['c']/p['daily']['close']-1) > .01:
            return 'historical_price_basis_changed'
        p['metadata_day'] = day(now)
        return 'ready'

    def observe(self, c, p, now):
        oq = self.db.get(c,'quote:'+p['contract']['symbol'])
        uq = self.db.get(c,'quote:'+p['underlying'])
        now = self.now(now)
        hours = session(day(now))
        opened = bool(hours and hours[0] <= now < hours[1])
        gap = max(trading_seconds(p['last_option_ts'],now),trading_seconds(p['last_underlying_ts'],now))
        if gap > MAX_GAP:
            return self.finish(c,p,now,'open_session_observation_gap')
        if now > p['exit_deadline']+5:
            return self.finish(c,p,now,'holding_deadline_quote_missing')
        if not opened:
            if p.get('observation_state') != 'scheduled_market_closure':
                p['observation_state'] = 'scheduled_market_closure'
                self.save(c,p,now)
            return
        metadata = self.metadata(c,p,now)
        if metadata not in ('ready','waiting'):
            return self.finish(c,p,now,metadata)
        if metadata == 'waiting':
            p['observation_state'] = 'waiting_for_session_contract_metadata'
            return self.save(c,p,now)
        now = self.now(now)
        if (not current(oq,now) or not current(uq,now) or min(oq['ts'],uq['ts']) < hours[0]
                or max(oq['ts'],uq['ts']) >= hours[1]):
            p['observation_state'] = 'waiting_for_fresh_quotes'
            return self.save(c,p,now)
        if oq['ts']<p['last_option_ts'] or uq['ts']<p['last_underlying_ts']:
            return
        changed = oq['ts']>p['last_option_ts'] or uq['ts']>p['last_underlying_ts']
        if not changed and now < p['exit_deadline']:
            return
        p['max_gap_seconds'] = max(p['max_gap_seconds'],gap)
        reopening = day(now) not in p['sessions_observed']
        if reopening:
            previous = p['last_quote']
            prior_stock = p['last_underlying_quote']
            p['overnight_gaps'].append(dict(day=day(now),observed_at=now,
                previous_option_ts=previous['ts'],option_ts=oq['ts'],option_bid_change=oq['bid']-previous['bid'],
                previous_underlying_ts=prior_stock['ts'],underlying_ts=uq['ts'],
                underlying_mid_change=(uq['bid']+uq['ask']-prior_stock['bid']-prior_stock['ask'])/2))
            p['sessions_observed'].append(day(now))
        p.update(last_quote=oq,last_underlying_quote=uq,last_option_ts=oq['ts'],last_underlying_ts=uq['ts'],
            observation_state='market_open',samples=p['samples']+1)
        mark = round(max(0,oq['bid']-SLIPPAGE),2)
        long = p['underlying_side']=='long'
        invalid = uq['bid']<=p['underlying_stop'] if long else uq['ask']>=p['underlying_stop']
        target = uq['bid']>=p['underlying_target'] if long else uq['ask']<=p['underlying_target']
        reason = ('premium_stop' if mark<=p['premium_stop'] else 'underlying_invalidation' if invalid
            else 'premium_target' if mark>=p['premium_target'] else 'underlying_target' if target
            else 'maximum_hold' if now>=p['exit_deadline'] else None)
        price = p['premium_target'] if reason=='premium_target' else mark
        net = (price-p['entry'])*100-2*FEE
        pct = net/(p['entry']*100+FEE)*100
        p.update(mark=price,mark_at=now,mark_quote_ts=oq['ts'],unrealized=round(net,4),mark_pct=pct)
        if p['best_pct'] is None or pct>p['best_pct']:
            p.update(best_pct=pct,peak_at=now)
        p['worst_pct'] = pct if p['worst_pct'] is None else min(p['worst_pct'],pct)
        if reason:
            return self.finish(c,p,now,reason,price)
        crossed = [level for level in (10,25,50) if pct>=level and level not in p['milestones']]
        if crossed:
            p['milestones'].extend(crossed)
            self.notify(c,p,now,'update_'+str(max(crossed)))
        elif reopening:
            self.notify(c,p,now,'reopen_'+day(now))
        self.save(c,p,now)

    def discover(self, c, now):
        universe = list(self.cfg.watch_symbols)
        hours = session(day(now))
        entry_open = bool(hours and hours[0] <= now < hours[1]-1800)
        if entry_open and now-self.flow_at >= 15:
            rows = c.execute(select(flow_records).where(flow_records.c.day==day(now),
                flow_records.c.source_ts>=now-1800,flow_records.c.source_ts<=now,
                flow_records.c.first_seen<=now).order_by(flow_records.c.source_ts.desc()).limit(5001)).mappings().all()
            self.flow_cache = summarize_flow(rows[:5000],now,self.cfg.swing_min_dte,self.cfg.swing_max_dte)
            matrix = self.db.get(c,'matrix:unusual_activity',{})
            check = flow_freshness(matrix,now,300)
            if not check['eligible_for_live_confirmation']:
                self.flow_cache = {}
            self.flow_coverage = dict(query_truncated=len(rows)>5000,vendor_status=matrix.get('status'),
                freshness=check,
                vendor_limited=matrix.get('limited'),recovery=self.db.get(c,'recovery:flow:'+day(now),{}),
                note='Observed filtered records only; missing or unclassified flow cannot confirm a swing')
            from .swing_study import flow_snapshot
            self.study_flow=flow_snapshot(rows[:5000],matrix,self.flow_coverage,now,
                self.cfg.swing_min_dte,self.cfg.swing_max_dte)
            self.flow_at = now
        # Twelve symbols per two-second turn bounds work and covers 144 in ~24s.
        chunk = (universe[self.cursor:]+universe[:self.cursor])[:12]
        self.cursor = (self.cursor+len(chunk))%len(universe)
        for symbol in chunk:
            daily = self.db.get(c,'swing_daily:'+symbol,{})
            proof = self.db.get(c,'daily_history_recovery:'+symbol,{})
            if not current_context(daily,now,proof):
                refreshed = self.db.get(c,RECOVERY_CACHE+symbol,{})
                daily = (refreshed if current_context(refreshed,now,proof)
                         else stored_daily_context(self.db,c,symbol,now))
                # Only the leased scanner writes this cache. A completed
                # recovery invalidates it on the next scan, without a writer
                # crossing into the collector's transaction.
                self.db.put(c,'swing_daily:'+symbol,daily)
                logging.getLogger('uvicorn.error').info('Swing daily history: symbol=%s status=%s sessions=%s archived_rows=%s missing=%s reason=%s',
                    symbol,daily['status'],daily['history']['available_sessions'],daily['history']['archived_rows'],
                    len(daily['history']['missing_sessions']),daily.get('reason'))
            minute = self.db.get(c,'scanner_features:'+symbol,{})
            price_ready = minute.get('status')=='ready' and 0 <= now-minute.get('asof',0) <= 90
            if not price_ready:
                minute = minute_context(self.db.recent(c,'bar',symbol,limit=8),now)
                price_ready = minute.get('status')=='ready'
            signals = technical_setups(symbol,daily,minute,now) if entry_open else []
            flow = self.flow_cache.get(symbol,{})
            qualified = [s for s in signals if confirms(flow,s['side'],now)]
            for signal in qualified:
                self.queue(c,signal,flow,now)
            # Retain every technical candidate even when flow prevents an entry.
            for signal in signals:
                key=identity('swing-technical-v1',symbol,signal['side'],signal['rule'],signal['signal_time'])
                inserted=self.db.append(c,'swing_candidate','swing_research',symbol,now,
                    dict(id=key,signal=signal,flow_confirmed=confirms(flow,signal['side'],now),
                         flow=flow,coverage=self.flow_coverage),key)
                if inserted:
                    from .swing_study import register
                    event=c.execute(select(events).where(events.c.key==key)).mappings().one()
                    register(self.db,c,event,self.now(now),snapshot=self.study_flow)
            scan_state='qualified' if qualified else 'waiting_for_flow' if signals else 'market_closed' if not entry_open else 'warming_up' if daily['status']!='ready' else 'waiting_for_price' if not price_ready else 'scanning'
            # Freeze cash-session evidence before overnight refresh overwrites current status.
            if entry_open:
                self.db.put(c,'swing_session_scan:'+day(now)+':'+symbol,
                    dict(symbol=symbol,at=now,status=scan_state,daily_ready=daily['status']=='ready',
                         price_ready=price_ready,technical_rules=[s['rule'] for s in signals],
                         flow_confirmed=bool(qualified),flow_coverage=self.flow_coverage))
            self.db.put(c,'swing_scan:'+symbol,dict(symbol=symbol,at=now,day=day(now),daily_ready=daily['status']=='ready',
                status='qualified' if qualified else 'waiting_for_flow' if signals else 'market_closed' if not entry_open else 'warming_up' if daily['status']!='ready' else 'waiting_for_price' if not price_ready else 'scanning',
                price_ready=price_ready,
                reason=daily.get('reason'),daily_history=daily.get('history',{}),technical_rules=[s['rule'] for s in signals],
                flow_confirmed=bool(qualified),flow_latest=flow.get('latest')))

    def tick(self, c, now):
        rows = c.execute(select(swings.c.payload).where(swings.c.status.in_(('pending','open')))
                         .order_by(swings.c.created)).scalars().all()
        for p in rows:
            if p['status']=='pending':
                self.pending(c,p,self.now(now))
            else:
                self.observe(c,p,self.now(now))
        if self.cfg.swing_ideas:
            self.discover(c,self.now(now))
        at = self.now(now)
        self.db.put(c,'swing_ideas:worker',dict(at=at,version=VERSION,enabled=self.cfg.swing_ideas,
            watch_symbols=len(self.cfg.watch_symbols),batch_size=12,flow_at=self.flow_at,flow_symbols=len(self.flow_cache)))
        if at-self.db.get(c,'swing_ideas:log',0)>=60:
            counts = dict(c.execute(select(swings.c.status,func.count()).where(swings.c.created>=at-86400)
                                   .group_by(swings.c.status)).all())
            logging.getLogger('uvicorn.error').info('Swing ideas worker: version=%s enabled=%s watch=%s states_24h=%s',
                VERSION,self.cfg.swing_ideas,len(self.cfg.watch_symbols),counts)
            self.db.put(c,'swing_ideas:log',at)

    def cycle(self):
        with self.db.tx() as c:
            if self.db.lease(c,'swing-ideas',self.owner,30):
                self.tick(c,time.time())

    async def run(self):
        self.clock = time.time
        while True:
            try:
                await asyncio.to_thread(self.cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('uvicorn.error').exception('Swing ideas worker failed; retrying')
            await asyncio.sleep(2)


def snapshot(db, c, cfg, now):
    records = list(c.execute(select(swings.c.payload).order_by(swings.c.created.desc()).limit(100)).scalars())
    # Include every active observation even if more than 100 recent ideas exist.
    active = c.execute(select(swings.c.payload).where(swings.c.status.in_(('pending','open')))).scalars().all()
    records = sorted({p['id']:p for p in [*records,*active]}.values(),key=lambda p:p['created_at'],reverse=True)
    counts = dict(c.execute(select(swings.c.status,func.count()).where(swings.c.created>=now-90*86400).group_by(swings.c.status)).all())
    scan = [v for v in db.prefix(c,'swing_scan:').values() if v['symbol'] in cfg.watch_symbols and v.get('day')==day(now)]
    # SQL aggregation keeps growth of the durable ledger off the dashboard path.
    groups = c.execute(select(swings.c.payload['rule'].as_string().label('rule'),
        swings.c.payload['underlying_side'].as_string().label('side'),swings.c.status,
        swings.c.payload['outcome'].as_string().label('outcome'),func.count().label('count'),
        func.avg(swings.c.payload['return_pct'].as_float()).label('avg_return_pct'))
        .where(swings.c.created>=now-90*86400).group_by('rule','side',swings.c.status,'outcome')).mappings().all()
    return dict(enabled=cfg.swing_ideas,worker=db.get(c,'swing_ideas:worker'),records=records,counts=counts,
        groups=[dict(g) for g in groups],scan_counts=dict(Counter(p['status'] for p in scan)),
        daily_ready=sum(p['daily_ready'] for p in scan),scanned=len(scan),watch_symbols=len(cfg.watch_symbols),
        coverage=sorted(scan,key=lambda p:p['symbol']),
        min_dte=cfg.swing_min_dte,max_dte=cfg.swing_max_dte,target_dte=cfg.swing_target_dte,
        max_hold_sessions=cfg.swing_hold_sessions,
        basis='Last 100 ideas plus all active; 90-day status and strategy counts. Independent overlapping contracts; not account returns. Unresolved paths are excluded from win/loss.')
