"""Isolated project observations. This module contains no broker or order route."""
import asyncio
import hmac
import json
import math
import re
import time
import uuid
from datetime import datetime

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Column, Float, Index, JSON, String, Table, select, func

from .store import meta, identity, leases

PROJECTS = {"morning": "Morning Algo", "smoothers": "New Smoothers", "futures": "Automated futures"}
STREAMS = {"mnq": ("MNQ", "MNQ Level Rejection + Speed", "0.10.3"),
           "mgc": ("MGC", "MGC Range MFI", "0.7.3")}
SOURCE_PROGRESS_SECONDS=25
SOURCE_SCAN_SECONDS=120


class SourceLeaseLost(RuntimeError):
    pass

records = Table("project_records_v1", meta,
    Column("project", String(24), primary_key=True), Column("key", String(64), primary_key=True),
    Column("source_id", String(1000), nullable=False), Column("symbol", String(100), nullable=False),
    Column("source_ts", Float, nullable=False), Column("first_seen", Float, nullable=False),
    Column("updated", Float, nullable=False), Column("revision", String(64), nullable=False),
    Column("payload", JSON, nullable=False), Column("context", JSON, nullable=False))
Index("project_records_source_time", records.c.project, records.c.source_ts)


def source_configs(cfg):
    return {"morning": (cfg.morning_url, cfg.morning_token) if cfg.morning_enabled else ("", ""),
            "smoothers": (cfg.smoothers_url, cfg.smoothers_token)}


def authorized(value, token):
    return bool(token) and hmac.compare_digest(value.encode(), ("Bearer " + token).encode())


def context_for(db, c, symbol, now):
    """Current observations, with explicit clocks; never a historical reconstruction."""
    symbol = symbol.split(":")[-1].upper()
    root = next((r for r in ("MNQ", "MES", "MGC", "NQ", "ES")
                 if re.fullmatch(r + r"(?:[12]!|[FGHJKMNQUVXZ]\d{1,4})?", symbol)), None)
    linked = {"MNQ": "QQQ", "NQ": "QQQ", "MES": "SPY", "ES": "SPY"}.get(root)
    result = {"symbol": symbol, "captured_at": now, "purpose": "observation_only",
        "timing_note": "Captured by Compass after the source signal; not proof of what the strategy knew at entry.",
        "quote": None, "quote_basis": "exact_underlying", "technical": None,
        "exposure_symbol": linked or (None if root else symbol), "exposure_relationship": "cross_asset_context" if linked else "same_underlying"}
    if root:
        # Continuous TradingView prices must not be substituted for the explicit
        # unadjusted data contract. Gold is not part of the current Databento feed.
        result["quote_basis"] = "separate_explicit_contract_reference_only"
        candidates = [(key[6:], q) for key, q in db.prefix(c, "quote:" + root).items()
                      if re.fullmatch(root + r"[FGHJKMNQUVXZ]\d{1,4}@\d+", key[6:])]
        if candidates:
            key, q = max(candidates, key=lambda x: x[1].get("ts", 0))
            result["quote"] = {**q, "symbol": key}
        if root == "MGC":
            result["quote_basis"] = "no_independent_MGC_quote_feed"
            result["exposure_relationship"] = "no_gold_exposure_feed"
    else:
        result["quote"] = db.get(c, "quote:" + symbol)
        result["technical"] = db.get(c, "scanner_features:" + symbol)
        result["levels"] = db.get(c, "levels:" + symbol)
    quote = result["quote"]
    if quote:
        age = now - quote.get("ts", 0)
        result["quote"] = {**quote, "age": round(age, 3),
            "freshness": "clock_error" if age < -1 else "current" if age <= 5 else "stale"}
    exposure_symbol = result["exposure_symbol"]
    if exposure_symbol:
        matrix = db.get(c, "matrix:" + exposure_symbol) or {}
        result["matrix"] = {k: matrix.get(k) for k in ("symbol", "source", "source_ts", "received", "status", "spot")}
        stamp = matrix.get("source_ts")
        result["matrix"]["freshness"] = ("source_time_unknown" if stamp is None else
            "clock_error" if stamp > now + 1 else "current" if now - stamp <= 180 else "stale")
        result["matrix"]["largest_concentrations"] = [
            {k: row.get(k) for k in ("strike", "gex", "vex")}
            for row in sorted(matrix.get("strikes", []), key=lambda r: abs(r.get("gex") or 0), reverse=True)[:8]]
        result["matrix"]["note"] = "Vendor units; concentrations are not verified dealer positions or trade triggers."
        matches = db.get(c, "research_matches:" + exposure_symbol)
        if isinstance(matches, dict):
            result["research_matches"] = {k: (v[:5] if isinstance(v, list) else v)
                for k, v in list(matches.items())[:12]}
        else: result["research_matches"] = matches[:8] if isinstance(matches, list) else matches
    return result


