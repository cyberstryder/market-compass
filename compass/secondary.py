"""Independent, versioned source-signal reviews and paired observations.

This worker only reads originals. It owns no positions, source filters or broker
route. A review is frozen before its alert; later source results cannot rescore it.
"""
import asyncio
import math
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import Column, Float, Index, Integer, JSON, String, Table, and_, delete, func, or_, select, update

from .market import day, fresh, is_open, session, number
from .futures import futures_session, active_selection
from .instruments import ROOTS, INDEX_FUTURES
from .scanner import ema, features, bars_from_window
from .store import events, identity, meta
from .projects import records as originals
from .quote_path import recorded_path
from .vendor_freshness import confirmation
from .flow_recovery import freshness as flow_freshness

VERSION = "secondary-context-v3"
LABELS = {"morning": "Morning Algo", "smoothers": "Smoothers",
          "futures": "TradingView futures", "compass_futures": "Compass futures"}
VERDICTS = ("supported", "watch", "rejected", "insufficient_data")
ROOT = re.compile(r"^("+ROOTS+r")(?:[12]!|[FGHJKMNQUVXZ]\d{1,4})(?:@\d+)?$")
reviews = Table("secondary_reviews_v1", meta,
    Column("id", String(64), primary_key=True),
    Column("project", String(30), nullable=False), Column("source_key", String(64), nullable=False),
    Column("source_id", String(1000), nullable=False), Column("symbol", String(100), nullable=False),
    Column("side", String(16), nullable=False), Column("source_ts", Float, nullable=False),
    Column("decided_at", Float, nullable=False), Column("version", String(50), nullable=False),
    Column("verdict", String(24), nullable=False), Column("tracking", String(20), nullable=False),
    Column("candidate", JSON, nullable=False), Column("inputs", JSON, nullable=False),
    Column("decision", JSON, nullable=False), Column("measurements", JSON, nullable=False),
    Column("source_outcome", JSON, nullable=False), Column("updated", Float, nullable=False))
Index("secondary_review_time", reviews.c.decided_at)
Index("secondary_tracking", reviews.c.tracking, reviews.c.decided_at)
handled = Table("secondary_events_v1", meta, Column("event_id", Integer, primary_key=True),
    Column("handled_at", Float, nullable=False))
pending = Table("secondary_pending_v1", meta,
    Column("id", String(64), primary_key=True), Column("project", String(30), nullable=False),
    Column("source_key", String(64), nullable=False), Column("deadline", Float, nullable=False),
    Column("first_seen", Float, nullable=False), Column("last_check", Float, nullable=False),
    Column("attempts", Integer, nullable=False), Column("candidate", JSON, nullable=False),
    Column("outcome", JSON, nullable=False), Column("missing", JSON, nullable=False))
Index("secondary_pending_deadline", pending.c.deadline)


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def current(stamp, now, seconds):
    return numeric(stamp) and 0 <= now - stamp <= seconds


def future_root(symbol):
    match = ROOT.fullmatch(symbol.split(":")[-1])
    return match.group(1) if match else None


def contract_code(symbol, now):
    value = symbol.split(":")[-1].split("@")[0]
    match = re.fullmatch(r"("+ROOTS+r")([FGHJKMNQUVXZ])(\d{1,4})", value)
    if not match:
        return None
    digits = match.group(3)
    year = int(digits)
    if len(digits) == 1:
        current_year = datetime.fromtimestamp(now, timezone.utc).year
        year += current_year // 10 * 10
        if year < current_year - 5:
            year += 10
        elif year > current_year + 5:
            year -= 10
    elif len(digits) == 2:
        year += 2000
    return match.group(1), match.group(2), year


def resolve_symbol(db, c, candidate, cfg, now):
    symbol = candidate["symbol"].split(":")[-1]
    root = future_root(symbol)
    if not root:
        return symbol, "same_underlying"
    quotes = db.prefix(c, "quote:" + root)
    if "@" in symbol and "quote:" + symbol in quotes:
        return symbol, "same_contract"
    if contract_code(symbol, now):
        matches = [key[6:] for key in quotes if "@" in key and contract_code(key[6:], now) == contract_code(symbol, now)]
        return (sorted(matches)[0], "same_contract") if matches else (None, "missing_contract")
    active = {r["raw_symbol"] for r in active_selection(db, c, cfg, now) if r["root"] == root}
    matches = [key[6:] for key in quotes if "@" in key and key[6:].split("@")[0] in active]
    # Continuous source levels are never compared directly to dated prices.
    return (sorted(matches)[0], "linked_contract_context") if matches else (None, "missing_contract")


