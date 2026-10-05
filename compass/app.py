import asyncio
import logging
import hmac
import json
import time
import uuid
import secrets
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI,HTTPException,Request,Query
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer,BadSignature,SignatureExpired
from pydantic import BaseModel,Field
from sqlalchemy import select,func,text
from .config import Config
from .store import Store,events
from .spy_timeframes import Study as SPYTimeframes
from .providers import Collectors
from .obsidian import run as run_obsidian, snapshot as obsidian_snapshot
from .engine import Engine
from .alerts import deliver,outbox_status
from .alert_format import alert_identity
from .market import is_open
from .readiness import decorate_health,quote_checks,clock
from .futures import futures_session,active_selection
from .instruments import configured
from .setup_study import snapshot as study_snapshot, trials as setup_trials
from .option_ideas import snapshot as ideas_snapshot
from .swing_ideas import SwingIdeas, snapshot as swing_snapshot
from .scanner import snapshot as scanner_snapshot
from .research import FEEDS
from .universe import data_symbols
from .projects import install as install_projects, run_sources, snapshot as projects_snapshot, records as project_records, PROJECTS
from .storage_health import run as run_storage_health, snapshot as storage_snapshot
from .strategy_tracking import run as run_strategy_tracking
from .morning_history import run as run_morning_history
from .morning_report import build_report as morning_report, run as run_morning_report
from .native_morning_worker import run as run_native_morning, install as install_native_morning, initialize as initialize_native_morning
from .native_smoothers import run as run_native_smoothers
from .smoothers_daily import run as run_smoothers_daily
from .native_api import install as install_native_api
from .secondary import Secondary, snapshot as secondary_snapshot, reviews as secondary_reviews

