"""Plain-English "Simple" view: per-type scoreboards, drill-downs, and a P&L
calendar. Read-only aggregations over the same stores the trackers write.

Each type block carries plain-English copy:
- watches: what the tracker follows
- fires: when it produces a signal
- scored: how a win/loss is measured

Dollar P&L exists for smoothers (paper options), futures (paper micros), and
flow pulse (paper tracks). Flash Agentic and 0DTE are scored by hit rate /
counts — no dollars, and the UI must say so.
"""

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .alert_format import ICT_SETUP_NAMES
from .ict_paper import STRATEGY_TAGS as ICT_PAPER_TAGS
from .native_smoothers import weekly as smoothers_weekly
from .smoothers_scorecard import option_result

CT = ZoneInfo("America/Chicago")


def _day(ts):
    try:
        return datetime.fromtimestamp(float(ts), CT).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _stats(trades):
    """trades: list of (won_bool, pnl_or_None)."""
    n = len(trades)
    wins = sum(1 for w, _ in trades if w)
    pnl = sum(p for _, p in trades if p is not None)
    measured = sum(1 for _, p in trades if p is not None)
    return {
        "tracked": n,
        "wins": wins,
        "win_rate": round(100.0 * wins / n, 1) if n else None,
        "pnl": round(pnl, 2),
        "measured": measured,
    }


def _block(key, title, watches, fires, scored, stats, drill, extra=None):
    b = {"key": key, "title": title, "watches": watches, "fires": fires,
         "scored": scored, "stats": stats, "drill": drill}
    if extra:
        b.update(extra)
    return b


def _drill(name, watches, stats):
    return {"name": name, "watches": watches, "stats": stats}


# ---------------------------------------------------------------- smoothers


def _smoothers(db, c, now):
    rows = list(c.execute(select(
        smoothers_weekly.c.ticker, smoothers_weekly.c.status,
        smoothers_weekly.c.payload)).mappings())
    per_ticker = defaultdict(list)
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    target_hits = 0
    for r in rows:
        p = r.get("payload") or {}
        if not isinstance(p, dict):
            continue
        res = option_result(p)
        pnl = res.get("net_pnl")
        # Public results show only the values we have: signals with real,
        # measured option P&L. Unmeasurable weeks (missing quotes, unresolved,
        # still open) are excluded from the public page, not deleted.
        if pnl is None:
            continue
        won = pnl > 0
        per_ticker[r.get("ticker") or "?"].append((won, pnl))
        if r.get("status") == "WIN":
            target_hits += 1
        d = _day(p.get("resolution_time"))
        if d:
            by_day[d]["pnl"] += pnl
            by_day[d]["count"] += 1
    all_trades = [t for v in per_ticker.values() for t in v]
    drill = []
    for ticker in sorted(per_ticker):
        st = _stats(per_ticker[ticker])
        drill.append(_drill(ticker, "Weekly Smoothers pick on %s." % ticker, st))
    stats = _stats(all_trades)
    stats["target_hits"] = target_hits
    return (_block(
        "smoothers", "Smoothers",
        "Weekly option picks on big liquid stocks.",
        "Every Monday morning: one call or put per stock, held for the week.",
        "Win = the paper option trade made money (bought at ask, sold at bid, minus fees). "
        "Target hit = the stock reached its target by Friday.",
        stats, drill), by_day)


# ---------------------------------------------------------------- futures