def daily_context(db, c, symbol, now):
    from datetime import date, timedelta
    from .swing_signals import sessions_between
    today = date.fromisoformat(day(now))
    expected = sessions_between((today-timedelta(days=160)).isoformat(),
                                (today-timedelta(days=1)).isoformat())
    raw = db.recent(c, "daily", symbol, limit=160)
    unique = {}
    for row in reversed(raw):
        p = row["payload"]
        values = [number(p.get(k)) for k in ("o", "h", "l", "c", "v")]
        valid = (all(v is not None for v in values) and min(values[:4]) > 0 and values[4] >= 0
                 and values[1] >= max(values[0], values[2], values[3]) and values[2] <= min(values[0], values[3]))
        if valid and row["ts"] < now and day(row["ts"]) in expected:
            unique[day(row["ts"])] = row
    if len(expected) < 50 or any(d not in unique for d in expected[-50:]):
        return {"status": "missing", "note": "Fifty consecutive completed daily sessions required for swing bias"}
    dates = []
    for d in reversed(expected):
        if d not in unique:
            break
        dates.insert(0, d)
    rows = [unique[d] for d in dates]
    closes = [r["payload"]["c"] for r in rows]
    fast, slow = ema(closes, 21)[-1], ema(closes, 50)[-1]
    return {"status": "ready", "source_ts": rows[-1]["ts"], "received": rows[-1].get('received'), "through": dates[-1], "bars": len(rows), "ema21": fast, "ema50": slow,
            "bias": 1 if fast > slow else -1 if fast < slow else 0}


def technical_ready(feature, now):
    return (feature.get("status") == "ready" and current(feature.get("asof"), now, 90)
            and numeric(feature.get("atr14")) and feature["atr14"] > 0)


def technical_context(db, c, symbol, now):
    feature = db.get(c, "scanner_features:" + symbol, {})
    if technical_ready(feature, now) or future_root(symbol):
        return feature
    # Connected equities can be outside the scanner universe, and a corrected
    # bar window can be newer than its scanner cache. Apply the same bar rules.
    rows = bars_from_window(db.get(c, "bar_window:" + symbol, []))
    if not rows:
        rows = db.recent(c, "bar", symbol, limit=1800)
    return features(symbol, rows, now, feature)


def capture(db, c, candidate, cfg, now):
    symbol, basis = resolve_symbol(db, c, candidate, cfg, now)
    root = future_root(candidate["symbol"])
    quote = db.get(c, "quote:" + symbol) if symbol else None
    feature = technical_context(db, c, symbol, now) if symbol else {}
    technical = {k: feature.get(k) for k in ("status", "reason", "asof", "atr14", "vwap", "ema9", "ema21",
        "htf15_bias", "rvol20", "prior_high", "prior_low", "or_high", "or_low")}
    exposure_symbol = {"MES": "SPY", "ES": "SPY", "MNQ": "QQQ", "NQ": "QQQ"}.get(root)
    if not root:
        exposure_symbol = symbol
    matrix = db.get(c, "matrix:" + exposure_symbol, {}) if exposure_symbol else {}
    concentrations = sorted(matrix.get("strikes", []), key=lambda r: abs(r.get("gex") or 0), reverse=True)[:5]
    vex = sorted(matrix.get("strikes", []), key=lambda r: abs(r.get("vex") or 0), reverse=True)[:5]
    equity_open = is_open(now)
    matrix_check = confirmation(matrix,now)
    matrix_live = bool(equity_open and matrix_check['eligible_for_live_confirmation'])
    flow_summary = db.get(c, "matrix:unusual_activity", {})
    flow_check = flow_freshness(flow_summary,now)
    flow = flow_summary.get("rows", []) if flow_check['eligible_for_live_confirmation'] else []
    recent = [r for r in flow if r.get("symbol") == exposure_symbol
        and current(r.get("source_ts"), now, 120) and (r.get("score") or 0) >= 85
        and (r.get("premium") or 0) >= 100000] if equity_open and exposure_symbol else []
    seen = set()
    flow_rows = []
    for row in sorted(recent, key=lambda r: r["source_ts"], reverse=True):
        key = row.get("vendor_id") or identity(row)
        if key not in seen:
            seen.add(key)
            flow_rows.append({k: row.get(k) for k in ("vendor_id", "symbol", "source_ts", "score", "premium", "sentiment")})
    peers = []
    for peer in (("SPY", "QQQ") if root is None or root in INDEX_FUTURES else ()):
        if peer == symbol:
            continue
        f, q = db.get(c, "scanner_features:" + peer, {}), db.get(c, "quote:" + peer)
        if equity_open and f.get("status") == "ready" and current(f.get("asof"), now, 90) and fresh(q, now):
            peers.append({"symbol": peer, "bias": f.get("htf15_bias"), "source_ts": f["asof"]})
    levels = []
    if matrix_live and not root:
        for prefix in ("apex:", "matrix_levels:"):
            obj = db.get(c, prefix + symbol, {})
            if confirmation(obj,now)['eligible_for_live_confirmation']:
                levels.extend({"price": r.get("price"), "kind": r.get("kind"), "source_ts": obj["source_ts"]}
                    for r in obj.get("levels", []) if numeric(r.get("known_at", obj.get("received")))
                    and r.get("known_at", obj.get("received")) <= now)
    return {"captured_at": now, "market_symbol": symbol, "price_basis": basis, "quote": quote,
        "technical": technical, "daily": daily_context(db, c, symbol, now) if candidate["project"] == "smoothers" and symbol else {},
        "peers": peers, "flow": flow_rows[:8], "levels": levels[:12],
        "vendor_checks": {"matrix":matrix_check,"flow":flow_check},
        "exposure": {"symbol": exposure_symbol, "relationship": "cross_asset_context" if root else "same_underlying",
            "status": "current" if matrix_live else "cash_market_closed" if not equity_open else "missing_or_stale",
            "source_ts": matrix.get("source_ts"), "received": matrix.get("received"),
            "gex": [{k: r.get(k) for k in ("strike", "gex")} for r in concentrations],
            "vex": [{k: r.get(k) for k in ("strike", "vex")} for r in vex],
            "note": "Concentrations are context; signs do not establish dealer inventory or direction."}}


