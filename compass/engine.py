"""Versioned deterministic scans and simulated fills. No order routing."""
import asyncio
import logging
import time
import uuid
import re
from .market import levels,fresh,day,session,dedup
from .store import identity
from .alert_format import alert_context
from .futures import futures_session,risk_day,future_levels,active_selection,prior_rth
from .instruments import future_spec, tick_price
from .setup_study import SetupStudy
from .option_ideas import OptionIdeas
from .simulation import bracket, exit_price, FILL_VERSION, FILL_DESCRIPTION
from . import paper_risk
from .operating_mode import policy, research_notice
from sqlalchemy import select
from .setup_study import trials

VERSION="orb15-breakout-v1"

def spec(symbol):
    future = future_spec(symbol)
    if future: return future
    if symbol.startswith("O:") or (len(symbol)>15 and any(x.isdigit() for x in symbol)):
        return {"tick":.01,"multiplier":100,"fee":.65,"max_qty":1,"asset":"option"}
    return {"tick":.01,"multiplier":1,"fee":0,"max_qty":100,"asset":"stock"}

def fill(q,side,tick,entering=True):
    buying=(side=="long")==entering
    return q["ask"]+tick if buying else max(tick,q["bid"]-tick)

def candidate(symbol,previous,bar,context,now):
    if not context.get("or_complete") or not context.get("atr14") or context["atr14"]<=0: return None
    if not 0<=now-(bar["ts"]+60)<=90 or bar["ts"]<context["session_open"]+900: return None
    if bar["ts"]>=context.get("entry_end",context["session_close"]-1800) or bar["ts"]-previous["ts"]!=60: return None
    p,b=previous["payload"]["c"],bar["payload"]["c"]
    high,low=context["or_high"],context["or_low"]
    side="long" if p<=high<b else "short" if p>=low>b else None
    if not side: return None
    version=context.get("rule_version",VERSION)
    return {"id":identity(version,symbol,bar["ts"],side),"strategy":version,"symbol":symbol,
        "side":side,"signal_time":bar["ts"]+60,"decided_at":now,"signal_price":b,
        "stop_distance":max(context["atr14"]*1.5,spec(symbol)["tick"]*4),
        "reason":"Closed minute crossed the completed 15-minute "+context.get("range_name","RTH")+" opening range",
        "context":context,"bar_event":bar["id"],"status":"candidate","track":"intraday"}