def _futures(db, c, now):
    try:
        trades = db.prefix(c, "trade:ict-")
    except Exception:
        trades = {}
    per_method = defaultdict(list)
    per_method_gaps = defaultdict(list)
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    open_count = 0
    for t in trades.values():
        if not isinstance(t, dict):
            continue
        if t.get("status") == "closed" or _num(t.get("exited_at")):
            method = t.get("strategy") or "unknown"
            if t.get("data_gap"):
                # Flew blind: stops/targets were never verifiably managed,
                # so this is not a valid test of the method. Excluded from
                # wins, losses, and P&L; counted separately for transparency.
                per_method_gaps[method].append(t)
                continue
            pnl = _num(t.get("pnl"))
            won = pnl is not None and pnl > 0
            per_method[method].append((won, pnl))
            if pnl is not None:
                d = _day(t.get("exited_at"))
                if d:
                    by_day[d]["pnl"] += pnl
                    by_day[d]["count"] += 1
        else:
            open_count += 1
    all_trades = [t for v in per_method.values() for t in v]
    drill = []
    # Full paper-trading roster: every detector wired for attribution shows up,
    # even before its first closed trade — the page answers "what are we testing?"
    seen = set()
    def with_gaps(tag, trades):
        st = _stats(trades)
        gaps = per_method_gaps.get(tag, [])
        if gaps:
            st["data_gaps"] = len(gaps)
            st["data_gap_pnl"] = round(sum(_num(t.get("pnl")) or 0 for t in gaps), 2)
        return st
    for tag in ICT_PAPER_TAGS.values():
        name = ICT_SETUP_NAMES.get(tag, tag)
        if tag in seen:
            continue
        seen.add(tag)
        drill.append(_drill(name, "Paper-traded micro futures signals.",
                            with_gaps(tag, per_method.get(tag, []))))
    # Any strategy tag seen in the data but not on the roster (legacy/renamed)
    for method in sorted(set(per_method) | set(per_method_gaps)):
        if method in seen:
            continue
        seen.add(method)
        name = ICT_SETUP_NAMES.get(method, method)
        drill.append(_drill(name, "Paper-traded micro futures signals.",
                            with_gaps(method, per_method.get(method, []))))
    stats = _stats(all_trades)
    stats["open"] = open_count
    total_gaps = sum(len(v) for v in per_method_gaps.values())
    if total_gaps:
        stats["data_gaps"] = total_gaps
        stats["data_gap_pnl"] = round(sum(
            _num(t.get("pnl")) or 0 for v in per_method_gaps.values() for t in v), 2)
    return (_block(
        "futures", "Futures Micros",
        "Paper-traded signals on micro futures (MNQ, MES, MGC, SIL, MCL).",
        "Intraday, whenever a method's setup triggers. Everything closes by 3:45 PM CT.",
        "Win = the paper trade closed with a profit, in micro dollars after fees.",
        stats, drill), by_day)


# ---------------------------------------------------------------- flow pulse


def _flow_pulse(db, c, now):
    try:
        tracks = db.prefix(c, "flow_pulse_track:")
    except Exception:
        tracks = {}
    per_dir = defaultdict(list)
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    open_count = 0
    peak_50 = 0
    for rec in tracks.values():
        if not isinstance(rec, dict):
            continue
        if rec.get("status") == "closed":
            pnl = _num(rec.get("pnl"))
            won = pnl is not None and pnl > 0
            per_dir[str(rec.get("direction") or "?").lower()].append((won, pnl))
            pr = _num(rec.get("peak_return_pct"))
            if pr is not None and pr >= 50:
                peak_50 += 1
            if pnl is not None:
                d = _day(rec.get("exited_at"))
                if d:
                    by_day[d]["pnl"] += pnl
                    by_day[d]["count"] += 1
        elif rec.get("status") == "open":
            open_count += 1
    all_trades = [t for v in per_dir.values() for t in v]
    drill = []
    # Pulses (the signals) per direction, for the examples view.
    try:
        prose = db.recent(c, "flow_pulse", limit=200)
    except Exception:
        prose = []
    per_dir_pulses = defaultdict(int)
    for r in prose:
        p = r.get("payload") or {}
        if isinstance(p, dict) and p.get("symbol"):
            per_dir_pulses[str(p.get("direction") or "?").lower()] += 1
    for direction in sorted(set(list(per_dir.keys()) + list(per_dir_pulses.keys()))):
        st = _stats(per_dir.get(direction, []))
        st["pulses"] = per_dir_pulses.get(direction, 0)
        drill.append(_drill(
            direction.capitalize() + " flow",
            "Big-money option flow betting %s." % ("up" if direction == "bullish" else "down"),
            st))
    stats = _stats(all_trades)
    stats["open"] = open_count
    stats["peak_50_plus"] = peak_50
    stats["pulses"] = sum(per_dir_pulses.values())
    return (_block(
        "flow_pulse", "Flow Pulse",
        "Unusually large directional option flow ($1M+ in 30 minutes).",
        "Whenever big money prints. Each pulse paper-tracks one suggested contract for two weeks.",
        "Win = the paper contract was worth more at the two-week exit than at entry, after fees.",
        stats, drill), by_day)


