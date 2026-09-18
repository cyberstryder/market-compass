"""Independent data collectors; no broker order endpoints."""
import asyncio
import json
import time
import uuid
from datetime import datetime,timedelta,timezone
from urllib.parse import urlparse,parse_qsl
import httpx
import websockets
from websockets.exceptions import ConnectionClosed
from .market import ts,number,day
from .store import identity
from .exposure import calculate
from .diagnostics import redacted_detail
from .vendor import matrix_summary
from .greek_diagnostics import diagnose
from .flow_recovery import collect as collect_flow
from .futures import selection
from .instruments import future_root
from .futures_replay import ReplayBars
from .futures_ingress import IngressProbe
from .futures_writer import FuturesWriter
from .feed_loop import FeedLoop
from .universe import focus_symbols, data_symbols
from .research import collect as collect_research
from .vendor_schedule import matrix_target, matrix_health
from .vendor_freshness import progress, log_observation
from .stock_stream import consume as consume_stocks, StockStreamError

class FeedError(Exception):
    def __init__(self,message,status_code=None):
        super().__init__(message)
        self.status_code=status_code

class Collectors:
    def __init__(self,db,cfg):
        self.db,self.cfg=db,cfg
        self.option_symbols=set()
        self.background_option_symbols=[]
        self.client=httpx.AsyncClient(timeout=20,follow_redirects=False)
        self.live=None
        self.extra_live={}
        self.matrix_last=0
        self.matrix_next=0
        self.matrix_lock=asyncio.Lock()
        self.matrix_cursor=0
        self.chain_cursor=0
        self.chain_selected={}
        self.history_owner=uuid.uuid4().hex
        self.chain_paging={}

    async def close(self):
        if self.live: self.live.stop()
        for client in list(self.extra_live.values()): client.terminate()
        await self.client.aclose()

    async def get(self,url,headers=None,params=None):
        return await self.get_with_client(self.client,url,headers,params)

    @staticmethod
    async def get_with_client(client,url,headers=None,params=None):
        r=await client.get(url,headers=headers,params=params)
        if r.status_code!=200:
            raise FeedError(f"HTTP {r.status_code}; check entitlement, quota and credentials",r.status_code)
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
                if isinstance(e,ConnectionClosed):
                    frame=e.rcvd or e.sent
                    detail='Connection closed '+str(frame.code if frame else 'without status')+': '+redacted_detail(
                        frame.reason if frame else 'No close frame',(self.cfg.alpaca_key,self.cfg.alpaca_secret,self.cfg.massive),160)
                self.db.health(name,"error",detail+f"; retry in {delay}s")
                await asyncio.sleep(delay)
                delay=min(delay*2,120)

    def tasks(self):
        c=self.cfg
        from .obsidian_history import step as history_obsidian
        from .obsidian import poll as poll_obsidian
        from .extra_futures import tasks as extra_tasks
        from .secondary_data import refresh as refresh_secondary_data
        from .index_reference import collect as collect_index_reference
        return [
            self.supervise("index_reference",bool(c.spy_morning_brief),lambda:collect_index_reference(self),60),
            self.supervise("obsidian_history",bool(c.obsidian_history and c.obsidian_url and c.massive),lambda:history_obsidian(self),3),
            self.supervise("obsidian",bool(c.obsidian_url),lambda:poll_obsidian(self),5),
            self.supervise("alpaca_stocks",bool(c.alpaca_key and c.alpaca_secret),self.stocks),
            self.supervise("alpaca_history",bool(c.alpaca_key and c.alpaca_secret),self.history,3600),
            self.supervise("secondary_data",bool(c.secondary and c.alpaca_key and c.alpaca_secret),lambda:refresh_secondary_data(self),2),
            self.supervise("databento_futures",bool(c.databento),self.futures),
            self.supervise("futures_history",bool(c.databento),self.future_history,3600),
            self.supervise("option_chain",bool(c.massive or (c.alpaca_key and c.alpaca_secret)),self.chains,2),
            self.supervise("option_stream",bool(c.massive),self.options),
            self.supervise("option_recovery",bool(c.massive),self.option_recovery),
            self.supervise("option_subscriptions",bool(c.massive),self.refresh_option_subscriptions,2),
            self.supervise("tradermatrix",bool(c.matrix),self.matrix,2),
            self.supervise("tradermatrix_flow",bool(c.matrix),self.flow,20),
            self.supervise("research",bool(c.matrix and c.research),lambda:collect_research(self),1)]+extra_tasks(self)

    async def flow(self):
        # Head, optional yesterday page, forward recovery, then a head recheck.
        return await collect_flow(self,time.time(),page_cap=4)

    @property
    def alpaca_headers(self):
        return {"APCA-API-KEY-ID":self.cfg.alpaca_key,"APCA-API-SECRET-KEY":self.cfg.alpaca_secret}

    def stock_symbols(self):
        with self.db.tx() as c:
            return data_symbols(self.db,c,self.cfg,time.time())

    def quote(self,source,symbol,q,instrument_id=None,record=True):
        if not q.get("ts") or number(q.get("bid")) is None or number(q.get("ask")) is None: return
        q={**q,"source":source,"symbol":symbol,"instrument_id":instrument_id,"received":time.time()}
        with self.db.tx() as c:
            if not self.db.put_quote(c,"quote:"+symbol,q): return
            if record: self.db.append(c,"quote",source,symbol,q["ts"],q,identity("quote",source,symbol,q["ts"]))

    def quote_batch(self,source,items):
        """One transaction per socket batch for latest state and sampled history."""
        # All concurrent stream/recovery writers must lock latest-quote keys in
        # the same order. Preserve within-symbol source order and every sample.
        items=sorted(items,key=lambda row:row[0])
        with self.db.tx() as c:
            retained=[]
            for symbol,q,record in items:
                if not q.get('ts') or number(q.get('bid')) is None or number(q.get('ask')) is None:
                    continue
                q={**q,'source':source,'symbol':symbol,'received':time.time()}
                accepted=self.db.put_quote(c,'quote:'+symbol,q)
                # Parallel stream/recovery readers may commit out of order. Preserve their
                # sampled history without regressing the latest cache.
                if not accepted and source not in ('alpaca','massive','massive_rest','alpaca_opra_recovery'): continue
                if record:
                    retained.append((symbol,q))

            self.db.append_quotes(c,source,retained)

    def bars(self,source,items,kind="bar"):
        items=sorted((row for row in items if row[1] is not None and
            all(number(row[2].get(k)) is not None for k in ['o','h','l','c','v'])),
            key=lambda row:(row[0],row[1]))
        if not items: return
        with self.db.tx() as c:
            self.db.append_bars(c,kind,source,items)
            latest={}
            windows={}
            for symbol,t,p in items:
                latest[symbol]=max(latest.get(symbol,0),t)
                if kind=='bar':
                    if symbol not in windows:
                        windows[symbol]={r[0]:r for r in self.db.locked_get(c,'bar_window:'+symbol,[])}
                    windows[symbol][t]=[t,p['o'],p['h'],p['l'],p['c'],p['v'],p.get('vw')]
            if kind=="bar":
                for symbol,t in latest.items():
                    self.db.put_max(c,"latestbar:"+symbol,t)
                for symbol,window in windows.items():
                    self.db.put(c,'bar_window:'+symbol,sorted(window.values(),key=lambda r:r[0])[-1800:])

    async def stocks(self):
        feed=FeedLoop('stocks')
        task=asyncio.run_coroutine_threadsafe(self.stock_connection(),feed.loop)
        try: await asyncio.wrap_future(task)
        finally: await asyncio.to_thread(feed.close)

    async def stock_connection(self):
        await asyncio.to_thread(self.db.health,"alpaca_stocks","connecting",self.cfg.feed.upper()+" stream")
        async with websockets.connect("wss://stream.data.alpaca.markets/v2/"+self.cfg.feed,ping_interval=20,max_queue=4096) as ws:
            await ws.send(json.dumps({"action":"auth","key":self.cfg.alpaca_key,"secret":self.cfg.alpaca_secret}))
            try:
                await consume_stocks(ws,self)
            except StockStreamError as error:
                raise FeedError(str(error)) from None

    async def option_recovery(self):
        feed=FeedLoop('option-recovery')
        task=asyncio.run_coroutine_threadsafe(self.recovery_connection(),feed.loop)
        try: await asyncio.wrap_future(task)
        finally: await asyncio.to_thread(feed.close)

    async def recovery_connection(self):
        # HTTP transports belong to this loop; never share the main-loop client.
        from types import SimpleNamespace
        from .option_recovery import recover
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            async def get(url,headers=None,params=None):
                return await self.get_with_client(client,url,headers,params)
            worker=SimpleNamespace(db=self.db,cfg=self.cfg,get=get,
                quote_batch=self.quote_batch,alpaca_headers=self.alpaca_headers)
            delay=2
            while True:
                try:
                    await recover(worker)
                    delay=2
                except asyncio.CancelledError: raise
                except Exception as error:
                    await asyncio.to_thread(self.db.health,'option_recovery','error',
                        type(error).__name__+f'; retry in {delay}s')
                    await asyncio.sleep(delay)
                    delay=min(delay*2,120)
                    continue
                await asyncio.sleep(2)

    async def history(self):
        newest=None
        failures=[]
        universe=await asyncio.to_thread(self.stock_symbols)
        async def batch_history(batch,frame,days,kind):
            nonlocal newest
            params={"symbols":",".join(batch),"timeframe":frame,
                "start":(datetime.now(timezone.utc)-timedelta(days=days)).isoformat(),
                "limit":10000,"feed":self.cfg.feed,"adjustment":"split","sort":"asc"}
            try:
                for _ in range(20):
                    data=await self.get("https://data.alpaca.markets/v2/stocks/bars",self.alpaca_headers,params)
                    items=[(symbol,ts(b["t"]),{k:b[k] for k in ["o","h","l","c","v","vw"] if k in b})
                        for symbol,bars in data.get("bars",{}).items() for b in bars]
                    await asyncio.to_thread(self.bars,"alpaca",items,kind)
                    if items: newest=max(newest or 0,max(item[1] for item in items))
                    if not data.get("next_page_token"): return
                    params["page_token"]=data["next_page_token"]
                raise FeedError("History pagination cap reached for a watchlist batch; incomplete")
            except FeedError as error:
                if error.status_code in (401,403,429): raise
                if error.status_code in (400,422) and len(batch)>1:
                    for symbol in batch: await batch_history((symbol,),frame,days,kind)
                else:
                    failures.append({"symbols":list(batch),"frame":frame,"reason":str(error)})
        for frame,days,kind in [("1Min",7,"bar"),("1Day",120,"daily")]:
            for offset in range(0,len(universe),8):
                await batch_history(universe[offset:offset+8],frame,days,kind)
        self.db.health("alpaca_history","partial" if failures else "available",
            "Batched full-watchlist history: 7 calendar days minute bars; 120 calendar days daily bars",newest,
            failed_batches=failures,watch_symbols=len(universe))

    async def future_history(self):
        import databento as sdk
        from .future_history import recover
        await asyncio.to_thread(recover,self,sdk.Historical(key=self.cfg.databento),time.time())

    def future_targets(self):
        targets=selection(self.cfg.futures,time.time())
        symbols={t["raw_symbol"] for t in targets}
        with self.db.tx() as c:
            for p in self.db.prefix(c,"position:").values():
                if p.get("status")=="open" and p.get("asset")=="future" and future_root(p["symbol"]) in ("MES","MNQ","ES","NQ"): symbols.add(p["symbol"].split("@")[0])
        return targets,sorted(symbols)

    async def futures(self):
        import databento as db
        targets,symbols=self.future_targets()
        self.db.health("databento_futures","connecting","GLBX.MDP3 explicit contracts: "+", ".join(symbols))
        def run():
            feed_loop=FeedLoop("CME")
            try:
                client=db.Live(key=self.cfg.databento,heartbeat_interval_s=15,reconnect_policy="none",slow_reader_behavior="warn",loop=feed_loop.loop)
            except BaseException:
                feed_loop.close()
                raise
            self.live=client
            last={}
            writer=FuturesWriter(self,"CME")
            replay=ReplayBars(writer)
            probe=IngressProbe("CME")
            def callback(r):
                replay.flush_due()
                if isinstance(r,db.ErrorMsg):
                    raise FeedError("Databento stream: "+redacted_detail(r.err,(self.cfg.databento,)))
                if isinstance(r,db.SymbolMappingMsg):
                    raw,iid=str(r.stype_in_symbol),r.instrument_id
                    def save_mapping(raw=raw,iid=iid):
                        with self.db.tx() as c:
                            target=next((t for t in targets if t["raw_symbol"]==raw),None)
                            self.db.append(c,"mapping","databento",raw,time.time(),
                                {"instrument_id":iid,"input":target["configured_symbol"] if target else raw,"output":raw})
                            if target:
                                self.db.put(c,"contract:"+target["root"],{**target,"instrument_id":iid,
                                    "resolved_symbol":f"{raw}@{iid}","mapping_source":"databento live mapping","at":time.time()})
                    writer.metadata(save_mapping)
                    return
                iid=getattr(r,"instrument_id",None)
                alias=client.symbology_map.get(iid)
                if alias is None: return
                symbol=f"{alias}@{iid}"
                t=r.ts_event/1e9
                if isinstance(r,db.MBP1Msg):
                    probe.quote(symbol,r)
                if isinstance(r,db.OHLCVMsg):
                    replay.add((symbol,t,{"o":r.open/1e9,"h":r.high/1e9,"l":r.low/1e9,"c":r.close/1e9,"v":r.volume,"instrument_id":iid,"alias":str(alias)}))
                elif isinstance(r,db.MBP1Msg) and time.monotonic()-last.get(symbol,0)>.25:
                    level=r.levels[0]
                    if level.bid_px<9e18 and level.ask_px<9e18:
                        probe.writing(symbol,lambda: writer.quote("databento",symbol,{"ts":t,"bid":level.bid_px/1e9,"ask":level.ask_px/1e9,"bid_size":level.bid_sz,"ask_size":level.ask_sz,"callback_at":time.time()},iid))
                    last[symbol]=time.monotonic()
                if time.monotonic()-last.get("health",0)>5:
                    writer.metadata(lambda stamp=t:self.db.health("databento_futures","receiving","Contract ID retained; BBO sampled up to 4 Hz",stamp,monotonic_source=True))
                    last["health"]=time.monotonic()
            errors=[]
            def on_error(error):
                errors.append(str(error) if isinstance(error,FeedError) else type(error).__name__)
                self.db.health("databento_futures","error","Callback failed; stopping stream for controlled reconnect")
                client.terminate()
            try:
                client.add_callback(probe.wrap(callback),exception_callback=on_error)
                client.subscribe(dataset="GLBX.MDP3",schema="ohlcv-1m",stype_in="raw_symbol",symbols=symbols,
                    start=(datetime.now(timezone.utc)-timedelta(hours=23)).isoformat())
                client.subscribe(dataset="GLBX.MDP3",schema="mbp-1",stype_in="raw_symbol",symbols=symbols)
                client.start()
                self.db.health("databento_futures","connected","Waiting for CME market events: "+", ".join(symbols))
                client.block_for_close()
            finally:
                client.stop()
                try: replay.flush()
                finally:
                    try: writer.close()
                    finally: feed_loop.close()
            if errors: raise FeedError("Databento callback failure: "+errors[0])
        try:
            task=asyncio.create_task(asyncio.to_thread(run))
            while not task.done():
                await asyncio.wait({task},timeout=30)
                if self.future_targets()[1]!=symbols and self.live:
                    self.live.stop()
                    break
            await task
        except db.BentoError as error:
            raise FeedError("Databento: "+redacted_detail(error,(self.cfg.databento,))) from None

    async def chains(self):
        from .swing_ideas import active_underlyings
        source="massive" if self.cfg.massive else "alpaca"
        now=time.time()
        with self.db.tx() as c:
            focus=focus_symbols(self.db,c,self.cfg,now,self.cfg.option_focus)
            universe=list(self.cfg.watch_symbols)
            rotation=universe[self.chain_cursor:]+universe[:self.chain_cursor]
            retry=lambda s: now>=self.db.get(c,'chain_retry:'+s,{}).get('retry_at',0)
            due=[s for s in focus if retry(s) and now-self.db.get(c,"chain:"+s,{}).get("asof",0)>=45]
            # Refresh each carried contract's terms before observing the new session.
            # Rotate all held swing names even when there are more than the focus cap.
            carried=active_underlyings(c)
            stale_carried=sorted((s for s in carried if retry(s)
                and now-self.db.get(c,'chain:'+s,{}).get('asof',0)>=900),
                key=lambda s:self.db.get(c,'chain:'+s,{}).get('asof',0))
            due=list(dict.fromkeys(stale_carried+due))
            background=next((s for s in rotation if s not in due and retry(s) and now-self.db.get(c,"chain:"+s,{}).get("asof",0)>=900),None)
            # One focused symbol and one background symbol per turn. A new price
            # setup can reach the front of the next turn instead of waiting for
            # a monolithic full-universe chain loop.
            targets=due[:1]+([background] if background else [])
            if background: self.chain_cursor=(universe.index(background)+1)%len(universe)
        for symbol in targets:
            try:
                contracts,complete=await (self.massive_chain(symbol) if source=="massive" else self.alpaca_chain(symbol))
                now=time.time()
                with self.db.tx() as c:
                    q=self.db.get(c,"quote:"+symbol)
                    spot=(q["bid"]+q["ask"])/2 if q and -1<=now-q["ts"]<120 and 0<q["bid"]<=q["ask"] else None
                    reference,reference_ts=spot,q["ts"] if spot else None
                    if reference is None:
                        last_bar=self.db.recent(c,"bar",symbol,limit=1)
                        if last_bar:
                            reference=number(last_bar[0]["payload"].get("c"))
                            reference_ts=last_bar[0]["ts"]
                    exposure=calculate(contracts,spot,now,source,complete)
                    exposure["spot_asof"]=q["ts"] if spot else None
                    chain={"contracts":contracts,"complete":complete,"asof":now,"source":source,
                        "selection_reference":reference,"selection_reference_ts":reference_ts,
                        "selection_note":"Historical references seed subscriptions only; execution needs fresh quotes"}
                    # Full chains remain in latest state; compressed archives
                    # retain research inputs without repeating huge JSON blocks.
                    import base64,gzip
                    archive={"encoding":"gzip+base64-json","data":base64.b64encode(gzip.compress(json.dumps(chain).encode())).decode(),"contracts":len(contracts)}
                    self.db.append(c,"chain_archive",source,symbol,now,archive)
                    self.db.put(c,"chain:"+symbol,chain)
                    self.db.put(c,'chain_inventory:'+symbol,{'contracts':len(contracts),'at':now})
                    self.db.put(c,'chain_retry:'+symbol,{'retry_at':0})
                    self.db.put(c,"exposure:"+symbol,exposure)
                    self.db.put(c,"greeks:"+symbol,{**diagnose(contracts,reference,reference_ts,now,complete),
                        "pagination":self.chain_paging.get(symbol,{})})
                    self.db.append(c,"exposure",source,symbol,now,exposure)
                chosen=select_contracts(contracts,reference,max(4,self.cfg.stream_limit//max(1,len(focus))))
                self.chain_selected[symbol]=[o["symbol"] for o in chosen]
                for o in chosen:
                    if o.get("quote"): self.quote(source,o["symbol"],o["quote"])
                self.db.health('chain_'+symbol,'available','Options chain fetched',poll_ts=now,contracts=len(contracts))
            except asyncio.CancelledError: raise
            except Exception as error:
                detail=str(error) if isinstance(error,FeedError) else type(error).__name__
                self.db.health('chain_'+symbol,'error',detail)
                # An unavailable/delisted name must not stop other chains.
                with self.db.tx() as c:
                    self.db.put(c,'chain_retry:'+symbol,{'at':now,'retry_at':now+300})
        with self.db.tx() as c:
            held=[p["symbol"] for p in self.db.prefix(c,"position:").values() if p.get("status")=="open" and p.get("asset")=="option"]
            contract_count=sum(v['contracts'] for k,v in self.db.prefix(c,'chain_inventory:').items() if k[16:] in self.cfg.watch_symbols)
        with self.db.tx() as c:
            for symbol in focus:
                if symbol not in self.chain_selected:
                    chain=self.db.get(c,'chain:'+symbol,{})
                    self.chain_selected[symbol]=[o['symbol'] for o in select_contracts(chain.get('contracts',[]),
                        chain.get('selection_reference'),max(4,self.cfg.stream_limit//max(1,len(focus))))]
        selected=[]
        choices=[self.chain_selected.get(s,[]) for s in focus]
        for i in range(max((len(rows) for rows in choices),default=0)):
            selected.extend(rows[i] for rows in choices if i<len(rows))
        self.background_option_symbols=selected
        await self.refresh_option_subscriptions()
        self.db.health("option_chain","available",source+"; focused chains target 45s, background target 15m; daily OI; actual source ages shown",
            stream_contracts=len(self.option_symbols),contracts=contract_count,focus_symbols=focus,watch_symbols=len(self.cfg.watch_symbols),poll_ts=time.time(),source_ts=None)


    async def refresh_option_subscriptions(self):
        """Reconcile tracked ideas every two seconds, independently of chain HTTP work."""
        from .option_ideas import stream_requests, choose_streams
        from .swing_ideas import stream_requests as swing_requests
        from .spy_study import stream_requests as spy_requests
        from .spy_timeframes import stream_requests as timeframe_requests
        from .obsidian import contracts as watchlist_contracts
        def reconcile():
            now=time.time()
            with self.db.tx() as c:
                held=[p["symbol"] for p in self.db.prefix(c,"position:").values()
                      if p.get("status")=="open" and p.get("asset")=="option"]
                # Every open contract precedes every unfilled candidate in both studies.
                requested=(stream_requests(c,now,'open')+swing_requests(c,now,'open')+spy_requests(c,now,'open')
                    +timeframe_requests(c,now,'open')+stream_requests(c,now,'pending')+swing_requests(c,now,'pending')
                    +spy_requests(c,now,'pending')+watchlist_contracts(c,now)+timeframe_requests(c,now,'pending'))
                selected=choose_streams(held,requested,self.background_option_symbols,self.cfg.stream_limit)
                self.option_symbols=set(selected)
                # This is requested subscription state; opening still requires actual fresh quotes.
                self.db.put(c,'options:subscriptions',dict(at=now,symbols=selected,
                    requested_ideas=len(requested),waiting_ideas=len(set(requested)-set(selected))))
            self.db.health('option_subscriptions','running',
                'Pinned options ideas and existing positions; actual quotes verify access',
                selected_contracts=len(selected),requested_ideas=len(requested),
                waiting_ideas=len(set(requested)-set(selected)),poll_ts=now)
        await asyncio.to_thread(reconcile)

    async def massive_chain(self,symbol):
        url="https://api.massive.com/v3/snapshot/options/"+symbol
        params={"apiKey":self.cfg.massive,"limit":250,"expiration_date.gte":day(time.time()),"expiration_date.lte":day(time.time()+self.cfg.chain_dte*86400)}
        out={}
        pages_seen=set()
        raw_count=overlaps=conflicts=0
        for _ in range(40):
            if url in pages_seen: raise FeedError("Options pagination repeated a page")
            pages_seen.add(url)
            data=await self.get(url,params=params)
            for x in data.get("results",[]):
                raw_count+=1
                d,g,q=x.get("details") or {},x.get("greeks") or {},x.get("last_quote") or {}
                o={"symbol":d.get("ticker"),"underlying":symbol,"expiry":d.get("expiration_date"),
                    "strike":d.get("strike_price"),"type":d.get("contract_type"),"multiplier":d.get("shares_per_contract"),
                    "oi":x.get("open_interest"),"oi_date":None,"gamma":g.get("gamma"),"delta":g.get("delta"),
                    "gamma_field":"absent" if "gamma" not in g else "null" if g["gamma"] is None else "value",
                    "greek_fields":sorted(k for k in g if k in {"gamma","delta","theta","vega"}),
                    "iv":x.get("implied_volatility"),"volume":x.get("day",{}).get("volume")}
                if o["symbol"] and o["expiry"] and o["strike"]:
                    o["quote"]={"ts":ts(q.get("last_updated")),"bid":q.get("bid"),"ask":q.get("ask"),
                        "bid_size":q.get("bid_size",0),"ask_size":q.get("ask_size",0)}
                    old=out.get(o["symbol"])
                    if old:
                        overlaps+=1
                        conflicts+=any(old.get(k)!=o.get(k) for k in ("gamma","oi","iv","gamma_field"))
                    if not old or (o["quote"]["ts"] or 0)>=(old["quote"]["ts"] or 0): out[o["symbol"]]=o
            self.chain_paging[symbol]={"pages":len(pages_seen),"returned_records":raw_count,
                "unique_contracts":len(out),"overlaps_merged":overlaps,"conflicting_overlaps":conflicts,
                "merge_policy":"One row per contract; newest quote timestamp wins, last occurrence breaks equal timestamps. Pagination is not an atomic snapshot."}
            url=data.get("next_url")
            if not url: return list(out.values()),True
            if urlparse(url).hostname not in {"api.massive.com","api.polygon.io"}:
                raise FeedError("Unexpected pagination host")
            # HTTPX params replace a URL's existing query. Preserve the vendor
            # cursor explicitly, or every request silently re-fetches page 1.
            params=[(k,v) for k,v in parse_qsl(urlparse(url).query,keep_blank_values=True) if k!="apiKey"]+[("apiKey",self.cfg.massive)]
        return list(out.values()),False

    async def alpaca_chain(self,symbol):
        metadata={}
        params={"underlying_symbols":symbol,"status":"active","expiration_date_gte":day(time.time()),
            "expiration_date_lte":day(time.time()+self.cfg.chain_dte*86400),"limit":1000}
        complete=False
        for _ in range(30):
            data=await self.get("https://paper-api.alpaca.markets/v2/options/contracts",self.alpaca_headers,params)
            for x in data.get("option_contracts",[]): metadata[x["symbol"]]=x
            if not data.get("next_page_token"):
                complete=True
                break
            params["page_token"]=data["next_page_token"]
        params={"feed":"opra","limit":1000,"expiration_date_gte":day(time.time()),"expiration_date_lte":day(time.time()+self.cfg.chain_dte*86400)}
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
        feed = FeedLoop("options")
        task = asyncio.run_coroutine_threadsafe(self.option_connection(), feed.loop)
        try:
            await asyncio.wrap_future(task)
        finally:
            await asyncio.to_thread(feed.close)

    async def option_connection(self):
        while not self.option_symbols:
            await asyncio.to_thread(self.db.health,"option_stream","waiting","Waiting for verified chain to select contracts")
            await asyncio.sleep(5)
        await asyncio.to_thread(self.db.health,"option_stream","connecting","Opening Massive options stream")
        async with websockets.connect("wss://socket.massive.com/options",max_queue=8192,ping_interval=20) as ws:
            await ws.send(json.dumps({"action":"auth","params":self.cfg.massive}))
            await asyncio.to_thread(self.db.health,"option_stream","authenticating","Waiting for Massive authentication acknowledgement")
            from .option_stream import consume, OptionStreamError
            try:
                await consume(ws,self)
            except OptionStreamError as error:
                raise FeedError(str(error)) from None

    def option_trade(self,symbol,t,x):
        self.option_trade_batch([(symbol,t,x)])

    def subscription_batch(self,items):
        with self.db.tx() as c:
            for row in items:
                self.db.append(c,'option_subscription','massive',row['symbol'],row['at'],row,
                    identity('option-subscription-v1',row['connection_id'],row['sequence']))

    def option_trade_batch(self,items):
        with self.db.tx() as c:
            for symbol,t,x in items:
                key=identity("option_trade",symbol,t,x.get("i"),x.get("q"),x)
                added=self.db.append(c,"option_trade","massive",symbol,t,x,key)
                premium=float(x.get("p",0))*float(x.get("s",0))*100
                if added and premium>=100000:
                    self.db.append(c,"flow","massive",symbol,t,{"premium":premium,"size":x.get("s"),"price":x.get("p"),
                        "classification":"Large print; opening/closing and intent unknown","scope":"Selected contracts only","raw":x},"flow:"+key)

    async def matrix_request(self,path,label):
        # A shared lock spaces request starts across flow, matrices and research.
        async with self.matrix_lock:
            await asyncio.sleep(max(0,self.matrix_next-time.monotonic()))
            self.matrix_next=time.monotonic()+2.6
        data=await self.get("https://api.traderdaddy.pro/api/v1"+path,{"X-API-Key":self.cfg.matrix})
        now=time.time()
        with self.db.tx() as c:
            self.db.append(c,"matrix_raw","tradermatrix",label,now,{"path":path,"data":data},
                key=identity("matrix_raw",day(now),path,data))
        return data,now

    async def matrix(self):
        now=time.time()
        with self.db.tx() as c:
            symbol=matrix_target(self.db,c,self.cfg,now,self.matrix_cursor)
            if symbol:
                job=self.db.get(c,'matrix_job:'+symbol,{})
                self.db.put(c,'matrix_job:'+symbol,{**job,'attempted_at':now})
        if symbol:
            self.matrix_cursor+=1
            try:
                data,received=await self.matrix_request("/gex/"+symbol+"/matrix",symbol)
                result=matrix_summary(data,received)
                if result['symbol']!=symbol:
                    raise FeedError('TraderMatrix returned a different symbol')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures=min(8,job.get('failures',0)+1)
                detail=str(error)[:180] if isinstance(error,(FeedError,ValueError)) else type(error).__name__
                with self.db.tx() as c:
                    self.db.put(c,'matrix_job:'+symbol,{'attempted_at':now,'failures':failures,
                        'error':detail,'retry_at':time.time()+min(900,30*2**(failures-1))})
                self.db.health('tradermatrix','partial',symbol+': '+detail+'; other symbols continue')
                return
            with self.db.tx() as c:
                self.db.put(c,'matrix_job:'+symbol,{'attempted_at':now,'succeeded_at':received,'failures':0})
                result['source_progress'] = progress(self.db.get(c,'matrix:'+symbol,{}),result)
                previous=self.db.get(c,'matrix_levels:'+symbol,{})
                known={(r['kind'],r['price']):r.get('known_at',received) for r in previous.get('levels',[])}
                concentrations=[]
                for field in ('gex','vex'):
                    maximum=max((abs(r.get(field) or 0) for r in result['strikes']),default=0)
                    if not maximum: continue
                    for row in result['strikes']:
                        if abs(row.get(field) or 0)>=maximum*.75:
                            kind=field+'_concentration'
                            concentrations.append({'kind':kind,'price':row['strike'],'value':row[field],
                                'known_at':known.get((kind,row['strike']),received)})
                self.db.put(c,'matrix_levels:'+symbol,{'symbol':symbol,'source':'tradermatrix',
                    'source_ts':result['source_ts'],'received':received,'levels':concentrations,
                    **{k:result.get(k) for k in ('cached','vendor_stale','vendor_refresh_seconds','cache_policy')},
                    'method':'Relative concentration within vendor GEX/VEX totals; not a dealer-inventory or direction claim'})
                self.db.put(c,"matrix:"+symbol,result)
            log_observation('matrix:'+symbol,result,received)
        now=time.time()
        with self.db.tx() as c:
            core=matrix_health(self.db,c,self.cfg,now)
        ready=all(r['context']['eligible_for_context'] and r['status']!='error' for r in core) and bool(core)
        self.db.health('tradermatrix','available' if ready else 'partial',
            'Core matrices: '+', '.join(r['symbol']+' cache='+r['context']['cache_status']+' live='+r['status'] for r in core)+
            '; flow refreshes independently. Shared request spacing remains 2.6s.',poll_ts=now,core=core)


def select_contracts(contracts,reference,limit):
    """Balance near-money calls and puts; a historical reference only seeds subscriptions."""
    if not reference or reference<=0 or limit<1: return []
    valid=[o for o in contracts if o.get("symbol") and o.get("expiry") and (number(o.get("strike")) or 0)>0
        and o.get("type") in {"call","put"} and number(o.get("multiplier"))==100]
    calls=sorted((o for o in valid if o["type"]=="call"),key=lambda o:(o["expiry"],abs(o["strike"]-reference)))
    puts=sorted((o for o in valid if o["type"]=="put"),key=lambda o:(o["expiry"],abs(o["strike"]-reference)))
    chosen=[]
    for i in range(max(len(calls),len(puts))):
        for side in (calls,puts):
            if i<len(side): chosen.append(side[i])
            if len(chosen)>=limit: return chosen
    return chosen