def validate_record(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 1000:
        raise ValueError("Invalid source identity")
    if not isinstance(row.get("symbol"), str) or not re.fullmatch(r"[A-Za-z0-9_.:!@-]{1,100}", row["symbol"]):
        raise ValueError("Invalid source symbol")
    stamp = row.get("source_ts")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("Invalid source time")
    if len(json.dumps(row, allow_nan=False)) > 150000:
        raise ValueError("Source record exceeds limit")


def ingest(db, c, project, row, now=None, notify=False):
    now = time.time() if now is None else now
    validate_record(row)
    if row["source_ts"] > now + 60:
        raise ValueError("Source clock is in the future")
    key, revision = identity(project, row["id"]), identity(row)
    old = c.execute(select(records).where(records.c.project == project, records.c.key == key)
                    .with_for_update()).mappings().first()
    if old and old["revision"] == revision:
        return False
    if old and (old["symbol"] != row["symbol"] or old["source_ts"] != row["source_ts"]):
        raise ValueError("Source identity changed")
    from .strategy_tracking import register
    register(db, c, project, row, now)
    captured = old["context"] if old else (
        context_for(db, c, row["symbol"], now) if now - row["source_ts"] <= 60 else
        {"captured_at": now, "purpose": "historical_import", "timing_note":
         "Imported more than 60 seconds after the signal. No market context was backfilled as entry evidence."})
    q = db.insert(records).values(project=project, key=key, source_id=row["id"], symbol=row["symbol"],
        source_ts=row["source_ts"], first_seen=now, updated=now, revision=revision, payload=row, context=captured)
    c.execute(q.on_conflict_do_update(index_elements=["project", "key"],
        set_={"updated": now, "revision": revision, "payload": row}))
    db.append(c, "project_update", project, row["symbol"], now,
        {"record_id": row["id"], "status": row.get("status"), "revision": revision,
         "source_ts": row["source_ts"], "first_observation": not bool(old)}, key="project:" + identity(project, key, revision))
    if not old and 0 <= now - row["source_ts"] <= 30:
        if project != "futures":
            db.put(c, "focus:" + row["symbol"], {"symbol": row["symbol"], "priority": 90, "at": now,
                "reason": PROJECTS[project] + " source signal"})
        if notify:
            db.append(c, "alert", "project:" + project, row["symbol"], row["source_ts"],
                {"status": "project_observation", "strategy": row.get("strategy"), "side": row.get("side"),
                 **{k: row[k] for k in ("fill_price", "source_price", "qty", "stop", "target") if row.get(k) is not None},
                 "project": project, "source_event": row.get("status"),
                 "reason": ("Pine " + row["status"].replace("_", " ") + ": " + row["outcome"]["reason"] +
                    ". Broker execution is unverified; bracket fills are not reported by this stream."
                    if row.get("source_format") == "pine_alert_v1" else
                    "TradingView strategy emulator event recorded. Broker execution is unverified; Compass sends no order."),
                 "source_record_id": row["id"]}, key="project-alert:" + key)
    return True


def source_health(health, now):
    result=dict(health)
    if result.get('status') in {'connected','syncing'}:
        if now-(result.get('checked_at') or 0)>SOURCE_PROGRESS_SECONDS:
            result.update(status='stale',detail='Source scan has stopped reporting page progress')
        elif result.get('scan_in_progress') and now-result.get('scan_started_at',now)>SOURCE_SCAN_SECONDS:
            result.update(status='stale',detail='Source scan exceeded its completion deadline')
    return result


def source_lease(db, project, owner, release=False):
    with db.tx() as c:
        if release:
            return c.execute(leases.update().where(leases.c.key=='project-'+project,
                leases.c.owner==owner).values(until=0)).rowcount
        return db.lease(c,'project-'+project,owner,120)


async def poll_source(db, cfg, project, client, cache=None, lease_owner=None):
    cache = {} if cache is None else cache
    url, token = source_configs(cfg)[project]
    if not url or not token:
        await asyncio.to_thread(db.health,"project_" + project,"not_configured","Set the source URL and scoped integration token",scan_in_progress=False)
        return
    started, count, changed, after = time.time(), 0, 0, ""
    # Stable daily boundary permits ETags; overlapping full snapshots recover late
    # updates and outages without a high-water cursor skipping concurrent commits.
    since_ms = (int(started // 86400) - 14) * 86400000
    if cache.get("since_ms") != since_ms:
        cache.clear()
        cache["since_ms"] = since_ms
    seen=set()
    for page in range(50):
        if time.time()-started>SOURCE_SCAN_SECONDS:
            raise TimeoutError('Source scan exceeded its completion deadline')
        if after in seen:raise ValueError('Source pagination repeated a cursor')
        seen.add(after)
        if lease_owner and not await asyncio.to_thread(source_lease,db,project,lease_owner):
            return  # The new owner alone may publish health or advance this scan.
        params = {"since_ms": since_ms, "limit": 100}
        if after: params["after"] = after
        saved = cache.get(after, {})
        headers = {"Authorization": "Bearer " + token}
        if saved.get("etag"): headers["If-None-Match"] = saved["etag"]
        async with client.stream("GET", url.rstrip("/") + "/api/compass/feed", params=params,
                                 headers=headers) as response:
            if response.status_code == 304 and saved:
                count += saved["count"]
                next_page=saved['next']
                payload=None
            else:
                if response.status_code != 200:
                    raise ValueError("Source HTTP " + str(response.status_code))
                chunks, size = [], 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > 8 * 1024 * 1024: raise ValueError("Source page exceeds limit")
                    chunks.append(part)
                payload = json.loads(b"".join(chunks))
                etag = response.headers.get("etag")
        if payload is not None:
            if payload.get("schema_version") != 1 or payload.get("project") != project or payload.get("mode") != "observe_only":
                raise ValueError("Unexpected source schema")
            rows = payload.get("records")
            if not isinstance(rows, list) or len(rows) > 100: raise ValueError("Invalid source page")
            next_page = payload.get("next")
            if next_page is not None and (not isinstance(next_page,str) or not next_page or next_page in seen):
                raise ValueError('Invalid source pagination')
            now=time.time()
            def save(batch):
                with db.tx() as c:
                    if lease_owner and not db.lease(c,'project-'+project,lease_owner,120):
                        raise SourceLeaseLost('Source scan ownership changed')
                    return sum(ingest(db,c,project,row,now) for row in batch)
            for offset in range(0,len(rows),25):
                changed+=await asyncio.to_thread(save,rows[offset:offset+25])
            count += len(rows)
            # Do not cache the ETag/cursor until every page chunk commits.
            cache[after] = {"etag": etag, "count": len(rows), "next": next_page}
        await asyncio.to_thread(db.health,'project_'+project,'syncing',
            f'Authenticated source scan progressing: {page+1} pages checked; {count} records checked',
            rows_checked=count,records_changed=changed,pages_checked=page+1,
            scan_started_at=started,scan_in_progress=True,last_page_at=time.time())
        if next_page is None: break
        if not isinstance(next_page, str) or not next_page or next_page == after:
            raise ValueError("Invalid source pagination")
        after = next_page
    else:
        raise ValueError("Source exceeds 5000-record rolling-window limit")
    await asyncio.to_thread(db.health,"project_" + project,"connected","Authenticated source scan completed; original strategy rules retained",
        rows_checked=count, records_changed=changed, cycle_seconds=round(time.time() - started, 2),
        poll_target_seconds=5, lookback_days=14,scan_in_progress=False,last_complete_at=time.time())


async def run_sources(db, cfg):
    from .feed_loop import FeedLoop
    feed=FeedLoop('project-imports')
    running={}
    async def run():
        running['task']=asyncio.current_task()
        await run_source_imports(db,cfg)
    async def stop():
        task=running.get('task')
        if task is not None:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    future=asyncio.run_coroutine_threadsafe(run(),feed.loop)
    try:
        await asyncio.shield(asyncio.wrap_future(future))
    finally:
        # Wait for lease cleanup before FeedLoop drains its remaining tasks.
        # Cancelling the wrapper and the loop together can cancel cleanup twice.
        try:await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(stop(),feed.loop))
        finally:await asyncio.to_thread(feed.close)


async def run_source(db, cfg, project):
    owner = uuid.uuid4().hex
    cache = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), follow_redirects=False) as client:
            while True:
                start = time.time()
                try:
                    active=await asyncio.to_thread(source_lease,db,project,owner)
                    if active:
                        async with asyncio.timeout(SOURCE_SCAN_SECONDS):
                            await poll_source(db,cfg,project,client,cache,lease_owner=owner)
                except asyncio.CancelledError:raise
                except SourceLeaseLost:pass
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    await asyncio.to_thread(db.health,"project_" + project,"error","Source read failed; retrying. Original strategy remains independent.",scan_in_progress=False)
                except TimeoutError:
                    await asyncio.to_thread(db.health,'project_'+project,'error','Source scan exceeded its completion deadline; retry scheduled',scan_in_progress=False)
                except Exception:
                    await asyncio.to_thread(db.health,"project_" + project,"error","Integration unavailable; saved observations retained and retry scheduled",scan_in_progress=False)
                await asyncio.sleep(max(1, 5 - (time.time() - start)))
    finally:
        await asyncio.to_thread(source_lease,db,project,owner,release=True)


