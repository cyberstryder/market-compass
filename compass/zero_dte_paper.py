"""Paper trading for 0DTE signals: 1 contract each, same-session only.

Two signal sources feed one paper book:
1. SPY morning plan — when the brief confirms CALL/PUT SETUP CONFIRMED, buy
   1 contract of the referenced 0DTE option at the ask (+1c slippage). Exit
   when SPY touches the plan's target/stop, or at the option flatten.
2. 0DTE scanner alerts — buy 1 contract of the alerted 0DTE contract at the
   ask (+1c). Exit at +50% / -50% of entry premium, or at the option flatten.

0DTE options expire the same day and equity options stop trading at 3:00pm
CT, so the flatten is OPTION_FLATTEN_MIN before the equity close (2:55pm CT
by default) — not the 3:45pm CT futures flatten. Nothing is ever held
overnight. No new entries inside the last hour of the session.

Trade keys: trade:0dte-*. Read-only aggregations live in compass/simple.py.
"""

import time

from sqlalchemy import select

from .market import day, session
from .store import events

FEE = 0.65
SLIPPAGE = 0.01
SCAN_THROTTLE = 60
ENTRY_CUTOFF_SEC = 3600  # no new 0DTE paper trades in the last hour

_p = events.c.payload


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fresh_quote(db, c, symbol, now, max_age=120):
    try:
        q = db.get(c, "quote:" + symbol) or {}
    except Exception:
        return None
    if not isinstance(q, dict):
        return None
    bid, ask = _num(q.get("bid")), _num(q.get("ask"))
    ts = _num(q.get("ts")) or 0
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    if now - ts > max_age:
        return None
    return q


def _flatten_at(now, cfg):
    try:
        hours = session(day(now))
    except Exception:
        return None
    if not hours:
        return None
    mins = getattr(cfg, "option_flatten_min", 5) or 5
    return hours[1] - mins * 60


def _open(db, c, key, rec):
    if db.get(c, key):
        return False
    db.put(c, key, rec)
    return True


def _open_spy_plans(db, c, now, flatten_at):
    """Open paper trades on newly confirmed SPY plans."""
    since = db.get(c, "zero_dte_paper:spy_ts", 0) or 0
    if not since:
        since = now  # no backfill: historical option quotes are gone
    try:
        rows = c.execute(select(events).where(
            events.c.kind == "alert",
            events.c.source == "spy_brief",
            events.c.ts > since,
        ).order_by(events.c.ts.asc()).limit(20)).mappings()
    except Exception:
        return 0
    opened = 0
    for r in rows:
        ts = _num(r.get("ts")) or 0
        db.put(c, "zero_dte_paper:spy_ts", max(ts, db.get(c, "zero_dte_paper:spy_ts", 0) or 0))
        rep = r.get("payload") or {}
        if not isinstance(rep, dict):
            continue
        decision = str(rep.get("decision") or "")
        if not decision.endswith("SETUP CONFIRMED"):
            continue
        side = "call" if decision.startswith("CALL") else "put" if decision.startswith("PUT") else None
        if not side:
            continue
        plan = (rep.get("plans") or {}).get(side) or {}
        opt = (rep.get("options") or {}).get(side) or {}
        symbol = opt.get("symbol")
        if not symbol or _num(plan.get("target")) is None or _num(plan.get("stop")) is None:
            continue
        key = "trade:0dte-spy-%s-%s" % (day(ts), side)
        if db.get(c, key):
            continue
        q = _fresh_quote(db, c, symbol, now)
        if not q:
            continue
        entry = _num(q.get("ask")) + SLIPPAGE
        rec = {
            "id": key, "kind": "spy_plan", "symbol": symbol, "underlying": "SPY",
            "side": "long", "qty": 1, "multiplier": 100,
            "entry": round(entry, 2), "entered_at": now,
            "target_spy": _num(plan.get("target")), "stop_spy": _num(plan.get("stop")),
            "decision": decision, "plan_ts": ts,
            "status": "open", "fee": FEE, "flatten_at": flatten_at,
        }
        if _open(db, c, key, rec):
            opened += 1
    return opened


def _find_0dte_contract(db, c, underlying, strike, typ, now):
    """Find today's contract for the alerted strike/type in the chain snapshot."""
    try:
        chain = db.get(c, "chain:" + underlying) or {}
    except Exception:
        return None
    contracts = chain.get("contracts") or []
    today = day(now)
    for o in contracts:
        if not isinstance(o, dict):
            continue
        if o.get("expiry") != today:
            continue
        if _num(o.get("strike")) != _num(strike):
            continue
        if str(o.get("type") or "").lower() != str(typ or "").lower():
            continue
        if o.get("symbol"):
            return o
    return None