def assess(candidate, inputs, now):
    """Transparent frozen rules. Supported is not a probability or an order."""
    missing, reject, caution, support = [], [], [], []
    side = 1 if candidate.get("side") == "long" else -1
    available_at = candidate.get("available_at", candidate.get("source_ts"))
    timely = current(available_at, now, 60) and numeric(candidate.get("source_ts")) and candidate["source_ts"] <= now
    if not timely:
        missing.append("Source arrived outside the 60-second review window; no timely decision")
    if candidate.get("side") not in ("long", "short"):
        missing.append("Source direction is unavailable")
    q, f = inputs.get("quote"), inputs.get("technical", {})
    quote_ok = bool(fresh(q, now) and current(q.get("ts"), now, 5))
    if not quote_ok:
        missing.append("Independent bid/ask quote is missing or older than five seconds")
    feature_ok = technical_ready(f, now)
    if not feature_ok:
        missing.append("Completed price context is missing or stale")
    comparable = inputs.get("price_basis") in ("same_underlying", "same_contract")
    mid = (q["bid"] + q["ask"]) / 2 if quote_ok else None
    root = future_root(candidate["symbol"])
    if quote_ok:
        spread_limit = max((.25 if root else .01) * 8, mid * .002)
        if q["ask"] - q["bid"] > spread_limit:
            reject.append("Underlying spread exceeds the review limit")
        if comparable:
            stop, target = candidate.get("stop"), candidate.get("target")
            if numeric(stop) and side * (mid - stop) <= 0:
                reject.append("Original invalidation already reached")
            if numeric(target) and side * (target - mid) <= 0:
                reject.append("Original target already reached; late entry")
            entry = candidate.get("entry")
            if feature_ok and numeric(entry) and candidate["project"] != "smoothers" and abs(mid - entry) > .5 * f["atr14"]:
                reject.append("Price moved more than half an ATR from the source reference")
        else:
            caution.append("Continuous-chart levels cannot validate the separate dated contract's stop or target")
    if root and not futures_session(now,candidate["symbol"])["entry_open"]:
        reject.append("Outside the current futures research entry window")
    if not root and not is_open(now):
        reject.append("Cash market is closed")
    if candidate.get("resolved_at_review"):
        reject.append("Source signal was already resolved when reviewed")
    if candidate["project"] == "smoothers":
        daily = inputs.get("daily", {})
        if daily.get("status") != "ready" or daily.get("bias") not in (-1, 0, 1):
            missing.append("Completed daily price history is unavailable; direction cannot be compared")
        elif daily.get("bias") == side:
            support.append("Completed daily EMA21/50 bias agrees with the weekly direction")
        elif daily.get("bias") == -side:
            caution.append("Completed daily EMA21/50 trend opposes the weekly direction")
        else:
            caution.append("Completed daily EMA21/50 trend is neutral")
        difficulty = candidate.get("target_atr_mult")
        if numeric(difficulty) and 0 < difficulty <= .75:
            support.append("Source target is within 0.75 of its recorded daily ATR")
        else:
            caution.append("Source target/ATR evidence is missing or above the study ceiling")
    elif feature_ok:
        if f.get("htf15_bias") == side:
            support.append("Completed 15-minute trend agrees")
        else:
            caution.append("Completed 15-minute trend is opposing, neutral or unavailable")
    if feature_ok and quote_ok:
        vwap = f.get("vwap")
        if numeric(vwap) and side * (mid - vwap) > 0:
            support.append("Price is on the supporting side of session VWAP")
        else:
            caution.append("Session VWAP does not confirm the direction")
        e9, e21 = f.get("ema9"), f.get("ema21")
        if numeric(e9) and numeric(e21) and side * (e9 - e21) > 0:
            support.append("Completed EMA9/21 structure agrees")
        else:
            caution.append("Fast price structure does not confirm the direction")
        for level in inputs.get("levels", []):
            value = level.get("price")
            if numeric(value) and 0 < side * (value - mid) < .35 * f["atr14"]:
                caution.append("A fresh exposure concentration is less than 0.35 ATR ahead; reaction is uncertain")
                break
    peer_bias = [p.get("bias") for p in inputs.get("peers", []) if p.get("bias") in (-1, 1)]
    if peer_bias and all(b == -side for b in peer_bias):
        caution.append("Available broad-market context opposes the direction")
    flow_sides = {str(r.get("sentiment") or "").lower() for r in inputs.get("flow", [])}
    opposing = "bearish" if side == 1 else "bullish"
    agreeing = "bullish" if side == 1 else "bearish"
    if opposing in flow_sides:
        caution.append("Recent high-score vendor flow contains opposing sentiment")
    elif agreeing in flow_sides:
        support.append("Recent high-score vendor flow agrees; intent remains vendor-classified")
    # Optional flow/exposure may be unavailable, especially during futures nights.
    optional = []
    if inputs.get("exposure", {}).get("status") != "current":
        optional.append("GEX/VEX unavailable as current confirmation")
    if not inputs.get("flow"):
        optional.append("No qualifying fresh unusual-flow observation")
    # A continuous contract's unmatched absolute levels are a limitation, not a
    # directional veto. Other cautions qualify the idea only as WATCH.
    directional_cautions = [r for r in caution if not r.startswith("Continuous-chart")]
    verdict = "insufficient_data" if missing else "rejected" if reject else "watch" if directional_cautions or len(support) < 3 else "supported"
    reasons = missing or reject or directional_cautions or support
    return {"verdict": verdict, "reasons": reasons, "support": support, "cautions": caution,
        "missing": missing, "rejections": reject, "optional_missing": optional, "timely": timely,
        "accepted": verdict == "supported", "rule_version": VERSION,
        "scope": "Underlying-direction review; option profitability and broker execution are unverified",
        "price_basis": inputs.get("price_basis"), "latency_seconds": round(now - available_at, 3),
        "source_reference_age_seconds": round(now - candidate["source_ts"], 3),
        "data_checks": {"quote": {"ready": quote_ok, "source_ts": (q or {}).get("ts")},
            "minute": {"ready": feature_ok, "source_ts": f.get("asof"), "reason": f.get("reason")},
            "daily": inputs.get("daily", {})}}