def create_app(cfg=None):
    cfg=cfg or Config()
    db=Store(cfg.db)
    root=Path(__file__).parent
    serializer=URLSafeTimedSerializer(cfg.secret or "local-testing-only")
    failures={}
    tasks=[]
    collectors=None
    worker_id=uuid.uuid4().hex

    async def heartbeat():
        while True:
            with db.tx() as c:
                db.put(c,"worker:"+cfg.role,{"at":time.time(),"id":worker_id})
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        cfg.validate()
        logging.getLogger("uvicorn.error").info("Strategy switches: morning_orb=%s orb_setups=%s role=%s",cfg.morning_enabled,cfg.orb_setups,cfg.role)
        db.initialize()
        initialize_native_morning(db)
        nonlocal collectors,serializer
        if not cfg.secret:
            from .store import state
            with db.tx() as c:
                c.execute(db.insert(state).values(key="internal:session_secret",value=secrets.token_urlsafe(48),updated=time.time()).on_conflict_do_nothing(index_elements=["key"]))
                cfg.secret=db.get(c,"internal:session_secret")
            serializer=URLSafeTimedSerializer(cfg.secret)
        if cfg.role in {"all","collector"}:
            collectors=Collectors(db,cfg)
            tasks.extend(asyncio.create_task(t) for t in collectors.tasks())
            tasks.append(asyncio.create_task(run_sources(db,cfg)))
            if cfg.morning_enabled:
                tasks.append(asyncio.create_task(run_morning_history(db,cfg)))
        if cfg.role in {"all","engine"}:
            engine = Engine(db,cfg,clock=time.time)
            tasks.extend([asyncio.create_task(engine.run()),asyncio.create_task(deliver(db,cfg))])
            tasks.append(asyncio.create_task(engine.study.run_reports()))
            tasks.append(asyncio.create_task(Secondary(db,cfg,clock=time.time).run()))
            tasks.append(asyncio.create_task(SwingIdeas(db,cfg).run()))
            from .discovery import Discovery
            tasks.append(asyncio.create_task(Discovery(db,cfg).run()))
            tasks.append(asyncio.create_task(run_obsidian(db,cfg)))
            if cfg.morning_enabled:
                tasks.append(asyncio.create_task(run_strategy_tracking(db)))
            from .native_reports import run as run_native_reports
            tasks.append(asyncio.create_task(run_native_reports(db,cfg)))
            from .native_outbox import run as run_native_outbox
            tasks.append(asyncio.create_task(run_native_outbox(db,cfg)))
            if cfg.magnet_push_enabled:
                from .magnet_push import run as run_magnet_push
                tasks.append(asyncio.create_task(run_magnet_push(db,cfg)))
            if cfg.flow_pulse_push_enabled:
                from .flow_pulse import run as run_flow_pulse_push
                tasks.append(asyncio.create_task(run_flow_pulse_push(db,cfg)))
            if cfg.ict_push_enabled:
                from .ict_push import run as run_ict_push
                tasks.append(asyncio.create_task(run_ict_push(db,cfg)))
            if cfg.gap_push_enabled:
                from .gap_continuation import run as run_gap_push
                tasks.append(asyncio.create_task(run_gap_push(db,cfg)))
            if cfg.day_board_push_enabled:
                from .day_trading_board import run as run_day_board_push
                tasks.append(asyncio.create_task(run_day_board_push(db,cfg)))
            if cfg.morning_enabled and cfg.morning_token:
                tasks.append(asyncio.create_task(run_native_morning(db,cfg,"intake")))
                tasks.append(asyncio.create_task(run_native_morning(db,cfg,"samples")))
            if cfg.alpaca_key and cfg.alpaca_secret:
                tasks.append(asyncio.create_task(run_smoothers_daily(db,cfg)))
                for role in ("schedule","target","premium"):
                    tasks.append(asyncio.create_task(run_native_smoothers(db,cfg,role)))
            from .program_parity import run as run_program_parity
            tasks.append(asyncio.create_task(run_program_parity(db,cfg)))
            tasks.append(asyncio.create_task(run_storage_health(db,cfg)))
            if cfg.morning_enabled:
                tasks.append(asyncio.create_task(run_morning_report(db)))
            from .spy_brief import BriefWorker
            tasks.append(asyncio.create_task(BriefWorker(db,cfg).run()))
            from .forward_audit import run as run_forward_audit
            tasks.append(asyncio.create_task(run_forward_audit(db,cfg)))
            from .observation_audit import run as run_observation_audit
            tasks.append(asyncio.create_task(run_observation_audit(db,cfg)))
            from .tm_study import Study as TMStudy
            tasks.append(asyncio.create_task(TMStudy(db,cfg).run()))
            from .swing_study import Study as SwingStudy
            tasks.append(asyncio.create_task(SwingStudy(db,cfg).run()))
            from .spy_study import Study as SPYStudy
            tasks.append(asyncio.create_task(SPYStudy(db,cfg).run()))
            if cfg.spy_timeframes_enabled:
                tasks.append(asyncio.create_task(SPYTimeframes(db,cfg).run()))
        tasks.append(asyncio.create_task(heartbeat()))
        try:
            yield
        finally:
            for t in tasks: t.cancel()
            try:
                if collectors: await collectors.close()
            finally:
                await asyncio.gather(*tasks,return_exceptions=True)
                await asyncio.to_thread(db.shutdown)

    app=FastAPI(title="Market Compass",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

    @app.get('/api/spy-morning-brief')
    def spy_morning_brief():
        from .spy_brief import build, delivery_payload
        from .spy_chart import prompt, delivery_payload as chart_payload
        from .spy_confirmation import SCHEDULE_CT, reports_for_day
        from .market import day
        now=time.time()
        with db.tx() as c:
            report=build(db,c,now)
            latest=db.get(c,'spy-brief:latest')
            provider_coverage=db.get(c,'provider_coverage:SPY:'+day(now))
            history=[p for p in reports_for_day(db,c,day(now)).values() if p]
        row={'id':'preview','symbol':'SPY','source':'spy_brief','ts':now,'payload':report}
        return {'preview':report,'discord_preview':delivery_payload(row,now),'latest_scheduled':latest,
                'tradingview_prompt':prompt(report,now),'chart_discord_preview':chart_payload(row,now),
                'enabled':cfg.spy_morning_brief,'schedule_ct':list(SCHEDULE_CT),
                'confirmation_history':history,
                'provider_coverage_comparison':provider_coverage,
                'delivery':'Two messages per check through spy_morning; later checks only after opening WAIT; stop after confirmation or 09:30 CT'}

    @app.get('/api/alerts/routes')
    def alert_routes():
        with db.tx() as c:
            return outbox_status(db,c,time.time())
    app.state.db=db
    install_projects(app,db,cfg)
    install_native_morning(app,db,cfg)
    install_native_api(app,db)
    app.mount("/static",StaticFiles(directory=root/"static"),name="static")

    @app.middleware("http")
    async def guard(request,call_next):
        path=request.url.path
        public=path in {"/health","/login","/simple","/api/simple","/api/simple/calendar"} or path.startswith("/static/")
        # These exact routes enforce independent scoped credentials in their handlers.
        integration=(path=="/hooks/native/morning" or bool(re.fullmatch(r"/hooks/native/morning/[^/]+",path)) or path=="/api/integrations/context" or bool(re.fullmatch(r"/hooks/projects/futures/[^/]+/(mnq|mgc)",path)))
        if integration and cfg.role not in {"all","web"}:
            return JSONResponse({"detail":"Worker service"},status_code=404)
        public=public or integration
        if not public:
            if not cfg.password:
                return JSONResponse({"detail":"Set COMPASS_PASSWORD in Railway to unlock the private workspace"},status_code=503)
            if cfg.role not in {"all","web"}: return JSONResponse({"detail":"Worker service"},status_code=404)
            try:
                token=serializer.loads(request.cookies.get("compass_session",""),max_age=86400)
                if token!="owner": raise BadSignature("invalid")
            except (BadSignature,SignatureExpired):
                if path.startswith("/api/"): return JSONResponse({"detail":"Sign in required"},status_code=401)
                return RedirectResponse("/login",status_code=303)
        if request.method=="POST":
            origin=request.headers.get("origin")
            if origin and urlparse(origin).netloc!=request.headers.get("host"):
                return JSONResponse({"detail":"Invalid origin"},status_code=403)
            try: size=int(request.headers.get("content-length","0"))
            except ValueError: return JSONResponse({"detail":"Invalid content length"},status_code=400)
            if size>(131072 if path=="/api/native/smoothers/config" else 32768 if path.startswith("/hooks/native/morning") else 16384):
                return JSONResponse({"detail":"Request too large"},status_code=413)
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["X-Frame-Options"]="DENY"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Cache-Control"]="no-store"
        response.headers["Content-Security-Policy"]="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.get("/health")
    def health():
        with db.tx() as c: c.execute(text("SELECT 1"))
        return {"status":"ok","service":"market-compass","role":cfg.role,"mode":"SIMULATED"}

    @app.get("/login",response_class=HTMLResponse)
    def login_page():
        html=(root/"static"/"login.html").read_text()
        if not cfg.password:
            html=html.replace("Your markets. One shared view.","Setup required: set COMPASS_PASSWORD (16+ characters) in the Railway dashboard service variables.")
        return html

    @app.get("/simple",response_class=HTMLResponse)
    def simple_public():
        # Public, view-only results page. No login. Only the Simple
        # aggregations are reachable without auth; everything else stays
        # behind the access password.
        return (root/"static"/"simple-public.html").read_text()

    @app.post("/login")
    async def login(request:Request):
        addr=request.client.host if request.client else "unknown"
        now=time.time()
        attempts=[t for t in failures.get(addr,[]) if now-t<300]
        failures[addr]=attempts
        if len(attempts)>=10: raise HTTPException(429,"Wait five minutes before trying again")
        data=await request.json()
        value=str(data.get("password",""))
        if not cfg.password or not hmac.compare_digest(value,cfg.password):
            failures[addr].append(now)
            raise HTTPException(401,"Incorrect access password")
        failures.pop(addr,None)
        response=JSONResponse({"ok":True})
        response.set_cookie("compass_session",serializer.dumps("owner"),max_age=86400,
            httponly=True,secure=not cfg.local,samesite="strict")
        return response

    @app.post("/logout")
    def logout():
        r=JSONResponse({"ok":True})
        r.delete_cookie("compass_session")
        return r

    @app.get("/",response_class=HTMLResponse)
    def dashboard(): return (root/"static"/"index.html").read_text()

    # The dashboard snapshot is split into two tiers to keep the 3-second
    # poll small enough to parse and render without blocking the main thread.
    # Fast tier (/api/state): header, quotes, positions, live status.
    # Slow tier (/api/state/studies): study reports, matrices, full lists.
    # Shapes are identical to the old monolith; the frontend merges them.
    def snapshot_fast():
        now=time.time()
        markets={"equities":is_open(now),"futures":is_open(now,True)}
        with db.tx() as c:
            health=list(db.prefix(c,"health:").values())
            workers=db.prefix(c,"worker:")
            quotes=db.prefix(c,"quote:")
            collected_symbols=data_symbols(db,c,cfg,now)
            now=time.time()
            watch={k[6:]:{**q,"age":round(now-q["ts"],2)} for k,q in quotes.items()
                if k[6:] in collected_symbols or "@" in k}
            from .quote_coverage import stock_archive_health
            health.append(stock_archive_health(c,collected_symbols,quotes,now,markets['equities']))
            health=decorate_health(health,workers,markets,now)
            checks=quote_checks(collected_symbols,configured(cfg),{k[6:]:q for k,q in quotes.items()},
                db.recent(c,"mapping",limit=100),markets,now,active_selection(db,c,cfg,now))
            from .paper_risk import snapshot as paper_risk_snapshot
            from .operating_mode import policy, paper_message
            all_positions=list(db.prefix(c,"position:").values())
            # The fast tier polls every 3s: ship open positions plus recent
            # history only. Nothing renders the full lifetime list (the
            # terminal blotter reads `trades`), and unbounded growth here
            # was inflating every poll.
            cutoff=now-7*86400
            positions=[p for p in all_positions
                       if p.get("status")=="open" or p.get("entered_at",0)>=cutoff]
            trades=sorted(db.prefix(c,"trade:").values(),key=lambda p:p.get("entered_at",0),reverse=True)[:100]
            # The engine writes its live operating policy to the shared DB on
            # every tick; prefer it over this service's own config so the
            # dashboard banner reflects what is actually paper trading.
            eng_policy=db.get(c,"operating_policy",{}) or {}
            if (isinstance(eng_policy,dict) and "paper_entries_enabled" in eng_policy
                    and now-eng_policy.get("at",0)<300):
                live_policy=eng_policy
            else:
                # Engine policy missing or stale: this service cannot know the
                # live paper state, so mark it and let the UI withhold the
                # paused claim rather than asserting it.
                live_policy=policy(cfg); live_policy["stale"]=True
            return {"asof":now,"asof_ct":clock(now),"mode":"SIMULATED","operating_policy":live_policy,"markets":markets,
                "health":health,"workers":workers,"quotes":watch,
                "quote_checks":checks,"delivery":outbox_status(db,c,now),
                "futures":{"session":futures_session(now),"selected":active_selection(db,c,cfg,now),
                    "contracts":list(db.prefix(c,"contract:").values()),
                    "history":sorted(db.prefix(c,"recovery:futures:").values(),key=lambda p:p.get("at",0),reverse=True)[:3]},
                "positions":positions,"trades":trades,
                "alerts":[{**row,"presentation":alert_identity(row,now)} for row in db.recent(c,"alert",limit=10) if cfg.paper_trading or not paper_message(row)],
                "flow":db.recent(c,"flow",limit=10),
                "risk":{"paper_portfolios":paper_risk_snapshot(db,c,cfg,now),
                    "legacy_combined":db.prefix(c,"risk:")},
                "limits":{"scope":"Each paper portfolio separately (paper-portfolios-v2)","risk_per_trade":cfg.risk,"max_positions":3,"max_entries":cfg.max_entries}}

    def snapshot_slow():
        now=time.time()
        with db.tx() as c:
            from .operating_mode import paper_message
            matrix={k:{field:value for field,value in v.items() if field!="data"} for k,v in db.prefix(c,"matrix:").items()}
            now=time.time()
            for item in matrix.values():
                if 'strikes' in item:
                    item['strikes']=[{k:v for k,v in row.items() if not k.endswith('_cells')} for row in item['strikes']]
                stamp=item.get("source_ts")
                item["source_asof"]=clock(stamp)
                item["fetched_at"]=clock(item.get("received"))
                item["source_age"]=round(now-stamp,1) if stamp is not None else None
                from .vendor_freshness import confirmation, context_check
                item['confirmation']=confirmation(item,now)
                item['context']=context_check(item,now)
                item["freshness"]=item['confirmation']['status']
                if 'recovery' in item:
                    from .flow_recovery import freshness as flow_freshness
                    item['flow_freshness']=flow_freshness(item,now)
            data={"storage":storage_snapshot(db,c,now),
                "scanner":scanner_snapshot(db,c,cfg,now),
                "projects":projects_snapshot(db,c,cfg,now),
                "obsidian":obsidian_snapshot(db,c,now),
                "forward_acceptance":db.get(c,"forward-acceptance-v1:report",{}),
                "tm_study":{**db.get(c,'tm-study-v1:report',{}),'worker':db.get(c,'tm-study-v1:worker',{})},
                "swing_study":{**db.get(c,'swing-flow-study-v1:report',{}),'worker':db.get(c,'swing-flow-study-v1:worker',{})},
                "spy_study":{**db.get(c,'spy-plan-0dte-v1:report',{}),'worker':db.get(c,'spy-plan-0dte-v1:worker',{}),
                    'timeframes':{**db.get(c,'spy-timeframes-v1:report',{}),'worker':db.get(c,'spy-timeframes-v1:worker',{})}},
                "secondary":secondary_snapshot(db,c,now,clock=time.time),
                "setup_study":study_snapshot(db,c,cfg,now),
                "option_ideas":ideas_snapshot(db,c,cfg,now),
                "swing_ideas":swing_snapshot(db,c,cfg,now),
                "greek_diagnostics":{k[7:]:v for k,v in db.prefix(c,"greeks:").items()},
                "levels":{k[7:]:v for k,v in db.prefix(c,"levels:").items()},
                "exposure":{k[9:]:v for k,v in db.prefix(c,"exposure:").items()},
                "alerts":[{**row,"presentation":alert_identity(row,now)} for row in db.recent(c,"alert",limit=60) if cfg.paper_trading or not paper_message(row)],
                "flow":db.recent(c,"flow",limit=60),"matrix":matrix,
                "notes":["Quotes are sampled up to 4 Hz; minute-bar decisions, not tick-perfect execution.",
                    "GEX/VEX are OI-based proxies. Open interest is daily; dealer inventory is unobserved.",
                    "TraderMatrix matrix fields are normalized from paid responses; flow rows follow the official schema and await open-session verification.",
                    "Large prints cover selected contracts; not a full-market unusual-flow feed.",
                    "Scanner alerts join completed-bar structure, observed exposure levels and fresh vendor flow. No profitability claim.",
                    "No execution adapter, broker order route, partial exits or runners."]}
            from .research_admin import snapshot as research_admin_snapshot
            data['research_admin']=research_admin_snapshot(db,c,cfg,{"asof":now})
            return data

    def snapshot():
        # Full snapshot for server-side consumers: merge both tiers,
        # preferring the slow tier's full alert/flow lists over the fast
        # tier's recent slices.
        slow=snapshot_slow()
        fast=snapshot_fast()
        data={**slow,**fast,"alerts":slow["alerts"],"flow":slow["flow"]}
        return data

    from .snapshot_cache import SnapshotCache
    dashboard_cache = SnapshotCache()
    studies_cache = SnapshotCache(ttl=300)

    @app.get("/api/state")
    def get_state(): return dashboard_cache.get(snapshot_fast)

    @app.get("/api/state/studies")
    def get_state_studies(): return studies_cache.get(snapshot_slow)

    @app.get('/api/research-admin')
    def get_research_admin(download:bool=False):
        headers={'Content-Disposition':'attachment; filename="compass-research-status.json"'} if download else None
        return JSONResponse(snapshot_slow()['research_admin'],headers=headers)

    @app.get("/api/setup-study")
    def get_setup_study():
        with db.tx() as c: return study_snapshot(db,c,cfg,time.time())

    @app.get("/api/calendar")
    def get_calendar(month: str = None):
        from .calendar_pnl import month_calendar
        with db.tx() as c:
            return month_calendar(db, c, month, cfg.dashboard_cutoff)

    @app.get('/api/session-gaps')
    def get_session_gaps(asset:str=Query('future',pattern=r'^(future|option)$'),
            scope:str=Query('cash',pattern=r'^(cash|full)$'),
            session_day:str|None=Query(None,pattern=r'^\d{4}-\d{2}-\d{2}$'),
            limit:int=Query(100,ge=1,le=100),cursor:str=Query('',max_length=1024)):
        from .session_gaps import window, totals, gap_page
        now=time.time()
        try:
            with db.tx() as c:
                page=gap_page(c,now,asset=asset,session_day=session_day,limit=limit,cursor=cursor,scope=scope)
                return {**page,'summary':totals(c,window(page['asof'],page['day'],asset,scope))}
        except (ValueError,OverflowError) as error:raise HTTPException(400,str(error)) from error

    @app.get('/api/tm-study')
    def get_tm_study():
        with db.tx() as c:
            return {**db.get(c,'tm-study-v1:report',{}),'worker':db.get(c,'tm-study-v1:worker',{})}

    @app.get('/api/apex-magnets')
    def get_apex_magnets():
        from .apex_magnet import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/apex-magnets/hit-rates')
    def get_apex_hit_rates(lookback_days:int=Query(14,ge=1,le=90)):
        from .apex_magnet import hit_rates
        with db.tx() as c:
            return hit_rates(db,c,time.time(),lookback_days=lookback_days)

    @app.get('/api/tape-confirmed')
    def get_tape_confirmed():
        from .tape_confirmed import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/flow-pulse')
    def get_flow_pulse():
        from .flow_pulse import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/day-digest')
    def get_day_digest():
        # End-of-day summary, one section per signal type. Read-only.
        from .day_digest import assemble
        with db.tx() as c:
            return assemble(db,c,time.time())

    @app.get('/api/simple')
    def get_simple():
        # Plain-English scoreboards + drill-downs. Read-only.
        from .simple import summary
        with db.tx() as c:
            return summary(db,c,time.time())

    @app.get('/api/simple/calendar')
    def get_simple_calendar(request: Request):
        # P&L / counts per day per type for a month. Read-only.
        from .simple import calendar
        from datetime import datetime
        from zoneinfo import ZoneInfo
        month = (request.query_params.get('month') or '').strip()  # YYYY-MM
        try:
            year, mon = [int(x) for x in month.split('-')]
            assert 1 <= mon <= 12
        except (ValueError, AssertionError):
            now_c = datetime.fromtimestamp(time.time(), ZoneInfo('America/Chicago'))
            year, mon = now_c.year, now_c.month
        with db.tx() as c:
            return calendar(db,c,time.time(),year,mon)

    @app.get('/api/option-stream-parity')
    def get_option_stream_parity():
        # Massive vs Alpaca side-by-side comparison for the migration.
        from .alpaca_option_stream import compare_sources
        return compare_sources(db)

    @app.get('/api/deck')
    def get_deck():
        # Command deck: one aggregated at-a-glance payload for the tile grid.
        # Each tile shows its key metric; details stay in the full sections.
        from . import darkpool, iv_rank, flow_pulse, squeeze, apex_magnet
        from .futures import futures_session
        now = time.time()
        with db.tx() as c:
            dp = darkpool.display(db, c, now)
            iv = iv_rank.display(db, c, now)
            fp = flow_pulse.display(db, c, now, days=1)
            sq = squeeze.display(db, c, now, days=1)
            ax = apex_magnet.display(db, c, now)
            # Gamma walls for the index ETFs.
            gamma = {}
            for sym in ("SPY", "QQQ"):
                g = db.get(c, "gamma_levels:" + sym) or {}
                if g.get("levels"):
                    gamma[sym] = g["levels"]
            # Day board A+ setups for today (Chicago date).
            import datetime
            today = datetime.datetime.fromtimestamp(
                now, datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d")
            board = db.get(c, "day_board:" + today, {}) or {}
            aplas = [s for s in (board.get("setups") or []) if s.get("grade") == "A+"]
            sess = futures_session(now)
            # Recent alerts for the deck tile.
            alert_rows = db.recent(c, "alert", limit=5)
            alerts = [{"ts": a.get("ts"), "kind": (a.get("payload") or {}).get("kind"),
                       "text": (a.get("payload") or {}).get("text") or (a.get("payload") or {}).get("title")}
                      for a in alert_rows]
            return {"asof": now,
                    "flow_pulse": (fp.get("pulses") or [])[:3],
                    "darkpool": {"print_count": dp.get("print_count", 0),
                                 "symbols": (dp.get("symbols") or [])[:5],
                                 "status": dp.get("status")},
                    "iv_rank": {s: v for s, v in (iv.get("ranks") or {}).items()
                                if s in ("SPY", "QQQ", "IWM")},
                    "gamma": gamma,
                    "day_board_aplus": [{"symbol": s.get("symbol"), "score": s.get("score")}
                                        for s in aplas[:5]],
                    "apex": (ax.get("signals") or [])[:5],
                    "squeeze": sq.get("states") or {},
                    "futures_session": {"is_open": sess.get("is_open"),
                                         "entry_open": sess.get("entry_open"),
                                         "day": sess.get("day")},
                    "alerts": alerts}

    @app.get('/api/darkpool')
    def get_darkpool():
        # Evidence-only dark pool flow via MCP.
        from .darkpool import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/iv-rank')
    def get_iv_rank():
        # Evidence: IV rank per symbol for premium bias.
        from .iv_rank import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/gap-continuation')
    def get_gap_continuation():
        from .gap_continuation import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/breakouts')
    def get_breakouts():
        from .breakouts import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/day-trading-board')
    def get_day_trading_board():
        from .day_trading_board import display
        with db.tx() as c:
            return display(db,c,time.time())

    # ICT futures concept detectors (Phase 1: evidence-only, read-only).
    @app.get('/api/ict-session-liquidity')
    def get_ict_session_liquidity():
        from .session_liquidity import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-htf-levels')
    def get_ict_htf_levels():
        from .htf_levels import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-turtle-soup')
    def get_ict_turtle_soup():
        from .turtle_soup import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-smt-divergence')
    def get_ict_smt_divergence():
        from .smt_divergence import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-aoi-zones')
    def get_ict_aoi_zones():
        from .aoi_zones import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-aoi-fade')
    def get_ict_aoi_fade():
        from .aoi_fade import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-continuation')
    def get_ict_continuation():
        from .continuation import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-tier-a-b')
    def get_ict_tier_a_b():
        from .tier_a_b import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-golden-zone')
    def get_ict_golden_zone():
        from .golden_zone import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-bos-fvg')
    def get_ict_bos_fvg():
        from .bos_fvg import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-bos-gz-vwap')
    def get_ict_bos_gz_vwap():
        from .bos_gz_vwap import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/ict-paper-attribution')
    def get_ict_paper_attribution():
        from .ict_paper import attribution
        with db.tx() as c:
            return {'asof': time.time(), 'attribution': attribution(db, c)}

    @app.get('/api/yt-morning-drive')
    def get_yt_morning_drive():
        from .morning_drive import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/yt-icc')
    def get_yt_icc():
        from .icc import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/yt-rumers-box')
    def get_yt_rumers_box():
        from .rumers_box import display
        with db.tx() as c:
            return display(db,c,time.time())

    @app.get('/api/trend-bias')
    def get_trend_bias():
        from .trend_bias import display
        with db.tx() as c:
            return display(db,c,time.time())

    class PickCheckIn(BaseModel):
        ticker:str=Field(min_length=1,max_length=12)
        direction:str=Field(min_length=1,max_length=10)
        entry:float|None=None
        target:float|None=None
        source:str=Field(default='',max_length=120)

    @app.post('/api/pick-check')
    def post_pick_check(pick:PickCheckIn):
        from .pick_check import check
        try:
            with db.tx() as c:
                return check(db,c,time.time(),pick.ticker,pick.direction,pick.entry,pick.target,pick.source,cfg)
        except ValueError as e:raise HTTPException(400,str(e))

    @app.get('/api/pick-check/auto')
    def auto_pick_check(ticker:str=Query(...,min_length=1,max_length=12),
                        direction:str=Query(...,min_length=1,max_length=10),
                        entry:float|None=None,target:float|None=None,
                        invalidation:float|None=None,
                        pattern:str|None=Query(None,max_length=80),
                        score:float|None=None,
                        source:str=Query('flash_agentic',max_length=120)):
        # GET variant of the pick checker for automation (e.g. the Flash Agentic
        # watch) that can only issue GETs. Same evidence-only check, same logging.
        from .pick_check import check
        try:
            with db.tx() as c:
                return check(db,c,time.time(),ticker,direction,entry,target,source,cfg,
                             pattern=pattern,setup_score=score,invalidation=invalidation)
        except ValueError as e:raise HTTPException(400,str(e))

    @app.get('/api/pick-check/recent')
    def get_pick_check_recent(limit:int=Query(1,ge=1,le=100)):
        from .pick_check import recent
        with db.tx() as c:
            return {'checks':recent(db,c,limit)}

    @app.get('/api/pick-check/scorecard')
    def get_pick_check_scorecard(limit:int=Query(1,ge=1,le=200),horizon:int=Query(5,ge=1,le=20)):
        from . import pick_scorecard
        with db.tx() as c:
            return pick_scorecard.scorecard(db,c,time.time(),limit=limit,horizon_sessions=horizon)

    @app.get('/api/ticker-read')
    def get_ticker_read(symbol:str=Query(...,min_length=1,max_length=12)):
        # Mechanical ticker read: structured directional read from Compass's
        # own data. Evidence only — no entries, no targets, no trade signal.
        from .ticker_read import read
        return read(db, symbol, time.time())

    @app.get('/api/discovery')
    def get_discovery():
        from .discovery import report
        with db.tx() as c:
            from .weekday_candidates import report as weekday_report
            now=time.time()
            return {**report(db,c,now),'weekday':weekday_report(db,c,now)}

    @app.get('/api/swing-study')
    def get_swing_study():
        with db.tx() as c:
            return {**db.get(c,'swing-flow-study-v1:report',{}),'worker':db.get(c,'swing-flow-study-v1:worker',{})}

    @app.get('/api/spy-study')
    def get_spy_study():
        with db.tx() as c:
            return {**db.get(c,'spy-plan-0dte-v1:report',{}),'worker':db.get(c,'spy-plan-0dte-v1:worker',{}),
                'timeframes':{**db.get(c,'spy-timeframes-v1:report',{}),'worker':db.get(c,'spy-timeframes-v1:worker',{})}}

    @app.get('/api/spy-timeframes/records')
    def get_spy_timeframe_records(timeframe:int=Query(0),offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=100)):
        from .spy_timeframe_report import records
        try:
            with db.tx() as c:return records(c,time.time(),timeframe=timeframe,offset=offset,limit=limit)
        except ValueError as error:raise HTTPException(400,str(error)) from error

    @app.get('/api/spy-study/records')
    def get_spy_study_records(cohort:str=Query('all',max_length=30),limit:int=Query(100,ge=1,le=100),
            asof:float|None=Query(None,gt=0,allow_inf_nan=False),cursor:str=Query('',max_length=1024)):
        from .spy_study_report import record_page
        try:
            with db.tx() as c:return record_page(c,time.time(),cohort=cohort,limit=limit,asof=asof,cursor=cursor)
        except ValueError as error:raise HTTPException(400,str(error)) from error

    @app.get('/api/spy-study/marks/{id}')
    def get_spy_study_marks(id:str,limit:int=Query(100,ge=1,le=100),
            asof:float|None=Query(None,gt=0,allow_inf_nan=False),cursor:str=Query('',max_length=1024)):
        from .spy_study_report import mark_page
        try:
            with db.tx() as c:return mark_page(c,time.time(),id,limit=limit,asof=asof,cursor=cursor)
        except ValueError as error:raise HTTPException(400,str(error)) from error
        except LookupError as error:raise HTTPException(404,str(error)) from error

    @app.get('/api/swing-study/records')
    def get_swing_study_records(cohort:str=Query('all',max_length=30),limit:int=Query(100,ge=1,le=100),
            asof:float|None=Query(None,gt=0,allow_inf_nan=False),cursor:str=Query('',max_length=1024)):
        from .swing_study_report import record_page
        try:
            with db.tx() as c:return record_page(c,time.time(),cohort=cohort,limit=limit,asof=asof,cursor=cursor)
        except ValueError as error:raise HTTPException(400,str(error)) from error

    @app.get('/api/tm-study/records')
    def get_tm_records(cohort:str=Query('all',max_length=30),limit:int=Query(100,ge=1,le=100),
            asof:float|None=Query(None,gt=0,allow_inf_nan=False),cursor:str=Query('',max_length=1024)):
        from .tm_reporting import record_page
        try:
            with db.tx() as c:return record_page(c,time.time(),cohort=cohort,limit=limit,asof=asof,cursor=cursor)
        except ValueError as error:raise HTTPException(400,str(error)) from error

    @app.get('/api/tm-study/record')
    def get_tm_record(id:str=Query(...,pattern=r'^[a-f0-9]{64}$')):
        from .tm_reporting import detail
        with db.tx() as c:result=detail(c,id)
        if result is None:raise HTTPException(404,'Unknown TM study record')
        return result

    @app.get('/api/setup-study/records')
    def get_setup_records(cohort:str=Query('all',pattern=r'^(all|alerted|quiet)$'),
            limit:int=Query(100,ge=1,le=100),
            asof:float|None=Query(None,gt=0,allow_inf_nan=False),
            cursor:str=Query('',max_length=1024)):
        from .setup_reporting import record_page
        try:
            with db.tx() as c:
                return record_page(c,setup_trials,time.time(),cohort=cohort,limit=limit,asof=asof,cursor=cursor)
        except ValueError as error:
            raise HTTPException(400,str(error)) from error

    @app.get("/api/setup-study/record")
    def get_setup_record(id: str):
        with db.tx() as c:
            row=c.execute(select(setup_trials.c.payload).where(setup_trials.c.id==id)).scalar_one_or_none()
        if row is None: raise HTTPException(404,"Unknown setup trial")
        return row

    @app.get("/api/projects")
    def get_projects():
        with db.tx() as c: return projects_snapshot(db,c,cfg,time.time())

    @app.get('/api/projects/morning/report')
    def get_morning_report(limit:int=Query(100,ge=1,le=200),
            start:str=Query('',max_length=10),end:str=Query('',max_length=10),
            ticker:str=Query('',max_length=50,pattern=r'^[A-Za-z0-9_:!.\-]*$'),
            stream:str=Query('',max_length=32,pattern=r'^[A-Za-z0-9_\-]*$'),download:bool=False):
        try:
            with db.tx() as c: report=morning_report(db,c,time.time(),limit,start,end,ticker.upper(),stream)
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        headers={'Content-Disposition':'attachment; filename="morning-stock-research.json"'} if download else None
        return JSONResponse(report,headers=headers)

    @app.get('/api/daily-results')
    def get_daily_results(session_day:str=Query('',max_length=10),download:bool=False):
        from .daily_results import build as daily_results
        try:
            with db.tx() as c: report=daily_results(db,c,cfg,time.time(),session_day or None)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        headers={'Content-Disposition':'attachment; filename="daily-results.json"'} if download else None
        return JSONResponse(report,headers=headers)

    @app.get('/api/summary')
    def get_summary():
        from .summary import build as summary_build
        with db.tx() as c:
            return summary_build(db, c, time.time())

    @app.get('/api/daily-status')
    def get_daily_status(day:str=Query('',max_length=10)):
        from .daily_status import build as daily_status_build
        try:
            with db.tx() as c:
                return daily_status_build(db, c, cfg, time.time(), day or None)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.get("/api/secondary")
    def get_secondary():
        with db.tx() as c: return secondary_snapshot(db,c,time.time(),clock=time.time)

    @app.get("/api/secondary/record")
    def get_secondary_record(id:str):
        if not re.fullmatch(r"[a-f0-9]{64}",id): raise HTTPException(422,"Invalid review identity")
        with db.tx() as c:
            row=c.execute(select(secondary_reviews).where(secondary_reviews.c.id==id)).mappings().first()
        if row is None: raise HTTPException(404,"Review not found")
        return dict(row)

    @app.get('/api/observation-audit')
    def observation_audit_report():
        with db.tx() as c:
            return db.get(c, 'observation_audit:report', {'status':'waiting'})

    @app.get('/api/secondary/report')
    def get_secondary_report(since:float):
        from .secondary import build_report
        try:
            with db.tx() as c:
                return build_report(db,c,time.time(),since=since)
        except ValueError as error:
            raise HTTPException(422,str(error)) from error

    @app.get("/api/projects/record")
    def get_project_record(project:str,id:str):
        if project not in PROJECTS or len(id)>1000: raise HTTPException(422,"Invalid source identity")
        with db.tx() as c:
            row=c.execute(select(project_records.c.payload,project_records.c.context).where(
                project_records.c.project==project,project_records.c.source_id==id)).mappings().first()
        if row is None: raise HTTPException(404,"Source record not found")
        return {"project":project,"record":row['payload'],"context":row['context']}

    @app.get('/api/research/feeds')
    def get_research_feeds():
        # Lightweight catalog for the Research tab dropdown. The full feeds
        # list also ships in the studies tier, but that payload is large
        # enough to stall first paint; this lets the tab populate instantly.
        from .research import catalog
        with db.tx() as c:
            return catalog(db,c,cfg,time.time())

    @app.get('/api/research')
    def get_research(key:str):
        allowed={feed.key for feed in FEEDS}|{'apex_'+symbol for symbol in cfg.watch_symbols}
        if key not in allowed:
            raise HTTPException(400,'Unknown research source')
        with db.tx() as c:
            value=db.get(c,'research:'+key)
        return value or {'key':key,'status':'waiting','items':[],'data':None,'source_ts':None,'received':None}

    @app.get('/api/matrix')
    def get_matrix(symbol:str):
        if symbol not in cfg.watch_symbols:
            raise HTTPException(400,'Unknown watchlist symbol')
        with db.tx() as c:
            item=db.get(c,'matrix:'+symbol)
        return item or {'symbol':symbol,'status':'waiting','strikes':[],'expirations':[]}

    @app.get('/api/gamma-flip')
    def get_gamma_flip(symbol:str):
        # Evidence-only regime context; futures GEX symbols included.
        if symbol not in set(cfg.watch_symbols) | {'NQ','ES'}:
            raise HTTPException(400,'Unknown symbol')
        with db.tx() as c:
            item=db.get(c,'gamma_flip:'+symbol)
        return item or {'symbol':symbol,'status':'waiting','flip':None}

    @app.get('/api/gamma-levels')
    def get_gamma_levels(symbol:str):
        # Evidence-only dealer levels (call wall, put wall, neutral gamma).
        if symbol not in set(cfg.watch_symbols) | {'NQ','ES'}:
            raise HTTPException(400,'Unknown symbol')
        with db.tx() as c:
            item=db.get(c,'gamma_levels:'+symbol)
        return item or {'symbol':symbol,'status':'waiting','levels':None}

    @app.get('/api/scanner')
    def get_scanner():
        with db.tx() as c:
            return scanner_snapshot(db,c,cfg,time.time())

    @app.get("/api/bars")
    def get_bars(symbol:str,limit:int=120):
        with db.tx() as c:
            return db.recent(c,"bar",symbol,limit=min(max(limit,1),1000))

    @app.get("/api/events")
    def get_events(kind:str="alert",limit:int=100):
        if kind not in {"alert","alert_delivery","signal","bar","daily","flow","vendor_flow","mapping","exposure","matrix_raw","research","opportunity","opportunity_update","secondary_review","secondary_error"}:
            raise HTTPException(400,"Unsupported event kind")
        with db.tx() as c: return db.recent(c,kind,limit=min(max(limit,1),1000))

    @app.get("/api/readiness")
    def readiness():
        data=snapshot()
        required=["alpaca_stocks","databento_futures","option_chain","option_stream"]
        status={h["name"]:h["status"] for h in data["health"]}
        return {"process_running":True,"live_validated":False,
            "checks":[{"name":k,"status":status.get(k,"not_started")} for k in required],
            "quotes":data["quote_checks"],"delivery":data["delivery"],
            "next":["Connections have been checked; open-market data quality remains unverified.",
                "Verify live quote ages and target-chain OI/Greek coverage during an open session.",
                "Verify nonempty TraderMatrix flow and advancing matrix source timestamps.",
                "Observe entries, stops and alerts before evaluating strategy performance."]}

    class AlertTest(BaseModel):
        request_id:uuid.UUID
        route:str='research'

    @app.post("/api/alerts/test")
    def test_alert(body:AlertTest):
        from .alert_routes import ROUTES
        if body.route not in ROUTES:
            raise HTTPException(422,'Unknown delivery route')
        now=time.time()
        key="test-alert:"+str(body.request_id)
        with db.tx() as c:
            existing=db.get(c,key)
            if existing: return existing
            status=db.get(c,"health:discord:"+body.route,db.get(c,"health:discord",{})).get("status")
            if status not in {"connected","delivered","error"}:
                raise HTTPException(409,"Discord worker has not verified its configuration yet")
            if not db.lease(c,"discord-test-rate",str(body.request_id),60):
                raise HTTPException(429,"A delivery test was requested in the last minute")
            db.append(c,"alert","owner","SYSTEM",now,{"mode":"TEST","status":"notification_test",
                "reason":"Explicit alert-channel check; no trade or position","request_id":str(body.request_id),
                "delivery_route":body.route},key)
            event_id=c.execute(select(events.c.id).where(events.c.key==key)).scalar_one()
            result={"event_id":event_id,"status":"queued","at":now}
            db.put(c,key,result)
        return result

    return app

app=create_app()