def _open_scanner(db, c, now, flatten_at):
    """Open paper trades on new 0DTE scanner alerts."""
    since = db.get(c, "zero_dte_paper:scan_ts", 0) or 0
    if not since:
        since = now  # no backfill
    try:
        rows = c.execute(select(events).where(
            events.c.kind == "alert",
            events.c.ts > since,
            _p["publication"]["category"].as_string() == "options_0dte",
        ).order_by(events.c.ts.asc()).limit(50)).mappings()
    except Exception:
        return 0
    opened = 0
    for r in rows:
        ts = _num(r.get("ts")) or 0
        db.put(c, "zero_dte_paper:scan_ts", max(ts, db.get(c, "zero_dte_paper:scan_ts", 0) or 0))
        pub = (r.get("payload") or {}).get("publication") or {}
        contract = pub.get("contract") or {}
        underlying = contract.get("underlying")
        strike, typ = contract.get("strike"), contract.get("type")
        if not underlying or _num(strike) is None or not typ:
            continue
        key = "trade:0dte-scan-%s" % (r.get("key") or int(ts))
        if db.get(c, key):
            continue
        o = _find_0dte_contract(db, c, underlying, strike, typ, now)
        if not o:
            continue
        q = _fresh_quote(db, c, o["symbol"], now)
        if not q:
            continue
        entry = _num(q.get("ask")) + SLIPPAGE
        rec = {
            "id": key, "kind": "scanner", "symbol": o["symbol"],
            "underlying": underlying, "side": "long", "qty": 1, "multiplier": 100,
            "entry": round(entry, 2), "entered_at": now,
            "exit_up_pct": 50.0, "exit_down_pct": -50.0,
            "alert_key": r.get("key"), "alert_event": pub.get("status") or pub.get("event"),
            "status": "open", "fee": FEE, "flatten_at": flatten_at,
        }
        if _open(db, c, key, rec):
            opened += 1
    return opened


def _spy_touched(db, c, trade, now):
    """Check minute bars since entry: did SPY touch target or stop first?"""
    target = _num(trade.get("target_spy"))
    stop = _num(trade.get("stop_spy"))
    if target is None or stop is None:
        return None
    side = "call" if "CALL" in str(trade.get("decision") or "") else "put"
    try:
        raw = db.get(c, "bar_window:SPY", []) or []
    except Exception:
        return None
    entered = _num(trade.get("entered_at")) or 0
    for b in raw:
        if not isinstance(b, (list, tuple)) or len(b) < 6 or b[0] < entered:
            continue
        h, l = _num(b[2]), _num(b[3])
        if h is None or l is None:
            continue
        if side == "call":
            if h >= target:
                return "target"
            if l <= stop:
                return "stop"
        else:
            if l <= target:
                return "target"
            if h >= stop:
                return "stop"
    return None


def _update_open(db, c, now):
    try:
        trades = db.prefix(c, "trade:0dte-")
    except Exception:
        return 0
    closed = 0
    for key, t in trades.items():
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        reason = None
        flatten_at = _num(t.get("flatten_at"))
        if flatten_at and now >= flatten_at:
            reason = "session flatten"
        elif t.get("kind") == "spy_plan":
            hit = _spy_touched(db, c, t, now)
            if hit == "target":
                reason = "target hit"
            elif hit == "stop":
                reason = "stopped"
        else:
            q = _fresh_quote(db, c, t.get("symbol"), now, max_age=300)
            bid = _num((q or {}).get("bid"))
            entry = _num(t.get("entry"))
            if bid and entry:
                if bid >= entry * 1.5:
                    reason = "target hit (+50%)"
                elif bid <= entry * 0.5:
                    reason = "stopped (-50%)"
        if not reason:
            continue
        q = _fresh_quote(db, c, t.get("symbol"), now, max_age=300) or {}
        exit_px = (_num(q.get("bid")) or 0) - SLIPPAGE
        if exit_px <= 0:
            continue  # no quote: stay open, retry next scan
        entry = _num(t.get("entry")) or 0
        pnl = round((exit_px - entry) * 100 - 2 * FEE, 2)
        t.update(status="closed", exit=round(exit_px, 2), exited_at=now,
                 exit_reason=reason, pnl=pnl)
        db.put(c, key, t)
        closed += 1
    return closed


def scan(db, c, cfg, now=None):
    """Throttled pass: open new 0DTE paper trades, update open ones."""
    now = now or time.time()
    if not getattr(cfg, "zero_dte_paper", True):
        return {"ran": False, "reason": "disabled"}
    if now - db.get(c, "zero_dte_paper:scanned_at", 0) < SCAN_THROTTLE:
        return {"ran": False, "reason": "throttled"}
    db.put(c, "zero_dte_paper:scanned_at", now)
    flatten_at = _flatten_at(now, cfg)
    if not flatten_at:
        _update_open(db, c, now)
        return {"ran": True, "reason": "no session"}
    opened = 0
    if now < flatten_at - ENTRY_CUTOFF_SEC:
        try:
            opened += _open_spy_plans(db, c, now, flatten_at)
        except Exception:
            pass
        try:
            opened += _open_scanner(db, c, now, flatten_at)
        except Exception:
            pass
    try:
        closed = _update_open(db, c, now)
    except Exception:
        closed = 0
    return {"ran": True, "opened": opened, "closed": closed}
