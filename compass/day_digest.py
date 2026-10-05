"""End-of-day digest: one summary per signal type for the trading day.

Assembles, for the America/Chicago calendar date:
- Smoothers entries/exits (native outbox)
- Futures ICT paper trades opened/closed today, realized day P&L, open positions
- Flow Pulse pulses fired today + running paper track record
- Flash Agentic setups logged today, grouped by pattern, + running by-pattern hit rates
- 0DTE: SPY morning plan + options_0dte scanner alerts

Read-only. A weekday-evening cron delivers this in chat; per-alert chat noise
stays off. Built 2026-10-05 at Josh's direction: one end-of-day summary per
type of trade instead of realtime alerts.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .alert_format import FUTURE_NAMES, ICT_SETUP_NAMES
from .native_outbox import outbox as native_outbox

CT = ZoneInfo("America/Chicago")


def _bounds(now):
    d = datetime.fromtimestamp(now, CT).date()
    start = datetime(d.year, d.month, d.day, tzinfo=CT).timestamp()
    return d.isoformat(), start


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _f(x):
    v = _num(x)
    return "n/a" if v is None else ("%g" % v)


def _money(x):
    v = _num(x)
    if v is None:
        return "n/a"
    return "%s$%s" % ("-" if v < 0 else "", "{:,.2f}".format(abs(v)))


def _big(x):
    v = _num(x)
    if v is None:
        return "n/a"
    if v >= 1e6:
        return "$%.1fM" % (v / 1e6)
    if v >= 1e3:
        return "$%.0fk" % (v / 1e3)
    return "$%g" % v


def _root(sym):
    return str(sym or "").split(".")[0].split("@")[0]


def _future_name(sym):
    root = _root(sym)
    return "%s · %s" % (root, FUTURE_NAMES[root]) if root in FUTURE_NAMES else root


def _section(key, title, lines):
    return {"key": key, "title": title, "lines": lines}


# ---------------------------------------------------------------- smoothers


def _smoothers(db, c, start):
    entries, exits = [], []
    try:
        rows = c.execute(
            select(native_outbox)
            .where(native_outbox.c.program == "smoothers",
                   native_outbox.c.created >= start)
            .order_by(native_outbox.c.created)
        ).mappings()
    except Exception:
        return _section("smoothers", "Smoothers", [])
    for r in rows:
        delivery = r.get("delivery") or {}
        pub = delivery.get("publication") or delivery.get("publication_input") or {}
        contract = pub.get("contract") or {}
        und = contract.get("underlying")
        if not und:
            continue
        head = "%s %s %s · %s" % (
            und, _f(contract.get("strike")), contract.get("type") or "",
            contract.get("expiration") or "?")
        ek = str(r.get("event_key") or "")
        is_exit = ek.endswith(":target") or ek.endswith(":close") or bool(pub.get("exit_reason"))
        quote = pub.get("quote") or {}
        if is_exit:
            reason = pub.get("exit_reason") or ""
            what = "Target hit" if reason == "target" else "Week closed"
            exits.append("%s — %s · opt %s/%s" % (head, what, _f(quote.get("bid")), _f(quote.get("ask"))))
        else:
            entries.append("%s — stock %s→%s · opt %s/%s" % (
                head, _f(pub.get("entry_price")), _f(pub.get("target_price")),
                _f(quote.get("bid")), _f(quote.get("ask"))))
    lines = []
    if entries:
        lines.append("Entries (%d):" % len(entries))
        lines.extend("  " + e for e in entries)
    if exits:
        lines.append("Exits (%d):" % len(exits))
        lines.extend("  " + e for e in exits)
    return _section("smoothers", "Smoothers", lines)


# ---------------------------------------------------------------- futures


_EXIT_WORD = {"stop": "stopped out", "target": "target hit", "flatten": "session flatten",
              "no-data": "no exit data"}


def _futures(db, c, start):
    opened, closed, open_now = [], [], []
    day_pnl = 0.0
    try:
        trades = db.prefix(c, "trade:ict-")
    except Exception:
        return _section("futures", "Futures (paper)", [])
    for t in trades.values():
        if not isinstance(t, dict):
            continue
        exited_at = _num(t.get("exited_at")) or 0
        entered_at = _num(t.get("entered_at")) or 0
        if exited_at and exited_at >= start:
            closed.append(t)
            day_pnl += _num(t.get("pnl")) or 0.0
        elif not exited_at:
            if entered_at >= start:
                opened.append(t)
            open_now.append(t)

    def head(t):
        direction = str(t.get("direction") or t.get("side") or "").upper()
        qty = _num(t.get("qty")) or 1
        setup = ICT_SETUP_NAMES.get(t.get("strategy") or "", t.get("strategy") or "")
        return "%s %s ×%g — %s" % (_future_name(t.get("symbol")), direction, qty, setup)

    lines = []
    for t in sorted(opened, key=lambda x: x.get("entered_at") or 0):
        lines.append("%s · entry %s · stop %s · target %s" % (
            head(t), _f(t.get("entry")), _f(t.get("stop")), _f(t.get("target"))))
    for t in sorted(closed, key=lambda x: x.get("exited_at") or 0):
        reason = _EXIT_WORD.get(str(t.get("exit_reason") or ""), str(t.get("exit_reason") or "closed"))
        lines.append("%s · %s → %s · %s (%s)" % (
            head(t), _f(t.get("entry")), _f(t.get("exit")), _money(t.get("pnl")), reason))
    if closed:
        lines.append("Realized today: %s across %d closed" % (_money(day_pnl), len(closed)))
    still = [t for t in open_now if (_num(t.get("entered_at")) or 0) < start]
    if still:
        lines.append("Still open (%d):" % len(still))
        for t in still:
            lines.append("  %s · entry %s · stop %s · target %s" % (
                head(t), _f(t.get("entry")), _f(t.get("stop")), _f(t.get("target"))))
    return _section("futures", "Futures (paper)", lines)


# ---------------------------------------------------------------- flow pulse


def _flow_pulse(db, c, start):
    lines = []
    try:
        rows = db.recent(c, "flow_pulse", limit=500, since=start)
    except Exception:
        rows = []
    for r in rows:
        p = r.get("payload") or {}
        sc = p.get("suggested_contract") or {}
        sug = ""
        if sc.get("strike"):
            sug = " · suggested %s %s %s" % (_f(sc.get("strike")), sc.get("type") or "", sc.get("expiration") or "")
        premium = _big(p.get("directional_premium", p.get("premium")))
        lines.append("%s %s — %s directional premium%s" % (
            p.get("symbol") or "?", str(p.get("direction") or "").upper(), premium, sug))
    try:
        from .flow_pulse import track_record
        rec = track_record(db, c)
        closed = rec.get("closed") or {}
        n = closed.get("count", 0)
        if n:
            lines.append("Paper record: %d closed · %d wins (%s) · avg %s · %s total" % (
                n, closed.get("wins", 0), closed.get("win_rate", "n/a"),
                closed.get("avg_return", "n/a"), _money(closed.get("total_pnl"))))
        elif rec.get("open"):
            lines.append("Paper record: %d open track(s), none closed yet" % len(rec["open"]))
    except Exception:
        pass
    return _section("flow_pulse", "Flow Pulse", lines)


# ---------------------------------------------------------------- flash agentic


def _flash(db, c, start, now):
    lines = []
    try:
        rows = db.recent(c, "pick_check", limit=500, since=start)
    except Exception:
        rows = []
    by_pat = {}
    for r in rows:
        p = r.get("payload") or {}
        if p.get("source") != "flash_agentic":
            continue
        by_pat.setdefault(p.get("pattern") or "unknown", []).append(p)
    for pat in sorted(by_pat):
        items = by_pat[pat]
        parts = []
        for p in items:
            parts.append("%s %s · score %s · %s→%s · inval %s · ev %s" % (
                p.get("ticker") or "?", p.get("direction") or "?",
                _f(p.get("setup_score")), _f(p.get("entry")), _f(p.get("target")),
                _f(p.get("invalidation")), _f(p.get("evidence_score"))))
        lines.append("%s (%d): %s" % (pat, len(items), " | ".join(parts)))
    try:
        from .pick_scorecard import scorecard
        sc = scorecard(db, c, now, limit=200)
        for row in sc.get("by_pattern") or []:
            if row.get("source") != "flash_agentic":
                continue
            lines.append("Running %s: %d tracked · %dW-%dL · %s" % (
                row.get("pattern"), row.get("checks", 0), row.get("wins", 0),
                row.get("losses", 0), row.get("hit_rate", "n/a")))
    except Exception:
        pass
    return _section("flash_agentic", "Flash Agentic", lines)


# ---------------------------------------------------------------- 0dte


def _spy_0dte(db, c, start):
    lines = []
    try:
        rows = db.recent(c, "alert", limit=500, since=start)
    except Exception:
        rows = []
    for r in rows:
        if r.get("source") == "spy_brief":
            rep = r.get("payload") or {}
            decision = rep.get("decision") or rep.get("bias")
            lines.append("SPY 0DTE morning plan — %s" % (decision if decision else "published"))
        pub = (r.get("payload") or {}).get("publication") or {}
        if pub.get("category") == "options_0dte":
            contract = pub.get("contract") or {}
            lines.append("0DTE alert: %s %s %s · %s" % (
                contract.get("underlying") or "?", _f(contract.get("strike")),
                contract.get("type") or "", pub.get("status") or pub.get("event") or ""))
    return _section("0dte", "0DTE", lines)


# ---------------------------------------------------------------- assemble


def assemble(db, c, now):
    date, start = _bounds(now)
    return {
        "asof": now,
        "date": date,
        "sections": [
            _smoothers(db, c, start),
            _futures(db, c, start),
            _flow_pulse(db, c, start),
            _flash(db, c, start, now),
            _spy_0dte(db, c, start),
        ],
    }
