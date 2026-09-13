"""Independent data collectors; no broker order endpoints."""
import asyncio
import json
import time
from datetime import datetime,timedelta,timezone
from urllib.parse import urlparse
import httpx
import websockets
from .market import ts,number,day
from .store import identity
from .exposure import calculate

class FeedError(Exception): pass

class Collectors:
    def __init__(self,db,cfg):
        self.db,self.cfg=db,cfg
        self.option_symbols=set()
        self.client=httpx.AsyncClient(timeout=20,follow_redirects=False)
        self.live=None
        self.matrix_last=0

    async def close(self):
        if self.live: self.live.stop()
        await self.client.aclose()

    async def get(self,url,headers=None,params=None):
        r=await self.client.get(url,headers=headers,params=params)
        if r.status_code!=200:
            raise FeedError(f"HTTP {r.status_code}; check entitlement, quota and credentials")
        return r.json()

    async def supervise(self,name,enabled,fn,interval=0):
        if not enabled:
            self.db.health(name,"not_configured","API credentials required")
            return
        delay=2
        while True:
            try:
                await fn()
                delay=2
                await asyncio.sleep(max(1,interval))
            except asyncio.CancelledError: raise
            except Exception as e:
                detail=str(e) if isinstance(e,FeedError) else type(e).__name__
                self.db.health(name,"error",detail+f"; retry in {delay}s")
                await asyncio.sleep(delay)
                delay=min(delay*2,120)

    def tasks(self):
        c=self.cfg
        return [
            self.supervise("alpaca_stocks",bool(c.alpaca_key and c.alpaca_secret),self.stocks),
            self.supervise("alpaca_history",bool(c.alpaca_key and c.alpaca_secret),self.history,3600),
            self.supervise("databento_futures",bool(c.databento),self.futures),
            self.supervise("option_chain",bool(c.massive or (c.alpaca_key and c.alpaca_secret)),self.chains,60),
            self.supervise("option_stream",bool(c.massive),self.options),
            self.supervise("tradermatrix",bool(c.matrix),self.matrix,15)]

    @property
    def alpaca_headers(self):
        return {"APCA-API-KEY-ID":self.cfg.alpaca_key,"APCA-API-SECRET-KEY":self.cfg.alpaca_secret}

    def quote(self,source,symbol,q,instrument_id=None):
        if not q.get("ts") or number(q.get("bid")) is None or number(q.get("ask")) is None: return
        q={**q,"source":source,"symbol":symbol,"instrument_id":instrument_id,"received":time.time()}
        with self.db.tx() as c:
            old=self.db.get(c,"quote:"+symbol)
            if old and old["ts"]>q["ts"]: return
            self.db.put(c,"quote:"+symbol,q)
            self.db.append(c,"quote",source,symbol,q["ts"],q,identity("quote",source,symbol,q["ts"]))

    def bars(self,source,items,kind="bar"):
        with self.db.tx() as c:
            for symbol,t,p in items:
                if t is None or any(number(p.get(k)) is None for k in ["o","h","l","c","v"]): continue
                self.db.append(c,kind,source,symbol,t,p)

    async def stocks(self):
        self.db.health("alpaca_stocks","connecting",self.cfg.feed.upper()+" stream")
        async with websockets.connect("wss://stream.data.alpaca.markets/v2/"+self.cfg.feed,ping_interval=20,max_queue=4096) as ws:
            await ws.send(json.dumps({"action":"auth","key":self.cfg.alpaca_key,"secret":self.cfg.alpaca_secret}))
            last={}
            async for raw in ws:
                for x in json.loads(raw):
                    if x.get("T")=="error": raise FeedError("Alpaca stream error code "+str(x.get("code")))
                    if x.get("T")=="success" and x.get("msg")=="authenticated":
                        await ws.send(json.dumps({"action":"subscribe","quotes":list(self.cfg.stocks),"bars":list(self.cfg.stocks)}))
                    if x.get("T")=="subscription":
                        self.db.health("alpaca_stocks","connected","Subscribed; waiting for market events",feed=self.cfg.feed)
                    symbol,t=x.get("S"),ts(x.get("t"))
                    if x.get("T")=="q" and time.monotonic()-last.get(symbol,0)>=.25:
                        self.quote("alpaca",symbol,{"ts":t,"bid":x["bp"],"ask":x["ap"],"bid_size":x["bs"],"ask_size":x["as"]})
                        last[symbol]=time.monotonic()
                    if x.get("T") in {"b","u"}:
                        self.bars("alpaca",[(symbol,t,{k:x[k] for k in ["o","h","l","c","v","vw"] if k in x})])
                    if t and symbol and time.monotonic()-last.get("health",0)>5:
                        self.db.health("alpaca_stocks","receiving","Quotes sampled up to 4 Hz; closed minute bars",t)
                        last["health"]=time.monotonic()

    async def history(self):
        for frame,days,kind in [("1Min",7,"bar"),("1Day",120,"daily")]:
            params={"symbols":",".join(self.cfg.stocks),"timeframe":frame,
                "start":(datetime.now(timezone.utc)-timedelta(days=days)).isoformat(),
                "limit":10000,"feed":self.cfg.feed,"adjustment":"split","sort":"asc"}
            for _ in range(20):
                data=await self.get("https://data.alpaca.markets/v2/stocks/bars",self.alpaca_headers,params)
                items=[(symbol,ts(b["t"]),{k:b[k] for k in ["o","h","l","c","v","vw"] if k in b})
                    for symbol,bars in data.get("bars",{}).items() for b in bars]
                await asyncio.to_thread(self.bars,"alpaca",items,kind)
                if not data.get("next_page_token"): break
                params["page_token"]=data["next_page_token"]
            else: raise FeedError("History pagination cap reached; incomplete")
        self.db.health("alpaca_history","available","7 calendar days minute bars; 120 calendar days daily bars",time.time())

    async def futures(self):
        import databento as db
        self.db.health("databento_futures","connecting","GLBX.MDP3: continuous input with resolved instrument IDs")
        def run():
            client=db.Live(key=self.cfg.databento,heartbeat_interval_s=15,reconnect_policy="none")
            self.live=client
            last={}
            def callback(r):
                if isinstance(r,db.ErrorMsg): raise FeedError("Databento stream rejected; verify CME entitlement")
                if isinstance(r,db.SymbolMappingMsg):
                    with self.db.tx() as c:
                        self.db.append(c,"mapping","databento",str(r.stype_out_symbol),time.time(),
                            {"instrument_id":r.instrument_id,"input":str(r.stype_in_symbol),"output":str(r.stype_out_symbol)})
                    return
                iid=getattr(r,"instrument_id",None)
                alias=client.symbology_map.get(iid)
                if alias is None: return
                symbol=f"{alias}@{iid}"
                t=r.ts_event/1e9
                if isinstance(r,db.OHLCVMsg):
                    self.bars("databento",[(symbol,t,{"o":r.open/1e9,"h":r.high/1e9,"l":r.low/1e9,"c":r.close/1e9,"v":r.volume,"instrument_id":iid,"alias":str(alias)})])
                elif isinstance(r,db.MBP1Msg) and time.monotonic()-last.get(symbol,0)>.25:
                    level=r.levels[0]
                    if level.bid_px<9e18 and level.ask_px<9e18:
                        self.quote("databento",symbol,{"ts":t,"bid":level.bid_px/1e9,"ask":level.ask_px/1e9,"bid_size":level.bid_sz,"ask_size":level.ask_sz},iid)
                    last[symbol]=time.monotonic()
                if time.monotonic()-last.get("health",0)>5:
                    self.db.health("databento_futures","receiving","Contract ID retained; BBO sampled up to 4 Hz",t)
                    last["health"]=time.monotonic()
            client.add_callback(callback)
            client.subscribe(dataset="GLBX.MDP3",schema="ohlcv-1m",stype_in="continuous",symbols=self.cfg.futures,
                start=(datetime.now(timezone.utc)-timedelta(hours=23)).isoformat())
            client.subscribe(dataset="GLBX.MDP3",schema="mbp-1",stype_in="continuous",symbols=self.cfg.futures)
            client.start()
            self.db.health("databento_futures","connected","Waiting for CME market events")
            try: client.block_for_close()
            finally: client.stop()
        await asyncio.to_thread(run)

    async def chains(self):
        source="massive" if self.cfg.massive else "alpaca"
        selected=[]
        for symbol in self.cfg.stocks:
            contracts,complete=await (self.massive_chain(symbol) if source=="massive" else self.alpaca_chain(symbol))
            now=time.time()
            with self.db.tx() as c:
                q=self.db.get(c,"quote:"+symbol)
                spot=(q["bid"]+q["ask"])/2 if q and now-q["ts"]<120 else None
                exposure=calculate(contracts,spot,now,source,complete)
                chain={"contracts":contracts,"complete":complete,"asof":now,"source":source}
                self.db.append(c,"chain",source,symbol,now,chain)
                self.db.put(c,"chain:"+symbol,chain)
                self.db.put(c,"exposure:"+symbol,exposure)
                self.db.append(c,"exposure",source,symbol,now,exposure)
            candidates=sorted(contracts,key=lambda o:(o["expiry"],abs(o["strike"]-(spot or o["strike"]))))
            chosen=candidates[:max(2,self.cfg.stream_limit//len(self.cfg.stocks))]
            selected += [o["symbol"] for o in chosen]
            for o in chosen:
                if o.get("quote"): self.quote(source,o["symbol"],o["quote"])
        with self.db.tx() as c:
            held=[p["symbol"] for p in self.db.prefix(c,"position:").values() if p.get("status")=="open" and p.get("asset")=="option"]
        self.option_symbols=set(held+selected[:max(0,self.cfg.stream_limit-len(held))])
        self.db.health("option_chain","available",source+"; daily OI with missing fields preserved",time.time(),contracts=len(selected))

    async def massive_chain(self,symbol):
        url="https://api.massive.com/v3/snapshot/options/"+symbol
        params={"apiKey":self.cfg.massive,"limit":250,"expiration_date.gte":day(time.time()),"expiration_date.lte":day(time.time()+45*86400)}
        out=[]
        for _ in range(40):
            data=await self.get(url,params=params)
            for x in data.get("results",[]):
                d,g,q=x.get("details",{}),x.get("greeks",{}),x.get("last_quote",{})
                o={"symbol":d.get("ticker"),"underlying":symbol,"expiry":d.get("expiration_date"),
                    "strike":d.get("strike_price"),"type":d.get("contract_type"),"multiplier":d.get("shares_per_contract"),
                    "oi":x.get("open_interest"),"oi_date":None,"gamma":g.get("gamma"),"delta":g.get("delta"),
                    "iv":x.get("implied_volatility"),"volume":x.get("day",{}).get("volume")}
                if o["symbol"] and o["expiry"] and o["strike"]:
                    out.append(o)
                    o["quote"]={"ts":ts(q.get("last_updated")),"bid":q.get("bid"),"ask":q.get("ask"),
                        "bid_size":q.get("bid_size",0),"ask_size":q.get("ask_size",0)}
            url=data.get("next_url")
            if not url: return out,True
            if urlparse(url).hostname not in {"api.massive.com","api.polygon.io"}:
                raise FeedError("Unexpected pagination host")
            params={"apiKey":self.cfg.massive}
        return out,False

    async def alpaca_chain(self,symbol):
        metadata={}
        params={"underlying_symbols":symbol,"status":"active","expiration_date_gte":day(time.time()),
            "expiration_date_lte":day(time.time()+45*86400),"limit":1000}
        complete=False
        for _ in range(30):
            data=await self.get("https://paper-api.alpaca.markets/v2/options/contracts",self.alpaca_headers,params)
            for x in data.get("option_contracts",[]): metadata[x["symbol"]]=x
            if not data.get("next_page_token"):
                complete=True
                break
            params["page_token"]=data["next_page_token"]
        params={"feed":"opra","limit":1000,"expiration_date_gte":day(time.time()),"expiration_date_lte":day(time.time()+45*86400)}
        out=[]
        for _ in range(30):
            data=await self.get("https://data.alpaca.markets/v1beta1/options/snapshots/"+symbol,self.alpaca_headers,params)
            for ticker,x in data.get("snapshots",{}).items():
                d,g,q=metadata.get(ticker,{}),x.get("greeks",{}),x.get("latestQuote",{})
                if not d:
                    out.append({"symbol":ticker,"underlying":symbol,"expiry":"9999-12-31","strike":0,"type":None,"multiplier":None,"oi":None,"gamma":g.get("gamma"),"join_status":"missing_contract_metadata"})
                    continue
                o={"symbol":ticker,"underlying":symbol,"expiry":d["expiration_date"],"strike":float(d["strike_price"]),
                    "type":d["type"],"multiplier":number(d.get("size")),"oi":number(d.get("open_interest")),
                    "oi_date":d.get("open_interest_date"),"gamma":g.get("gamma"),"delta":g.get("delta"),"iv":x.get("impliedVolatility")}
                out.append(o)
                o["quote"]={"ts":ts(q.get("t")),"bid":q.get("bp"),"ask":q.get("ap"),
                    "bid_size":q.get("bs",0),"ask_size":q.get("as",0)}
            if not data.get("next_page_token"):
                seen={o["symbol"] for o in out}
                for ticker,d in metadata.items():
                    if ticker not in seen:
                        out.append({"symbol":ticker,"underlying":symbol,"expiry":d["expiration_date"],"strike":float(d["strike_price"]),
                            "type":d["type"],"multiplier":number(d.get("size")),"oi":number(d.get("open_interest")),
                            "oi_date":d.get("open_interest_date"),"gamma":None,"join_status":"missing_snapshot"})
                return out,complete
            params["page_token"]=data["next_page_token"]
        return out,False

    async def options(self):
        while not self.option_symbols:
            self.db.health("option_stream","waiting","Waiting for verified chain to select contracts")
            await asyncio.sleep(5)
        async with websockets.connect("wss://socket.massive.com/options",max_queue=8192,ping_interval=20) as ws:
            await ws.send(json.dumps({"action":"auth","params":self.cfg.massive}))
            subscribed=set()
            authenticated=False
            last={}
            while True:
                wanted=set(self.option_symbols)
                if authenticated:
                    for action,syms in [("unsubscribe",subscribed-wanted),("subscribe",wanted-subscribed)]:
                        if syms: await ws.send(json.dumps({"action":action,"params":",".join(f"{k}.{s}" for s in sorted(syms) for k in ["T","Q"])}))
                    subscribed=wanted
                try: raw=await asyncio.wait_for(ws.recv(),10)
                except asyncio.TimeoutError: continue
                for x in json.loads(raw):
                    if x.get("status") in {"auth_failed","error","not_authorized"} or x.get("ev")=="error":
                        raise FeedError("Massive options subscription rejected")
                    if x.get("status")=="auth_success":
                        authenticated=True
                        self.db.health("option_stream","connected","Authenticated; waiting for selected-contract events")
                    symbol,t=x.get("sym"),ts(x.get("t"))
                    if x.get("ev")=="Q" and time.monotonic()-last.get(symbol,0)>1:
                        self.quote("massive",symbol,{"ts":t,"bid":x.get("bp"),"ask":x.get("ap"),"bid_size":x.get("bs",0),"ask_size":x.get("as",0)})
                        last[symbol]=time.monotonic()
                    if x.get("ev")=="T" and t:
                        with self.db.tx() as c:
                            key=identity("option_trade",symbol,t,x.get("i"),x.get("q"),x)
                            added=self.db.append(c,"option_trade","massive",symbol,t,x,key)
                            premium=float(x.get("p",0))*float(x.get("s",0))*100
                            if added and premium>=100000:
                                self.db.append(c,"flow","massive",symbol,t,{"premium":premium,"size":x.get("s"),"price":x.get("p"),
                                    "classification":"Large print; opening/closing and intent unknown","scope":"Selected contracts only","raw":x},"flow:"+key)
                    if t and time.monotonic()-last.get("health",0)>5:
                        self.db.health("option_stream","receiving","Selected-contract trades; $100k large-print filter; not full-market unusual activity",t,contracts=len(subscribed))
                        last["health"]=time.monotonic()

    async def matrix(self):
        paths=["/unusual-activity?timeFrame=today&page=1&pageSize=100"]
        if time.time()-self.matrix_last>=60:
            paths += ["/gex/"+s+"/matrix" for s in self.cfg.stocks]
            self.matrix_last=time.time()
        for path in paths:
            data=await self.get("https://api.traderdaddy.pro/api/v1"+path,{"X-API-Key":self.cfg.matrix})
            now=time.time()
            label="unusual_activity" if path.startswith("/unusual") else path.split("/")[2]
            with self.db.tx() as c:
                self.db.append(c,"matrix_raw","tradermatrix",label,now,{"path":path,"data":data})
                self.db.put(c,"matrix:"+label,{"received":now,"data":data,"normalization":"Pending paid-account schema validation"})
            await asyncio.sleep(2.6)
        self.db.health("tradermatrix","available","Raw analytics captured; schema and completeness verification pending",time.time())
