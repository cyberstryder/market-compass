"""Bounded data repair for connected reviews, independent of their decision loop."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import select
from .market import fresh, is_open, ts, day
from .projects import records
from .secondary import current, daily_context, pending, technical_context, technical_ready
from .universe import connected_symbols, data_symbols, symbols


def coverage(db, c, cfg, now, refreshed=()):
    projects = {}
    for project, symbol in c.execute(select(records.c.project, records.c.symbol).where(
            records.c.project.in_(("morning", "smoothers") if cfg.morning_enabled else ("smoothers",)), records.c.source_ts >= now-30*86400).distinct()):
        try:
            normalized = symbols((symbol,))[0]
        except (ValueError, IndexError):
            continue
        projects.setdefault(normalized, set()).add(project)
    subscribed = set(data_symbols(db, c, cfg, now))
    rows = []
    universe=set(connected_symbols(db,c,now,cfg.morning_enabled))
    if cfg.swing_ideas: universe.update(cfg.watch_symbols)
    from .discovery import requested
    discovery=set(requested(db,c,cfg,now)) if cfg.discovery else set()
    universe.update(discovery)
    for symbol in sorted(universe):
        if cfg.swing_ideas and symbol in cfg.watch_symbols: projects.setdefault(symbol,set()).add("swing")
        if symbol in discovery: projects.setdefault(symbol,set()).add("discovery")
        daily = daily_context(db, c, symbol, now) if "smoothers" in projects.get(symbol, ()) else {}
        swing=db.get(c,"swing_daily:"+symbol,{})
        if {"swing","discovery"}&set(projects.get(symbol,())):
            from .swing_signals import stored_daily_context
            from .daily_history import RECOVERY_CACHE, current_context
            proof=db.get(c,'daily_history_recovery:'+symbol,{})
            cached=db.get(c,RECOVERY_CACHE+symbol,{})
            swing=max((v for v in (swing,cached) if current_context(v,now,proof)),
                key=lambda v:v['computed_at'],default={})
            if not swing or symbol in refreshed:
                swing=stored_daily_context(db,c,symbol,now)
                # Collector-owned cache: never acquire the scanner's row locks
                # while refreshing the complete coverage universe.
                db.put(c,RECOVERY_CACHE+symbol,swing)
            if swing.get("status")!="ready":daily={"status":"warming_up","note":swing.get("reason")}
            elif "smoothers" not in projects.get(symbol,()):daily={"status":"ready","through":swing.get("through") }
        minute = technical_context(db, c, symbol, now)
        minute_reason = minute.get("reason")
        if not technical_ready(minute, now):
            stamps = {int(r[0]) for r in db.get(c, "bar_window:" + symbol, []) if r[0]+60 <= now}
            if stamps:
                last = max(stamps)
                missing = [t for t in range(last-29*60, last+1, 60) if t not in stamps]
                times = ', '.join(datetime.fromtimestamp(t, timezone.utc).strftime('%H:%M') for t in missing[:6])
                minute_reason = (minute_reason or 'Minute context unavailable') + (
                    f'; last bar {datetime.fromtimestamp(last, timezone.utc).strftime("%H:%M")} UTC'
                    + (f'; {len(missing)} gaps: {times} UTC' if missing else '; last 30 minute slots present'))
        rows.append(dict(symbol=symbol, projects=sorted(projects.get(symbol, ())),
            collection_enabled=symbol in subscribed, daily_ready=daily.get("status") == "ready",
            daily_through=daily.get("through"), daily_reason=daily.get("note"),
            daily_bias=daily.get("bias"),daily_history=swing.get('history',{}), minute_ready=technical_ready(minute, now),
            minute_asof=minute.get("asof"), minute_reason=minute_reason))
    result = dict(at=now, rows=rows, market_open=is_open(now),
        note="Current input readiness only. It does not rescore historical reviews or generate trade signals.")
    db.put(c, "secondary:data_status", result)
    return result


def plan(collector, now):
    db, cfg = collector.db, collector.cfg
    with db.tx() as c:
        state = db.get(c, "secondary:data_status", {})
        if not current(state.get("at"), now, 15):
            state = coverage(db, c, cfg, now)
        waiting = c.execute(select(pending).where(pending.c.deadline >= now)
            .order_by(pending.c.deadline).limit(200)).mappings().all()
        allowed = set(data_symbols(db, c, cfg, now))
        requested = [r["candidate"]["symbol"].split(':')[-1].upper() for r in waiting]
        priority = list(dict.fromkeys(s for s in requested if s in allowed))
        quotes = [s for s in priority if not fresh(db.get(c, "quote:" + s), now)] if is_open(now) else []
        attempts = db.get(c, "secondary:data_attempts", {})
        jobs = {}
        for kind, wait in (("daily", 300), ("minute", 60)):
            needed = [r["symbol"] for r in state.get("rows", []) if r["collection_enabled"]
                and not r[kind + "_ready"] and (kind != "daily" or bool({"smoothers","swing","discovery"}&set(r["projects"])))
                and (kind != "minute" or is_open(now))]
            if kind == "minute":
                needed += [s for s in priority if not technical_ready(technical_context(db, c, s, now), now)]
            needed = sorted(set(needed), key=lambda s: (s not in priority, attempts.get(kind + ":" + s, 0), s))
            chosen = [s for s in needed if now-attempts.get(kind + ":" + s, 0) >= (10 if kind == "minute" and s in priority else wait)][:8]
            for s in chosen:
                attempts[kind + ":" + s] = now
            jobs[kind] = chosen
        db.put(c, "secondary:data_attempts", {k: v for k, v in attempts.items() if now-v < 86400})
        return dict(quotes=quotes[:100], **jobs)


async def refresh(collector):
    """At most one quote batch and two small history batches per pass.

    Source timestamps remain unchanged. REST cannot make a stale market quote
    fresh. History is observed now and never backdated into a frozen review.
    """
    jobs = await asyncio.to_thread(plan, collector, time.time())
    recovered = []
    async def quotes():
        if not jobs["quotes"]:
            return
        data = await asyncio.wait_for(collector.get("https://data.alpaca.markets/v2/stocks/quotes/latest",
            collector.alpaca_headers, {"symbols": ",".join(jobs["quotes"]), "feed": collector.cfg.feed}), timeout=6)
        items = []
        for symbol, q in data.get("quotes", {}).items():
            if symbol in jobs["quotes"]:
                items.append((symbol, dict(ts=ts(q.get("t")), bid=q.get("bp"), ask=q.get("ap"),
                    bid_size=q.get("bs", 0), ask_size=q.get("as", 0)), True))
        await asyncio.to_thread(collector.quote_batch, "alpaca", items)

    async def history(kind, frame, seconds):
        wanted = jobs[kind]
        if not wanted:
            return
        now = time.time()
        end = int(now//60)*60-1 if kind == "minute" else int(now//86400)*86400-1
        iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        params = dict(symbols=",".join(wanted), timeframe=frame, start=iso(now-seconds), end=iso(end),
            limit=10000, feed=collector.cfg.feed, adjustment="split", sort="asc")
        try:
            data = await asyncio.wait_for(collector.get("https://data.alpaca.markets/v2/stocks/bars",
                collector.alpaca_headers, params), timeout=6)
            if data.get("next_page_token") or not isinstance(data.get('bars'),dict):
                raise ValueError("Bounded secondary history response incomplete; coverage remains unready")
            items = [(s, ts(b["t"]), {k: b[k] for k in ("o", "h", "l", "c", "v", "vw") if k in b})
                     for s, bars in data['bars'].items() if s in wanted for b in bars]
            if kind == 'daily':
                from .spy_daily import valid_daily
                from .swing_signals import sessions_between
                expected=set(sessions_between(day(now-seconds),day(now)))
                seen=set()
                for symbol,stamp,payload in items:
                    label=day(stamp) if stamp is not None else None
                    if (label not in expected or label>=day(now) or not valid_daily(payload)
                            or (symbol,label) in seen):
                        raise ValueError('Invalid completed daily history response')
                    seen.add((symbol,label))
            await asyncio.to_thread(collector.bars, "alpaca", items, "daily" if kind == "daily" else "bar")
        except Exception as error:
            if kind == 'daily':
                def failed():
                    with collector.db.tx() as c:
                        for symbol in wanted:
                            old=collector.db.get(c,'daily_history_recovery:'+symbol,{})
                            collector.db.put(c,'daily_history_recovery:'+symbol,{**old,
                                'day':day(now),'at':time.time(),'status':'error','error':type(error).__name__})
                await asyncio.to_thread(failed)
                logging.getLogger('uvicorn.error').warning('Daily history recovery failed: symbols=%s type=%s',
                    ','.join(wanted),type(error).__name__)
            raise
        if kind == 'daily':
            def complete():
                with collector.db.tx() as c:
                    for symbol in wanted:
                        dates=sorted(label for s,label in seen if s==symbol)
                        proof=dict(day=day(now),at=time.time(),status='complete',error=None,
                            requested_start_day=day(now-seconds),requested_end_day=day(end),
                            sessions=len(dates),first_session=dates[0] if dates else None,
                            last_session=dates[-1] if dates else None)
                        collector.db.put(c,'daily_history_recovery:'+symbol,proof)
                        logging.getLogger('uvicorn.error').info('Daily history recovery: symbol=%s sessions=%s first=%s last=%s',
                            symbol,len(dates),proof['first_session'],proof['last_session'])
            await asyncio.to_thread(complete)
        recovered.append(kind + ': ' + ', '.join(s + '=' + str(sum(row[0] == s for row in items)) for s in wanted))

    # Each input has its own timeout and failure boundary. Slow daily history
    # cannot prevent a pending intraday review from receiving quotes or bars.
    results = await asyncio.gather(quotes(), history("minute", "1Min", 3*3600),
        history("daily", "1Day", 120*86400), return_exceptions=True)
    failures = {kind: type(result).__name__ for kind, result in zip(("quotes", "minute", "daily"), results)
                if isinstance(result, Exception)}
    if jobs["daily"] or jobs["minute"]:
        def update_coverage():
            with collector.db.tx() as c:
                return coverage(collector.db, c, collector.cfg, time.time(),refreshed=jobs['daily'])
        await asyncio.to_thread(update_coverage)
    await asyncio.to_thread(collector.db.health, "secondary_data", "partial" if failures else "running",
        "Independent bounded quote/minute/daily recovery; original review records unchanged"
        + ('; returned bars ' + ' / '.join(recovered) if recovered else '; no history returned')
        + ('; failed inputs ' + str(failures) if failures else ''),
        quote_requests=len(jobs["quotes"]), daily_symbols=len(jobs["daily"]), minute_symbols=len(jobs["minute"]),
        failures=failures)
