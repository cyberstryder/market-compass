"""Versioned deterministic scans and simulated fills. No order routing."""
import asyncio
import time
import uuid
from .market import levels,fresh,day,session,dedup
from .store import identity
from .futures import futures_session,risk_day,future_levels,selection

VERSION="orb15-breakout-v1"

def spec(symbol):
    if symbol.startswith("MES"): return {"tick":.25,"multiplier":5,"fee":1.5,"max_qty":2,"asset":"future"}
    if symbol.startswith("MNQ"): return {"tick":.25,"multiplier":2,"fee":1.5,"max_qty":2,"asset":"future"}
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
    def __init__(self,db,cfg):
        self.db,self.cfg=db,cfg
        self.owner=uuid.uuid4().hex

    def alert(self,c,symbol,data,key):
        self.db.append(c,"alert","engine",symbol,time.time(),{"mode":"SIMULATED",**data},key)

    def entry_check(self,c,signal,now):
        symbol,side=signal["symbol"],signal["side"]
        s=spec(symbol)
        q=self.db.get(c,"quote:"+symbol)
        hours=session(day(now))
        reason=None
        if s["asset"]=="future":
            if not futures_session(now)["entry_open"]: reason="Outside futures entry session or exchange pause"
        elif not hours or not hours[0]<=now<hours[1]-1800: reason="Outside research entry session"
        if reason: return reason,None
        if self.db.get(c,"position:"+symbol,{}).get("status")=="open": reason="Existing simulated position"
        elif not fresh(q,now): reason="Missing, stale, locked/invalid, or empty bid/ask quote"
        elif q["ts"]<signal["signal_time"]: reason="No quote after signal"
        elif q["ask"]-q["bid"]>max(s["tick"]*8,(q["ask"]+q["bid"])/2*(.08 if s["asset"]=="option" else .002)):
            reason="Spread exceeds simulation liquidity limit"
        risk=self.db.get(c,"risk:"+risk_day(now),{"realized":0,"entries":0})
        if risk["realized"]<=-self.cfg.daily_loss or risk["entries"]>=10: reason="Daily simulated loss or 10-entry cap"
        if sum(p.get("status")=="open" for p in self.db.prefix(c,"position:").values())>=3:
            reason="Portfolio cap: three simultaneous simulated positions"
        if reason:
            return reason,None
        price=fill(q,side,s["tick"])
        distance=signal["stop_distance"]
        per_unit=distance*s["multiplier"]+2*s["fee"]+2*s["tick"]*s["multiplier"]
        qty=min(s["max_qty"],int(self.cfg.risk/per_unit),int(q["ask_size"] if side=="long" else q["bid_size"]))
        if qty<1 or (side=="long" and price-distance<=0):
            return "One unit exceeds risk or stop invalid",None
        flatten=futures_session(now)["flatten_at"] if s["asset"]=="future" else hours[1]-900
        return None,{"spec":s,"quote":q,"price":price,"distance":distance,"per_unit":per_unit,"qty":qty,"risk":risk,"flatten_at":flatten}

    def enter(self,c,signal,now):
        reason,plan=self.entry_check(c,signal,now)
        symbol,side=signal["symbol"],signal["side"]
        if reason:
            self.alert(c,symbol,{**signal,"status":"skipped","reason":reason},"skip:"+signal["id"])
            return False
        s,q,price,distance,per_unit,qty,risk=(plan[k] for k in ("spec","quote","price","distance","per_unit","qty","risk"))
        direction=1 if side=="long" else -1
        trade={**signal,**s,"status":"open","entry":price,"entered_at":now,"entry_quote_ts":q["ts"],
            "stop":price-direction*distance,"target":price+direction*distance*2,"qty":qty,"initial_risk":per_unit*qty,
            "fill_model":"Observed bid/ask side plus one adverse tick; sample-based simulation",
            "last_quote_ts":q["ts"],"last_bar_checked":signal["signal_time"]-60,
            "risk_day":risk_day(now),"flatten_at":plan["flatten_at"]}
        self.db.put(c,"position:"+symbol,trade)
        self.db.put(c,"trade:"+signal["id"],trade)
        risk["entries"]+=1
        self.db.put(c,"risk:"+risk_day(now),risk)
        self.alert(c,symbol,{**trade,"status":"entered"},"entry:"+signal["id"])
        return True

    def exits(self,c,now):
        for key,p in self.db.prefix(c,"position:").items():
            if p.get("status")!="open": continue
            q=self.db.get(c,"quote:"+p["symbol"])
            if not fresh(q,now) or q["ts"]<=p["last_quote_ts"]:
                if now-p.get("last_quote_ts",now)>15:
                    self.alert(c,p["symbol"],{"status":"management_blocked","trade_id":p["id"],
                        "reason":"No fresh exit quote; position remains unresolved"},"stale:"+p["id"]+":"+str(int(now//300)))
                continue
            price=fill(q,p["side"],p["tick"],False)
            long=p["side"]=="long"
            stopped=price<=p["stop"] if long else price>=p["stop"]
            target=price>=p["target"] if long else price<=p["target"]
            reason="stop" if stopped else "target" if target else None
            bars=self.db.recent(c,"bar",p["symbol"],limit=120,since=p["last_bar_checked"])
            for b in sorted(bars,key=lambda b:b["ts"]):
                if b["ts"]<=p["last_bar_checked"] or b["ts"]<p["entered_at"] or b["ts"]+60>now: continue
                p["last_bar_checked"]=b["ts"]
                crossed=b["payload"]["l"]<=p["stop"] if long else b["payload"]["h"]>=p["stop"]
                if crossed:
                    reason="stop_detected_in_bar"
                    price=min(price,p["stop"]-p["tick"]) if long else max(price,p["stop"]+p["tick"])
            hours=session(day(now))
            deadline=p.get("flatten_at",hours[1]-900 if hours else None)
            if p.get("track")!="swing" and deadline and now>=deadline: reason=reason or "session_flatten"
            p["last_quote_ts"]=q["ts"]
            p["mark"]=price
            p["unrealized"]=(price-p["entry"])*(1 if long else -1)*p["qty"]*p["multiplier"]-2*p["fee"]*p["qty"]
            if reason:
                p.update(status="closed",exit=price,exited_at=now,exit_reason=reason,pnl=p["unrealized"])
                risk=self.db.get(c,"risk:"+risk_day(now),{"realized":0,"entries":0})
                risk["realized"]+=p["pnl"]
                self.db.put(c,"risk:"+risk_day(now),risk)
                self.alert(c,p["symbol"],p,"exit:"+p["id"])
            self.db.put(c,key,p)
            self.db.put(c,"trade:"+p["id"],p)

    def options(self,c,signal,now):
        chain=self.db.get(c,"chain:"+signal["symbol"],{})
        if now-chain.get("asof",0)>120:
            self.alert(c,signal["symbol"],{"status":"options_skipped","reason":"No recent options chain",
                "parent_signal":signal["id"]},"option-skip:"+signal["id"])
            return
        kind="call" if signal["side"]=="long" else "put"
        opts=[o for o in chain.get("contracts",[]) if o.get("expiry")==day(now) and o.get("type")==kind and o.get("multiplier")==100]
        opts.sort(key=lambda o:abs(o["strike"]-signal["signal_price"]))
        rejected=[]
        for o in opts[:8]:
            q=self.db.get(c,"quote:"+o["symbol"])
            if fresh(q,now):
                option_signal={**signal,"id":identity(signal["id"],o["symbol"]),"symbol":o["symbol"],
                    "underlying":signal["symbol"],"strategy":"0dte-underlying-orb-v2","side":"long",
                    "stop_distance":max(.05,q["ask"]*.3)}
                reason,_=self.entry_check(c,option_signal,now)
                if reason:
                    rejected.append({"symbol":o["symbol"],"reason":reason})
                    continue
                if self.enter(c,{**option_signal,"selection_rejections":rejected},now): return
            else: rejected.append({"symbol":o["symbol"],"reason":"Missing or invalid fresh quote"})
        self.alert(c,signal["symbol"],{"status":"options_skipped","reason":"No eligible 0DTE contract passes quote, spread and risk checks","rejections":rejected,
            "parent_signal":signal["id"]},"option-skip:"+signal["id"])

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
            if not fresh(self.db.get(c,"quote:"+symbol),now): continue
            signal={"id":sid,"strategy":"swing20-v1","symbol":symbol,"side":"long","signal_time":last["ts"]+86400,
                "decided_at":now,"signal_price":last["payload"]["c"],"track":"swing","status":"candidate",
                "stop_distance":max((max(b["payload"]["h"] for b in prior)-min(b["payload"]["l"] for b in prior))*.2,.1),
                "reason":"Completed daily close above preceding 20 daily highs","bar_event":last["id"]}
            self.enter(c,signal,now)
            self.db.put(c,"seen:"+sid,{"at":now})

    def tick(self,now=None):
        now=now or time.time()
        with self.db.tx() as c:
            if not self.db.lease(c,"engine",self.owner,30): return
            self.exits(c,now)
            active={p["raw_symbol"] for p in selection(self.cfg.futures,now)}
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
                context=future_levels(rows,now) if future else levels(rows,bar["ts"]+60)
                self.db.put(c,"levels:"+symbol,context)
                s=candidate(symbol,rows[-2],bar,context,now)
                if s:
                    self.db.append(c,"signal","engine",symbol,s["signal_time"],s,s["id"])
                    self.enter(c,s,now)
                    if symbol in self.cfg.stocks: self.options(c,s,now)
                self.db.put(c,"cursor:"+symbol,bar["ts"])
            self.swings(c,now)
            self.db.put(c,"worker:engine",{"at":now,"mode":"SIMULATED","strategy_version":VERSION})

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.tick)
                self.db.health("engine","running","Closed-bar scans and simulated position management",time.time())
            except asyncio.CancelledError: raise
            except Exception as e: self.db.health("engine","error",type(e).__name__)
            await asyncio.sleep(2)