async def run_source_imports(db, cfg):
    tasks=[asyncio.create_task(run_source(db,cfg,project)) for project in source_configs(cfg)]
    try:await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done() and not task.cancelling():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


def snapshot(db, c, cfg, now):
    status = []
    for project, name in PROJECTS.items():
        health = source_health(db.get(c, "health:project_" + project, {}),now)
        state = health.get("status", "not_configured")
        if project == "futures":
            streams = []
            for stream in STREAMS:
                saved = db.get(c, "project_stream:" + stream, {"stream": stream})
                configured = bool(cfg.futures_observer_token) and stream in cfg.observer_streams
                streams.append({**saved,"configured":configured,"status":
                    "configuration_pending" if not configured else "events_received" if saved.get("last_received") else "awaiting_first_event"})
            state = "not_configured" if not cfg.futures_observer_token else "configuration_pending"
            if all(s["configured"] for s in streams):
                state = "events_received" if all(s.get("last_received") for s in streams) else "awaiting_first_event"
            elif any(s["configured"] for s in streams): state = "partial"
        else: streams = []
        count = c.execute(select(func.count()).select_from(records).where(records.c.project == project)).scalar_one()
        status.append({**health,"project": project, "name": name, "status": state, "record_count": count,
            "streams": streams, "strategy_behavior_changed": False})
    recent = c.execute(select(records).order_by(records.c.source_ts.desc()).limit(200)).mappings().all()
    from .strategy_tracking import snapshot as tracking_snapshot
    from .morning_history import snapshot as history_snapshot
    from .native_program_status import snapshot as native_snapshot
    return {"mode": "observe_only", "native_programs":native_snapshot(db,c,now), "program_parity":db.get(c,"program-parity-v1:report",{}), "history_import": history_snapshot(db, c, now), "migration": tracking_snapshot(db, c), "projects": status, "records": [
        {"project": r["project"], **{k:v for k,v in r["payload"].items() if k != "option_samples"},
         "option_sample_count": len(r["payload"].get("option_samples", [])),
         "first_seen": r["first_seen"], "updated": r["updated"],
         "context": r["context"]} for r in recent]}


