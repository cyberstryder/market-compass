"""Seat/shadow split for the 0DTE scanner book (Josh, 2026-10-25).

The underlying-first validation (files/options-underlying-first-validation-
2026-10-25.md) decided which scanner family x name cells may book paper
trades and which are measured in shadow instead:

- PAPER SEATS: session_sweep_reclaim x {AMD, MU, NFLX} — the only family
  that passed the promotion rule, on the names whose cells cleared
  >= 20 trades and PF >= 1.2 at both passing horizons (+120m and close).
- SHADOW: every other scanner family x name cell in the 15-name universe.
  The alert still fires with its evidence snapshot, but instead of a
  booking, a shadow record is opened and its hypothetical outcome is
  scored under the paper book's own exit rules (+50% / -50% premium,
  2:55 PM CT flatten) on fresh quotes only. Unquotable outcomes are
  data_gap — never wins or losses (house rule) — and are excluded from
  PF. For the two options-native families (exposure_level_break,
  flow_price_breakout), which no historical data can validate, the
  trigger inputs are recorded with the alert: that forward record is
  what makes them validatable at all.

Graduation: a shadow cell re-earns a paper seat when its shadow record
clears >= 20 scored alerts and PF >= 1.2, within a family that itself
holds promotion status. The scorecard FLAGS a cell that clears the bar;
promotion itself is Josh's decision (it spends premium), never automatic.
Family promotion status is configuration, re-checked on the underlying
each 4-week window by the validation process — a demotion lands here as
a config change at the window boundary, per the spec.

The seated set is ONE structure (PAPER_SEATS / PROMOTED_FAMILIES below,
env-overridable via SCANNER_PAPER_SEATS / SCANNER_PROMOTED_FAMILIES),
consulted at the single booking point in zero_dte_paper._open_scanner.
Non-scanner producers that share the options_0dte category (earnings
plays; the MU scalp has its own book) carry no scanner family and are
outside this split — their behavior is unchanged.
"""

from .live_combo_scorecard import combo_stats

# The six families produced by compass/scanner.py.
SCANNER_FAMILIES = frozenset({
    "session_sweep_reclaim", "trend_pullback", "volume_breakout",
    "level_breakdown", "exposure_level_break", "flow_price_breakout",
})

# Validation verdict, 2026-10-25: the only promoted family, and its seats.
PROMOTED_FAMILIES = frozenset({"session_sweep_reclaim"})
PAPER_SEATS = {
    "session_sweep_reclaim": frozenset({"AMD", "MU", "NFLX"}),
}

SHADOW_PREFIX = "shadow:0dte-"
GRADUATION_MIN_SCORED = 20
GRADUATION_MIN_PF = 1.2


def _parse_seats(raw):
    """'family:NAME,NAME;family2:NAME' -> {family: frozenset(names)}."""
    out = {}
    for segment in str(raw or "").split(";"):
        family, sep, names = segment.partition(":")
        family = family.strip()
        if not sep or family not in SCANNER_FAMILIES:
            continue
        seats = frozenset(n.strip().upper() for n in names.split(",") if n.strip())
        if seats:
            out[family] = seats
    return out


def paper_seats(cfg=None):
    """Effective seated cells: env override when it parses to a non-empty
    map, else the validated default. A typo must not silently shadow (or
    seat) the whole book, so an empty/invalid override falls back."""
    raw = getattr(cfg, "scanner_paper_seats", "") if cfg is not None else ""
    parsed = _parse_seats(raw)
    return parsed if parsed else dict(PAPER_SEATS)


def promoted_families(cfg=None):
    raw = getattr(cfg, "scanner_promoted_families", "") if cfg is not None else ""
    parsed = frozenset(f.strip() for f in str(raw or "").split(",")
                       if f.strip() in SCANNER_FAMILIES)
    return parsed if parsed else PROMOTED_FAMILIES


def seats_source(cfg=None):
    raw = getattr(cfg, "scanner_paper_seats", "") if cfg is not None else ""
    return "env" if _parse_seats(raw) else "default"


