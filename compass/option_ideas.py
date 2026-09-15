"""Intraday option ideas from fresh scanner triggers; independent paper observations."""
import logging
import re
from datetime import date
from sqlalchemy import Table, Column, String, Float, JSON, Index, select, update, func
from .store import meta, identity
from .market import fresh, number, day, session

VERSION = 'option-ideas-v2'
MAX_GAP = 15
FEE = .65
SLIPPAGE = .01
OPTION = re.compile(r'^(?:O:)?([A-Z.]+)(\d{6})([CP])(\d{8})$')
ideas = Table('option_ideas_v1', meta,
    Column('id', String(64), primary_key=True),
    Column('underlying', String(100), nullable=False),
    Column('status', String(24), nullable=False),
    Column('created', Float, nullable=False),
    Column('updated', Float, nullable=False),
    Column('payload', JSON, nullable=False))
Index('option_ideas_active', ideas.c.status, ideas.c.created)


def current(q, now):
    return fresh(q, now) and 0 <= now-q['ts'] <= 5


def liquid(q, now):
    if not current(q, now):
        return False
    bid, ask = number(q['bid']), number(q['ask'])
    return bid >= .10 and ask-bid <= min(.30, (ask+bid)/2*.10)+1e-9


def eligible_contracts(chain, underlying, side, spot, now, cfg):
    """Listed standard contracts only. OI is a daily liquidity input, not live flow."""
    if not 0 <= now-chain.get('asof', 0) <= 120 or not spot or spot <= 0:
        return []
    kind = 'call' if side == 'long' else 'put'
    result = []
    for o in chain.get('contracts', []):
        match = OPTION.fullmatch(str(o.get('symbol', '')))
        if not match or match[1] != underlying or o.get('underlying') != underlying:
            continue
        try:
            expiry = date.fromisoformat(o.get('expiry', ''))
            dte = (expiry-date.fromisoformat(day(now))).days
        except (ValueError, TypeError):
            continue
        strike = number(o.get('strike'))
        if (o.get('type') != kind or match[3] != ('C' if kind == 'call' else 'P')
                or match[2] != expiry.strftime('%y%m%d') or strike is None
                or abs(int(match[4])/1000-strike) > 1e-6 or o.get('multiplier') != 100
                or not cfg.ideas_min_dte <= dte <= cfg.ideas_max_dte
                or abs(strike/spot-1) > .03):
            continue
        if max(number(o.get('oi')) or 0, number(o.get('volume')) or 0) < 100:
            continue
        delta = number(o.get('delta'))
        if delta is not None and not .30 <= abs(delta) <= .70:
            continue
        result.append({k:o.get(k) for k in ('symbol','underlying','expiry','strike','type',
            'multiplier','oi','oi_date','volume','delta')} | {'dte':dte,'delta':delta,'strike':strike,
            'oi':number(o.get('oi')),'volume':number(o.get('volume'))})
    return sorted(result, key=lambda o:(abs(o['dte']-cfg.ideas_target_dte),
        abs(o['strike']/spot-1), abs(abs(o['delta'])-.45) if number(o['delta']) is not None else 1,
        o['symbol']))[:4]


def stream_requests(c, now, status=None):
    rows = c.execute(select(ideas.c.payload).where(ideas.c.status.in_((status,) if status else ('pending','open')))
                     .order_by(ideas.c.created)).scalars().all()
    held = [p['contract']['symbol'] for p in rows if p['status']=='open']
    pending = [p for p in rows if p['status']=='pending' and now < p['expires_at']]
    wanted = held[:]
    # One candidate per pending idea before second choices.
    for index in range(4):
        wanted.extend(p['candidates'][index]['symbol'] for p in pending if len(p.get('candidates',[])) > index)
    return list(dict.fromkeys(wanted))


def choose_streams(held, requested, background, limit):
    return list(dict.fromkeys([*held, *requested, *background]))[:limit]