def script_alert_record(stream, data, now):
    """Observe the existing scripts' alert() JSON; never interpret it as an order.

    Both scripts use disable_alert=true on their strategy orders. Their custom
    entry messages include an emulator price and proposed bracket. Bracket fills
    have no custom exit message, and a session-cutoff message may arrive flat.
    Consequently this stream cannot reconstruct positions or realized P&L.
    """
    root, name, version = STREAMS[stream]
    extra = data.get("extras")
    if not isinstance(extra, dict) or extra.get("version") != root + version:
        raise ValueError("Unexpected script alert version")
    symbol = data.get("ticker")
    if not isinstance(symbol, str) or not re.fullmatch(root + r"(?:[12]!|[FGHJKMNQUVXZ]\d{4})", symbol):
        raise ValueError("Unexpected script symbol")
    if str(data.get("interval")) != "3" or data.get("orderType") != "market":
        raise ValueError("Unexpected script timeframe or message type")
    reason = extra.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 128 or not reason.isprintable():
        raise ValueError("Invalid script reason")
    stamp = datetime.fromisoformat(str(data["time"]).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Source timestamp requires timezone")
    source_ts = stamp.timestamp()
    if not now - 7 * 86400 <= source_ts <= now + 60:
        raise ValueError("Source clock outside recovery window")

    def positive(value):
        if isinstance(value, bool): raise ValueError("Invalid script price or quantity")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid script price or quantity")
        return value

    price = positive(data["price"])
    action = data.get("action")
    qty = stop = target = None
    if action in {"buy", "sell"}:
        side = "long" if action == "buy" else "short"
        if (reason != "entry" or data.get("sentiment") != side or
            data.get("quantityType") != "fixed_quantity" or data.get("cancel") is not False or
            (stream == "mgc" and side != "short")):
            raise ValueError("Invalid script entry")
        qty = positive(data["quantity"])
        if qty != 1: raise ValueError("Unexpected script contract count")
        bracket = data.get("stopLoss")
        profit = data.get("takeProfit")
        if not isinstance(bracket, dict) or bracket.get("type") != "stop" or not isinstance(profit, dict):
            raise ValueError("Missing script bracket")
        stop, target = positive(bracket["stopPrice"]), positive(profit["limitPrice"])
        if not (stop < price < target if side == "long" else target < price < stop):
            raise ValueError("Invalid script bracket direction")
        status, basis = "entry_alert", "Pine emulator entry reference; broker fill unverified"
    elif action == "exit":
        if (reason == "entry" or data.get("cancel") is not True or
            data.get("ignoreTradingWindows") is not True or
            any(k in data for k in ("sentiment", "quantity", "stopLoss", "takeProfit"))):
            raise ValueError("Invalid script exit")
        side = "exit"
        status = "flatten_alert" if reason == "session_cutoff" else "exit_alert"
        basis = "Pine exit instruction reference; not a confirmed fill or position close"
    else:
        raise ValueError("Unknown script action")
    canonical = {k: data[k] for k in ("ticker", "action", "sentiment", "quantity", "quantityType",
        "orderType", "cancel", "ignoreTradingWindows", "price", "time", "interval", "stopLoss", "takeProfit") if k in data}
    canonical["extras"] = {"version": extra["version"], "reason": reason}
    return {"id": identity("pine_alert_v1", stream, canonical), "symbol": symbol, "source_ts": source_ts,
        "strategy": name, "version": version, "stream": stream, "side": side, "status": status,
        "source_format": "pine_alert_v1", "source_price": price, "source_price_basis": basis,
        "entry": price if status == "entry_alert" else None, "fill_price": None,
        "qty": qty, "stop": stop, "target": target,
        "outcome": {"basis": basis, "action": action, "reason": reason,
            "position": "unverified", "broker_pnl": None, "bracket_fills_observed": False,
            "complete_trade_history": False, "timestamp_basis": "Pine alert dispatch time"},
        "original": canonical}


def futures_record(stream, data, now):
    if stream not in STREAMS or not isinstance(data, dict): raise ValueError("Unknown futures stream")
    if "extras" in data:
        return script_alert_record(stream, data, now)
    root, name, version = STREAMS[stream]
    symbol = str(data.get("ticker", "")).split(":")[-1]
    if symbol != root + "1!" or str(data.get("version")) != version or str(data.get("interval")) != "3":
        raise ValueError("Unexpected symbol, version or timeframe")
    if data.get("action") not in {"buy", "sell"} or data.get("position") not in {"long", "short", "flat"}:
        raise ValueError("Invalid strategy action")
    if data.get("prev_position") not in {"long", "short", "flat"}: raise ValueError("Invalid previous position")
    qty, price = float(data["quantity"]), float(data["price"])
    if not all(math.isfinite(v) and v > 0 for v in (qty, price)): raise ValueError("Invalid fill")
    stamp = datetime.fromisoformat(str(data["time"]).replace("Z", "+00:00"))
    if stamp.tzinfo is None: raise ValueError("Source timestamp requires timezone")
    source_ts = stamp.timestamp()
    if source_ts > now + 60 or source_ts < now - 7 * 86400: raise ValueError("Source clock outside recovery window")
    order_id = str(data.get("order_id", ""))
    if not 1 <= len(order_id) <= 100: raise ValueError("Missing order identity")
    canonical = {k: data.get(k) for k in ("ticker", "action", "position", "prev_position", "quantity",
        "price", "time", "bar_time", "order_id", "interval", "version")}
    return {"id": identity(stream, canonical), "symbol": symbol, "source_ts": source_ts,
        "strategy": name, "version": version, "stream": stream, "side": data["action"],
        "status": "emulator_fill", "entry": None, "fill_price": price, "qty": qty, "target": None,
        "outcome": {"basis": "TradingView strategy emulator; broker fills and P&L unverified",
            "position": data["position"], "prev_position": data["prev_position"], "order_id": order_id},
        "original": canonical}


def install(app, db, cfg):
    @app.get("/api/integrations/context")
    def read_context(request: Request, symbol: str):
        auth = request.headers.get("authorization", "")
        project = next((p for p, (_, token) in source_configs(cfg).items() if authorized(auth, token)), None)
        if project is None: raise HTTPException(401, "Invalid integration credentials")
        if not re.fullmatch(r"[A-Z0-9_.:!]{1,50}", symbol): raise HTTPException(422, "Invalid symbol")
        with db.tx() as c: return {"project": project, **context_for(db, c, symbol, time.time())}

    @app.post("/hooks/projects/futures/{token}/{stream}")
    async def receive_futures(token: str, stream: str, request: Request):
        if not cfg.futures_observer_token or not hmac.compare_digest(token.encode(), cfg.futures_observer_token.encode()):
            raise HTTPException(401, "Invalid observer credentials")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192: raise HTTPException(413, "Observer message exceeds 8 KiB")
        try:
            now = time.time()
            row = futures_record(stream, json.loads(body), now)
            with db.tx() as c:
                inserted = ingest(db, c, "futures", row, now, notify=True)
                db.put(c, "project_stream:" + stream, {"stream": stream, "status": "events_received",
                    "last_received": now, "last_source_ts": row["source_ts"], "version": row["version"]})
        except (ValueError, KeyError, TypeError, OverflowError):
            raise HTTPException(422, "Invalid futures observation") from None
        return JSONResponse({"status": "accepted" if inserted else "duplicate", "id": row["id"],
            "mode": "observe_only", "broker_order_sent": False}, status_code=202 if inserted else 200)