# ---------------------------------------------------------------- flash agentic


def _flash(db, c, now):
    try:
        rows = db.recent(c, "pick_check", limit=5000)
    except Exception:
        rows = []
    per_pattern = defaultdict(int)
    by_day = defaultdict(lambda: {"count": 0})
    for r in rows:
        p = r.get("payload") or {}
        if p.get("source") != "flash_agentic":
            continue
        per_pattern[p.get("pattern") or "unknown"] += 1
        d = _day(p.get("at") or r.get("ts"))
        if d:
            by_day[d]["count"] += 1
    hit = {}
    try:
        from .pick_scorecard import scorecard
        sc = scorecard(db, c, now, limit=500)
        for row in sc.get("by_pattern") or []:
            if row.get("source") == "flash_agentic":
                hit[row.get("pattern")] = row
    except Exception:
        pass
    drill = []
    total_r, r_n = 0.0, 0
    for pattern in sorted(per_pattern):
        h = hit.get(pattern) or {}
        checks = h.get("checks", 0)
        if h.get("total_r") is not None:
            total_r += h["total_r"]
            r_n += h.get("r_count") or 0
        drill.append(_drill(
            pattern,
            "Flash Agentic chart setups of this pattern type.",
            {"tracked": per_pattern[pattern],
             "wins": h.get("wins", 0),
             "win_rate": h.get("hit_rate"),
             "resolved": checks,
             "expectancy_r": h.get("expectancy_r")}))
    total_tracked = sum(per_pattern.values())
    total_wins = sum((hit.get(p) or {}).get("wins", 0) for p in per_pattern)
    total_resolved = sum((hit.get(p) or {}).get("checks", 0) for p in per_pattern)
    stats = {"tracked": total_tracked, "wins": total_wins,
             "win_rate": round(100.0 * total_wins / total_resolved, 1) if total_resolved else None,
             "resolved": total_resolved, "pnl": None,
             "expectancy_r": round(total_r / r_n, 2) if r_n else None}
    return (_block(
        "flash", "Flash Agentic",
        "Chart setups from the Obsidian Cipher scanner.",
        "Whenever a setup triggers intraday. Tracked silently; one summary a day.",
        "Win = price hit the target before the invalidation level, within five sessions. "
        "Scored by hit rate, not dollars — these are setups, not trades.",
        stats, drill), by_day)


# ---------------------------------------------------------------- 0dte


