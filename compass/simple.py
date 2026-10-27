"""Plain-English "Simple" view: per-type scoreboards, drill-downs, and a P&L
calendar. Read-only aggregations over the same stores the trackers write.

Each type block carries plain-English copy:
- watches: what the tracker follows
- fires: when it produces a signal
- scored: how a win/loss is measured

Dollar P&L exists for smoothers (paper options), futures (paper micros), and
flow pulse (paper tracks). 0DTE is scored by hit rate /
counts — no dollars, and the UI must say so.
"""

import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, func

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


def _smoothers_delivery(db, c):
    """Entry-alert delivery evidence for Smoothers, or None.

    Josh's rule (2026-10-14): only Smoothers whose alert was actually
    DELIVERED count in the stats. The receipt of record is the native
    outbox: a pick's entry alert is the per-signal ':entry' row (the
    join /api/admin/repair-smoothers-alerts uses), or the weekly compact
    batch row naming the ticker for picks without an individual alert.
    Rows from the primary ('smoothers') and shared ('smoothers_shared')
    destinations are merged: delivery to either configured destination
    is a delivered alert.

    Returns None when the native outbox is not this store's delivery
    record — no accepted Compass ownership grant. Before the weekly
    handoff, the original Smoothers sender delivered through its own
    channel and kept no receipts here ('original_delivery_evidence:
    unrecorded' in the handoff plan), so there is nothing to join
    against and stats keep their historical basis.
    """
    try:
        own = db.get(c, "native:ownership:smoothers", {}) or {}
        if own.get("owner") != "compass" or not own.get("effective_from"):
            return None
        from .native_outbox import outbox as native_outbox
        rows = list(c.execute(select(
            native_outbox.c.event_key, native_outbox.c.status,
            native_outbox.c.payload).where(
            native_outbox.c.program.in_(("smoothers", "smoothers_shared")))).mappings())
    except Exception:
        return None
    entries = defaultdict(set)
    compact = defaultdict(list)
    for r in rows:
        ek = r.get("event_key") or ""
        if ek.endswith(":entry"):
            entries[ek[:-len(":entry")]].add(r.get("status"))
        elif ":compact:" in ek:
            p = r.get("payload") or {}
            compact[ek.split(":compact:")[0]].append(
                (r.get("status"), p.get("content") if isinstance(p, dict) else None))
    return {"cutoff": _num(own.get("effective_from")),
            "effective_week": own.get("effective_week"),
            "entries": dict(entries), "compact": dict(compact)}


def _compact_names_ticker(content, ticker):
    """True when a compact-batch message carries this ticker's one-liner.

    Compact lines lead with the ticker ("AAPL CALL $210.00→$215.00 Q72.5",
    joined by ' | '), so match on token boundaries, not substrings —
    'A' must not match inside 'AAPL'.
    """
    if not content or not ticker:
        return False
    return re.search(r"(?<![A-Z0-9.\-])" + re.escape(ticker) +
                     r"(?![A-Z0-9.\-])", content) is not None


def _smoother_alert_delivered(ev, sid, week, ticker, p):
    """True when the pick's entry alert was delivered to Discord.

    Native era (cohort at/after the handoff cutoff): a delivered
    ':entry' row — or a delivered compact batch naming the ticker —
    counts; a recorded send that never delivered (failed / suppressed /
    still pending / ambiguous) does not, and neither does a pick with
    no delivery record at all.

    Pre-handoff cohorts were the original sender's: its delivery is
    unrecorded here and the native rows are shadow previews, so a
    missing or shadow-only record for those cohorts is not evidence of
    non-delivery. A recorded non-shadow outcome still decides, whenever
    it exists.
    """
    statuses = set(ev["entries"].get(sid) or ()) if sid else set()
    batches = ev["compact"].get(week) or []
    named = [s for s, content in batches
             if _compact_names_ticker(content, ticker)]
    statuses.update(named)
    if "delivered" in statuses:
        return True
    if statuses - {"shadow"}:
        return False
    cohort = _num(p.get("model_entry_time")) or _num(p.get("created_at"))
    if cohort is not None and ev.get("cutoff") is not None:
        return cohort < ev["cutoff"]
    if week and ev.get("effective_week"):
        return week < ev["effective_week"]
    return False