def start_measurement(candidate, inputs, decision, now):
    q = inputs.get("quote")
    valid = decision["timely"] and fresh(q, now) and current(q.get("ts"), now, 5)
    result = {"basis": "Direction-adjusted underlying midpoint change from secondary decision; not trade P&L",
        "state": "tracking" if valid else "unmeasurable", "horizons": {}, "samples": 0,
        "source_price_comparable": inputs.get("price_basis") in ("same_contract", "same_underlying")}
    if not valid:
        return result
    mid = (q["bid"] + q["ask"]) / 2
    hours = futures_session(now,candidate["symbol"]) if future_root(candidate["symbol"]) else None
    cash = session(day(now)) if not hours else None
    deadline = hours["flatten_at"] if hours else cash[1] if cash else now
    result.update(anchor=mid, anchor_ts=q["ts"], anchor_bid=q["bid"], anchor_ask=q["ask"],
        market_symbol=inputs["market_symbol"], max_price=mid, min_price=mid,
        last_quote_ts=q["ts"], last_sample_at=now, max_gap_seconds=0, samples=1,
        checkpoint_basis="First usable retained quote at/after each deadline, received within 30 seconds; live fallback if available; missing windows stay missing")
    for minutes in (15, 30, 60):
        due = now + minutes * 60
        result["horizons"][str(minutes)] = {"due": due,
            "status": "pending" if due < deadline else "session_boundary"}
    result["sampling_deadline"] = min(deadline, max((p["due"] + 30 for p in result["horizons"].values()
        if p["status"] == "pending"), default=now))
    if not any(x["status"] == "pending" for x in result["horizons"].values()):
        result["state"] = "complete"
    return result


def advance_measurement(measurement, side, quote, now):
    m = {**measurement, "horizons": {k: dict(v) for k, v in measurement.get("horizons", {}).items()}}
    if m["state"] != "tracking":
        return m
    usable = bool(fresh(quote, now) and current(quote.get("ts"), now, 5) and quote["ts"] > m["last_quote_ts"]
                  and now <= m["sampling_deadline"] and quote["ts"] <= m["sampling_deadline"])
    if usable:
        mid = (quote["bid"] + quote["ask"]) / 2
        m.update(max_gap_seconds=max(m["max_gap_seconds"], now - m["last_sample_at"]),
            max_price=max(m["max_price"], mid), min_price=min(m["min_price"], mid),
            last_quote_ts=quote["ts"], last_sample_at=now, samples=m["samples"] + 1)
    sign = 1 if side == "long" else -1
    for point in m["horizons"].values():
        if point["status"] != "pending":
            continue
        if usable and point["due"] <= quote["ts"] <= point["due"] + 30 and now <= point["due"] + 30:
            point.update(status="observed", source_ts=quote["ts"], captured_at=now, price=mid,
                observation_source="live_quote", return_pct=sign * (mid / m["anchor"] - 1) * 100)
        elif now > point["due"] + 30:
            point.update(status="missing", reason="No fresh quote captured in the checkpoint window")
    m["sampled_mfe_pct"] = ((m["max_price"] / m["anchor"] - 1) if sign == 1 else (1 - m["min_price"] / m["anchor"])) * 100
    m["sampled_mae_pct"] = ((m["min_price"] / m["anchor"] - 1) if sign == 1 else (1 - m["max_price"] / m["anchor"])) * 100
    if not any(p["status"] == "pending" for p in m["horizons"].values()):
        m["state"] = "complete"
    return m