class Engine:
    def __init__(self,db,cfg,clock=None):
        self.db,self.cfg=db,cfg
        self.clock=clock
        self.owner=uuid.uuid4().hex
        self.policy_logged=False
        from .scanner import Scanner
        self.scanner=Scanner(db,cfg)
        self.study=SetupStudy(db,cfg,clock)
        self.ideas=OptionIdeas(db,cfg,clock)

    specification=staticmethod(spec)

    def alert(self,c,symbol,data,key):
        kind = 'alert' if self.cfg.paper_trading else 'paper_decision'
        self.db.append(c,kind,"engine",symbol,time.time(),{"mode":"SIMULATED",**data},key)

    def entry_check(self,c,signal,now):
        symbol,side=signal["symbol"],signal["side"]
        s=spec(symbol)
        q=self.db.get(c,"quote:"+symbol)
        if self.clock:
            now=self.clock()
        hours=session(day(now))
        reason=None
        if s["asset"]=="future":
            if not futures_session(now,symbol)["entry_open"]: reason="Outside futures entry session or exchange pause"
        elif not hours or not hours[0]<=now<hours[1]-1800: reason="Outside research entry session"
        if reason: return reason,None
        if self.db.get(c,"position:"+symbol,{}).get("status")=="open": reason="Existing simulated position"
        elif not fresh(q,now) or q['ts']>now: reason="Missing, stale, locked/invalid, or empty bid/ask quote"
        elif q["ts"]<signal["signal_time"]: reason="No quote after signal"
        elif q["ask"]-q["bid"]>max(s["tick"]*8,(q["ask"]+q["bid"])/2*(.08 if s["asset"]=="option" else .002)):
            reason="Spread exceeds simulation liquidity limit"
        risk=paper_risk.account(self.db,c,now,s["asset"],persist=True)
        if self.cfg.max_entries and risk["entries"]>=self.cfg.max_entries: reason="Configured simulated entry limit"
        if sum(p.get("status")=="open" and p.get("asset")==s["asset"] for p in self.db.prefix(c,"position:").values())>=3:
            reason="Portfolio cap: three simultaneous simulated positions"
        if not risk["ready"]: reason="Paper risk migration requires reconciliation"
        if reason:
            return reason,None
        try:
            prices=bracket(signal,q,s)
        except ValueError as error:
            return str(error),None
        price,distance=prices['entry'],prices['distance']
        per_unit=distance*s["multiplier"]+2*s["fee"]+2*s["tick"]*s["multiplier"]
        if s["asset"]=="option":
            # Long-option risk is defined by the premium paid, not by a
            # stop-distance dollar cap. The legacy SHADOW_RISK_DOLLARS filter
            # is intentionally not applied to options; the 1-contract cap and
            # the spread/liquidity filters above remain the option controls.
            # Futures and stocks keep dollar-based sizing (prop drawdown).
            qty=min(s["max_qty"],int(q["ask_size"] if side=="long" else q["bid_size"]))
            if qty<1 or (side=="long" and price-distance<=0):
                return "No size available or stop invalid",None
        else:
            qty=min(s["max_qty"],int(self.cfg.risk/per_unit),int(q["ask_size"] if side=="long" else q["bid_size"]))
            if qty<1 or (side=="long" and price-distance<=0):
                return "One unit exceeds risk or stop invalid",None
        flatten=futures_session(now)["flatten_at"] if s["asset"]=="future" else hours[1]-900
        return None,{"spec":s,"quote":q,"price":price,"distance":distance,"per_unit":per_unit,"qty":qty,"risk":risk,"flatten_at":flatten,
                     'observed_at':now,'stop':prices['stop'],'target':prices['target']}

    def observe_setup(self,c,signal,now,alerted=False,primary=False):
        """Record each hypothesis independently of account and notification gates."""
        trial=self.study.start(c,signal,now,spec(signal['symbol']),alerted=alerted,primary=primary)
        if (spec(signal['symbol'])['asset']=='stock' and signal['symbol'] in self.cfg.watch_symbols
                and signal.get('track')!='swing'):
            stop=signal.get('invalidation')
            if stop is None and signal.get('stop_distance'):
                stop=signal['signal_price']-(1 if signal['side']=='long' else -1)*signal['stop_distance']
            signal={**signal,'invalidation':stop}
            if stop is not None:
                direction=1 if signal['side']=='long' else -1
                distance=(signal['signal_price']-stop)*direction
                if distance>0:
                    self.ideas.queue(c,{**signal,'stop':stop,
                        'target':signal['signal_price']+direction*2*distance,
                        'research_only':True,'notify_eligible':alerted and primary},now)
            key='pending_research_options:'+signal['id']
            if stop is not None and not self.db.get(c,key):
                self.db.put(c,key,dict(signal={**signal,'notify_eligible':alerted and primary},created_at=now,expires_at=now+120,
                    status='waiting',research_only=True))
            from .weekday_candidates import capture
            capture(self.db,c,signal,now,trial)
        return trial

    def enter(self,c,signal,now,quiet=False):
        if not self.cfg.paper_trading and signal.get('track')=='swing':
            from .operating_mode import observe_daily_breakout
            trial=observe_daily_breakout(self.db,c,signal,now,self.clock)
            if not quiet: research_notice(self.db,c,signal,trial,now)
            return False
        trial_id=self.observe_setup(c,signal,now,alerted=True,primary=True)
        if not self.cfg.paper_trading:
            if not quiet:
                trial=c.execute(select(trials.c.payload).where(trials.c.id==trial_id)).scalar_one_or_none() if trial_id else None
                research_notice(self.db,c,signal,trial,now)
            return False
        reason,plan=self.entry_check(c,signal,now)
        symbol,side=signal["symbol"],signal["side"]
        if reason:
            if quiet:
                self.db.append(c,'paper_decision','engine',symbol,now,{**signal,'status':'skipped','reason':reason},'skip:'+signal['id'])
            else:
                self.alert(c,symbol,{**signal,"status":"skipped","reason":reason},"skip:"+signal["id"])
            return False
        s,q,price,distance,per_unit,qty,risk=(plan[k] for k in ("spec","quote","price","distance","per_unit","qty","risk"))
        now=plan['observed_at']
        direction=1 if side=="long" else -1
        if s["asset"]=="option":
            # A long option's worst case is the premium paid, not the modeled
            # stop distance: R accounting uses premium + round-trip fees so a
            # full premium loss reads as exactly -1R.
            initial_risk=price*s["multiplier"]*qty+2*s["fee"]*qty
            risk_basis="premium_paid"
        else:
            initial_risk=per_unit*qty
            risk_basis="stop_distance"
        trade={**signal,**s,"status":"open","entry":price,"entered_at":now,"entry_quote_ts":q["ts"],
            "stop":plan['stop'],"target":plan['target'],"qty":qty,"initial_risk":initial_risk,
            "risk_basis":risk_basis,"fill_model":FILL_DESCRIPTION,"fill_version":FILL_VERSION,
            "last_quote_ts":q["ts"],"last_bar_checked":signal["signal_time"]-60,
            "risk_day":risk_day(now),"risk_policy":paper_risk.VERSION,
            "paper_portfolio":paper_risk.portfolio(s["asset"]),"flatten_at":plan["flatten_at"]}
        self.db.put(c,"position:"+symbol,trade)
        self.db.put(c,"trade:"+signal["id"],trade)
        risk["entries"]+=1
        self.db.put(c,paper_risk.key(now,s["asset"]),risk)
        if quiet:
            self.db.append(c,'paper_decision','engine',symbol,now,{**trade,'status':'entered'},'entry:'+signal['id'])
        else:
            self.alert(c,symbol,{**trade,"status":"entered"},"entry:"+signal["id"])
        return True

    def exits(self,c,now):
        for key,p in self.db.prefix(c,"position:").items():
            if p.get("status")!="open": continue
            q=self.db.get(c,"quote:"+p["symbol"])
            if self.clock:
                now=self.clock()
            if not fresh(q,now) or q['ts']>now or q["ts"]<=p["last_quote_ts"]:
                if now-p.get("last_quote_ts",now)>15:
                    self.alert(c,p["symbol"],{**alert_context(p),"status":"management_blocked","trade_id":p["id"],
                        "reason":"No fresh exit quote; position remains unresolved"},"stale:"+p["id"]+":"+str(int(now//300)))
                continue
            price=fill(q,p["side"],p["tick"],False)
            long=p["side"]=="long"
            trigger=(q['bid'] if long else q['ask']) if p.get('fill_version')==FILL_VERSION else price
            stopped=trigger<=p["stop"] if long else trigger>=p["stop"]
            target=trigger>=p["target"] if long else trigger<=p["target"]
            reason="stop" if stopped else "target" if target else None
            if p.get('underlying_invalidation') is not None:
                underlying=self.db.get(c,'quote:'+p['underlying'])
                if fresh(underlying,now):
                    invalidated=(underlying['bid']<=p['underlying_invalidation'] if p.get('underlying_side')=='long'
                                 else underlying['ask']>=p['underlying_invalidation'])
                    if invalidated: reason=reason or 'underlying_invalidation'
            bars=self.db.recent(c,"bar",p["symbol"],limit=120,since=p["last_bar_checked"])
            for b in sorted(bars,key=lambda b:b["ts"]):
                if b["ts"]<=p["last_bar_checked"] or b["ts"]<p["entered_at"] or b["ts"]+60>now: continue
                p["last_bar_checked"]=b["ts"]
                payload=b["payload"]
                stop_crossed=payload["l"]<=p["stop"] if long else payload["h"]>=p["stop"]
                target_touched=payload["h"]>=p["target"] if long else payload["l"]<=p["target"]
                if stop_crossed:
                    # A bar touching both levels is ambiguous intrabar; the
                    # stop wins by conservative convention.
                    reason="stop_detected_in_bar"
                    price=min(price,p["stop"]-p["tick"]) if long else max(price,p["stop"]+p["tick"])
                    break
                if target_touched:
                    # First touch in chronological bar order wins: a target
                    # touched in a completed bar fills before any later quote
                    # or bar can stop the position out.
                    reason="target_detected_in_bar"
                    price=max(price,p["target"]) if long else min(price,p["target"])
                    break
            hours=session(day(now))
            deadline=p.get("flatten_at",hours[1]-900 if hours else None)
            if p.get("track")!="swing" and deadline and now>=deadline: reason=reason or "session_flatten"
            if p.get('fill_version')==FILL_VERSION and reason and reason not in ('stop_detected_in_bar','target_detected_in_bar'):
                price=exit_price(p,q,reason)
            p["last_quote_ts"]=q["ts"]
            p["mark"]=price
            p["unrealized"]=(price-p["entry"])*(1 if long else -1)*p["qty"]*p["multiplier"]-2*p["fee"]*p["qty"]
            if reason:
                p.update(status="closed",exit=price,exited_at=now,exit_reason=reason,pnl=p["unrealized"])
                risk=paper_risk.account(self.db,c,now,p["asset"],persist=True)
                risk["realized"]+=p["pnl"]
                self.db.put(c,paper_risk.key(now,p["asset"]),risk)
                p.update(exit_risk_policy=paper_risk.VERSION,exit_risk_day=risk_day(now),
                         exit_paper_portfolio=paper_risk.portfolio(p["asset"]))
                self.alert(c,p["symbol"],p,"exit:"+p["id"])
            self.db.put(c,key,p)
            self.db.put(c,"trade:"+p["id"],p)

    def options(self,c,signal,now,quiet=False,diagnostics=None,research_only=False):
        research_only = research_only or not self.cfg.paper_trading
        chain=self.db.get(c,"chain:"+signal["symbol"],{})
        now=self.clock() if self.clock else now
        audit=diagnostics if diagnostics is not None else {}
        audit.update(version='0dte-selection-v2',at=now,status='blocked',reason=None,
            chain_age_seconds=now-chain['asof'] if chain.get('asof') is not None else None,
            chain_source=chain.get('source'),chain_complete=chain.get('complete'),
            same_day_contracts=0,checked_contracts=0,rejections=[])
        if now-chain.get("asof",0)>120:
            audit['reason']='No recent options chain'
            if not quiet:
                self.alert(c,signal["symbol"],{**alert_context(signal),"status":"options_skipped","reason":"No recent options chain",
                    "parent_signal":signal["id"]},"option-skip:"+signal["id"])
            return False
        kind="call" if signal["side"]=="long" else "put"
        opts=[o for o in chain.get("contracts",[]) if o.get("expiry")==day(now) and o.get("type")==kind and o.get("multiplier")==100]
        opts.sort(key=lambda o:abs(o["strike"]-signal["signal_price"]))
        audit['same_day_contracts']=len(opts)
        audit['contracts_truncated']=len(opts)>8
        rejected=audit['rejections']
        for o in opts[:8]:
            q=self.db.get(c,"quote:"+o["symbol"])
            checked_at=self.clock() if self.clock else now
            audit['checked_contracts']+=1
            evidence=dict(symbol=o['symbol'],checked_at=checked_at,quote_source=q.get('source') if q else None,
                quote_age_seconds=checked_at-q['ts'] if q and q.get('ts') is not None else None)
            if fresh(q,checked_at):
                option_signal={**signal,"id":identity(signal["id"],o["symbol"]),"symbol":o["symbol"],
                    "underlying":signal["symbol"],"underlying_side":signal['side'],
                    "underlying_invalidation":signal.get('invalidation'),
                    "strategy":"0dte-"+signal['strategy'] if signal.get('strategy','').startswith('compass-scanner') else "0dte-underlying-orb-v2","side":"long",
                    "stop_distance":max(.05,q["ask"]*.3)}
                if research_only:
                    # A paper balance, position, contract size or entry cap is never
                    # consulted when selecting an independent observation.
                    if q['ts']<signal['signal_time'] or q['ts']>checked_at:
                        rejected.append({**evidence,'reason':'Fresh quote after signal required'})
                        continue
                    try:
                        bracket(option_signal,q,spec(o['symbol']))
                    except ValueError as error:
                        rejected.append({**evidence,'reason':str(error)})
                        continue
                    eligible=signal.get('notify_eligible',False)
                    trial=self.study.start(c,option_signal,checked_at,spec(o['symbol']),alerted=eligible,primary=eligible)
                    if trial:
                        saved=c.execute(select(trials.c.payload).where(trials.c.id==trial)).scalar_one()
                        if saved['status']=='excluded':
                            rejected.append({**evidence,'reason':saved.get('reason') or 'Unmeasurable research observation'})
                            continue
                        if eligible:
                            self.db.append(c,'alert','setup_study',o['symbol'],checked_at,
                                {**saved,'status':'option_setup_new','last_quote':q,
                                 'expires_at':checked_at+120,'underlying_invalidation':signal.get('invalidation')},
                                'option-setup-entry:'+trial)
                        audit.update(status='observed',reason='Independent 0DTE observation recorded',
                            selected_contract=o['symbol'],selected_quote_evidence=evidence,setup_trial_id=trial)
                        return True
                    rejected.append({**evidence,'reason':'Independent study disabled'})
                    continue
                reason,_=self.entry_check(c,option_signal,checked_at)
                if reason:
                    rejected.append({**evidence,"reason":reason})
                    continue
                if self.enter(c,{**option_signal,"selection_rejections":rejected},checked_at):
                    audit.update(status='entered',reason='Eligible contract entered',selected_contract=o['symbol'],
                        selected_quote_evidence=evidence)
                    return True
                rejected.append({**evidence,'reason':'Entry recheck declined; see saved skip event'})
            else: rejected.append({**evidence,"reason":"Missing or invalid fresh quote"})
        reasons=list(dict.fromkeys(r['reason'] for r in rejected))
        audit['reason']='No eligible listed same-day '+kind+' contract' if not opts else '; '.join(reasons)
        if not quiet:
            self.alert(c,signal["symbol"],{**alert_context(signal),"status":"options_skipped","reason":"No eligible 0DTE contract passes quote, spread and risk checks","rejections":rejected,
                "parent_signal":signal["id"]},"option-skip:"+signal["id"])
        return False

    def swings(self,c,now):
        hours=session(day(now))
        if not hours or not hours[0]<=now<hours[1]-1800: return
        for symbol in self.cfg.stocks:
            raw=self.db.recent(c,"daily",symbol,limit=150)
            unique={day(r["ts"]):r for r in reversed(raw) if day(r["ts"])<day(now)}
            rows=sorted(unique.values(),key=lambda r:r["ts"])
            if len(rows)<22: continue
            last,prior=rows[-1],rows[-21:-1]
            sid=identity("swing20-v1",symbol,last["ts"])
            if self.db.get(c,"seen:"+sid) or now-last["ts"]>5*86400: continue
            if last["payload"]["c"]<=max(b["payload"]["h"] for b in prior): continue
            if self.cfg.paper_trading and not fresh(self.db.get(c,"quote:"+symbol),now): continue
            signal={"id":sid,"strategy":"swing20-v1","symbol":symbol,"side":"long","signal_time":last["ts"]+86400,
                "decided_at":now,"signal_price":last["payload"]["c"],"track":"swing","status":"candidate",
                "stop_distance":max((max(b["payload"]["h"] for b in prior)-min(b["payload"]["l"] for b in prior))*.2,.1),
                "reason":"Completed daily close above preceding 20 daily highs","bar_event":last["id"],
                "daily":{"through":day(last['ts']),"close":last['payload']['c']}}
            self.enter(c,signal,now)
            self.db.put(c,"seen:"+sid,{"at":now})

    def tick(self,now=None):
        now=now or time.time()
        with self.db.tx() as c:
            if not self.db.lease(c,"engine",self.owner,30): return
            if self.cfg.paper_trading:
                paper_risk.ledgers(self.db,c,now,persist=True)
            active_policy=policy(self.cfg)
            previous=self.db.get(c,'operating_policy',{})
            activated=previous.get('activated_at',now) if previous.get('mode')==active_policy['mode'] and previous.get('version')==active_policy['version'] else now
            self.db.put(c,'operating_policy',dict(active_policy,at=now,activated_at=activated))
            if not self.policy_logged:
                logging.getLogger('uvicorn.error').info('Compass operating policy: %s',active_policy)
                self.policy_logged=True
            self.study.tick(c,now)
            self.exits(c,now)
            active={p["raw_symbol"] for p in active_selection(self.db,c,self.cfg,now)}
            future_symbols={k[6:] for k in self.db.prefix(c,"quote:") if "@" in k and k[6:].split("@")[0] in active}
            future_symbols|={k[10:] for k in self.db.prefix(c,"latestbar:") if "@" in k and k[10:].split("@")[0] in active}
            symbols=list(self.cfg.stocks)+sorted(future_symbols)
            for symbol in symbols:
                latest=self.db.get(c,"latestbar:"+symbol)
                if latest is not None and latest<=self.db.get(c,"cursor:"+symbol,0): continue
                rows=dedup(self.db.recent(c,"bar",symbol,limit=8000,since=now-8*86400),now)
                if len(rows)<2: continue
                bar=rows[-1]
                if bar["ts"]<=self.db.get(c,"cursor:"+symbol,0): continue
                future=spec(symbol)["asset"]=="future"
                coverage=self.db.get(c,"historycoverage:"+symbol+":"+prior_rth(risk_day(now))[0]) if future else None
                context=future_levels(rows,now,coverage,symbol) if future else levels(rows,bar["ts"]+60)
                self.db.put(c,"levels:"+symbol,context)
                if future and self.cfg.setup_study:
                    from .scanner import features
                    from .futures_variants import observe_bar
                    f=features(symbol,rows,now,coverage=coverage)
                    observe_bar(self.db,c,symbol,f,now)
                    from .futures_research import observe
                    observe(self.db,c,symbol,f,now,self.study,spec(symbol),orb_enabled=self.cfg.orb_setups)
                s=candidate(symbol,rows[-2],bar,context,now) if self.cfg.orb_setups else None
                if s:
                    self.db.append(c,"signal","engine",symbol,s["signal_time"],s,s["id"])
                    self.enter(c,s,now)
                    if symbol in self.cfg.stocks and self.cfg.paper_trading: self.options(c,s,now)
                self.db.put(c,"cursor:"+symbol,bar["ts"])
            self.swings(c,now)
            if self.cfg.scanner:
                self.scanner.scan(c,now,self)
            else:
                self.scanner.retry_options(c,{k[6:]:q for k,q in self.db.prefix(c,'quote:').items()},now,self)
            magnet_summary = None
            if self.cfg.apex_magnet:
                from .apex_magnet import scan as apex_magnet_scan
                magnet_summary = apex_magnet_scan(self.db,c,self.cfg,now)
            tape_summary = None
            if self.cfg.tape_confirmed:
                from .tape_confirmed import scan as tape_confirmed_scan
                tape_summary = tape_confirmed_scan(self.db,c,self.cfg,now)
            gap_summary = None
            if self.cfg.gap_continuation:
                from .gap_continuation import scan as gap_continuation_scan
                gap_summary = gap_continuation_scan(self.db,c,self.cfg,now)
            breakout_summary = None
            if self.cfg.breakouts:
                from .breakouts import scan as breakouts_scan
                breakout_summary = breakouts_scan(self.db,c,self.cfg,now)
            board_summary = None
            if self.cfg.day_trading_board:
                from .day_trading_board import scan as day_board_scan
                board_summary = day_board_scan(self.db,c,self.cfg,now)
            # ICT futures concept detectors (Phase 1: evidence-only). Ordered
            # by dependency: liquidity map -> HTF levels -> event detectors ->
            # confluence tiering.
            ict_summaries = {}
            if self.cfg.ict_session_liquidity:
                from .session_liquidity import scan as ict_session_liquidity_scan
                ict_summaries['session_liquidity'] = ict_session_liquidity_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_htf_levels:
                from .htf_levels import scan as ict_htf_levels_scan
                ict_summaries['htf_levels'] = ict_htf_levels_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_turtle_soup:
                from .turtle_soup import scan as ict_turtle_soup_scan
                ict_summaries['turtle_soup'] = ict_turtle_soup_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_smt_divergence:
                from .smt_divergence import scan as ict_smt_divergence_scan
                ict_summaries['smt_divergence'] = ict_smt_divergence_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_aoi_zones:
                from .aoi_zones import scan as ict_aoi_zones_scan
                ict_summaries['aoi_zones'] = ict_aoi_zones_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_continuation:
                from .continuation import scan as ict_continuation_scan
                ict_summaries['continuation'] = ict_continuation_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_trendline_liquidity:
                from .trendline_liquidity import scan as ict_trendline_liquidity_scan
                ict_summaries['trendline_liquidity'] = ict_trendline_liquidity_scan(self.db,c,self.cfg,now)
            if self.cfg.ict_tier_a_b:
                from .tier_a_b import scan as ict_tier_a_b_scan
                ict_summaries['tier_a_b'] = ict_tier_a_b_scan(self.db,c,self.cfg,now)
            self.ideas.tick(c,now)
            from .observation_recovery import tick as recovery_tick
            recovery_tick(self.db,c,self.clock() if self.clock else now)
            self.db.put(c,"worker:engine",{"at":now,"mode":policy(self.cfg)['mode'],"strategy_version":VERSION,"orb_setups_enabled":self.cfg.orb_setups,"morning_enabled":self.cfg.morning_enabled})
        if magnet_summary and magnet_summary.get('ran'):
            self.db.health('apex_magnet','running','%d symbols in radius, %d excluded' % (
                magnet_summary['symbols'], sum(magnet_summary['excluded'].values())))
        if tape_summary and tape_summary.get('ran'):
            self.db.health('tape_confirmed','running','%d evaluated, %d confirmed' % (
                tape_summary['evaluated'], tape_summary['confirmed']))
        if gap_summary and gap_summary.get('ran'):
            self.db.health('gap_continuation','running','%d symbols, %d qualified' % (
                gap_summary['symbols'], gap_summary['qualified']))
        if breakout_summary and breakout_summary.get('ran'):
            self.db.health('breakouts','running','%d new events, %d forming' % (
                breakout_summary['new_events'], breakout_summary['forming']))
        if board_summary and board_summary.get('ran'):
            self.db.health('day_trading_board','running','board %s, %d symbols' % (
                board_summary.get('action') or 'steady', board_summary['symbols']))
        for _ict_name in ('session_liquidity','htf_levels','turtle_soup','smt_divergence',
                          'aoi_zones','continuation','trendline_liquidity','tier_a_b'):
            _s = ict_summaries.get(_ict_name)
            if _s and _s.get('ran'):
                self.db.health('ict_' + _ict_name, 'running',
                               'ran, %d symbols' % (_s.get('symbols', 0)))

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.tick)
                self.db.health("engine","running","Independent setup research; paper entries " + ('enabled' if self.cfg.paper_trading else 'paused'),time.time())
            except asyncio.CancelledError: raise
            except Exception as e:
                logging.getLogger('uvicorn.error').exception('Engine scan failed')
                try:
                    self.db.health("engine","error",type(e).__name__)
                except Exception:
                    logging.getLogger('uvicorn.error').exception('Cannot record engine health')
            await asyncio.sleep(2)