def _0dte(db, c, now):
    from .store import events
    p = events.c.payload
    plans = 0
    alerts = 0
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    # Targeted queries: the alert stream is high-volume (~100+/hour), so a
    # blunt recent(limit=5000) silently drops morning rows after a few days.
    try:
        plan_rows = c.execute(select(events).where(
            events.c.kind == "alert",
            events.c.source == "spy_brief",
            events.c.ts >= now - 90 * 86400,
        )).mappings()
    except Exception:
        plan_rows = []
    try:
        alert_rows = c.execute(select(events).where(
            events.c.kind == "alert",
            events.c.ts >= now - 90 * 86400,
            p["publication"]["category"].as_string() == "options_0dte",
        )).mappings()
    except Exception:
        alert_rows = []
    for r in plan_rows:
        plans += 1
        d = _day(r.get("ts"))
        if d:
            by_day[d]["count"] += 1
    for r in alert_rows:
        alerts += 1
        d = _day(r.get("ts"))
        if d:
            by_day[d]["count"] += 1
    # Paper trades: 1 contract each, same-session only (see zero_dte_paper.py)
    try:
        paper = db.prefix(c, "trade:0dte-")
    except Exception:
        paper = {}
    per_kind = defaultdict(list)
    open_count = 0
    for t in paper.values():
        if not isinstance(t, dict):
            continue
        if t.get("status") == "closed" or _num(t.get("exited_at")):
            pnl = _num(t.get("pnl"))
            won = pnl is not None and pnl > 0
            per_kind[t.get("kind") or "?"].append((won, pnl))
            if pnl is not None:
                d = _day(t.get("exited_at"))
                if d:
                    by_day[d]["pnl"] += pnl
        else:
            open_count += 1
    all_trades = [t for v in per_kind.values() for t in v]
    stats = _stats(all_trades)
    stats["open"] = open_count
    stats["plans"] = plans
    stats["alerts"] = alerts
    drill = [_drill("SPY morning plan",
                    "The pre-market SPY plan: bias, levels, stop, target. Paper buys 1 contract at the ask on confirmation; exits at the plan target/stop or 2:55pm CT.",
                    _stats(per_kind.get("spy_plan", []))),
             _drill("0DTE scanner",
                    "Same-day-expiry setups on the liquid watchlist. Paper buys 1 contract at the ask; exits at +50%/-50% or 2:55pm CT.",
                    _stats(per_kind.get("scanner", [])))]
    return (_block(
        "0dte", "0DTE",
        "Same-day-expiry option plans and scanner alerts.",
        "The SPY plan every morning; scanner alerts intraday.",
        "Win = the 1-contract paper trade exited above its entry, after fees. "
        "Same-session only — 0DTE expires at the close, flat by 2:55pm CT.",
        stats, drill), by_day)


# ---------------------------------------------------------------- level 3 detail
#
# One more level down: per-ticker weekly history, per-method trades,
# per-direction tracks, per-pattern setups, per-plan/alert lists.
# Items use the same plain-English card shape as the drill-down.


def _m(pnl):
    if pnl is None:
        return "—"
    v = int(round(pnl))
    if v == 0:
        return "$0"
    return "%s$%s" % ("-" if v < 0 else "+", f"{abs(v):,}")


def _ts(ts):
    try:
        return datetime.fromtimestamp(float(ts), CT).strftime("%b %-d, %-I:%M%p CT")
    except (TypeError, ValueError, OSError):
        return "—"


def _detail_smoothers(db, c, now, name):
    rows = list(c.execute(select(
        smoothers_weekly.c.week, smoothers_weekly.c.status,
        smoothers_weekly.c.payload)
        .where(smoothers_weekly.c.ticker == name)
        .order_by(smoothers_weekly.c.week.desc()).limit(104)).mappings())
    items = []
    for r in rows:
        p = r.get("payload") or {}
        if not isinstance(p, dict):
            continue
        res = option_result(p)
        pnl = res.get("net_pnl")
        # Public detail shows only weeks with real, measured option P&L.
        if pnl is None:
            continue
        status = r.get("status") or "?"
        week = r.get("week") or "?"
        try:
            dt = datetime.fromisoformat(week)
            wl = dt.strftime("%b %d, %Y")
            tag = dt.strftime("%m/%d")
        except (ValueError, TypeError):
            wl, tag = week, "?"
        direction = (p.get("direction") or "").upper()
        entry, target = p.get("entry_price"), p.get("target_price")
        sub = ("%s %s" % (name, direction)).strip()
        if entry and target:
            try:
                sub += " · $%.2f → $%.2f" % (float(entry), float(target))
            except (TypeError, ValueError):
                pass
        orows = [["Status", status], ["Net P&L", _m(pnl)]]
        orows.append(["Target hit", "Yes" if status == "WIN" else "No"])
        items.append({
            "tag": tag, "title": "Week of " + wl, "sub": sub, "rows": orows,
            "result": _m(pnl),
            "result_cls": "pos" if pnl > 0 else ("neg" if pnl < 0 else ""),
        })
    return {"type": "smoothers", "name": name,
            "title": "%s — week by week" % name,
            "sub": "Every weekly signal on %s with measured option P&L, newest first." % name,
            "items": items}