def _smoothers(db, c, now):
    rows = list(c.execute(select(
        smoothers_weekly.c.id, smoothers_weekly.c.week,
        smoothers_weekly.c.ticker, smoothers_weekly.c.status,
        smoothers_weekly.c.payload)).mappings())
    evidence = _smoothers_delivery(db, c)
    # Headline basis (2026-10-26, Claude §6 / Josh): WIN/LOSS is the
    # underlying outcome and is counted by status for every delivered
    # resolved pick. Option P&L is a separate, measured subset: resolved
    # picks whose entry and exit quotes are both usable. A resolved pick
    # without a usable exit quote is tallied as 'excluded' beside the
    # headline — the futures data_gap pattern — never silently dropped
    # (previously such picks vanished and the win rate divided by the
    # measured subset only).
    per_ticker = defaultdict(lambda: {"wins": 0, "losses": 0, "pnls": [],
                                      "excluded": 0, "unresolved": 0})
    per_ticker_undelivered = defaultdict(list)
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    for r in rows:
        p = r.get("payload") or {}
        if not isinstance(p, dict):
            continue
        status = r.get("status") or p.get("status")
        pnl = option_result(p).get("net_pnl")
        if evidence is not None and not _smoother_alert_delivered(
                evidence, r.get("id") or p.get("id"),
                r.get("week") or p.get("week"), r.get("ticker") or "?", p):
            # Josh 2026-10-14: only delivered alerts count. A pick whose
            # entry alert never reached Discord is excluded from the
            # headline and bucketed separately (measured picks only, as
            # before) — the same transparency pattern as futures data_gap
            # trades and the 0DTE pre-evidence reset (audit #8). It stays
            # in the DB and in the trade-by-trade detail views.
            if pnl is not None:
                per_ticker_undelivered[r.get("ticker") or "?"].append(pnl)
            continue
        if status not in ("WIN", "LOSS", "UNRESOLVED"):
            continue  # still open: no final outcome to count or exclude
        t = per_ticker[r.get("ticker") or "?"]
        if status == "WIN":
            t["wins"] += 1
        elif status == "LOSS":
            t["losses"] += 1
        else:
            t["unresolved"] += 1
        if pnl is None:
            t["excluded"] += 1
            continue
        t["pnls"].append(pnl)
        d = _day(p.get("resolution_time"))
        if d:
            by_day[d]["pnl"] += pnl
            by_day[d]["count"] += 1

    def _outcome_stats(t):
        n = t["wins"] + t["losses"]
        return {
            "tracked": n,
            "wins": t["wins"],
            "win_rate": round(100.0 * t["wins"] / n, 1) if n else None,
            "pnl": round(sum(t["pnls"]), 2),
            "measured": len(t["pnls"]),
        }

    drill = []
    for ticker in sorted(per_ticker):
        t = per_ticker[ticker]
        st = _outcome_stats(t)
        if t["excluded"]:
            st["excluded"] = t["excluded"]
        if t["unresolved"]:
            st["unresolved"] = t["unresolved"]
        und = per_ticker_undelivered.get(ticker)
        if und:
            st["undelivered"] = len(und)
            st["undelivered_pnl"] = round(sum(und), 2)
        drill.append(_drill(ticker, "Weekly Smoothers pick on %s." % ticker, st))
    # Note: "current-week" is the default view (auto-opened by frontend),
    # not a drill list item. The stock list is for research.
    tot = {"wins": 0, "losses": 0, "pnls": [], "excluded": 0, "unresolved": 0}
    for t in per_ticker.values():
        tot["wins"] += t["wins"]
        tot["losses"] += t["losses"]
        tot["pnls"].extend(t["pnls"])
        tot["excluded"] += t["excluded"]
        tot["unresolved"] += t["unresolved"]
    stats = _outcome_stats(tot)
    stats["target_hits"] = tot["wins"]
    stats["excluded"] = tot["excluded"]
    stats["unresolved"] = tot["unresolved"]
    undelivered = [pnl for v in per_ticker_undelivered.values() for pnl in v]
    if undelivered:
        stats["undelivered"] = len(undelivered)
        stats["undelivered_pnl"] = round(sum(undelivered), 2)
    return (_block(
        "smoothers", "Smoothers",
        "Weekly option picks on big liquid stocks.",
        "Every Monday morning: one call or put per stock, held for the week.",
        "Win = the stock reached its target by Friday; the win rate counts "
        "every delivered pick that resolved WIN or LOSS. Paper P&L = the "
        "paper option estimate (bought at ask, sold at bid, minus fees) over "
        "the measured subset: resolved picks with usable entry and exit "
        "quotes. Resolved picks without a usable exit quote (including "
        "unresolved weeks) are tallied as 'excluded', never silently "
        "dropped. Only picks whose entry alert was actually delivered to "
        "Discord count; measured picks that were never alerted are excluded "
        "and tallied separately.",
        stats, drill), by_day)


# ---------------------------------------------------------------- futures


def _futures(db, c, now):
    from .ict_paper import symbol_allowed
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

        # Josh 2026-10-20: the webpage shows ONLY trades TradersPost
        # took (live on Tradeify). Paper-only trades stay in the DB
        # for attribution but are not displayed here.
        if not t.get("traderspost_sent"):
            continue
        # Mix/match filter (Josh 2026-10-14): only count KEEP detector+symbol
        # combos in totals. Dropped combos are hidden from stats (they're
        # still in the DB for history, but don't count toward the totals).
        detector = t.get("detector") or ""
        symbol = t.get("symbol") or ""
        if not symbol_allowed(detector, symbol):
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
            # by_day tracks all detectors (2026-10-14: filter removed)
            if pnl is not None:
                d = _day(t.get("exited_at"))
                if d:
                    by_day[d]["pnl"] += pnl
                    by_day[d]["count"] += 1
        else:
            # Count all open trades (2026-10-14: filter removed)
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
    # 2026-10-14: Show ALL detectors on the page (filter removed).
    # All detectors that have trades appear in the roster.
    ALL_TAGS = sorted(per_method.keys())
    for tag in ALL_TAGS:
        name = ICT_SETUP_NAMES.get(tag, tag)
        if tag in seen:
            continue
        seen.add(tag)
        st = with_gaps(tag, per_method.get(tag, []))
        drill.append(_drill(name, "Micro futures trades sent to TradersPost (live on Tradeify). Paper-only signals are not shown here.",
                            st))
    # 2026-10-14: Stats include ALL detectors (filter removed).
    # All trades across all detectors count toward totals.
    all_trades_list = [t for method, trades in per_method.items()
                       for t in trades]
    stats = _stats(all_trades_list)
    stats["open"] = open_count
    # Count all data gaps across all detectors (2026-10-14: filter removed)
    total_gaps = sum(len(v) for v in per_method_gaps.values())
    if total_gaps:
        stats["data_gaps"] = total_gaps
        stats["data_gap_pnl"] = round(sum(
            _num(t.get("pnl")) or 0
            for v in per_method_gaps.values()
            for t in v), 2)
    return (_block(
        "futures", "Futures Micros",
        "Micro futures trades routed live to TradersPost (Tradeify). Only trades TradersPost took are shown; paper-only signals stay in the background. Single position: 2 contracts at the detector target.",
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


# ---------------------------------------------------------------- tape


def _tape(db, c, now):
    try:
        rows = db.recent(c, "pick_check", limit=5000)
    except Exception:
        rows = []
    per_pattern = defaultdict(int)
    by_day = defaultdict(lambda: {"count": 0})
    for r in rows:
        p = r.get("payload") or {}
        if p.get("source") != "tradermatrix_tape":
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
            if row.get("source") == "tradermatrix_tape":
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
            "TraderMatrix high-conviction option flow of this pattern type.",
            {"tracked": per_pattern[pattern],
             "wins": h.get("wins", 0),
             "win_rate": h.get("hit_rate"),
             "resolved": checks,
             "pnl": h.get("total_pnl"),
             "expectancy_r": h.get("expectancy_r")}))
    total_tracked = sum(per_pattern.values())
    total_wins = sum((hit.get(p) or {}).get("wins", 0) for p in per_pattern)
    total_resolved = sum((hit.get(p) or {}).get("checks", 0) for p in per_pattern)
    total_pnl = sum((hit.get(p) or {}).get("total_pnl") or 0 for p in per_pattern)
    stats = {"tracked": total_tracked, "wins": total_wins,
             "win_rate": round(100.0 * total_wins / total_resolved, 1) if total_resolved else None,
             "resolved": total_resolved, "pnl": round(total_pnl, 2) if total_pnl else None,
             "expectancy_r": round(total_r / r_n, 2) if r_n else None}
    return (_block(
        "tape", "Tape Flow",
        "High-conviction option flow from TraderMatrix (score 95+).",
        "Whenever unusual flow prints intraday. Tracked silently via pick checker.",
        "Win = price hit the target before the invalidation level, within five sessions. "
        "Paper-traded at 100 shares for dollar P&L.",
        stats, drill), by_day)