class OptionIdeas:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock

    def save(self, c, p, now):
        p['updated_at'] = now
        c.execute(update(ideas).where(ideas.c.id==p['id']).values(
            status=p['status'], updated=now, payload=p))

    def queue(self, c, signal, now):
        if (not self.cfg.option_ideas or signal['symbol'] not in self.cfg.watch_symbols
                or signal.get('side') not in ('long','short') or signal.get('track')=='swing'):
            return
        if not 0 <= now-signal.get('signal_time',0) <= 90:
            return
        hours = session(day(now))
        if not hours or not hours[0] <= now < hours[1]-1800:
            return
        key = identity(VERSION, signal['id'])
        p = dict(id=key, source_id=signal['id'], version=VERSION,
            underlying=signal['symbol'], underlying_side=signal['side'], strategy=signal['strategy'],
            matched_rules=signal.get('matched_rules',[signal.get('rule')]),
            reason=signal.get('reason'), evidence=signal.get('evidence',[])[:20],
            signal_time=signal['signal_time'], signal_price=signal['signal_price'],
            underlying_stop=signal['stop'], underlying_target=signal['target'],
            atr=signal.get('context',{}).get('atr14'),
            status='pending', created_at=now, updated_at=now, expires_at=min(now+120,hours[1]-1800),
            flatten_at=hours[1]-900, candidates=[], contract=None,
            waiting_reason='Refreshing chain and requesting live option quotes',
            entry=None, exit=None, pnl=None, return_pct=None, samples=0, max_gap_seconds=0,
            milestones=[], best_pct=None, worst_pct=None, peak_at=None,
            selection='Listed 1–21 DTE by default; nearest configured 7 DTE, near money; fresh spread check',
            basis='Independent one-contract intraday simulation; ask + $0.01 entry, bid - $0.01 exit, $0.65 per side; not account P&L')
        inserted = c.execute(self.db.insert(ideas).values(id=key, underlying=p['underlying'],
            status=p['status'], created=now, updated=now, payload=p)
            .on_conflict_do_nothing(index_elements=['id']).returning(ideas.c.id)).first()
        if inserted:
            self.db.put(c,'focus:'+p['underlying'],dict(symbol=p['underlying'],priority=110,
                at=now,reason='Options idea: select contract and verify live quote'))

    def notify(self, c, p, now, event):
        if not self.cfg.ideas_alerts:
            return
        self.db.append(c,'alert','option_ideas',p['contract']['symbol'],now,
            {**p, 'status':'option_idea_'+event, 'idea_status':p['status'],
             'asset':'option','side':'long','alert_category':'options_ideas','track':'intraday',
             'mode':'SIMULATED'}, 'option-idea:'+p['id']+':'+event)

    def finish(self, c, p, now, reason, price=None):
        p.update(status='closed' if price is not None else 'unresolved',
                 exit=price, finished_at=now, exit_reason=reason)
        if price is not None:
            p['pnl'] = round((price-p['entry'])*100-2*FEE, 4)
            p['return_pct'] = p['pnl']/(p['entry']*100+FEE)*100
            p['outcome'] = 'win' if p['pnl']>0 else 'loss' if p['pnl']<0 else 'breakeven'
        else:
            p.update(pnl=None,return_pct=None,outcome='unresolved')
        self.save(c,p,now)
        self.notify(c,p,now,'closed' if price is not None else 'unresolved')

    def pending(self, c, p, now):
        if now >= p['expires_at']:
            p.update(status='excluded',finished_at=now,exit_reason=p['waiting_reason'])
            self.save(c,p,now)
            return
        q = self.db.get(c,'quote:'+p['underlying'])
        if not current(q,now) or q['ts'] < p['signal_time']:
            p['waiting_reason'] = 'Fresh underlying quote after the trigger required'
            self.save(c,p,now)
            return
        long = p['underlying_side']=='long'
        price = (q['bid']+q['ask'])/2
        invalid = q['bid']<=p['underlying_stop'] if long else q['ask']>=p['underlying_stop']
        reached = q['bid']>=p['underlying_target'] if long else q['ask']<=p['underlying_target']
        moved = abs(price-p['signal_price']) > max((number(p['atr']) or 0)*.5,.04)
        if invalid or reached or moved:
            p.update(status='excluded',finished_at=now,exit_reason='Underlying invalidated, target reached, or moved beyond entry tolerance')
            self.save(c,p,now)
            return
        chain = self.db.get(c,'chain:'+p['underlying'],{})
        p['candidates'] = eligible_contracts(chain,p['underlying'],p['underlying_side'],price,now,self.cfg)
        p['waiting_reason'] = 'No recent eligible listed contract' if not p['candidates'] else 'Waiting for a fresh, liquid streamed option quote'
        stream = self.db.get(c,'options:subscriptions',{})
        selected = set(stream.get('symbols',[])) if 0 <= now-stream.get('at',0) <= 15 else set()
        choices = []
        for o in p['candidates']:
            oq = self.db.get(c,'quote:'+o['symbol'])
            if o['symbol'] in selected and liquid(oq,now) and oq['ts']>=p['signal_time']:
                choices.append((o,oq))
        if choices:
            o,oq = choices[0]
            entry = round(oq['ask']+SLIPPAGE,2)
            p.update(status='open',contract=o,opened_at=now,entry=entry,
                entry_quote_ts=oq['ts'],option_source=oq.get('source'),
                premium_stop=round(entry*.70,2),premium_target=round(entry*1.50,2),
                last_option_ts=oq['ts'],last_underlying_ts=q['ts'],waiting_reason=None,
                chain_asof=chain.get('asof'),chain_source=chain.get('source'),
                pagination_complete=chain.get('complete'),last_quote=oq,last_underlying_quote=q,
                observation_model='paired-recorded-quotes-v2',collection_version=oq.get('collection_version','pre-option-reliability'))
            self.notify(c,p,now,'new')
        self.save(c,p,now)

    def observe(self, c, p, now):
        if p.get('observation_model') not in ('paired-recorded-quotes-v1','paired-recorded-quotes-v2'):
            return self.observe_pair(c,p,now)
        from .quote_path import recorded_path
        option_path,ot,oc=recorded_path(self.db,c,p['contract']['symbol'],max(p.get('seen_option_ts',p['last_option_ts']),p['opened_at']),now)
        stock_path,ut,uc=recorded_path(self.db,c,p['underlying'],max(p.get('seen_underlying_ts',p['last_underlying_ts']),p['opened_at']),now)
        p['archive_check']={'option':oc,'underlying':uc}
        # Do not consume beyond either stream's loaded prefix.
        frontier=min([now]+([option_path[-1]['ts']] if ot and option_path else [])+
            ([stock_path[-1]['ts']] if ut and stock_path else []))
        if (ot and not option_path) or (ut and not stock_path):
            p['gap_detail']={'reason':'archive_batch_has_no_usable_quotes'}
            return self.finish(c,p,now,'observation_gap')
        oq=p.get('seen_option_quote',p['last_quote']);uq=p.get('seen_underlying_quote',p['last_underlying_quote'])
        timeline=sorted([(q['ts'],0,q) for q in option_path if q['ts']<=frontier]+
                        [(q['ts'],1,q) for q in stock_path if q['ts']<=frontier],key=lambda x:(x[0],x[1]))
        stock_times={t for t,k,_ in timeline if k==1}
        for stamp,kind,q in timeline:
            if kind==0:oq=q
            else:uq=q
            # Equal source times are evaluated together, independent of SQL row order.
            if kind==0 and stamp in stock_times:continue
            self.observe_pair(c,p,stamp,oq,uq)
            if p['status']!='open':return
        p['replay_pending']=ot or ut
        if not p['replay_pending']:
            self.observe_pair(c,p,now)
        if p['status']=='open':self.save(c,p,now)

    def observe_pair(self, c, p, now, oq=None, uq=None):
        oq = oq if oq is not None else self.db.get(c,'quote:'+p['contract']['symbol'])
        uq = uq if uq is not None else self.db.get(c,'quote:'+p['underlying'])
        independent=p.get('observation_model')=='paired-recorded-quotes-v2'
        option_last=p.get('seen_option_ts',p['last_option_ts']) if independent else p['last_option_ts']
        underlying_last=p.get('seen_underlying_ts',p['last_underlying_ts']) if independent else p['last_underlying_ts']
        if (now-option_last>MAX_GAP and (not current(oq,now) or oq['ts']-option_last>MAX_GAP)
                or now-underlying_last>MAX_GAP and (not current(uq,now) or uq['ts']-underlying_last>MAX_GAP)):
            p['gap_detail']={'checked_at':now,'option_last':option_last,
                'underlying_last':underlying_last,'option_next':(oq or {}).get('ts'),
                'underlying_next':(uq or {}).get('ts'),'reason':'missing_continuous_paired_quotes'}
            self.finish(c,p,now,'observation_gap')
            return
        if independent:
            # Receipt continuity belongs to each feed, even when the other is
            # too old for a paired mark. Never value an exit with stale quotes.
            if current(oq,now) and oq['ts']>=option_last:
                p.update(seen_option_ts=oq['ts'],seen_option_quote=oq)
            if current(uq,now) and uq['ts']>=underlying_last:
                p.update(seen_underlying_ts=uq['ts'],seen_underlying_quote=uq)
            if not current(oq,now) or not current(uq,now):
                p['unpaired_checks']=p.get('unpaired_checks',0)+1
        if not current(oq,now) or not current(uq,now):
            return
        if oq['ts']>p['flatten_at']+5 or uq['ts']>p['flatten_at']+5:
            self.finish(c,p,now,'session_quote_missing')
            return
        if oq['ts']<p['last_option_ts'] or uq['ts']<p['last_underlying_ts']:
            return
        changed = oq['ts']>p['last_option_ts'] or uq['ts']>p['last_underlying_ts']
        if not changed and now<p['flatten_at']:
            return
        p['max_gap_seconds'] = max(p['max_gap_seconds'],oq['ts']-option_last,uq['ts']-underlying_last)
        p.update(last_option_ts=oq['ts'],last_underlying_ts=uq['ts'],last_quote=oq,last_underlying_quote=uq)
        p['samples'] += 1
        mark = round(max(0,oq['bid']-SLIPPAGE),2)
        long = p['underlying_side']=='long'
        invalid = uq['bid']<=p['underlying_stop'] if long else uq['ask']>=p['underlying_stop']
        target = uq['bid']>=p['underlying_target'] if long else uq['ask']<=p['underlying_target']
        reason = ('premium_stop' if mark<=p['premium_stop'] else 'underlying_invalidation' if invalid
            else 'premium_target' if mark>=p['premium_target'] else 'underlying_target' if target
            else 'session_flatten' if now>=p['flatten_at'] else None)
        observed_exit = p['premium_target'] if reason=='premium_target' else mark
        net = (observed_exit-p['entry'])*100-2*FEE
        pct = net/(p['entry']*100+FEE)*100
        p.update(mark=observed_exit,mark_at=now,mark_quote_ts=oq['ts'],unrealized=round(net,4),mark_pct=pct)
        if p['best_pct'] is None or pct>p['best_pct']:
            p.update(best_pct=pct,peak_at=now)
        p['worst_pct'] = min(p['worst_pct'],pct) if p['worst_pct'] is not None else pct
        if reason:
            self.finish(c,p,now,reason,observed_exit)
            return
        crossed = [level for level in (10,25) if pct>=level and level not in p['milestones']]
        if crossed:
            p['milestones'].extend(crossed)
            p['milestone'] = max(crossed)
            self.notify(c,p,now,'update_'+str(max(crossed)))
        self.save(c,p,now)

    def tick(self, c, now):
        # Disabling new ideas never strands previously opened observations.
        active = c.execute(select(ideas.c.payload).where(ideas.c.status.in_(('pending','open')))
                           .order_by(ideas.c.created)).scalars().all()
        for p in active:
            observed = self.clock() if self.clock else now
            if p['status']=='pending':
                if not self.cfg.option_ideas:
                    p.update(status='excluded',finished_at=observed,exit_reason='New ideas disabled')
                    self.save(c,p,observed)
                else:
                    self.pending(c,p,observed)
            else:
                self.observe(c,p,observed)
        observed = self.clock() if self.clock else now
        self.db.put(c,'option_ideas:worker',dict(at=observed,version=VERSION,enabled=self.cfg.option_ideas))
        last = self.db.get(c,'option_ideas:log',0)
        if observed-last>=60:
            counts = dict(c.execute(select(ideas.c.status,func.count()).where(ideas.c.created>=observed-86400)
                                   .group_by(ideas.c.status)).all())
            logging.getLogger('uvicorn.error').info('Options ideas worker: version=%s enabled=%s states_24h=%s',
                VERSION,self.cfg.option_ideas,counts)
            self.db.put(c,'option_ideas:log',observed)


def snapshot(db, c, cfg, now):
    recent = c.execute(select(ideas.c.payload).order_by(ideas.c.created.desc()).limit(100)).scalars().all()
    counts = dict(c.execute(select(ideas.c.status,func.count()).where(ideas.c.created>=now-30*86400)
                           .group_by(ideas.c.status)).all())
    return dict(enabled=cfg.option_ideas,worker=db.get(c,'option_ideas:worker'),records=recent,counts=counts,
        min_dte=cfg.ideas_min_dte,max_dte=cfg.ideas_max_dte,target_dte=cfg.ideas_target_dte,
        basis='Last 100 ideas; status counts over 30 days. Independent one-contract intraday observations, not account returns.')