def _detail_futures(db, c, now, name):
    from .alert_format import FUTURES_EXIT_REASONS
    name_to_tag = {ICT_SETUP_NAMES.get(t, t): t for t in ICT_PAPER_TAGS.values()}
    tag = name_to_tag.get(name, name)
    try:
        trades = db.prefix(c, "trade:ict-")
    except Exception:
        trades = {}
    items = []
    for t in trades.values():
        if not isinstance(t, dict) or t.get("strategy") != tag:
            continue
        sym = str(t.get("symbol") or "?").split(".")[0]
        side = str(t.get("side") or "?")
        entered = _num(t.get("entered_at")) or _num(t.get("decided_at"))
        exited = _num(t.get("exited_at"))
        pnl = _num(t.get("pnl"))
        entry, exit = _num(t.get("entry")), _num(t.get("exit"))
        status = t.get("status") or "?"
        if exited:
            sub = "%s → %s" % (_ts(entered), _ts(exited))
        else:
            sub = "Opened %s · still open" % _ts(entered)
        reason = t.get("exit_reason")
        orows = [["Entry", "%.2f" % entry if entry is not None else "—"],
                 ["Exit", "%.2f" % exit if exit is not None else "—"]]
        if status == "closed" and reason:
            orows.append(["Exit reason", FUTURES_EXIT_REASONS.get(reason, reason)])
        elif status != "closed":
            orows.append(["Stop", "%.2f" % _num(t.get("stop")) if _num(t.get("stop")) is not None else "—"])
            orows.append(["Target", "%.2f" % _num(t.get("target")) if _num(t.get("target")) is not None else "—"])
        gap = t.get("data_gap")
        if gap:
            orows.append(["Data", "⚠️ flew blind — excluded from wins/losses"])
        items.append({
            "tag": "L" if side == "long" else ("S" if side == "short" else "?"),
            "title": "%s %s" % (sym, side), "sub": sub, "rows": orows,
            "_sort": entered or 0,
            "result": ("⚠️ " if gap else "") + (_m(pnl) if pnl is not None else ("Open" if status != "closed" else "—")),
            "result_cls": "" if gap else ("pos" if pnl is not None and pnl > 0 else ("neg" if pnl is not None and pnl < 0 else "")),
        })
    items.sort(key=lambda i: i.pop("_sort"), reverse=True)
    return {"type": "futures", "name": name,
            "title": "%s — trade by trade" % name,
            "sub": "Every paper trade this detector has taken, newest first. Micros, flat by 3:45pm CT.",
            "items": items[:100]}