# ---------------------------------------------------------------- swings


def _swings(db, c, now):
    from .swing_ideas import swings
    from sqlalchemy import select, func
    try:
        rows = c.execute(select(
            swings.c.status,
            swings.c.payload['outcome'].as_string().label('outcome'),
            swings.c.payload['return_pct'].as_float().label('ret'),
            swings.c.created,
        ).where(swings.c.created >= now - 90 * 86400)).mappings().all()
    except Exception:
        rows = []
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    per_rule = defaultdict(list)
    for r in rows:
        d = _day(r.get("created"))
        if d:
            by_day[d]["count"] += 1
        outcome = r.get("outcome")
        ret = r.get("ret")
        # Group by rule if available, otherwise by status
        rule = "swing"
        try:
            # Try to get rule from payload if we have full payload
            pass
        except Exception:
            pass
        per_rule[rule].append((outcome == "win", ret))
    drill = []
    for rule in sorted(per_rule):
        trades = per_rule[rule]
        s = _stats(trades)
        drill.append(_drill(
            rule,
            "Swing option ideas (1-21 DTE).",
            s))
    all_trades = [t for trades in per_rule.values() for t in trades]
    stats = _stats(all_trades)
    return (_block(
        "swings", "Swings",
        "Bullish and bearish daily setups with fresh options flow.",
        "Fresh vendor-classified flow + technical setup align.",
        "Win = target hit before stop/invalidation. Paper-tracked, 1 contract.",
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
        # Fetch recent alerts and filter by category in Python (JSON path
        # in SQL can timeout on 133M rows; Python filter is more reliable)
        raw_alerts = c.execute(select(events).where(
            events.c.kind == "alert",
            events.c.ts >= now - 90 * 86400,
        ).order_by(events.c.ts.desc()).limit(5000)).mappings()
        alert_rows = []
        for r in raw_alerts:
            try:
                pub = (r.get("payload") or {}).get("publication") or {}
                if pub.get("category") == "options_0dte":
                    alert_rows.append(r)
            except Exception:
                continue
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
    # Also include MU scalp paper trades (trade:mu-) which use the same 0DTE framework
    try:
        paper = db.prefix(c, "trade:0dte-")
    except Exception:
        paper = {}
    try:
        mu_paper = db.prefix(c, "trade:mu-")
        # Merge MU trades into the paper dict with kind='mu_scalp'
        for k, v in mu_paper.items():
            if isinstance(v, dict) and 'kind' not in v:
                v = dict(v)
                v['kind'] = 'mu_scalp'
            paper[k] = v
    except Exception:
        pass
    per_kind = defaultdict(list)
    per_rule = defaultdict(list)
    per_underlying = defaultdict(list)
    data_gap_count = 0
    data_gap_pnl = 0.0
    pre_evidence_count = 0
    pre_evidence_pnl = 0.0
    open_count = 0
    for t in paper.values():
        if not isinstance(t, dict):
            continue
        if t.get("status") == "closed" or _num(t.get("exited_at")):
            if t.get("data_gap"):
                # Flew blind: not a valid test of the signal. Excluded from
                # wins/losses, counted separately.
                data_gap_count += 1
                if _num(t.get("pnl")) is not None:
                    data_gap_pnl += _num(t.get("pnl"))
                continue
            if t.get("kind") == "scanner" and not isinstance(t.get("evidence"), dict):
                # Stats reset (audit #8, 2026-10-21): scanner trades opened
                # before evidence enrichment carry no snapshot. They stay in
                # the DB and in the trade-by-trade detail views, but the
                # scorecard (scanner section, per-underlying drills, and the
                # scanner share of the headline total / P&L calendar, which
                # are computed in this same loop) counts only trades that
                # carry an evidence snapshot.
                pre_evidence_count += 1
                if _num(t.get("pnl")) is not None:
                    pre_evidence_pnl += _num(t.get("pnl"))
                continue
            pnl = _num(t.get("pnl"))
            won = pnl is not None and pnl > 0
            per_kind[t.get("kind") or "?"].append((won, pnl))
            # Track scanner trades by underlying (MU, AAPL, TSLA, etc.)
            if t.get("kind") == "scanner":
                underlying = t.get("underlying") or "?"
                per_underlying[underlying].append((won, pnl))
            # Track scalp trades by symbol (MU, TSLA, NVDA, etc.)
            if t.get("kind") == "mu_scalp":
                scalp_sym = t.get("symbol") or "?"
                per_underlying["SCALP:" + scalp_sym].append((won, pnl))
            if t.get("kind") == "spy_plan":
                per_rule[t.get("target_rule") or "wall"].append((won, pnl))
            if pnl is not None:
                d = _day(t.get("exited_at"))
                if d:
                    by_day[d]["pnl"] += pnl
        else:
            if t.get("kind") == "scanner" and not isinstance(t.get("evidence"), dict):
                # Pre-enrichment scanner trade still open at the reset: not
                # part of the fresh book (it remains in the detail views).
                pre_evidence_count += 1
                continue
            open_count += 1
    all_trades = [t for v in per_kind.values() for t in v]
    stats = _stats(all_trades)
    stats["open"] = open_count
    stats["plans"] = plans
    stats["alerts"] = alerts
    if data_gap_count:
        stats["data_gaps"] = data_gap_count
        stats["data_gap_pnl"] = round(data_gap_pnl, 2)
    if pre_evidence_count:
        stats["pre_evidence"] = pre_evidence_count
        stats["pre_evidence_pnl"] = round(pre_evidence_pnl, 2)
    drill = [_drill("SPY morning plan (wall target)",
                    "Paper buys 1 contract at the ask on confirmation; exits at the wall-aware target, stop, or 2:55pm CT. The default since 2026-10-06.",
                    _stats(per_rule.get("wall", []))),
             _drill("SPY morning plan (2R target)",
                    "Same entry, blind 2R target instead. Paper-traded side by side for comparison.",
                    _stats(per_rule.get("2r", []))),
             _drill("0DTE scanner",
                    "Same-day-expiry setups on the liquid watchlist. Paper buys 1 contract at the ask; exits at +50%/-50% or 2:55pm CT. "
                    "Scorecard reset 2026-10-21: only trades carrying a Compass evidence snapshot count; earlier trades remain in the trade-by-trade history.",
                    _stats(per_kind.get("scanner", []))),
             _drill("Stock scalp (all)",
                    "Gap fade (premarket) and spike fade (9:45-11am ET) on volatile stocks. 100 shares, underlying P&L net of costs.",
                    _stats(per_kind.get("mu_scalp", [])))]
    # Add per-underlying drill-downs for scanner trades (MU, AAPL, TSLA, etc.)
    # and per-symbol scalp drill-downs (SCALP:MU, SCALP:TSLA, etc.)
    # Sort by trade count descending so most active underlyings appear first
    for underlying in sorted(per_underlying.keys(),
                             key=lambda u: len(per_underlying[u]), reverse=True):
        trades = per_underlying[underlying]
        if len(trades) >= 1:  # Show even single-trade underlyings
            if underlying.startswith("SCALP:"):
                sym = underlying[6:]
                drill.append(_drill(
                    f"Scalp {sym}",
                    f"Gap/spike fade paper trades on {sym}. 100 shares, net of costs.",
                    _stats(trades)))
            else:
                drill.append(_drill(
                    f"0DTE {underlying}",
                    f"Scanner paper trades on {underlying}. 1 contract at ask; +50%/-50% or 2:55pm CT.",
                    _stats(trades)))
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


def _detail_smoothers_this_week(db, c, now):
    """Show all Smoothers picks for the current week with their live status."""
    # Find the most recent week (max week value)
    week_row = c.execute(select(func.max(smoothers_weekly.c.week))).scalar()
    if not week_row:
        return {"type": "smoothers", "name": "current-week",
                "title": "This Week's Smoothers Picks",
                "sub": "No picks found.", "items": []}
    rows = list(c.execute(select(
        smoothers_weekly.c.id, smoothers_weekly.c.week,
        smoothers_weekly.c.ticker, smoothers_weekly.c.status,
        smoothers_weekly.c.payload)
        .where(smoothers_weekly.c.week == week_row)
        .order_by(smoothers_weekly.c.ticker)).mappings())
    evidence = _smoothers_delivery(db, c)
    items = []
    for r in rows:
        p = r.get("payload") or {}
        if not isinstance(p, dict):
            continue
        ticker = r.get("ticker") or "?"
        status = r.get("status") or "?"
        res = option_result(p)
        pnl = res.get("net_pnl")
        direction = (p.get("direction") or "").upper()
        entry, target = p.get("entry_price"), p.get("target_price")
        sub = ("%s %s" % (ticker, direction)).strip()
        if entry and target:
            try:
                sub += " · $%.2f → $%.2f" % (float(entry), float(target))
            except (TypeError, ValueError):
                pass
        # Show status and P&L if available
        orows = [["Status", status]]
        if pnl is not None:
            orows.append(["Net P&L", _m(pnl)])
        else:
            # For open positions, show target hit status
            orows.append(["Target hit", "Yes" if status == "WIN" else ("No" if status == "LOSS" else "Pending")])
        # Add quality tier if available
        tier = p.get("quality_tier") or p.get("tier")
        if tier:
            orows.append(["Tier", str(tier)])
        # History stays complete: an undelivered pick is marked, not
        # removed (it is only excluded from the headline stats).
        if evidence is not None and not _smoother_alert_delivered(
                evidence, r.get("id") or p.get("id"),
                r.get("week") or p.get("week"), ticker, p):
            orows.append(["Alert", "Not delivered — excluded from stats"])
        items.append({
            "tag": ticker, "title": "%s %s" % (ticker, direction), "sub": sub, "rows": orows,
            "result": _m(pnl) if pnl is not None else status,
            "result_cls": "pos" if (pnl or 0) > 0 else ("neg" if (pnl or 0) < 0 else ""),
        })
    try:
        dt = datetime.fromisoformat(week_row)
        wl = dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        wl = week_row
    return {"type": "smoothers", "name": "current-week",
            "title": "This Week's Picks — Week of %s" % wl,
            "sub": "%d picks, live status. Newest signals first." % len(items),
            "items": items}


def _detail_smoothers(db, c, now, name):
    # Special case: "current-week" shows all picks for the current week
    if name == "current-week":
        return _detail_smoothers_this_week(db, c, now)
    rows = list(c.execute(select(
        smoothers_weekly.c.id, smoothers_weekly.c.week,
        smoothers_weekly.c.status, smoothers_weekly.c.payload)
        .where(smoothers_weekly.c.ticker == name)
        .order_by(smoothers_weekly.c.week.desc()).limit(104)).mappings())
    evidence = _smoothers_delivery(db, c)
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
        # Undelivered picks stay in the history, marked — they are only
        # excluded from the headline stats (Josh 2026-10-14).
        if evidence is not None and not _smoother_alert_delivered(
                evidence, r.get("id") or p.get("id"), week, name, p):
            orows.append(["Alert", "Not delivered — excluded from stats"])
        # Holding time from entry to exit quote timestamps (2026-10-13)
        try:
            eq = p.get("quote") or {}
            xq = p.get("exit_quote") or {}
            start_ms = eq.get("quote_at_ms")
            end_ms = xq.get("quote_at_ms")
            if start_ms and end_ms and end_ms > start_ms:
                hold_s = (end_ms - start_ms) / 1000.0
                if hold_s < 3600:
                    hold_str = "%.1f min" % (hold_s / 60.0)
                elif hold_s < 86400:
                    hold_str = "%.1f hrs" % (hold_s / 3600.0)
                else:
                    hold_str = "%.1f days" % (hold_s / 86400.0)
                orows.append(["Holding time", hold_str])
        except (TypeError, ValueError, AttributeError):
            pass
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
    from .ict_paper import symbol_allowed
    name_to_tag = {ICT_SETUP_NAMES.get(t, t): t for t in ICT_PAPER_TAGS.values()}
    tag = name_to_tag.get(name, name)
    try:
        trades = db.prefix(c, "trade:ict-")
    except Exception:
        trades = {}
    # Group legs by group_id (2/2/1 scale-out: T1, T2, Trail are legs of ONE trade).
    # Without grouping, a single trade shows as 3 separate cards, which looks
    # like 9 trades when there are really 3 (e.g. MES, MGC, MCL).
    groups = {}
    for t in trades.values():
        if not isinstance(t, dict) or t.get("strategy") != tag:
            continue
        # Mix/match filter: only show KEEP detector+symbol combos
        if not symbol_allowed(t.get("detector") or "", t.get("symbol") or ""):
            continue
        # Josh 2026-10-20: only trades TradersPost took are displayed.
        if not t.get("traderspost_sent"):
            continue
        gid = t.get("group_id") or t.get("id")
        groups.setdefault(gid, []).append(t)
    items = []
    for gid, legs in groups.items():
        # Use the T1 leg (or first leg) as the representative for the group
        legs_sorted = sorted(legs, key=lambda x: (x.get("leg") or "") != "t1")
        t = legs_sorted[0]
        sym = str(t.get("symbol") or "?").split(".")[0]
        side = str(t.get("side") or "?")
        entered = _num(t.get("entered_at")) or _num(t.get("decided_at"))
        # Group status: open if ANY leg is open, closed if ALL legs closed
        any_open = any(l.get("status") == "open" for l in legs)
        all_closed = all(l.get("status") == "closed" for l in legs)
        status = "open" if any_open else ("closed" if all_closed else t.get("status") or "?")
        # Group P&L: sum of closed legs' P&L (open legs have no P&L yet).
        # Exclude data_gap legs (same as _futures): their P&L is not valid
        # because stops/targets were never verifiably managed. E.g. SMT
        # phantom TRAIL leg had $112k fake P&L from MNQ-price bug.
        total_pnl = sum(_num(l.get("pnl")) or 0 for l in legs
                        if l.get("status") == "closed" and not l.get("data_gap"))
        has_closed = any(l.get("status") == "closed" and not l.get("data_gap") for l in legs)
        has_data_gap = any(l.get("data_gap") for l in legs)
        pnl = total_pnl if has_closed else None
        entry = _num(t.get("entry"))
        # Exit: use the latest exit among closed legs
        closed_legs = [l for l in legs if l.get("status") == "closed"]
        exit = _num(closed_legs[-1].get("exit")) if closed_legs else None
        exited = max([_num(l.get("exited_at")) or 0 for l in closed_legs], default=0) or None
        if exited:
            sub = "%s → %s" % (_ts(entered), _ts(exited))
        else:
            sub = "Opened %s · still open" % _ts(entered)
        # Add leg count to subtitle if multiple legs
        if len(legs) > 1:
            leg_status = []
            for leg in sorted(legs, key=lambda x: x.get("leg") or ""):
                leg_name = leg.get("leg") or "?"
                leg_st = leg.get("status") or "?"
                leg_tgt = _num(leg.get("target"))
                if leg.get("data_gap"):
                    leg_status.append("%s: ⚠️ data gap" % leg_name.upper())
                elif leg_st == "closed":
                    leg_pnl = _num(leg.get("pnl"))
                    leg_status.append("%s: %s" % (leg_name.upper(), _m(leg_pnl) if leg_pnl is not None else "closed"))
                elif leg_tgt is not None:
                    leg_status.append("%s: tgt %.2f" % (leg_name.upper(), leg_tgt))
                else:
                    leg_status.append("%s: trailing" % leg_name.upper())
            sub += " · " + ", ".join(leg_status)
        if has_data_gap:
            sub += " · ⚠️ data gap (excluded from P&L)"
        reason = t.get("exit_reason")
        orows = [["Entry", "%.2f" % entry if entry is not None else "—"],
                 ["Exit", "%.2f" % exit if exit is not None else "—"]]
        if status == "closed" and reason:
            orows.append(["Exit reason", FUTURES_EXIT_REASONS.get(reason, reason)])
        elif status != "closed":
            orows.append(["Stop", "%.2f" % _num(t.get("stop")) if _num(t.get("stop")) is not None else "—"])
            # Show targets for each leg
            for leg in sorted(legs, key=lambda x: x.get("leg") or ""):
                leg_tgt = _num(leg.get("target"))
                if leg_tgt is not None:
                    orows.append(["Target %s" % (leg.get("leg") or "?").upper(), "%.2f" % leg_tgt])
                elif leg.get("is_trailer"):
                    orows.append(["Target TRAIL", "Trailing"])
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
            "sub": "Every paper trade this detector has taken, newest first. Micros, flat by 3:45pm CT. 2/2/1 legs grouped.",
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


def _detail_tape(db, c, now, name):
    try:
        from .pick_scorecard import scorecard
        sc = scorecard(db, c, now, limit=500)
        checks = sc.get("checks") or []
    except Exception:
        checks = []
    items = []
    for ch in checks:
        if ch.get("source") != "tradermatrix_tape":
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
    return {"type": "tape", "name": name,
            "title": "%s — signal by signal" % name,
            "sub": "Every logged tape signal of this pattern, newest first. Target before invalidation wins.",
            "items": items[:100]}


def _detail_0dte(db, c, now, name):
    from .store import events
    from sqlalchemy import or_
    p = events.c.payload
    items = []
    if name == "MU scalp" or name == "Stock scalp (all)" or name.startswith("Scalp "):
        # Stock scalp paper trades (trade:mu- prefix, now multi-symbol)
        # Filter by symbol if name is "Scalp {SYM}"
        filter_sym = None
        if name.startswith("Scalp ") and name != "Stock scalp (all)":
            filter_sym = name[6:]  # Strip "Scalp " prefix
        elif name == "MU scalp":
            filter_sym = "MU"  # Legacy: MU scalp drill was MU-only
        try:
            mu_trades = db.prefix(c, "trade:mu-")
        except Exception:
            mu_trades = {}
        for k, t in mu_trades.items():
            if not isinstance(t, dict):
                continue
            if t.get("status") != "closed" and not _num(t.get("exited_at")):
                continue
            symbol = t.get("symbol") or "MU"
            if filter_sym and symbol != filter_sym:
                continue
            pnl = _num(t.get("pnl"))
            side = t.get("side") or ""
            entry = _num(t.get("entry"))
            exit_px = _num(t.get("exit"))
            exited_at = _num(t.get("exited_at")) or 0
            orows = []
            if entry:
                orows.append(["Entry", "$%.2f" % entry])
            if exit_px:
                orows.append(["Exit", "$%.2f" % exit_px])
            if pnl is not None:
                orows.append(["P&L", "$%.2f" % pnl])
            reason = t.get("exit_reason") or t.get("reason") or ""
            if reason:
                orows.append(["Exit reason", reason])
            won = pnl is not None and pnl > 0
            items.append({
                "tag": "MU", "title": "%s %s" % (symbol, side),
                "sub": _ts(exited_at), "rows": orows,
                "result": "win" if won else "loss",
                "result_cls": "pos" if won else "neg",
                "_sort": exited_at,
            })
        items.sort(key=lambda i: i.pop("_sort"), reverse=True)
        if filter_sym:
            title = f"Scalp {filter_sym} — trade by trade"
            sub = f"{filter_sym} gap fade and spike fade paper trades, newest first."
        else:
            title = "Stock scalp — trade by trade"
            sub = "Gap fade and spike fade paper trades on all symbols, newest first."
    elif name.startswith("0DTE ") and name != "0DTE scanner":
        # Per-underlying scanner drill-down (e.g., "0DTE MU", "0DTE AAPL")
        underlying = name[5:]  # Strip "0DTE " prefix
        try:
            all_trades = db.prefix(c, "trade:0dte-")
        except Exception:
            all_trades = {}
        for k, t in all_trades.items():
            if not isinstance(t, dict):
                continue
            if t.get("kind") != "scanner":
                continue
            if (t.get("underlying") or "?") != underlying:
                continue
            if t.get("status") != "closed" and not _num(t.get("exited_at")):
                continue
            pnl = _num(t.get("pnl"))
            symbol = t.get("symbol") or "?"
            entry = _num(t.get("entry"))
            exit_px = _num(t.get("exit"))
            exited_at = _num(t.get("exited_at")) or 0
            orows = []
            if symbol and symbol != "?":
                orows.append(["Contract", symbol])
            if entry:
                orows.append(["Entry", "$%.2f" % entry])
            if exit_px:
                orows.append(["Exit", "$%.2f" % exit_px])
            if pnl is not None:
                orows.append(["P&L", "$%.2f" % pnl])
            reason = t.get("exit_reason") or ""
            if reason:
                orows.append(["Exit reason", reason])
            gap = t.get("data_gap")
            if gap:
                orows.append(["Data", "⚠️ flew blind — excluded from wins/losses"])
            won = pnl is not None and pnl > 0
            items.append({
                "tag": underlying[:6], "title": "%s scanner" % underlying,
                "sub": _ts(exited_at), "rows": orows,
                "result": "⚠️ data gap" if gap else ("win" if won else "loss"),
                "result_cls": "" if gap else ("pos" if won else "neg"),
                "_sort": exited_at,
            })
        items.sort(key=lambda i: i.pop("_sort"), reverse=True)
        title = f"0DTE {underlying} — trade by trade"
        sub = f"Scanner paper trades on {underlying}, newest first."
    elif name == "SPY morning plan":
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


def _detail_swings(db, c, now, name):
    from .swing_ideas import swings
    from sqlalchemy import select
    try:
        rows = c.execute(select(swings).where(
            swings.c.created >= now - 90 * 86400,
        ).order_by(swings.c.created.desc()).limit(100)).mappings()
    except Exception:
        rows = []
    items = []
    for r in rows:
        p = r.get("payload") or {}
        underlying = r.get("underlying") or p.get("underlying") or "?"
        status = r.get("status") or "?"
        outcome = p.get("outcome") or "—"
        ret = p.get("return_pct")
        ts = _num(r.get("created")) or 0
        title_txt = "%s (%s)" % (underlying, status)
        orows = [["Status", status], ["Outcome", outcome], ["Time", _ts(ts)]]
        if ret is not None:
            orows.append(["Return", "%.1f%%" % ret])
        items.append({
            "tag": underlying[:6], "title": title_txt,
            "sub": _ts(ts), "rows": orows,
            "result": outcome, "result_cls": "win" if outcome == "win" else "loss" if outcome == "loss" else "",
        })
    title = "Swings — idea by idea"
    sub = "Every swing idea (1-21 DTE), newest first."
    return {"type": "swings", "name": name, "title": title, "sub": sub, "items": items}


def detail(db, c, now, dtype, name):
    builders = {
        "smoothers": _detail_smoothers,
        "futures": _detail_futures,
        "flow_pulse": _detail_flow,
        "tape": _detail_tape,
        "0dte": _detail_0dte,
        "swings": _detail_swings,
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


# ------------------------------------------------------------- tracking
#
# "What we're tracking" (Josh, 2026-10-25): the proving grounds behind
# the headline scoreboards — the live futures book per combo, the 0DTE
# scanner seat/shadow split, and the shadow iceberg watch. Read-only;
# every number comes from the same server-side scorecards the APIs
# serve (live_combo_scorecard, scanner_shadow, iceberg), never from a
# second, divergent aggregation.

_DETECTOR_LABELS = {
    "aoi_fade": "AOI Fade", "turtle_soup": "Turtle Soup",
    "golden_zone": "Golden Zone", "rumers_box": "Rumers Box",
}

_FAMILY_LABELS = {
    "session_sweep_reclaim": "Session sweep reclaim",
    "trend_pullback": "Trend pullback",
    "volume_breakout": "Volume breakout",
    "level_breakdown": "Level breakdown",
    "exposure_level_break": "Exposure level break",
    "flow_price_breakout": "Flow price breakout",
}

# The validated scanner seats (underlying-first validation, 2026-10-25).
# Fallback for the window before compass/scanner_shadow.py lands; once
# it has, its scorecard is the source of truth for the effective seats.
_DEFAULT_PAPER_SEATS = {"session_sweep_reclaim": ["AMD", "MU", "NFLX"]}


def _pct1(frac):
    """Scorecard fractions (0..1) -> the page's percent convention."""
    return round(100.0 * frac, 1) if frac is not None else None


def _tracking_futures_book(db, c, now):
    from . import live_combo_scorecard
    from .ict_paper import PAPER_ONLY_DETECTORS
    card = live_combo_scorecard.summarize(db, c, now)
    combos = []
    for x in card["combos"]:
        combos.append({
            "detector": x["detector"],
            "label": _DETECTOR_LABELS.get(x["detector"], x["detector"]),
            "symbol": x["symbol"],
            # Post-prune book (2026-10-24/25; Turtle Soup carved out
            # 2026-10-26; AOI Fade carved out 2026-10-26 evening —
            # live TradersPost sends paused): all four are paper-only
            # carve-outs; no detector is currently mirrored.
            # ict_paper owns that set.
            "live": x["detector"] not in PAPER_ONLY_DETECTORS,
            "trades": x["trades"], "wins": x["wins"],
            "losses": x["losses"], "win_rate": _pct1(x["win_rate"]),
            "pf": x["pf"], "net": x["net"], "kill_watch": x["kill_watch"],
        })
    return {"window_days": card["window_days"], "combos": combos,
            "kill_watch": card["kill_watch"]}


def _seated_stats(db, c, seats):
    """Paper-book stats for the seated scanner cells. Counting mirrors
    _0dte exactly: closed, non-data_gap scanner trades carrying an
    evidence snapshot (the post-2026-10-21 scorecard epoch) count; PF
    comes from the futures scorecard's aggregation so the conventions
    match across the page."""
    from .live_combo_scorecard import combo_stats
    try:
        paper = db.prefix(c, "trade:0dte-scan-")
    except Exception:
        paper = {}
    cells = {}
    for family, names in seats.items():
        for name in names:
            cells[(family, str(name).upper())] = {"booked": [], "open": 0}
    for t in paper.values():
        if not isinstance(t, dict) or t.get("kind") != "scanner":
            continue
        cell = cells.get((t.get("family"),
                          str(t.get("underlying") or "").upper()))
        if cell is None:
            continue
        if t.get("status") == "closed" or _num(t.get("exited_at")):
            if t.get("data_gap") or not isinstance(t.get("evidence"), dict):
                continue
            cell["booked"].append(t)
        else:
            cell["open"] += 1
    out = []
    for (family, name), cell in sorted(cells.items()):
        st = combo_stats(cell["booked"])
        out.append({
            "family": family,
            "family_label": _FAMILY_LABELS.get(family, family),
            "underlying": name, "trades": st["trades"], "wins": st["wins"],
            "losses": st["losses"], "win_rate": _pct1(st["win_rate"]),
            "pf": st["pf"], "net": st["net"], "open": cell["open"],
        })
    return out


def _tracking_scanner(db, c, now):
    """The seat/shadow split, readably. Wired against scanner_shadow's
    scorecard surface; while that build is in flight (module absent or
    scorecard failing) the section degrades to the validated seats and
    shadow=None — a clean empty state, never an error."""
    try:
        from . import scanner_shadow
    except Exception:
        scanner_shadow = None
    card = None
    if scanner_shadow is not None:
        try:
            from .config import Config
            cfg = Config()
        except Exception:
            cfg = None
        try:
            card = scanner_shadow.scorecard(db, c, cfg)
        except Exception:
            card = None
    if card is None:
        seats = {k: list(v) for k, v in _DEFAULT_PAPER_SEATS.items()}
        return {"available": scanner_shadow is not None,
                "paper_seats": seats, "seats_source": None,
                "promoted_families": sorted(_DEFAULT_PAPER_SEATS),
                "seated": _seated_stats(db, c, seats), "shadow": None}
    seats = card.get("paper_seats") or {}
    cells = card.get("shadow_cells") or []
    top = sorted(cells, key=lambda x: (-(x.get("scored") or 0),
                                       -(x.get("alerts") or 0)))[:8]
    shaped = []
    for x in top:
        grad = x.get("graduation") or {}
        shaped.append({
            "family": x.get("family"),
            "family_label": _FAMILY_LABELS.get(x.get("family"),
                                               x.get("family")),
            "underlying": x.get("underlying"),
            "alerts": x.get("alerts"), "open": x.get("open"),
            "scored": x.get("scored"), "data_gaps": x.get("data_gaps"),
            "win_rate": _pct1(x.get("win_rate")), "pf": x.get("pf"),
            "net": x.get("net"),
            "scored_needed": grad.get("scored_needed"),
            "pf_met": grad.get("pf_met"),
            "ready": bool(grad.get("ready_for_josh")),
        })
    ready = ["%s × %s" % (_FAMILY_LABELS.get(x.get("family"),
                                             x.get("family")),
                          x.get("underlying"))
             for x in cells
             if (x.get("graduation") or {}).get("ready_for_josh")]
    return {"available": True, "paper_seats": seats,
            "seats_source": card.get("seats_source"),
            "promoted_families": card.get("promoted_families") or [],
            "seated": _seated_stats(db, c, seats),
            "shadow": {"totals": card.get("totals") or {},
                       "cells_tracking": len(cells), "cells": shaped,
                       "ready": ready,
                       "bar": card.get("graduation_bar") or {}}}


def _tracking_iceberg(db, c, now):
    from . import iceberg
    summ = iceberg.summarize(db, c, now)
    stat = iceberg.status(db, c, now)
    diag = stat.get("diag") or {}
    latest = []
    for a in reversed(summ.get("recent") or []):
        close = ((a.get("scores") or {}).get("close") or {})
        latest.append({
            "underlying": a.get("underlying"), "symbol": a.get("symbol"),
            "type": a.get("type"), "strike": a.get("strike"),
            "expiry": a.get("expiry"), "side": a.get("side"),
            "alert_ts": a.get("alert_ts"),
            "est_contracts": a.get("est_parent_contracts"),
            "est_premium": a.get("est_parent_premium"),
            "score": a.get("score"), "status": a.get("status"),
            "close_ret_pct": close.get("contract_ret_pct"),
        })
        if len(latest) >= 5:
            break
    horizons = summ.get("horizons") or {}
    promo = summ.get("promotion") or {}
    return {
        "diag_present": bool(diag),
        "enabled": diag.get("enabled"),
        "prints_seen": diag.get("prints_seen"),
        "quotes_seen": diag.get("quotes_seen"),
        "contracts_tracked": diag.get("contracts_tracked"),
        "alerts_fired": diag.get("alerts_fired"),
        "errors": diag.get("errors"),
        "last_alert_at": diag.get("last_alert_at"),
        "alerts_total": stat.get("alerts_total"),
        "alerts_open": stat.get("alerts_open"),
        "alerts_window": summ.get("alerts"),
        "close": horizons.get("close"), "expiry": horizons.get("expiry"),
        "promotion": {
            "alerts_total": promo.get("alerts_total"),
            "scored_total": promo.get("scored_total"),
            "two_consecutive_positive":
                promo.get("two_consecutive_positive"),
            "meets_bar": promo.get("meets_bar"),
        },
        "latest": latest,
    }


def tracking(db, c, now):
    """The proving grounds behind the scoreboards (Josh, 2026-10-25).
    Each section fails independently to None so one bad store never
    blanks the page."""
    out = {}
    for key, fn in (("futures_book", _tracking_futures_book),
                    ("scanner", _tracking_scanner),
                    ("iceberg", _tracking_iceberg)):
        try:
            out[key] = fn(db, c, now)
        except Exception:
            out[key] = None
    return out


def summary(db, c, now):
    blocks = {}
    for fn in (_smoothers, _futures, _flow_pulse, _tape, _0dte, _swings):
        try:
            block, _ = fn(db, c, now)
        except Exception:
            continue
        blocks[block["key"]] = block
    out = {"asof": now, "types": blocks}
    try:
        out["tracking"] = tracking(db, c, now)
    except Exception:
        out["tracking"] = None
    return out


def calendar(db, c, now, year, month):
    days = defaultdict(lambda: defaultdict(lambda: {"pnl": 0.0, "count": 0}))
    fns = (_smoothers, _futures, _flow_pulse, _tape, _0dte, _swings)
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