def alert_family(payload):
    """Scanner family carried by an options_0dte alert payload, or None
    when the alert is not from compass/scanner.py (earnings plays etc.)."""
    if not isinstance(payload, dict):
        return None
    rule = payload.get("rule")
    if rule in SCANNER_FAMILIES:
        return rule
    strategy = str(payload.get("strategy") or "")
    # '0dte-compass-scanner-v2:<rule>' — fallback when 'rule' was dropped.
    if "compass-scanner" in strategy and ":" in strategy:
        rule = strategy.rsplit(":", 1)[-1]
        if rule in SCANNER_FAMILIES:
            return rule
    return None


def is_seated(family, underlying, cfg=None):
    if family not in SCANNER_FAMILIES or not underlying:
        return False
    return str(underlying).upper() in paper_seats(cfg).get(family, frozenset())


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _trigger_inputs(db, c, payload):
    """Trigger context for a shadow record: the signal price and underlying
    invalidation always; for the options-native families, the vendor
    trigger evidence (level crossed / flow premium+score) from the
    originating scanner signal, when it is still retrievable."""
    out = {
        "signal_price": _num(payload.get("signal_price")),
        "invalidation": _num(payload.get("underlying_invalidation")),
    }
    source_id = payload.get("source_id")
    if source_id:
        try:
            pending = db.get(c, "pending_research_options:" + str(source_id)) or {}
            evidence = (pending.get("signal") or {}).get("evidence") or []
        except Exception:
            evidence = []
        for e in evidence:
            if not isinstance(e, dict) or e.get("source") != "tradermatrix":
                continue
            if e.get("kind") == "unusual_flow":
                out["flow"] = {k: e.get(k) for k in
                               ("premium", "score", "sentiment",
                                "classification", "vendor_id")}
            elif e.get("level") is not None:
                out["level"] = {k: e.get(k) for k in ("kind", "level", "score")}
    return out


def new_record(db, c, key, payload, family, underlying, symbol, entry, now,
               alert_ts, flatten_at, alert_key=None):
    """Build the shadow record for one non-seated scanner alert. `entry`
    is the fresh-quote hypothetical entry (ask + slippage) or None when
    no fresh quote existed — then the record is a data gap from birth
    and can never be scored."""
    from . import zero_dte_paper as zp  # lazy: zp imports this module

    pub = payload.get("publication") or {}
    contract = pub.get("contract") or {}
    rec = {
        "id": key, "kind": "scanner_shadow", "family": family,
        "symbol": symbol, "underlying": underlying,
        "side": "long", "qty": 1, "multiplier": 100,
        "entry": entry, "entered_at": now if entry is not None else None,
        "exit_up_pct": 50.0, "exit_down_pct": -50.0,
        "alert_key": alert_key, "alert_ts": alert_ts,
        "alert_event": pub.get("status") or pub.get("event"),
        "strike": contract.get("strike"), "option_type": contract.get("type"),
        "trigger": _trigger_inputs(db, c, payload),
        "status": "open" if entry is not None else "closed",
        "fee": zp.FEE, "flatten_at": flatten_at,
    }
    if entry is None:
        rec.update(exit=None, exited_at=now, pnl=None, data_gap=True,
                   exit_reason="entry quote unavailable (data gap)")
    return rec