def _detail_flow(db, c, now, name):
    """Level 3 for Flow Pulse: the actual pulses (examples), newest first,
    each annotated with its paper-track status."""
    direction = "bearish" if "bear" in name.lower() else "bullish"
    try:
        rows = db.recent(c, "flow_pulse", limit=200)
    except Exception:
        rows = []
    try:
        track_by_key = {}
        for t in db.prefix(c, "flow_pulse_track:").values():
            if isinstance(t, dict) and t.get("key"):
                track_by_key[t["key"]] = t
    except Exception:
        track_by_key = {}

    def _big(n):
        v = _num(n)
        if v is None:
            return "—"
        if abs(v) >= 1_000_000:
            return "$%.1fM" % (v / 1_000_000)
        if abs(v) >= 1_000:
            return "$%.0fK" % (v / 1_000)
        return "$%.0f" % v

    items = []
    for r in rows:
        p = r.get("payload") or {}
        if not isinstance(p, dict):
            continue
        if str(p.get("direction") or "").lower() != direction:
            continue
        sym = p.get("symbol") or "?"
        prem = _num(p.get("directional_premium"))
        opp = _num(p.get("opposing_premium"))
        score = p.get("max_score")
        sug = p.get("suggested_contract") or {}
        fired = _num(p.get("first_seen")) or _num(r.get("ts")) or 0
        contract_txt = "None — no contract suggested"
        if isinstance(sug, dict) and sug.get("strike"):
            contract_txt = "%s%s%s" % (sug.get("strike"),
                                       str(sug.get("type") or "")[:1].upper(),
                                       (" · " + str(sug.get("expiry"))) if sug.get("expiry") else "")
        tr = track_by_key.get(r.get("key") or "") or {}
        tstatus = tr.get("status")
        tpnl = _num(tr.get("pnl"))
        if tstatus == "closed" and tpnl is not None:
            track_txt = "%s closed" % _m(tpnl)
            result, result_cls = _m(tpnl), "pos" if tpnl > 0 else ("neg" if tpnl < 0 else "")
        elif tstatus == "open":
            track_txt = "Track open"
            result, result_cls = "Track open", ""
        else:
            track_txt = "No track opened"
            result, result_cls = "No track", ""
        orows = [["Directional premium", _big(prem)],
                 ["Opposing premium", _big(opp)],
                 ["Prints", str(p.get("print_count") or "—")],
                 ["Suggested contract", contract_txt],
                 ["Paper track", track_txt]]
        sub = _ts(fired)
        if prem:
            sub += " · %s directional" % _big(prem)
        if score is not None:
            try:
                sub += " · score %g" % float(score)
            except (TypeError, ValueError):
                pass
        items.append({
            "tag": sym[:6], "title": "%s %s" % (sym, direction),
            "sub": sub, "rows": orows, "_sort": fired,
            "result": result, "result_cls": result_cls,
        })
    items.sort(key=lambda i: i.pop("_sort"), reverse=True)
    return {"type": "flow_pulse", "name": name,
            "title": "%s — pulse by pulse" % name,
            "sub": "Every big-money pulse caught on the %s side, newest first. Each opens a two-week paper track when a contract can be suggested." % direction,
            "items": items[:100]}


def _detail_flash(db, c, now, name):
    try:
        from .pick_scorecard import scorecard
        sc = scorecard(db, c, now, limit=500)
        checks = sc.get("checks") or []
    except Exception:
        checks = []
    items = []
    for ch in checks:
        if ch.get("source") != "flash_agentic":
            continue
        if (ch.get("pattern") or "unknown") != name:
            continue
        out = ch.get("outcome") or {}
        st = out.get("status") or "unknown"
        olabel = {"win": "Target hit", "loss": "Invalidated",
                  "open": "Still open"}.get(st, "—")
        ticker = ch.get("ticker") or "?"
        direction = ch.get("direction") or "?"
        at = _num(ch.get("at"))
        sub = "Logged %s" % _ts(at)
        if ch.get("setup_score") is not None:
            sub += " · score %g" % ch["setup_score"]
        def _f(x):
            v = _num(x)
            return "%.2f" % v if v is not None else "—"
        orows = [["Entry", _f(ch.get("entry"))],
                 ["Target", _f(ch.get("target"))],
                 ["Invalidation", _f(ch.get("invalidation"))],
                 ["Outcome", olabel]]
        items.append({
            "tag": ticker[:6], "title": "%s %s" % (ticker, direction),
            "sub": sub, "rows": orows, "_sort": at or 0,
            "result": olabel,
            "result_cls": "pos" if st == "win" else ("neg" if st == "loss" else ""),
        })
    items.sort(key=lambda i: i.pop("_sort"), reverse=True)
    return {"type": "flash", "name": name,
            "title": "%s — setup by setup" % name,
            "sub": "Every logged setup of this pattern, newest first. Target before invalidation wins.",
            "items": items[:100]}