def advance_recorded_measurement(db, c, measurement, side, quote, now):
    """Recover pending checkpoint windows without rewriting frozen decisions.

    Query each 30-second window directly, so an engine restart never scans a
    whole hour of quotes or lets a path batch limit hide a later checkpoint.
    Extrema remain engine-sampled; checkpoint recovery does not reconstruct MFE.
    """
    m = {**measurement, "horizons": {k: dict(v) for k, v in measurement.get("horizons", {}).items()}}
    if m["state"] != "tracking":
        return m
    for point in m["horizons"].values():
        if point["status"] != "pending" or now < point["due"]:
            continue
        until = min(now, point["due"] + 30, m["sampling_deadline"])
        path, truncated, check = recorded_path(db, c, m["market_symbol"],
            max(m["anchor_ts"], math.nextafter(point["due"], -math.inf)), until)
        point["archive_check"] = {**check, "truncated": truncated}
        if path:
            q = path[0]
            mid = (q["bid"] + q["ask"]) / 2
            point.update(status="observed", source_ts=q["ts"], captured_at=q["recorded_at"],
                recovered_at=now, observation_source="retained_quote", price=mid,
                return_pct=(1 if side == "long" else -1) * (mid / m["anchor"] - 1) * 100)
        elif truncated:
            # A bounded read of invalid rows is not proof that the rest is empty.
            point.update(status="missing", reason="Checkpoint archive scan limit reached; coverage unknown")
        elif now > point["due"] + 30:
            point.update(status="missing", reason="No usable retained quote received in the checkpoint window")
    return advance_measurement(m, side, quote, now)


def source_result(payload, project):
    result = {"status": payload.get("status"), "outcome": payload.get("outcome"),
        "option": payload.get("option"), "checkpoints": payload.get("checkpoints", []),
        "option_samples": payload.get("option_samples", []),
        "basis": "Original source observations, separate from secondary midpoint measurements"}
    if project == "compass_futures":
        result = {"status": payload.get("status"), "paper_pnl": payload.get("pnl"),
            "basis": "Original Compass paper result; not actual broker P&L"}
    return result