def update(db, c, now):
    """Advance open shadow records under the paper book's scanner exit
    rules, importing its constants/helpers so the two books can never
    drift apart: +50% / -50% of entry premium on the bid, session
    flatten, stale marks flagged data_gap, quoteless records force-
    closed as data gaps after the flatten grace."""
    from . import zero_dte_paper as zp  # lazy: zp imports this module

    try:
        records = db.prefix(c, SHADOW_PREFIX)
    except Exception:
        return 0
    closed = 0
    for key, t in records.items():
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        q = zp._fresh_quote(db, c, t.get("symbol"), now, max_age=300)
        if q and (_num(q.get("ts")) or 0) > (_num(t.get("last_quote_ts")) or 0):
            t["last_quote_ts"] = _num(q.get("ts"))
            t["last_bid"] = _num(q.get("bid"))
            db.put(c, key, t)
        reason = None
        flatten_at = _num(t.get("flatten_at"))
        if flatten_at and now >= flatten_at:
            reason = "session flatten"
        else:
            bid = _num((q or {}).get("bid"))
            entry = _num(t.get("entry"))
            if bid and entry:
                if bid >= entry * 1.5:
                    reason = "target hit (+50%)"
                elif bid <= entry * 0.5:
                    reason = "stopped (-50%)"
        if not reason:
            continue
        exit_px = (_num((q or {}).get("bid")) or 0) - zp.SLIPPAGE
        if exit_px <= 0:
            if (reason == "session flatten" and flatten_at
                    and now >= flatten_at + zp.FORCE_CLOSE_AFTER_FLATTEN):
                last_bid = _num(t.get("last_bid")) or 0
                gap_px = (round(last_bid - zp.SLIPPAGE, 2)
                          if last_bid > zp.SLIPPAGE else None)
                entry = _num(t.get("entry")) or 0
                pnl = (round((gap_px - entry) * 100 - 2 * zp.FEE, 2)
                       if gap_px is not None and entry else None)
                t.update(status="closed", exit=gap_px, exited_at=now,
                         exit_reason="session flatten (data gap)", pnl=pnl,
                         data_gap=True)
                db.put(c, key, t)
                closed += 1
            continue
        entry = _num(t.get("entry")) or 0
        pnl = round((exit_px - entry) * 100 - 2 * zp.FEE, 2)
        quote_age = now - (_num((q or {}).get("ts")) or 0)
        saw_after_entry = ((_num(t.get("last_quote_ts")) or 0)
                           > (_num(t.get("entered_at")) or 0))
        data_gap = quote_age > zp.STALE_EXIT_AGE or not saw_after_entry
        t.update(status="closed", exit=round(exit_px, 2), exited_at=now,
                 exit_reason=reason, pnl=pnl, data_gap=data_gap)
        db.put(c, key, t)
        closed += 1
    return closed


def scorecard(db, c, cfg=None):
    """The split, readably: effective seats, per-cell shadow stats, and
    graduation flags. Scored = closed, non-data_gap shadow records; PF
    and win rate come from the live-combo scorecard's aggregation so the
    conventions match the futures book exactly."""
    seats = paper_seats(cfg)
    promoted = promoted_families(cfg)
    cells = {}
    totals = {"alerts": 0, "open": 0, "scored": 0, "data_gaps": 0}
    try:
        records = db.prefix(c, SHADOW_PREFIX)
    except Exception:
        records = {}
    for key, t in records.items():
        if not isinstance(t, dict):
            continue
        cell = cells.setdefault((t.get("family") or "unknown",
                                 t.get("underlying") or "unknown"),
                                {"scored_trades": [], "alerts": 0,
                                 "open": 0, "data_gaps": 0})
        cell["alerts"] += 1
        totals["alerts"] += 1
        if t.get("status") == "open":
            cell["open"] += 1
            totals["open"] += 1
        elif t.get("data_gap"):
            cell["data_gaps"] += 1
            totals["data_gaps"] += 1
        elif t.get("status") == "closed":
            cell["scored_trades"].append(t)
            totals["scored"] += 1
    out_cells = []
    for (family, underlying), cell in sorted(cells.items()):
        stats = combo_stats(cell["scored_trades"])
        scored = stats["trades"]
        pf = stats["pf"]
        family_promoted = family in promoted
        ready = (family_promoted and scored >= GRADUATION_MIN_SCORED
                 and pf is not None and pf >= GRADUATION_MIN_PF)
        out_cells.append({
            "family": family, "underlying": underlying,
            "alerts": cell["alerts"], "open": cell["open"],
            "data_gaps": cell["data_gaps"],
            "scored": scored, "wins": stats["wins"],
            "losses": stats["losses"], "win_rate": stats["win_rate"],
            "pf": pf, "net": stats["net"],
            "graduation": {
                "bar": {"min_scored": GRADUATION_MIN_SCORED,
                        "min_pf": GRADUATION_MIN_PF},
                "family_promoted": family_promoted,
                "scored_needed": max(0, GRADUATION_MIN_SCORED - scored),
                "pf_met": pf is not None and pf >= GRADUATION_MIN_PF,
                "ready_for_josh": ready,
            },
        })
    return {
        "paper_seats": {fam: sorted(names) for fam, names in
                        sorted(seats.items())},
        "seats_source": seats_source(cfg),
        "promoted_families": sorted(promoted),
        "shadow_cells": out_cells,
        "totals": totals,
        "graduation_bar": {"min_scored": GRADUATION_MIN_SCORED,
                           "min_pf": GRADUATION_MIN_PF},
        "note": ("Graduation is flagged for Josh's decision, never "
                 "auto-promoted. data_gap records are excluded from PF "
                 "(house rule)."),
    }
