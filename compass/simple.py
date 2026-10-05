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
        won = pnl is not None and pnl > 0
        per_ticker[r.get("ticker") or "?"].append((won, pnl))
        if r.get("status") == "WIN":
            target_hits += 1
        if pnl is not None:
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
    by_day = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    open_count = 0
    for t in trades.values():
        if not isinstance(t, dict):
            continue
        if t.get("status") == "closed" or _num(t.get("exited_at")):
            pnl = _num(t.get("pnl"))
            won = pnl is not None and pnl > 0
            method = t.get("strategy") or "unknown"
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
    for method in sorted(per_method):
        name = ICT_SETUP_NAMES.get(method, method)
        drill.append(_drill(name, "Paper-traded micro futures signals.", _stats(per_method[method])))
    stats = _stats(all_trades)
    stats["open"] = open_count
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
    for direction in sorted(per_dir):
        drill.append(_drill(
            direction.capitalize() + " flow",
            "Big-money option flow betting %s." % ("up" if direction == "bullish" else "down"),
            _stats(per_dir[direction])))
    stats = _stats(all_trades)
    stats["open"] = open_count
    stats["peak_50_plus"] = peak_50
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
    for pattern in sorted(per_pattern):
        h = hit.get(pattern) or {}
        checks = h.get("checks", 0)
        drill.append(_drill(
            pattern,
            "Flash Agentic chart setups of this pattern type.",
            {"tracked": per_pattern[pattern],
             "wins": h.get("wins", 0),
             "win_rate": h.get("hit_rate"),
             "resolved": checks}))
    total_tracked = sum(per_pattern.values())
    total_wins = sum((hit.get(p) or {}).get("wins", 0) for p in per_pattern)
    total_resolved = sum((hit.get(p) or {}).get("checks", 0) for p in per_pattern)
    stats = {"tracked": total_tracked, "wins": total_wins,
             "win_rate": round(100.0 * total_wins / total_resolved, 1) if total_resolved else None,
             "resolved": total_resolved, "pnl": None}
    return (_block(
        "flash", "Flash Agentic",
        "Chart setups from the Obsidian Cipher scanner.",
        "Whenever a setup triggers intraday. Tracked silently; one summary a day.",
        "Win = price hit the target before the invalidation level, within five sessions. "
        "Scored by hit rate, not dollars — these are setups, not trades.",
        stats, drill), by_day)


# ---------------------------------------------------------------- 0dte


def _0dte(db, c, now):
    try:
        rows = db.recent(c, "alert", limit=5000)
    except Exception:
        rows = []
    plans = 0
    alerts = 0
    by_day = defaultdict(lambda: {"count": 0})
    for r in rows:
        p = r.get("payload") or {}
        pub = p.get("publication") or {}
        is_plan = r.get("source") == "spy_brief"
        is_alert = pub.get("category") == "options_0dte"
        if not (is_plan or is_alert):
            continue
        if is_plan:
            plans += 1
        else:
            alerts += 1
        d = _day(r.get("ts"))
        if d:
            by_day[d]["count"] += 1
    stats = {"tracked": plans + alerts, "plans": plans, "alerts": alerts,
             "wins": 0, "win_rate": None, "pnl": None}
    drill = [_drill("SPY morning plan",
                    "The pre-market SPY plan: bias, levels, stop, target.",
                    {"tracked": plans, "wins": 0, "win_rate": None, "pnl": None}),
             _drill("0DTE scanner",
                    "Same-day-expiry setups on the liquid watchlist.",
                    {"tracked": alerts, "wins": 0, "win_rate": None, "pnl": None})]
    return (_block(
        "0dte", "0DTE",
        "Same-day-expiry option plans and scanner alerts.",
        "The SPY plan every morning; scanner alerts intraday.",
        "Not scored yet — outcomes are not tracked for 0DTE. Counts only.",
        stats, drill), by_day)


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