def source_candidate(project, payload, key):
    original = payload.get("original") or {}
    available_at = payload["source_ts"]
    if project == "smoothers" and payload.get("source_time_basis") == "model_entry_time":
        try:
            created = datetime.fromisoformat(str(original.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is not None and created.timestamp() >= payload["source_ts"]:
                available_at = created.timestamp()
        except ValueError:
            pass
    return {"project": project, "source_key": key, "source_id": payload["id"],
        "symbol": payload["symbol"], "side": payload.get("side", "unknown"),
        "source_ts": payload["source_ts"], "available_at": available_at,
        "availability_basis": "source_row_created_at" if available_at != payload["source_ts"] else "source_signal_time",
        "strategy": payload.get("strategy", "Unknown source"),
        "source_version": payload.get("version", "unrecorded"), "stream": payload.get("stream", ""),
        "entry": payload.get("entry"), "stop": payload.get("stop"), "target": payload.get("target"),
        "target_atr_mult": original.get("target_atr_mult"),
        "resolved_at_review": project == "smoothers" and payload.get("status") not in ("open", "tracking"),
        "option_policy": payload.get("option_policy"), "option_contract": (payload.get("option") or {}).get("occ_symbol")}


class Secondary:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.owner = db, cfg, uuid.uuid4().hex
        self.clock = clock

    def review(self, c, candidate, outcome, now, waiting=None):
        key = identity(VERSION, candidate["project"], candidate["source_key"])
        if c.execute(select(reviews.c.id).where(reviews.c.project == candidate["project"],
                reviews.c.source_key == candidate["source_key"])).first():
            c.execute(delete(pending).where(pending.c.id == key))
            return
        inputs = capture(self.db, c, candidate, self.cfg, now)
        inputs['capture_started_at'] = now
        # A READ COMMITTED quote can arrive after the loop's initial clock.
        # Evaluate against a local clock sampled after the read, never against
        # a clock inferred from the provider's timestamp.
        if self.clock:
            now = self.clock()
        inputs['captured_at'] = now
        inputs['clock_basis'] = 'Local clock after input reads; source timestamps remain unchanged'
        decision = assess(candidate, inputs, now)
        deadline = candidate.get("available_at", candidate["source_ts"]) + 60
        if (decision["timely"] and decision["missing"] and not decision["rejections"]
                and candidate.get("side") in ("long", "short") and now < deadline):
            previous = waiting or c.execute(select(pending).where(pending.c.id == key)).mappings().first()
            values = {"id": key, "project": candidate["project"], "source_key": candidate["source_key"],
                "deadline": deadline, "first_seen": previous["first_seen"] if previous else now,
                "last_check": now, "attempts": previous["attempts"]+1 if previous else 1,
                "candidate": candidate, "outcome": outcome, "missing": decision["missing"]}
            c.execute(self.db.insert(pending).values(**values).on_conflict_do_update(index_elements=["id"],
                set_={k: v for k, v in values.items() if k not in ("id", "first_seen", "deadline")}))
            return
        if waiting:
            decision["readiness_wait_seconds"] = round(now-waiting["first_seen"], 3)
            decision["readiness_attempts"] = waiting["attempts"]+1
            if now > deadline:
                decision["reasons"] = ["Review deadline passed before independent data became usable", *waiting["missing"]]
        c.execute(delete(pending).where(pending.c.id == key))
        measures = start_measurement(candidate, inputs, decision, now)
        row = {"id": key, "project": candidate["project"], "source_key": candidate["source_key"],
            "source_id": candidate["source_id"], "symbol": candidate["symbol"], "side": candidate["side"],
            "source_ts": candidate["source_ts"], "decided_at": now, "version": VERSION,
            "verdict": decision["verdict"], "tracking": measures["state"], "candidate": candidate,
            "inputs": inputs, "decision": decision, "measurements": measures, "source_outcome": outcome, "updated": now}
        c.execute(self.db.insert(reviews).values(**row).on_conflict_do_nothing(index_elements=["id"]))
        self.db.append(c, "secondary_review", "secondary", row["symbol"], now,
            {"id": key, "project": row["project"], "source_id": row["source_id"], "verdict": row["verdict"],
             "version": VERSION, "decision": decision}, "secondary-review:" + key)
        if self.cfg.secondary_alerts and decision["timely"]:
            self.db.append(c, "alert", "secondary", row["symbol"], now,
                {"status": "secondary_review", "project": "futures" if candidate["project"] == "compass_futures" else candidate["project"],
                 "review_source": LABELS[candidate["project"]], "strategy": candidate["strategy"], "side": row["side"],
                 "review_id": key, "source_record_id": row["source_id"], "source_time": row["source_ts"],
                 "available_at": candidate.get("available_at", row["source_ts"]),
                 "verdict": row["verdict"], "rule_version": VERSION, "reason": "; ".join(decision["reasons"][:3]),
                 "support": decision["support"], "optional_missing": decision["optional_missing"],
                 "reference_price": measures.get("anchor"), "market_symbol": inputs["market_symbol"],
                 "quote_ts": (inputs.get("quote") or {}).get("ts"), "price_basis": inputs["price_basis"],
                 "expires_at": min(now + 120, candidate.get("available_at", row["source_ts"]) + 120)}, "secondary-alert:" + key)

    def source_event(self, c, event, now):
        p = event["payload"]
        project = event["source"]
        key = identity(project, p["record_id"])
        source = c.execute(select(originals).where(originals.c.project == project, originals.c.key == key)).mappings().first()
        if not source:
            return
        payload = source["payload"]
        c.execute(update(reviews).where(reviews.c.project == project, reviews.c.source_key == key).values(
            source_outcome=source_result(payload, project), updated=now))
        queued = c.execute(select(pending).where(pending.c.project == project, pending.c.source_key == key)).mappings().first()
        if queued:
            # Outcome revisions never reset candidate availability or the deadline.
            frozen = dict(queued["candidate"])
            if project == "smoothers" and payload.get("status") not in ("open", "tracking"):
                frozen["resolved_at_review"] = True
            c.execute(update(pending).where(pending.c.id == queued["id"]).values(
                outcome=source_result(payload, project), candidate=frozen))
        if not p.get("first_observation") or (project == "futures" and payload.get("status") not in ("entry_alert", "emulator_fill")):
            return
        self.review(c, source_candidate(project, payload, key), source_result(payload, project), now)

    def native_event(self, c, event, now):
        p = event["payload"]
        if event["source"] not in ("scanner", "engine") or not future_root(event["symbol"]):
            return
        if p.get("status") not in ("setup_triggered", "entered", "closed") or not p.get("id"):
            return
        key = identity("compass_futures", p["id"])
        if p["status"] == "closed":
            c.execute(update(reviews).where(reviews.c.project == "compass_futures", reviews.c.source_key == key).values(
                source_outcome=source_result(p, "compass_futures"), updated=now))
            queued = c.execute(select(pending).where(pending.c.project == "compass_futures", pending.c.source_key == key)).mappings().first()
            if queued:
                c.execute(update(pending).where(pending.c.id == queued["id"]).values(
                    outcome=source_result(p, "compass_futures"), candidate={**queued["candidate"], "resolved_at_review": True}))
            return
        payload = {**p, "symbol": event["symbol"], "source_ts": event["ts"], "version": p.get("strategy"),
                   "stream": event["source"]}
        self.review(c, source_candidate("compass_futures", payload, key), source_result(payload, "compass_futures"), now)

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if now is None else now
        with self.db.tx() as c:
            if not self.db.lease(c, "secondary", self.owner, 30):
                return
            cursor = self.db.get(c, "secondary:cursor")
            if cursor is None:
                cursor = c.execute(select(func.max(events.c.id))).scalar_one() or 0
                self.db.put(c, "secondary:cursor", cursor)
                self.db.put(c, "secondary:activation", {"at": now, "version": VERSION,
                    "note": "Forward reviews begin here; historical originals are not rescored as timely alerts"})
            activation = self.db.get(c, "secondary:activation")
            # Producers commit concurrently: a lower sequence ID can become
            # visible after a higher one. Durable per-event acknowledgements,
            # rather than id > cursor, prevent that candidate from being lost.
            eligible = or_(and_(events.c.kind == "project_update", events.c.source.in_(("morning", "smoothers", "futures"))),
                           and_(events.c.kind == "alert", events.c.source.in_(("scanner", "engine"))))
            batch = c.execute(select(events).outerjoin(handled, events.c.id == handled.c.event_id)
                .where(handled.c.event_id.is_(None), events.c.ts >= activation["at"], eligible)
                .order_by(events.c.id).limit(200)).mappings().all()
            for event in batch:
                if live_clock:
                    now = time.time()
                # A malformed optional source cannot starve every later review.
                try:
                    with c.begin_nested():
                        if event["kind"] == "project_update" and event["source"] in ("morning", "smoothers", "futures"):
                            self.source_event(c, event, now)
                        elif self.cfg.secondary_native:
                            self.native_event(c, event, now)
                except (ValueError, TypeError, KeyError, OverflowError):
                    self.db.append(c, "secondary_error", "secondary", event["symbol"], now,
                        {"source_event_id": event["id"], "reason": "Invalid candidate or context; no review published"},
                        "secondary-error:" + str(event["id"]))
                c.execute(self.db.insert(handled).values(event_id=event["id"], handled_at=now)
                    .on_conflict_do_nothing(index_elements=["event_id"]))
                cursor = max(cursor, event["id"])
            self.db.put(c, "secondary:cursor", cursor)
            waiting = c.execute(select(pending).where(pending.c.last_check < now)
                .order_by(pending.c.deadline, pending.c.id).limit(200)).mappings().all()
            for row in waiting:
                if live_clock:
                    now = time.time()
                with c.begin_nested():
                    self.review(c, row["candidate"], row["outcome"], now, waiting=row)
            if live_clock:
                now = time.time()
            active = c.execute(select(reviews).where(reviews.c.tracking == "tracking")
                .order_by(reviews.c.updated).limit(500)).mappings().all()
            for row in active:
                quote = self.db.get(c, "quote:" + row["measurements"]["market_symbol"])
                if self.clock or live_clock:
                    now = self.clock() if self.clock else time.time()
                m = advance_recorded_measurement(self.db, c, row["measurements"], row["side"], quote, now)
                c.execute(update(reviews).where(reviews.c.id == row["id"]).values(measurements=m, tracking=m["state"], updated=now))
            self.db.put(c, "secondary:status", {"at": now, "version": VERSION, "enabled": True,
                "alerts_enabled": self.cfg.secondary_alerts, "events_checked": len(batch),
                "active_measured": len(active),
                "waiting_for_data": c.execute(select(func.count()).select_from(pending)).scalar_one(),
                "originals_changed": False})
            report = self.db.get(c, "secondary:report", {})
            if now - report.get("at", 0) >= 60:
                self.db.put(c, "secondary:report", build_report(self.db, c, now))

    async def run(self):
        if not self.cfg.secondary:
            with self.db.tx() as c:
                self.db.put(c, "secondary:status", {"enabled": False, "at": time.time(), "version": VERSION})
            self.db.health("secondary", "disabled", "Secondary review disabled; originals run independently")
            return
        while True:
            try:
                await asyncio.to_thread(self.tick)
                self.db.health("secondary", "running", "Separate source reviews and paired observations; originals unchanged", time.time())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.db.health("secondary", "error", type(error).__name__ + "; original strategies remain independent")
            await asyncio.sleep(2)


def comparisons(rows):
    grouped = defaultdict(list)
    for row in rows:
        group = (row["project"], row["candidate"].get("strategy"), row["candidate"].get("source_version"), row["version"],
                 row["candidate"].get("symbol"), row["candidate"].get("side"))
        grouped[group].append(row)
    result = []
    for (project, strategy, source_version, version, symbol, side), group in sorted(grouped.items(), key=lambda x: str(x[0])):
        counts = {v: sum(r["verdict"] == v for r in group) for v in VERDICTS}
        horizons = []
        for horizon in ("15", "30", "60"):
            eligible = [r for r in group if r["decision"]["timely"] and r["verdict"] != "insufficient_data"]
            observed = [(r, r["measurements"].get("horizons", {}).get(horizon, {})) for r in eligible]
            measured = [(r, p["return_pct"]) for r, p in observed if p.get("status") == "observed"]
            selected = [(r, value) for r, value in measured if r["verdict"] == "supported"]
            skipped = [(r, value) for r, value in measured if r["verdict"] in ("watch", "rejected")]
            horizons.append({"minutes": int(horizon), "eligible": len(eligible), "measured": len(measured),
                "unmeasured": len(eligible) - len(measured), "selected": len(selected),
                "original_mean_pct": sum(v for _, v in measured) / len(measured) if measured else None,
                "selected_mean_pct": sum(v for _, v in selected) / len(selected) if selected else None,
                "filter_per_candidate_pct": sum(v for _, v in selected) / len(measured) if measured else None,
                "supported_positive": sum(v > 0 for _, v in selected),
                "supported_negative": sum(v < 0 for _, v in selected),
                "supported_flat": sum(v == 0 for _, v in selected),
                "skipped_flat": sum(v == 0 for _, v in skipped),
                "retained_quote_measurements": sum(p.get('status')=='observed' and p.get('observation_source')=='retained_quote' for _,p in observed),
                "live_quote_measurements": sum(p.get('status')=='observed' and p.get('observation_source')=='live_quote' for _,p in observed),
                "missed_positive": sum(v > 0 for _, v in skipped), "avoided_negative": sum(v < 0 for _, v in skipped),
                "pending": sum(p.get("status") == "pending" for _, p in observed),
                "missing": sum(p.get("status") in (None, "missing") for _, p in observed),
                "session_boundary": sum(p.get("status") == "session_boundary" for _, p in observed)})
        weekly = [r for r in group if r["project"] == "smoothers" and r["decision"]["timely"] and r["verdict"] != "insufficient_data"
                  and r["source_outcome"].get("status") in ("win", "loss")]
        selected_weekly = [r for r in weekly if r["verdict"] == "supported"]
        result.append({"project": project, "label": LABELS[project], "strategy": strategy,
            "symbol": symbol, "side": side,
            "source_version": source_version, "version": version, "total": len(group), "counts": counts, "horizons": horizons,
            "weekly": {"resolved": len(weekly), "target_hits": sum(r["source_outcome"]["status"] == "win" for r in weekly),
                "selected_resolved": len(selected_weekly), "selected_target_hits": sum(r["source_outcome"]["status"] == "win" for r in selected_weekly),
                "basis": "Original Smoothers underlying target result; not option profit"}})
    return result


def build_report(db, c, now, since=None):
    since = now - 30 * 86400 if since is None else since
    if not math.isfinite(since) or not now-30*86400 <= since <= now:
        raise ValueError('Report start must be within the last 30 days and no later than now')
    columns = [reviews.c[k] for k in ("project", "candidate", "version", "verdict", "decision", "measurements", "source_outcome")]
    rows = c.execute(select(*columns).where(reviews.c.decided_at >= since,reviews.c.decided_at <= now)
        .order_by(reviews.c.decided_at.desc(), reviews.c.id).limit(5001)).mappings().all()
    truncated = len(rows) > 5000
    rows = [dict(r) for r in rows[:5000]]
    return {"at": now, "window": {"since": since, "through": now, "reviewed": len(rows), "truncated": truncated, "limit": 5000},
        "counts": {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS}, "comparisons": comparisons(rows)}


def snapshot(db, c, now, clock=None):
    report = db.get(c, "secondary:report", {"at": None, "counts": {v: 0 for v in VERDICTS}, "comparisons": [], "window": {"reviewed": 0}})
    columns = [reviews.c[k] for k in ("id", "project", "symbol", "side", "source_id", "source_ts", "decided_at", "version", "verdict", "candidate", "decision", "measurements")]
    recent = []
    for project in LABELS:
        recent.extend(c.execute(select(*columns).where(reviews.c.project == project)
            .order_by(reviews.c.decided_at.desc(), reviews.c.id).limit(100)).mappings().all())
    recent.sort(key=lambda r: (-r["decided_at"], r["id"]))
    waiting = c.execute(select(pending).order_by(pending.c.deadline).limit(100)).mappings().all()
    coverage = db.get(c, "secondary:data_status", {})
    coverage = {**coverage, "rows": [dict(row) for row in coverage.get("rows", [])]}
    quotes = db.prefix(c, 'quote:')
    if clock:
        now = clock()
    coverage['evaluated_at'] = now
    for row in coverage["rows"]:
        q = quotes.get('quote:' + row["symbol"])
        row["quote_ready"] = bool(fresh(q, now) and current(q.get("ts"), now, 5))
        row["quote_age"] = round(now-q["ts"], 2) if q else None
        row["minute_ready"] = bool(row.get("minute_ready") and current(row.get("minute_asof"), now, 90))
        row["ready"] = bool(current(coverage.get("at"), now, 45) and row["collection_enabled"]
            and row["quote_ready"] and row["minute_ready"] and
            ("smoothers" not in row["projects"] or row.get("daily_ready")))
    return {"status": db.get(c, "secondary:status", {"enabled": False, "at": None}),
        "activation": db.get(c, "secondary:activation"), "version": VERSION,
        "report_at": report["at"], "window": report["window"], "counts": report["counts"],
        "comparisons": report["comparisons"], "reviews": [dict(r) for r in recent], "originals_changed": False,
        "data_readiness": coverage,
        "pending": [{"project": r["project"], "symbol": r["candidate"]["symbol"], "deadline": r["deadline"],
            "first_seen": r["first_seen"], "attempts": r["attempts"], "missing": r["missing"]} for r in waiting],
        "method": "Same secondary-decision quote and checkpoint for original-candidate and selected subsets. Missing data are excluded, never losses.",
        "limitation": "Midpoint moves are before spreads, fees and execution slippage; not option or broker P&L. No improvement is established by activating the filter."}
