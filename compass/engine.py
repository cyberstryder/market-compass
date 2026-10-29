"""Versioned deterministic scans and simulated fills. No order routing."""
import asyncio
import logging
import time
import uuid
import re
from datetime import date
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
        from .ice_shadow import IceShadow
        self.ice_shadow=IceShadow(db,cfg)

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
        else:
            # Options get a shorter late-session entry cutoff (default 5 min,
            # matching the option flatten deadline) since late-day is prime
            # 0DTE time; stocks keep the 30-minute research cutoff.
            cutoff=self.cfg.option_entry_cutoff_min*60 if s["asset"]=="option" else 1800
            if not hours or not hours[0]<=now<hours[1]-cutoff: reason="Outside research entry session"
        if reason: return reason,None
        if self.db.get(c,"position:"+symbol,{}).get("status")=="open": reason="Existing simulated position"
        # 0DTE option quotes routinely go 15-60s without an update on all but
        # the hottest strikes; options use a dedicated freshness age.
        quote_age=self.cfg.option_quote_max_age if s["asset"]=="option" else 5
        if not fresh(q,now,quote_age) or q['ts']>now: reason="Missing, stale, locked/invalid, or empty bid/ask quote"
        elif q["ts"]<signal["signal_time"]: reason="No quote after signal"
        elif q["ask"]-q["bid"]>max(s["tick"]*8,(q["ask"]+q["bid"])/2*(self.cfg.option_max_spread_pct if s["asset"]=="option" else .002)):
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
        flatten=futures_session(now)["flatten_at"] if s["asset"]=="future" else hours[1]-self.cfg.option_flatten_min*60
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
                # Selection window 30 minutes (2026-10-27, was 120s): the
                # funnel audit showed the 120s ticket passing ~0.3% of
                # signals, with fresh-quote timing the top killer. The
                # research selection may retry for 30 minutes; the alert
                # itself (option_setup_new) keeps its 120s freshness.
                self.db.put(c,key,dict(signal={**signal,'notify_eligible':alerted and primary},created_at=now,expires_at=now+1800,
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
        if spec(signal["symbol"])["asset"]=="stock" and not self.cfg.stock_paper_trades:
            skip_reason="Stock paper entries disabled (options and futures only)"
            skipped={**signal,"status":"skipped","reason":skip_reason}
            if quiet:
                self.db.append(c,'paper_decision','engine',signal["symbol"],now,skipped,'skip:'+signal["id"])
            else:
                self.alert(c,signal["symbol"],skipped,"skip:"+signal["id"])
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
        positions=self.db.prefix(c,"position:")
        for key,p in positions.items():
            if p.get("status")!="open": continue
            q=self.db.get(c,"quote:"+p["symbol"])
            if q is None and p.get("asset")=="future":
                # Futures quotes land under dated-contract keys
                # (quote:MNQZ25@<iid>) while positions are stored under the
                # alias form (MNQ.c.0). Resolve so exit management sees them.
                from .ict_common import resolve_quote_key
                resolved=resolve_quote_key(self.db,c,p["symbol"])
                if resolved:
                    q=self.db.get(c,"quote:"+resolved)
            if self.clock:
                now=self.clock()
            hours=session(day(now))
            deadline=p.get("flatten_at",hours[1]-self.cfg.option_flatten_min*60 if hours else None)
            # Prop-firm rule: flat at end of trading day, never held between
            # sessions or over the weekend. Time-based; fires even when quotes
            # have gone stale (e.g. after the close). Exit at the last mark.
            # Also flattens any open futures position while the market is
            # closed (outside the intraday halt), so a missed deadline can
            # never carry a position into the next session.
            flatten_due = p.get("track")!="swing" and deadline and now>=deadline
            if not flatten_due and p.get("asset")=="future":
                s=futures_session(now,p["symbol"])
                if not s["is_open"] and not (s["halt_start"]<=now<s["halt_end"]):
                    flatten_due=True
            if flatten_due:
                price=p.get("mark",p["entry"])
                long=p["side"]=="long"
                pnl=(price-p["entry"])*(1 if long else -1)*p["qty"]*p["multiplier"]-2*p["fee"]*p["qty"]
                p.update(status="closed",exit=price,exited_at=now,exit_reason="session_flatten",
                         pnl=pnl,unrealized=pnl,mark=price)
                risk=paper_risk.account(self.db,c,now,p["asset"],persist=True)
                risk["realized"]+=p["pnl"]
                self.db.put(c,paper_risk.key(now,p["asset"]),risk)
                p.update(exit_risk_policy=paper_risk.VERSION,exit_risk_day=risk_day(now),
                         exit_paper_portfolio=paper_risk.portfolio(p["asset"]))
                self.alert(c,p["symbol"],p,"exit:"+p["id"])
                self.db.put(c,key,p)
                self.db.put(c,"trade:"+p["id"],p)
                # ICT exit alert
                if p["id"].startswith("ict-"):
                    try:
                        from . import ict_push
                        ict_push.maybe_queue_exit(self.db, c, self.cfg, p)
                    except Exception:
                        pass
                    # TradersPost flatten (2026-10-14): send close signal so
                    # live TradersPost positions flatten at 15:45 CT, matching
                    # paper. Only send once per ticker per flatten (the T1 leg
                    # triggers it; T2/Trail legs will find no open TradersPost
                    # position and the close is a no-op).
                    # Use leg suffix to dedupe: only T1 sends the close.
                    # Audit #4: queue the close in this transaction — the
                    # POST is dispatched after the tick commits, never
                    # from inside it. ref (the position id) is the close's
                    # send-once identity.
                    # 2026-10-25: gated on traderspost_sent, same as the
                    # exits() close below — this branch previously queued a
                    # close for ANY -t1 position, including ones never sent
                    # (duplicate/hedging skips, and now paper-only detectors
                    # golden_zone/rumers_box). A never-sent position must
                    # never emit a broker close.
                    if p["id"].endswith("-t1") and p.get("traderspost_sent"):
                        try:
                            from . import traderspost
                            traderspost.queue_close_signal(self.db, c, p["symbol"], ref=p["id"])
                        except Exception:
                            pass
                continue
            # Per-symbol freshness: quote arrival gaps vary by liquidity.
            # MNQ/MES tick every ~0.5s; SIL/YM can go 2-4s between quotes.
            # Thresholds set at ~10x normal max gap (freshness) and ~30x (alert).
            sym_root = p["symbol"].split('.')[0]
            if sym_root in ('MNQ', 'MES', 'ES', 'NQ'):
                fresh_age, alert_age = 10, 30
            elif sym_root in ('SIL', 'SI', 'YM'):
                fresh_age, alert_age = 20, 60
            else:  # MGC, MCL, GC, CL, MYM, etc.
                fresh_age, alert_age = 15, 45
            if not fresh(q,now,age=fresh_age) or q['ts']>now or q["ts"]<=p["last_quote_ts"]:
                if now-p.get("last_quote_ts",now)>alert_age:
                    # Diagnose for the alert: is the quote stream missing
                    # entirely, or just stale? Drives very different fixes.
                    if q is None:
                        quote_state = "no quote stream"
                    elif q.get("ts") is None:
                        quote_state = "quote has no timestamp"
                    else:
                        age = now - q["ts"]
                        quote_state = "quote %.1fs old" % age if age >= 0 else "quote from the future"
                    # This position flew blind: stops/targets could not be
                    # verified. Mark it so the eventual exit is never counted
                    # as a strategy win or loss.
                    if not p.get("data_gap"):
                        p["data_gap"] = True
                        p["data_gap_since"] = now
                        self.db.put(c, key, p)
                    self.alert(c,p["symbol"],{**alert_context(p),"status":"management_blocked","trade_id":p["id"],
                        "reason":"No fresh exit quote; position remains unresolved",
                        "quote_state": quote_state,
                        "entry":p.get("entry"),"stop":p.get("stop"),"target":p.get("target"),
                        "qty":p.get("qty",1)},"stale:"+p["id"]+":"+str(int(now//300)))
                continue
            price=fill(q,p["side"],p["tick"],False)
            long=p["side"]=="long"
            trigger=(q['bid'] if long else q['ask']) if p.get('fill_version')==FILL_VERSION else price
            # stop=None: bracket-less horizon trades (Claude survivors
            # S1/S3, 2026-10-28) exit on their flatten_at only. Every
            # pre-existing record carries a numeric stop, so the guard
            # is inert for them.
            stopped=(trigger<=p["stop"] if long else trigger>=p["stop"]) if p.get("stop") is not None else False
            # A position with no fixed target (target None) never
            # target-exits in this loop; it exits on its stop or the
            # session flatten. (The 2/2/1 trailer legs this comment used
            # to describe were removed 2026-10-18 — single 2-contract leg.)
            tgt = p.get("target")
            target = (trigger>=tgt if long else trigger<=tgt) if tgt is not None else False
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
                stop_crossed=(payload["l"]<=p["stop"] if long else payload["h"]>=p["stop"]) if p.get("stop") is not None else False
                target_touched=(payload["h"]>=tgt if long else payload["l"]<=tgt) if tgt is not None else False
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
                # Variant B progressive stops (Josh 2026-10-08):
                # T1 (1R) hit → T2 and runner stops → breakeven
                # T2 (2R) hit → runner stop → 1R profit (ratchet up only)
                # NOTE: mutate the loop's `positions` snapshot objects, not a
                # fresh db.get copy. The loop's bottom put() persists the
                # snapshot object, so updates to a detached copy are clobbered
                # when this tick later reaches that key. The immediate db.put
                # covers the case where that key already ran earlier this tick.
                if reason in ("target", "target_detected_in_bar"):
                    try:
                        leg = p.get("leg")
                        entry = p.get("entry")
                        side_long = p.get("side") == "long"
                        if leg == "t1":
                            # Move T2 to breakeven
                            t2_key = key.replace(":t1", ":t2") if key.endswith(":t1") else None
                            if t2_key:
                                t2 = positions.get(t2_key) or self.db.get(c, t2_key)
                                if t2 and t2.get("status") == "open":
                                    t2["stop"] = t2["entry"]
                                    t2["breakeven_moved"] = True
                                    t2["breakeven_at"] = now
                                    t2["breakeven_reason"] = "t1_target_hit"
                                    self.db.put(c, t2_key, t2)
                                    self.db.put(c, "trade:"+t2["id"], t2)
                            # Ensure runner stop is at least breakeven
                            trail_key = key.replace(":t1", ":trail") if key.endswith(":t1") else None
                            if trail_key:
                                tr = positions.get(trail_key) or self.db.get(c, trail_key)
                                if tr and tr.get("status") == "open":
                                    be = tr["entry"]
                                    cur = tr.get("stop")
                                    # Only ratchet up (for long: stop up; for short: stop down)
                                    if side_long and (cur is None or cur < be):
                                        tr["stop"] = be
                                    elif not side_long and (cur is None or cur > be):
                                        tr["stop"] = be
                                    self.db.put(c, trail_key, tr)
                                    self.db.put(c, "trade:"+tr["id"], tr)
                        elif leg == "t2":
                            # Move runner stop to 1R profit (ratchet up only)
                            trail_key = key.replace(":t2", ":trail") if key.endswith(":t2") else None
                            if trail_key:
                                tr = positions.get(trail_key) or self.db.get(c, trail_key)
                                if tr and tr.get("status") == "open":
                                    # 1R profit level, derived from the t2 leg's own
                                    # target (the 2R level): t2["stop"] is already at
                                    # breakeven by now (moved on the t1 hit), so it
                                    # cannot be used to recover the original risk.
                                    # Using it made this step a silent no-op.
                                    t2_target = p.get("target")
                                    if t2_target is not None and entry is not None:
                                        one_r = entry + (t2_target - entry) / 2.0
                                        cur = tr.get("stop")
                                        if side_long and (cur is None or cur < one_r):
                                            tr["stop"] = one_r
                                            tr["locked_1r"] = True
                                        elif not side_long and (cur is None or cur > one_r):
                                            tr["stop"] = one_r
                                            tr["locked_1r"] = True
                                        self.db.put(c, trail_key, tr)
                                        self.db.put(c, "trade:"+tr["id"], tr)
                    except Exception:
                        pass
            self.db.put(c,key,p)
            self.db.put(c,"trade:"+p["id"],p)
            # ICT exit alert (target/stop)
            if p.get("status") == "closed" and p["id"].startswith("ict-"):
                try:
                    from . import ict_push
                    ict_push.maybe_queue_exit(self.db, c, self.cfg, p)
                except Exception:
                    pass
            # Fail-closed TradersPost stop protection (Josh 2026-10-20):
            # when a live-sent ICT trade stops out in paper, fire the
            # TradersPost exit for the ticker even though the broker
            # bracket stop should already have closed it. A bracket stop
            # can be REJECTED at the broker when the live fill slips to
            # or past the stop price (2026-10-20 07:06 CT: MNQ fill
            # 29511.25 vs stop 29512.75 — stop leg rejected, live
            # position left naked while paper stopped out). TradersPost
            # 'exit' on an already-flat ticker is a no-op, so this costs
            # nothing when the bracket worked and closes a naked
            # position seconds after the cross when it did not.
            # Audit #4: the exit is QUEUED here (inside the tick
            # transaction) and POSTed by tick-end dispatch after commit;
            # ref (the position id) makes it send-once.
            #
            # 2026-10-23: extended from stop-only to EVERY paper close of a
            # live-sent position (target, target_detected_in_bar,
            # underlying_invalidation, ...). The live bracket's take-profit
            # is priced off the signal reference, not the actual fill
            # (2026-10-20 17:20 CT MCL: TP above fill), so a paper target
            # hit does not imply the live TP filled. Without this, paper
            # closes and frees the symbol while the live position persists,
            # and the next signal stacks on top of it. 'exit' on a flat
            # ticker is a no-op at TradersPost, so the extra send is free.
            if p.get("status") == "closed" and p["id"].startswith("ict-") \
                    and p["id"].endswith("-t1") \
                    and p.get("traderspost_sent"):
                try:
                    from . import traderspost
                    traderspost.queue_close_signal(self.db, c, p["symbol"], ref=p["id"])
                except Exception:
                    pass

    def options(self,c,signal,now,quiet=False,diagnostics=None,research_only=False):
        research_only = research_only or not self.cfg.paper_trading
        chain=self.db.get(c,"chain:"+signal["symbol"],{})
        now=self.clock() if self.clock else now
        audit=diagnostics if diagnostics is not None else {}
        audit.update(version='0dte-selection-v3',at=now,status='blocked',reason=None,
            chain_age_seconds=now-chain['asof'] if chain.get('asof') is not None else None,
            chain_source=chain.get('source'),chain_complete=chain.get('complete'),
            eligible_contracts=0,checked_contracts=0,rejections=[],
            max_dte=self.cfg.option_0dte_max_dte)
        if now-chain.get("asof",0)>120:
            audit['reason']='No recent options chain'
            if not quiet:
                self.alert(c,signal["symbol"],{**alert_context(signal),"status":"options_skipped","reason":"No recent options chain",
                    "parent_signal":signal["id"]},"option-skip:"+signal["id"])
            return False
        kind="call" if signal["side"]=="long" else "put"
        today=day(now)
        # Swings use 7-21 DTE (not 0DTE). 0DTE uses cfg.option_0dte_max_dte.
        if signal.get('strategy','').startswith('swing-'):
            max_dte = 21  # Swing options: up to 21 DTE
            min_dte = 7   # At least 7 DTE to allow time for move
        else:
            max_dte = self.cfg.option_0dte_max_dte
            min_dte = 0
        def eligible(o):
            # Same-day plus weekly expiries: names without daily expirations (e.g. MU)
            # still get 0DTE-style coverage on their nearest weekly.
            if o.get("type")!=kind or o.get("multiplier")!=100: return False
            try: dte=(date.fromisoformat(str(o.get("expiry")))-date.fromisoformat(today)).days
            except (ValueError,TypeError): return False
            return min_dte<=dte<=max_dte
        opts=[o for o in chain.get("contracts",[]) if eligible(o)]
        # Nearest expiry first (most gamma for 0DTE-style moves), then closest strike.
        opts.sort(key=lambda o:(str(o.get("expiry")),abs(o["strike"]-signal["signal_price"])))
        audit['eligible_contracts']=len(opts)
        audit['contracts_truncated']=len(opts)>8
        rejected=audit['rejections']
        for o in opts[:8]:
            q=self.db.get(c,"quote:"+o["symbol"])
            checked_at=self.clock() if self.clock else now
            audit['checked_contracts']+=1
            evidence=dict(symbol=o['symbol'],checked_at=checked_at,quote_source=q.get('source') if q else None,
                quote_age_seconds=checked_at-q['ts'] if q and q.get('ts') is not None else None)
            if fresh(q,checked_at,self.cfg.option_quote_max_age):
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
                            # Compass evidence snapshot (audit #8): rendered
                            # into the Discord alert by alert_ownership and
                            # mirrored onto the paper trade at booking time
                            # (zero_dte_paper rebuilds it fresh at entry).
                            try:
                                from .entry_evidence import build as _entry_evidence
                                evidence_snapshot=_entry_evidence(self.db,c,signal['symbol'],checked_at,
                                    price=signal.get('signal_price'))
                            except Exception:
                                evidence_snapshot=None
                            # Publication metadata required for zero_dte_paper._open_scanner
                            # to find this alert (filters on publication.category == "options_0dte").
                            # Without it, scanner observations never become paper trades.
                            self.db.append(c,'alert','setup_study',o['symbol'],checked_at,
                                {**saved,'status':'option_setup_new','last_quote':q,
                                 'expires_at':checked_at+120,'underlying_invalidation':signal.get('invalidation'),
                                 'evidence_snapshot':evidence_snapshot,
                                 'publication': {
                                     'category': 'options_0dte',
                                     'contract': {
                                         'symbol': o['symbol'],
                                         'underlying': signal['symbol'],
                                         'strike': o.get('strike'),
                                         'type': o.get('type'),
                                         'expiry': o.get('expiry'),
                                     },
                                     'status': 'option_setup_new',
                                 }},
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
        audit['reason']='No eligible listed '+kind+' contract within '+str(max_dte)+' DTE' if not opts else '; '.join(reasons)
        if not quiet:
            self.alert(c,signal["symbol"],{**alert_context(signal),"status":"options_skipped","reason":"No eligible 0DTE contract passes quote, spread and risk checks","rejections":rejected,
                "parent_signal":signal["id"]},"option-skip:"+signal["id"])
        return False

    def swings(self,c,now):
        """Options swings: pullback after 20-day breakout.

        Changed 2026-10-17: Was stock breakout (blocked by options-only rule).
        Now generates options signals: detect breakout, wait for pullback to
        breakout level or 20 EMA, then buy CALL (bullish) or PUT (bearish)
        with 14 DTE. Uses options() to find contract and create alert.
        """
        hours=session(day(now))
        if not hours or not hours[0]<=now<hours[1]-1800: return
        for symbol in self.cfg.stocks:
            raw=self.db.recent(c,"daily",symbol,limit=150)
            unique={day(r["ts"]):r for r in reversed(raw) if day(r["ts"])<day(now)}
            rows=sorted(unique.values(),key=lambda r:r["ts"])
            if len(rows)<22: continue
            # Look for breakout in last 5 days
            for i in range(len(rows)-5, len(rows)):
                if i < 20: continue  # Need 20 prior bars for breakout check
                breakout_bar = rows[i]
                prior = rows[i-20:i]
                breakout_high = max(b["payload"]["h"] for b in prior)
                breakout_low = min(b["payload"]["l"] for b in prior)
                # Bullish breakout: close above 20-day high
                is_bullish = breakout_bar["payload"]["c"] > breakout_high
                # Bearish breakdown: close below 20-day low
                is_bearish = breakout_bar["payload"]["c"] < breakout_low
                if not (is_bullish or is_bearish):
                    continue
                # Check for pullback in subsequent bars (up to today)
                for j in range(i+1, len(rows)):
                    pullback_bar = rows[j]
                    current_price = pullback_bar["payload"]["c"]
                    # 20 EMA (approx from closes)
                    ema20 = sum(r["payload"]["c"] for r in rows[max(0,j-20):j]) / min(20, j)
                    # Pullback to breakout level (within 1%) or to 20 EMA (within 1%)
                    if is_bullish:
                        breakout_level = breakout_high
                        pulled_back = abs(current_price - breakout_level) / breakout_level < 0.01 or \
                                     abs(current_price - ema20) / ema20 < 0.01
                        side = "long"
                        kind = "call"
                    else:
                        breakout_level = breakout_low
                        pulled_back = abs(current_price - breakout_level) / breakout_level < 0.01 or \
                                     abs(current_price - ema20) / ema20 < 0.01
                        side = "short"
                        kind = "put"
                    if not pulled_back:
                        continue
                    sid = identity("swing-options-v1", symbol, pullback_bar["ts"], side)
                    if self.db.get(c, "seen:"+sid):
                        continue
                    if now - pullback_bar["ts"] > 5*86400:
                        continue
                    if self.cfg.paper_trading and not fresh(self.db.get(c, "quote:"+symbol), now):
                        continue
                    signal = {
                        "id": sid, "strategy": "swing-options-v1", "symbol": symbol,
                        "side": side, "signal_time": pullback_bar["ts"],
                        "decided_at": now, "signal_price": current_price,
                        "track": "swing", "status": "candidate",
                        "stop_distance": max(abs(current_price - breakout_level) * 0.5, 0.1),
                        "reason": f"Pullback after 20-day {'breakout' if is_bullish else 'breakdown'}",
                        "bar_event": pullback_bar["id"],
                        "daily": {"through": day(pullback_bar['ts']), "close": current_price},
                        "swing_breakout_level": breakout_level,
                        "swing_breakout_date": day(breakout_bar["ts"]),
                    }
                    # Use options() to find contract and create alert (not enter() which blocks stocks)
                    self.options(c, signal, now)
                    self.db.put(c, "seen:"+sid, {"at": now})
                    break


    def _pre_ict_scans(self,c,now):
        """Pre-ICT tick scans (swings, earnings, scanner, magnet, tape,
        pulse, 0DTE, darkpool, IV rank, squeeze, gaps, breakouts, day
        board) as one failure-contained unit.

        2026-10-22 second-stage freeze: after the levels pass was
        isolated, these scans still shared one transaction with the
        ICT block, and on the strained production database their
        cumulative work crossed the transaction timeout on essentially
        every tick (deaths observed in scanner.retry_options' quote
        prefix scan and tape_confirmed's recents read). The ICT
        detectors — individually light KV readers — never got a turn
        for 35+ hours even though none of them ever crashed. tick()
        now runs this unit in its own transaction under try/except
        and proceeds to the ICT block in a fresh transaction whether
        or not this unit survived. Returns the summary dict the
        post-ICT health writes consume."""
        self.swings(c,now)
        # Earnings plays: post-earnings gap momentum (runs 9:45-10:30 AM CT)
        # Checks OPRA tickers for earnings gaps >3%, generates 0DTE signals
        try:
            from .earnings_play import scan_earnings_plays
            from .tradermatrix_intel import OPRA_TICKERS
            earnings_signals = scan_earnings_plays(self.db, c, self.cfg, now, list(OPRA_TICKERS))
            for sig in earnings_signals:
                # The publication contract must be the resolved listed
                # option (OCC symbol), never the bare underlying ticker:
                # zero_dte_paper books whatever symbol is here, and a stock
                # symbol produced a phantom stock-priced "option" trade.
                contract = sig.get('contract') or {}
                if not contract.get('symbol'):
                    logging.getLogger('uvicorn.error').warning(
                        f"Earnings play {sig['symbol']}: signal has no resolved contract, skipping alert")
                    continue
                # Create alert for 0DTE paper trading (same as scanner signals)
                # Uses publication.category = options_0dte so zero_dte_paper picks it up
                self.db.append(c, 'alert', 'setup_study', sig['symbol'], now, {
                    'strategy': 'earnings-play-v1',
                    'symbol': sig['symbol'],
                    'direction': sig['direction'],
                    'option_type': sig['option_type'],
                    'entry': sig['entry'],
                    'stop': sig['stop'],
                    'target': sig['target'],
                    'gap_pct': sig['gap_pct'],
                    'earnings_date': sig['earnings_date'],
                    'publication': {
                        'category': 'options_0dte',
                        'contract': {
                            'symbol': contract['symbol'],
                            'underlying': sig['symbol'],
                            'strike': contract.get('strike'),
                            'type': contract.get('type'),
                            'expiry': contract.get('expiry'),
                        },
                        'status': 'option_setup_new',
                    },
                }, f"earnings-{sig['symbol']}-{int(now)}")
        except Exception as e:
            # NOTE: Do NOT import logging here - it shadows module-level import
            logging.getLogger('uvicorn.error').warning(f"Earnings play scan failed: {e}")
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
        pulse_summary = None
        if self.cfg.flow_pulse:
            from .flow_pulse import scan as flow_pulse_scan
            pulse_summary = flow_pulse_scan(self.db,c,self.cfg,now)
        zero_dte_summary = None
        if getattr(self.cfg, 'zero_dte_paper', True):
            from .zero_dte_paper import scan as zero_dte_paper_scan
            zero_dte_summary = zero_dte_paper_scan(self.db,c,self.cfg,now)
        darkpool_summary = None
        if self.cfg.darkpool:
            from .darkpool import scan as darkpool_scan
            darkpool_summary = darkpool_scan(self.db,c,self.cfg,now)
        iv_rank_summary = None
        if self.cfg.iv_rank:
            from .iv_rank import scan as iv_rank_scan
            iv_rank_summary = iv_rank_scan(self.db,c,self.cfg,now)
        squeeze_summary = None
        if self.cfg.squeeze:
            from .squeeze import scan as squeeze_scan
            squeeze_summary = squeeze_scan(self.db,c,self.cfg,now)
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
        return {'magnet_summary': magnet_summary,
                'tape_summary': tape_summary,
                'pulse_summary': pulse_summary,
                'zero_dte_summary': zero_dte_summary,
                'darkpool_summary': darkpool_summary,
                'iv_rank_summary': iv_rank_summary,
                'squeeze_summary': squeeze_summary,
                'gap_summary': gap_summary,
                'breakout_summary': breakout_summary,
                'board_summary': board_summary}

    def _levels_pass(self,now):
        """Per-symbol levels/entry pass, isolated per symbol.

        2026-10-22 root-cause fix: this loop used to run inside tick's
        single transaction, re-fetching up to 8000 bars per active
        symbol. When Postgres slowed (post volume-full bloat plus
        archive contention), the transaction hit the transaction
        timeout mid-loop, the connection was terminated, and every
        later tick section (swings, scanner, 0DTE, ICT detectors,
        end-of-day flatten) starved. Cursors never advanced, so each
        tick retried the full backlog and died again \u2014 the
        evaluation layer froze at 2026-10-21 08:13 CT and never
        recovered across restarts. Now the pass runs between tick's
        two transaction segments, each symbol in its own transaction
        with its own failure boundary: one slow symbol loses only its
        own levels update for this tick."""
        with self.db.tx() as c:
            active={p["raw_symbol"] for p in active_selection(self.db,c,self.cfg,now)}
            future_symbols={k[6:] for k in self.db.prefix(c,"quote:") if "@" in k and k[6:].split("@")[0] in active}
            future_symbols|={k[10:] for k in self.db.prefix(c,"latestbar:") if "@" in k and k[10:].split("@")[0] in active}
            symbols=list(self.cfg.stocks)+sorted(future_symbols)
        for symbol in symbols:
            try:
                self._levels_symbol(symbol,now)
            except Engine.LeaseLost:
                raise
            except Exception as e:
                logging.getLogger('uvicorn.error').error(
                    f'Levels pass failed for {symbol}: {e}')
                try:
                    with self.db.tx() as c:
                        self.db.put(c,'health:levels_error',
                                    {'symbol':symbol,'error':str(e)[:300],'at':now})
                except Exception:
                    pass

    def _levels_symbol(self,symbol,now):
        with self.db.tx() as c:
            self._hold(c)
            latest=self.db.get(c,"latestbar:"+symbol)
            if latest is not None and latest<=self.db.get(c,"cursor:"+symbol,0): return
            rows=dedup(self.db.recent(c,"bar",symbol,limit=8000,since=now-8*86400),now)
            if len(rows)<2: return
            bar=rows[-1]
            if bar["ts"]<=self.db.get(c,"cursor:"+symbol,0): return
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

    class LeaseLost(RuntimeError):
        """Raised when a tick segment finds another engine owns the lease."""

    def _hold(self,c,seconds=30):
        """Renew the engine lease at the start of every transaction segment.

        2026-10-23: tick() runs as several transactions (levels per
        symbol, pre-ICT scans, ICT block, MU scalp). The 30-second lease
        was taken once in the first segment and never renewed, so during
        a deployment overlap a second engine could take the lease and
        both would run exits()/submit() concurrently -> duplicate live
        orders. Every segment now re-asserts ownership and aborts the
        remainder of the tick if it is lost."""
        if not self.db.lease(c,"engine",self.owner,seconds):
            raise Engine.LeaseLost("engine lease lost mid-tick")

    def _refresh_intel_if_due(self,now):
        """Hourly TraderMatrix intel refresh, OUTSIDE any tick transaction.

        2026-10-23: this used to run inside tick's first transaction
        (the one that also runs paper_risk.ledgers and exits()). Three
        30-second vendor HTTP calls inside a transaction with a 30s
        idle-in-transaction timeout aborted the connection, which
        silently skipped that tick's exits/flatten; and because
        intel:last_refresh is only written on success it retried every
        tick. Now: decide in a short tx, do HTTP with no connection
        held, write results in a second short tx, and record the
        attempt time so a failing vendor is retried hourly, not every
        two seconds."""
        try:
            with self.db.tx() as c:
                last_refresh=self.db.get(c,"intel:last_refresh",{})
                last_at=last_refresh.get("at",0)
                if isinstance(last_at,str):
                    try:
                        from datetime import datetime
                        last_at=datetime.fromisoformat(last_at).timestamp()
                    except Exception:
                        last_at=0
                last_attempt=self.db.get(c,"intel:last_attempt",0)
                due=now-last_at>3600 and now-last_attempt>3600
                if due: self.db.put(c,"intel:last_attempt",now)
            if not due: return
            from .tradermatrix_intel import refresh_intel
            refresh_intel(self.db,None)
        except Exception as e:
            # NOTE: Do NOT import logging here - it shadows the module-level
            # logging import and causes UnboundLocalError later in tick().
            logging.getLogger('uvicorn.error').warning(f"Intel refresh failed: {e}")

    def tick(self,now=None):
        now=now or time.time()
        try:
            self._tick(now)
        except Engine.LeaseLost as e:
            logging.getLogger('uvicorn.error').warning('%s; another engine owns the lease, skipping remainder of tick',e)

    def _tick(self,now):
        with self.db.tx() as c:
            if not self.db.lease(c,"engine",self.owner,30): return
        self._refresh_intel_if_due(now)
        with self.db.tx() as c:
            self._hold(c)
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
        # ICE shadow scorecard (2026-10-29): its own fault domain — a
        # failure here must never roll back the study/exits segment or
        # starve the ICT pass. On any failure the in-memory detector
        # state is dropped so it reloads from the persisted state (the
        # rolled-back transaction never advanced it on disk).
        try:
            with self.db.tx() as c:
                self._hold(c)
                self.ice_shadow.tick(c,now)
        except Engine.LeaseLost:
            raise
        except Exception as e:
            logging.getLogger('uvicorn.error').error(f'ICE shadow tick failed: {e}')
            self.ice_shadow.reset()
            try:
                with self.db.tx() as c:
                    self.db.put(c, 'health:ice_shadow_error',
                                {'error': str(e)[:300], 'at': now})
            except Exception:
                pass
        self._levels_pass(now)
        _summaries = {}
        try:
            with self.db.tx() as c:
                self._hold(c)
                _summaries = self._pre_ict_scans(c, now)
        except Engine.LeaseLost:
            raise
        except Exception as e:
            logging.getLogger('uvicorn.error').error(f'Pre-ICT scans failed: {e}')
            try:
                with self.db.tx() as c:
                    self.db.put(c, 'health:pre_ict_error',
                                {'error': str(e)[:300], 'at': now})
            except Exception:
                pass
        magnet_summary = _summaries.get('magnet_summary')
        tape_summary = _summaries.get('tape_summary')
        pulse_summary = _summaries.get('pulse_summary')
        zero_dte_summary = _summaries.get('zero_dte_summary')
        darkpool_summary = _summaries.get('darkpool_summary')
        iv_rank_summary = _summaries.get('iv_rank_summary')
        squeeze_summary = _summaries.get('squeeze_summary')
        gap_summary = _summaries.get('gap_summary')
        breakout_summary = _summaries.get('breakout_summary')
        board_summary = _summaries.get('board_summary')
        # VIX data feed: keep the regime keys fresh before the gating block
        # below reads them. 2026-10-23: moved out of the ICT transaction
        # into its own short one — fetch_vix is a 20s vendor HTTP call and
        # must not be able to abort the transaction that runs the detectors
        # and ict_paper submits. Throttled inside scan() to every 5 min.
        vix_regime_summary = None
        if getattr(self.cfg, "massive", ""):
            try:
                from .vix_regime import scan as vix_regime_scan
                with self.db.tx() as c:
                    vix_regime_summary = vix_regime_scan(self.db, c, self.cfg, now)
            except Exception as e:
                logging.getLogger('uvicorn.error').warning('VIX scan failed: %s', type(e).__name__)
        with self.db.tx() as c:
            self._hold(c)
            # ICT futures concept detectors (Phase 1: evidence-only). Ordered
            # by dependency: liquidity map -> HTF levels -> event detectors ->
            # confluence tiering.
            ict_summaries = {}
            # 2026-10-14: Per-detector isolation — one crashing detector must
            # never nuke the whole tick (KeyError: 0 in vwap_fade killed
            # 0DTE, futures, ideas, alerts, health writes for a week).
            def _safe_scan(name, scan_fn):
                """Run a detector scan, catching exceptions so one bad
                detector doesn't roll back the entire tick."""
                try:
                    return scan_fn()
                except Exception as e:
                    import traceback
                    logging.getLogger('uvicorn.error').error(
                        f'Detector {name} crashed: {e}\n{traceback.format_exc(limit=3)}')
                    try:
                        self.db.put(c, f'health:detector_{name}_error',
                                    {'error': str(e)[:500], 'at': now})
                    except Exception:
                        pass
                    return None
            # VIX regime filter: skip detectors that underperform in current regime
            _vix_regime = 'normal'
            if getattr(self.cfg, 'vix_regime_enabled', False):
                try:
                    from .vix_regime import get_regime_info, should_run_detector
                    _regime_info = get_regime_info(self.db, c, self.cfg)
                    _vix_regime = _regime_info.get('regime', 'normal')
                except Exception:
                    pass
            def _should_run(detector_name):
                if not getattr(self.cfg, 'vix_regime_enabled', False):
                    return True
                try:
                    from .vix_regime import should_run_detector
                    return should_run_detector(detector_name, _vix_regime)
                except Exception:
                    return True
            if self.cfg.ict_session_liquidity:
                from .session_liquidity import scan as ict_session_liquidity_scan
                ict_summaries['session_liquidity'] = _safe_scan('session_liquidity', lambda: ict_session_liquidity_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_htf_levels:
                from .htf_levels import scan as ict_htf_levels_scan
                ict_summaries['htf_levels'] = _safe_scan('htf_levels', lambda: ict_htf_levels_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_turtle_soup and _should_run('turtle_soup'):
                from .turtle_soup import scan as ict_turtle_soup_scan
                ict_summaries['turtle_soup'] = _safe_scan('turtle_soup', lambda: ict_turtle_soup_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_smt_divergence:
                from .smt_divergence import scan as ict_smt_divergence_scan
                ict_summaries['smt_divergence'] = _safe_scan('smt_divergence', lambda: ict_smt_divergence_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_aoi_zones and _should_run('aoi_zones'):
                from .aoi_zones import scan as ict_aoi_zones_scan
                ict_summaries['aoi_zones'] = _safe_scan('aoi_zones', lambda: ict_aoi_zones_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_aoi_fade and _should_run('aoi_fade'):
                from .aoi_fade import scan as ict_aoi_fade_scan
                ict_summaries['aoi_fade'] = _safe_scan('aoi_fade', lambda: ict_aoi_fade_scan(self.db,c,self.cfg,now))
            if getattr(self.cfg, 'ict_trend_rider', False):
                from .trend_rider import scan as ict_trend_rider_scan
                ict_summaries['trend_rider'] = _safe_scan('trend_rider', lambda: ict_trend_rider_scan(self.db,c,self.cfg,now))
            if getattr(self.cfg, 'gap_fade_enabled', False) and _should_run('gap_fade'):
                from .gap_fade import scan as gap_fade_scan
                ict_summaries['gap_fade'] = _safe_scan('gap_fade', lambda: gap_fade_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_tier_a_b:
                from .tier_a_b import scan as ict_tier_a_b_scan
                ict_summaries['tier_a_b'] = _safe_scan('tier_a_b', lambda: ict_tier_a_b_scan(self.db,c,self.cfg,now))
            # ICT futures Phase 2 detectors: evidence snapshots always; paper
            # fills only when ICT_FUTURES_PAPER_ENABLED is also on (handled
            # inside each scan via ict_paper).
            if self.cfg.ict_golden_zone:
                from .golden_zone import scan as ict_golden_zone_scan
                ict_summaries['golden_zone'] = _safe_scan('golden_zone', lambda: ict_golden_zone_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_bos_fvg and _should_run('bos_fvg'):
                from .bos_fvg import scan as ict_bos_fvg_scan
                ict_summaries['bos_fvg'] = _safe_scan('bos_fvg', lambda: ict_bos_fvg_scan(self.db,c,self.cfg,now))
            if self.cfg.ict_bos_gz_vwap and _should_run('bos_gz_vwap'):
                from .bos_gz_vwap import scan as ict_bos_gz_vwap_scan
                ict_summaries['bos_gz_vwap'] = _safe_scan('bos_gz_vwap', lambda: ict_bos_gz_vwap_scan(self.db,c,self.cfg,now))
            # YouTube-trader method detectors: evidence snapshots always; paper
            # fills only when ICT_FUTURES_PAPER_ENABLED is also on (handled
            # inside each scan via ict_paper).
            if self.cfg.ict_rumers_box and _should_run('rumers_box'):
                from .rumers_box import scan as yt_rumers_box_scan
                ict_summaries['rumers_box'] = _safe_scan('rumers_box', lambda: yt_rumers_box_scan(self.db,c,self.cfg,now))
            # Daily trend-bias regime (Rumers 3R rule): evidence-only context,
            # never a filter or sizer.
            if self.cfg.ict_trend_bias:
                from .trend_bias import scan as ict_trend_bias_scan
                ict_summaries['trend_bias'] = _safe_scan('trend_bias', lambda: ict_trend_bias_scan(self.db,c,self.cfg,now))
            # Claude gauntlet survivors (2026-10-28): S1-S4 live
            # detectors in compass/survivors.py — own submit path,
            # tripwire-gated TradersPost sends.
            if getattr(self.cfg, 'survivor_enabled', False):
                from .survivors import scan as survivor_scan
                ict_summaries['survivors'] = _safe_scan('survivors', lambda: survivor_scan(self.db,c,self.cfg,now))
            self.ideas.tick(c,now)
            from .observation_recovery import tick as recovery_tick
            recovery_tick(self.db,c,self.clock() if self.clock else now)
            self.db.put(c,"worker:engine",{"at":now,"mode":policy(self.cfg)['mode'],"strategy_version":VERSION,"orb_setups_enabled":self.cfg.orb_setups,"morning_enabled":self.cfg.morning_enabled})
        # TradersPost dispatch (audit #4): entries/closes queued during
        # the tick (ict_paper.submit, exits) are POSTed HERE, after the
        # tick transaction has committed — a slow or hanging webhook
        # must never hold the tick's DB transaction open. A dispatch
        # failure must never break the tick loop.
        try:
            from . import traderspost as _traderspost
            _traderspost.dispatch_pending(self.cfg, self.db)
        except Exception:
            logging.getLogger('uvicorn.error').exception('TradersPost dispatch failed')
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
                          'aoi_zones','aoi_fade','tier_a_b',
                          'golden_zone','bos_fvg','bos_gz_vwap',
                          'rumers_box'):
            _s = ict_summaries.get(_ict_name)
            if _s and _s.get('ran'):
                self.db.health('ict_' + _ict_name, 'running',
                               'ran, %d symbols' % (_s.get('symbols', 0)))
        _tb = ict_summaries.get('trend_bias')
        if _tb and _tb.get('ran'):
            self.db.health('trend_bias', 'running',
                           'ran, %d symbols' % (_tb.get('symbols', 0)))
        _sv = ict_summaries.get('survivors')
        if _sv and _sv.get('ran'):
            self.db.health('survivors', 'running',
                           'live=%s, %d signals, %d submitted, %d sent, %d stale skips' % (
                               _sv.get('live'), _sv.get('signals', 0),
                               _sv.get('submitted', 0), _sv.get('sent', 0),
                               _sv.get('stale_skips', 0)))
        # Runner: trailing stops for ICT positions at 1R+ profit.
        # Lets winners run (overnight trends) instead of fixed 2R exits.
        # 2026-10-24 (audit-adoption deviation): the audit set deleted
        # ict_trail; we keep it. At HEAD this block ran on the already-
        # closed ICT connection (the same bug as the MU scalp block
        # below) and failed silently every tick. It now runs in its own
        # transaction segment with the lease re-asserted, and failures
        # are logged and reported via health:ict_trail instead of
        # swallowed.
        try:
            with self.db.tx() as c:
                self._hold(c)
                from .ict_trail import manage_all as _trail_all
                _trail_result = _trail_all(self.db, c, now)
            if _trail_result.get('trailed'):
                self.db.health('ict_trail', 'running',
                               '%d positions trailing' % _trail_result['trailed'])
        except Engine.LeaseLost:
            raise
        except Exception as e:
            logging.getLogger('uvicorn.error').error('ICT trail segment failed: %s', e)
            try:
                with self.db.tx() as c:
                    self.db.put(c, 'health:ict_trail', {'name': 'ict_trail', 'status': 'error',
                                                        'detail': str(e)[:300], 'checked_at': now})
            except Exception:
                pass
        # MU / volatile single-stock scalp detectors (Josh 2026-10-08):
        # gap fade premarket, spike fade 9:45-11am ET. Generalized
        # 2026-10-11 to a fixed list of volatile names. Alerts feed the
        # 0DTE paper book via options_0dte category. Gated by
        # MU_SCALP_ENABLED (default true).
        #
        # 2026-10-23 FIX: this block (and the ict_trail runner that sat
        # above it) ran AFTER the ICT `with self.db.tx() as c:` block had
        # exited, so every db call hit a closed SQLAlchemy connection
        # (ResourceClosedError), swallowed by `except Exception: pass`.
        # Net effect: no MU/stock scalp trade was ever recorded in
        # production ("Stock scalp (all): 0 tracked" since 10-12). It now
        # runs in its own transaction segment with the lease re-asserted.
        # The ict_trail runner directly above got the same treatment
        # (2026-10-24 adoption deviation: the audit set deleted it; we
        # keep the feature, fixed onto a live connection).
        if getattr(self.cfg, 'mu_scalp', True):
            try:
                with self.db.tx() as c:
                    self._hold(c)
                    self._mu_scalp_segment(c, now)
            except Engine.LeaseLost:
                raise
            except Exception as e:
                logging.getLogger('uvicorn.error').error('MU scalp segment failed: %s', e)
                try:
                    with self.db.tx() as c:
                        self.db.put(c, 'health:mu_scalp', {'name': 'mu_scalp', 'status': 'error',
                                                           'detail': str(e)[:300], 'checked_at': now})
                except Exception:
                    pass

    SCALP_SYMBOLS = ('MU', 'TSLA', 'NVDA', 'AAPL', 'AMD', 'META',
                     'AMZN', 'MSFT', 'GOOGL', 'NFLX', 'AVGO', 'PLTR')

    def _mu_scalp_segment(self, c, now):
        from . import mu_scalp, mu_paper
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        et = ZoneInfo('America/New_York')
        now_et = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(et)
        hm = now_et.strftime('%H:%M')
        ran = {'gap': 0, 'spike': 0}
        # Gap fade: premarket 08:30-09:25 ET, once per day per symbol
        if '08:30' <= hm <= '09:25':
            day_key = now_et.strftime('%Y-%m-%d')
            for symbol in self.SCALP_SYMBOLS:
                done_key = 'mu_scalp:gap_done:%s:%s' % (symbol, day_key)
                if self.db.get(c, done_key):
                    continue
                sig = mu_scalp.check_gap_fade(self.db, c, now, symbol=symbol)
                if sig:
                    # Dedupe by signal key (not timestamp)
                    if not self.db.get(c, 'alert:' + sig.get('dedupe_key', '')):
                        mu_scalp.publish_alert(self.db, c, sig, now)
                        mu_paper.open_trade(self.db, c, sig, now)
                        ran['gap'] += 1
                self.db.put(c, done_key, True)
        # Spike fade: 09:45-11:00 ET, every minute, per symbol
        if '09:45' <= hm <= '11:00':
            for symbol in self.SCALP_SYMBOLS:
                sig = mu_scalp.check_spike_fade(self.db, c, now, symbol=symbol)
                if sig:
                    # Dedupe by spike event key (stable across ticks)
                    if not self.db.get(c, 'alert:' + sig.get('dedupe_key', '')):
                        mu_scalp.publish_alert(self.db, c, sig, now)
                        mu_paper.open_trade(self.db, c, sig, now)
                        ran['spike'] += 1
        # Manage open MU paper trades (target/stop/15min exits)
        mu_paper.manage_trades(self.db, c, now)
        self.db.put(c, 'health:mu_scalp', {'name': 'mu_scalp', 'status': 'running',
                                           'detail': 'et %s gap=%d spike=%d' % (hm, ran['gap'], ran['spike']),
                                           'checked_at': now, 'ran': ran})
        return ran

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