def _detail_0dte(db, c, now, name):
    from .store import events
    from sqlalchemy import or_
    p = events.c.payload
    items = []
    if name == "SPY morning plan":
        from .day_digest import _plan_outcome
        try:
            rows = c.execute(select(events).where(
                events.c.kind == "alert",
                events.c.source == "spy_brief",
                events.c.ts >= now - 90 * 86400,
            ).order_by(events.c.ts.desc()).limit(60)).mappings()
        except Exception:
            rows = []
        for r in rows:
            rep = r.get("payload") or {}
            if not isinstance(rep, dict):
                continue
            ts = _num(r.get("ts")) or 0
            decision = rep.get("decision") or rep.get("bias") or "published"
            plans = rep.get("plans") or {}
            orows = [["Decision", decision]]
            for side in ("call", "put"):
                plan = plans.get(side) or {}
                if plan.get("stop") or plan.get("target"):
                    orows.append(["%s stop/target" % side.title(),
                                  "%.2f / %.2f" % (_num(plan.get("stop")) or 0,
                                                   _num(plan.get("target")) or 0)])
            outcome = ""
            if str(decision).endswith("SETUP CONFIRMED"):
                try:
                    outcome = _plan_outcome(db, c, rep, decision, ts) or ""
                except Exception:
                    outcome = ""
            if outcome:
                orows.append(["Outcome", outcome])
            day = _day(ts) or "?"
            items.append({
                "tag": "SPY", "title": "%s — %s" % (day, decision),
                "sub": "Pre-market plan, %s" % _ts(ts), "rows": orows,
                "result": outcome or "No confirmed setup",
                "result_cls": "pos" if "target" in outcome.lower() else ("neg" if "stop" in outcome.lower() else ""),
            })
        title = "SPY morning plan — day by day"
        sub = "Each morning's SPY plan and what happened to it."
    else:
        try:
            rows = c.execute(select(events).where(
                events.c.kind == "alert",
                events.c.ts >= now - 90 * 86400,
                p["publication"]["category"].as_string() == "options_0dte",
            ).order_by(events.c.ts.desc()).limit(100)).mappings()
        except Exception:
            rows = []
        for r in rows:
            pub = (r.get("payload") or {}).get("publication") or {}
            contract = pub.get("contract") or {}
            ts = _num(r.get("ts")) or 0
            underlying = contract.get("underlying") or "?"
            strike = contract.get("strike")
            typ = contract.get("type") or ""
            event = pub.get("status") or pub.get("event") or ""
            title_txt = "%s %s%s" % (underlying,
                                     ("%g" % _num(strike)) if _num(strike) is not None else "?",
                                     typ[:1].upper() if typ else "")
            orows = [["Event", event or "—"], ["Time", _ts(ts)]]
            items.append({
                "tag": underlying[:6], "title": title_txt,
                "sub": _ts(ts), "rows": orows,
                "result": event or "Alert", "result_cls": "",
            })
        title = "0DTE scanner — alert by alert"
        sub = "Every same-day-expiry scanner alert, newest first."
    return {"type": "0dte", "name": name, "title": title, "sub": sub, "items": items}


def detail(db, c, now, dtype, name):
    builders = {
        "smoothers": _detail_smoothers,
        "futures": _detail_futures,
        "flow_pulse": _detail_flow,
        "flash": _detail_flash,
        "0dte": _detail_0dte,
    }
    fn = builders.get(dtype)
    if not fn or not name:
        return None
    try:
        return fn(db, c, now, str(name)[:80])
    except Exception:
        return {"type": dtype, "name": name, "title": str(name),
                "sub": "", "items": []}


# ---------------------------------------------------------------- public


def summary(db, c, now):
    blocks = {}
    for fn in (_smoothers, _futures, _flow_pulse, _flash, _0dte):
        try:
            block, _ = fn(db, c, now)
        except Exception:
            continue
        blocks[block["key"]] = block
    return {"asof": now, "types": blocks}


def calendar(db, c, now, year, month):
    days = defaultdict(lambda: defaultdict(lambda: {"pnl": 0.0, "count": 0}))
    fns = (_smoothers, _futures, _flow_pulse, _flash, _0dte)
    for fn in fns:
        try:
            block, by_day = fn(db, c, now)
        except Exception:
            continue
        key = block["key"]
        for d, v in by_day.items():
            try:
                dt = datetime.fromisoformat(d).date()
            except ValueError:
                continue
            if dt.year == year and dt.month == month:
                days[d][key] = {"pnl": round(v.get("pnl", 0.0), 2), "count": v.get("count", 0)}
    return {"year": year, "month": month,
            "days": {d: dict(v) for d, v in sorted(days.items())}}
