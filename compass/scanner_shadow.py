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
consulted at both booking points: zero_dte_paper._open_scanner and the
scanner's own pending_options paper path (scanner.retry_options, gated
2026-10-27 after that path booked non-seated cells the split had judged
shadow-only).
Non-scanner producers that share the options_0dte category (earnings
plays; the MU scalp has its own book) carry no scanner family and are
outside this split — their behavior is unchanged.

SUPERSEDED IN PART, 2026-11-01 (whole-list book respec): the universe
is now the full watchlist, not the 15-name study universe, and there
is NO grandfathering — PAPER_SEATS defaults to EMPTY and AMD/MU/NFLX
start in Shadow. The family gate is unchanged (session_sweep_reclaim
is the only promoted family; the three dead families are explicitly
BARRED from graduating in BARRED_FAMILIES). Seating is an operator
action via ODTE_BOOK_SEATED_NAMES after a name clears the unchanged
name gate (>= 20 scored PREMIUM shadow trades at PF >= 1.2), which
this scorecard flags as ready_for_josh. Bookings additionally pass
the G1-G4 gate set in compass/odte_book.py.

UNSEATED, 2026-11-02 (tiebreak replay; Josh's decision): the
production-code replay of session_sweep_reclaim on the 2-year /
163-name panel (n=107,146 counted trades) returned negative
expectancy at both headline horizons (+120m -1.68 bps, t -2.99;
close -1.45 bps) and a 3.5th-percentile placebo — verdict SEAT
FALLS — and Josh ruled "get rid of that". The family moves to
BARRED_FAMILIES (shadow-only, barred from graduating, exactly like
the three dead families) and PROMOTED_FAMILIES is now EMPTY: the
book has NO booking family. Detection and shadow scoring are
unchanged — the family's setups still open shadow records and the
sample keeps accruing as evidence — but no name can graduate to
booking on it, even if an operator env tried to seat or promote it
(the barred check in is_seated wins over both). A future family can
hold the seat only by passing frozen underlying validation, with
Josh seating it via the same config mechanism.
"""

from . import odte_book
from .live_combo_scorecard import combo_stats

# The six families produced by compass/scanner.py.
SCANNER_FAMILIES = frozenset({
    "session_sweep_reclaim", "trend_pullback", "volume_breakout",
    "level_breakdown", "exposure_level_break", "flow_price_breakout",
})

# Promotion status. 2026-10-25 validation promoted the one family
# below; the 2026-11-02 tiebreak replay unseated it (see module
# docstring). NO family is currently promoted — the book has no
# booking family. Promotion remains a config state a future family
# can earn through frozen underlying validation (env override
# SCANNER_PROMOTED_FAMILIES, or a change here at a window boundary).
PROMOTED_FAMILIES = frozenset()

# Barred families: shadow-only, BARRED from graduating regardless of
# their shadow record. The first three are the dead families of the
# 2026-10-25 underlying-first validation; session_sweep_reclaim
# joined them 2026-11-02 on the tiebreak verdict (SEAT FALLS) and
# Josh's decision. The unvalidatable pair (exposure_level_break,
# flow_price_breakout) is shadow-only by virtue of never holding
# promotion status — it is NOT barred, so the promotion mechanism
# above remains reachable for it if validation ever earns it.
BARRED_FAMILIES = frozenset({
    "session_sweep_reclaim", "trend_pullback", "volume_breakout",
    "level_breakdown",
})

# Whole-list book (respec 2026-11-01): NO grandfathering — the seated
# set starts EMPTY and AMD/MU/NFLX start in Shadow like every other
# name. Seating is an operator action (env ODTE_BOOK_SEATED_NAMES, or
# the legacy SCANNER_PAPER_SEATS) taken after a name-gate flag; this
# constant is the default and it seats nobody.
PAPER_SEATS = {}

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
    """Effective seated cells: an OPERATOR action, never a default.
    cfg.odte_book_seated_names (env ODTE_BOOK_SEATED_NAMES) wins; the
    legacy SCANNER_PAPER_SEATS override is honored when the new one is
    unset. Default EMPTY (no grandfathering). An unparseable override
    seats nobody — fail closed, deliberately the opposite of the old
    fallback: a typo must never silently seat a book."""
    for attr in ("odte_book_seated_names", "scanner_paper_seats"):
        raw = getattr(cfg, attr, "") if cfg is not None else ""
        parsed = _parse_seats(raw)
        if parsed:
            return parsed
    return dict(PAPER_SEATS)


def promoted_families(cfg=None):
    raw = getattr(cfg, "scanner_promoted_families", "") if cfg is not None else ""
    parsed = frozenset(f.strip() for f in str(raw or "").split(",")
                       if f.strip() in SCANNER_FAMILIES)
    return parsed if parsed else PROMOTED_FAMILIES


def seats_source(cfg=None):
    return "env" if paper_seats(cfg) else "none"


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
    """A cell books only when its family holds promotion status, is not
    barred, AND the name has been seated by the operator action. Under
    the respec's default configuration nobody is seated."""
    if family not in SCANNER_FAMILIES or not underlying:
        return False
    if family in BARRED_FAMILIES or family not in promoted_families(cfg):
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


def _tm_gex_stamps(db, c, underlying, now):
    """TraderMatrix GEX measurement stamps for a shadow record
    (tm_gex.shadow_stamps). Defensive wrapper: a stamp failure must
    never cost the record — all-None is the honest fallback."""
    try:
        from . import tm_gex
        return tm_gex.shadow_stamps(db, c, underlying, now)
    except Exception:
        return {"spy_flip_dist_pct": None, "name_pocket_kind": None,
                "name_gex_bias": None}


def new_record(db, c, key, payload, family, underlying, symbol, entry, now,
               alert_ts, flatten_at, alert_key=None, quote=None):
    """Build the shadow record for one non-seated scanner alert. `entry`
    is the fresh-quote hypothetical entry (ask + slippage) or None when
    no fresh quote existed — then the record is a data gap from birth
    and can never be scored. `quote` is the consulted quote at the
    booking decision: its bid/ask/ts/source/age are stamped on the
    record (Stage 1: fills carry the evidence too, not just blocks),
    along with the planned max loss and the contract's DTE bucket."""
    from . import zero_dte_paper as zp  # lazy: zp imports this module

    pub = payload.get("publication") or {}
    contract = pub.get("contract") or {}
    dte = odte_book.contract_dte(contract.get("expiry"), now)
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
        "dte": dte, "dte_bucket": odte_book.dte_bucket(dte),
        "entry_quote": odte_book.quote_evidence(quote, now),
        "planned_max_loss": None,
        # GEX/VEX entry stamp (frozen 2026-10-07 divergence heuristic):
        # the same spread the seated booking path applies in
        # zero_dte_paper, so shadow records carry the divergence data
        # too and the sample keeps growing while nobody is seated.
        # SPY records read exposure:SPY (SPX is never booked). Stamp
        # only — no behavior change; the fallback when no exposure row
        # exists is gex/vex None, divergent False, exactly as in
        # zero_dte_paper._gex_vex_at_entry.
        **zp._gex_vex_at_entry(db, c, underlying or "SPY"),
        # TraderMatrix GEX measurement stamps (incorporation plan §1,
        # Build #2): SPY distance-to-flip plus the name's own gamma
        # pocket kind / GEX bias when a fresh snapshot exists. Evidence
        # fields only — the scorecard may split on them once cells
        # reach the standing graduation math; nothing books or blocks
        # on them. All three are None when their sources are absent.
        **_tm_gex_stamps(db, c, underlying, now),
    }
    if rec["entry_quote"] and rec["entry_quote"].get("ask") is not None:
        rec["planned_max_loss"] = odte_book.planned_max_loss(
            rec["entry_quote"]["ask"])
    if entry is None:
        rec.update(exit=None, exited_at=now, pnl=None, data_gap=True,
                   exit_reason="entry quote unavailable (data gap)")
    return rec


def _crosscheck(db, c, now):
    """Underlying cross-check (respec section 8): on every closed,
    non-data_gap booked trade and shadow record, stamp the underlying's
    R at signal+120m and at the signal day's close (R denominator:
    signal price to invalidation), computed from stored 1-minute bars.
    When the bars are no longer retrievable the record is marked
    unavailable and counted — never silently skipped. Statuses:
    stamped (both marks), partial (one mark; retried on later passes),
    unavailable. Records older than 7 days finalize with what exists."""
    from .market import day as _day

    targets = []
    try:
        targets += list(db.prefix(c, SHADOW_PREFIX).items())
    except Exception:
        pass
    try:
        targets += [(k, t) for k, t in
                    db.prefix(c, "trade:0dte-scan-").items()
                    if isinstance(t, dict) and t.get("kind") == "scanner"]
    except Exception:
        pass
    done = 0
    for key, t in targets:
        if done >= 25:
            break
        if not isinstance(t, dict) or t.get("status") != "closed" \
                or t.get("data_gap"):
            continue
        if t.get("crosscheck_status") in ("stamped", "unavailable"):
            continue
        alert_ts = _num(t.get("alert_ts")) or _num(t.get("entered_at"))
        trig = t.get("trigger") or {}
        sig = _num(trig.get("signal_price"))
        inv = _num(trig.get("invalidation"))
        und = t.get("underlying")
        if not alert_ts or sig is None or inv is None or not und \
                or sig == inv:
            t["crosscheck_status"] = "unavailable"
            db.put(c, key, t)
            done += 1
            continue
        if now - alert_ts < 7200:
            continue  # the +120m mark cannot exist yet
        try:
            rows = db.recent(c, "bar", und, limit=1800)
        except Exception:
            rows = []
        bars = sorted(
            (r for r in rows if isinstance(r.get("payload"), dict)),
            key=lambda r: r.get("ts") or 0)
        direction = 1 if sig > inv else -1
        risk = abs(sig - inv)
        alert_day = _day(alert_ts)

        def price_at(target):
            for r in bars:
                if (r.get("ts") or 0) >= target:
                    return _num(r["payload"].get("c"))
            return None

        if "underlying_r_120m" not in t:
            p120 = price_at(alert_ts + 7200)
            if p120 is not None:
                t["underlying_r_120m"] = round(
                    (p120 - sig) * direction / risk, 3)
        if "underlying_r_close" not in t and _day(now) > alert_day:
            day_bars = [r for r in bars
                        if _day(r.get("ts") or 0) == alert_day]
            if day_bars:
                pc = _num(day_bars[-1]["payload"].get("c"))
                if pc is not None:
                    t["underlying_r_close"] = round(
                        (pc - sig) * direction / risk, 3)
        have = ("underlying_r_120m" in t, "underlying_r_close" in t)
        if all(have):
            t["crosscheck_status"] = "stamped"
        elif not any(have):
            t["crosscheck_status"] = "unavailable"
        elif now - alert_ts > 7 * 86400:
            t["crosscheck_status"] = "stamped"
        else:
            t["crosscheck_status"] = "partial"
        db.put(c, key, t)
        done += 1
    return done


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
                 exit_reason=reason, pnl=pnl, data_gap=data_gap,
                 exit_quote_age_s=round(quote_age, 1))
        db.put(c, key, t)
        closed += 1
    try:
        _crosscheck(db, c, now)
    except Exception:
        pass
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
        family_barred = family in BARRED_FAMILIES
        ready = (family_promoted and not family_barred
                 and scored >= GRADUATION_MIN_SCORED
                 and pf is not None and pf >= GRADUATION_MIN_PF)
        dte_split = {"0": 0, "1-7": 0}
        for t in cell["scored_trades"]:
            bucket = t.get("dte_bucket")
            if bucket in dte_split:
                dte_split[bucket] += 1
        out_cells.append({
            "family": family, "underlying": underlying,
            "alerts": cell["alerts"], "open": cell["open"],
            "data_gaps": cell["data_gaps"],
            "scored": scored, "wins": stats["wins"],
            "losses": stats["losses"], "win_rate": stats["win_rate"],
            "pf": pf, "net": stats["net"],
            "dte_split": dte_split,
            "graduation": {
                "bar": {"min_scored": GRADUATION_MIN_SCORED,
                        "min_pf": GRADUATION_MIN_PF},
                "family_promoted": family_promoted,
                "family_barred": family_barred,
                "scored_needed": max(0, GRADUATION_MIN_SCORED - scored),
                "pf_met": pf is not None and pf >= GRADUATION_MIN_PF,
                "ready_for_josh": ready,
            },
        })
    # The seated book itself, readably (respec section 5.4): booked
    # stats INCLUDE the data_gap count (the pre-respec surface dropped
    # it), the DTE split is surfaced, and a seated name whose last-20
    # closed booked trades fall below PF 1.0 is flagged as a demotion
    # candidate — unseating stays an operator action, never automatic.
    book = {"cells": [], "totals": {"booked": 0, "open": 0, "data_gaps": 0},
            "dte_split": {"0": 0, "1-7": 0}}
    try:
        trades = db.prefix(c, "trade:0dte-scan-")
    except Exception:
        trades = {}
    booked_cells = {}
    for t in trades.values():
        if not isinstance(t, dict) or t.get("kind") != "scanner":
            continue
        cell = booked_cells.setdefault(
            (t.get("family") or "unknown", t.get("underlying") or "unknown"),
            {"scored_trades": [], "open": 0, "data_gaps": 0})
        if t.get("status") == "open":
            cell["open"] += 1
            book["totals"]["open"] += 1
        elif t.get("data_gap"):
            cell["data_gaps"] += 1
            book["totals"]["data_gaps"] += 1
        elif t.get("status") == "closed":
            cell["scored_trades"].append(t)
            bucket = t.get("dte_bucket")
            if bucket in book["dte_split"]:
                book["dte_split"][bucket] += 1
    for (family, underlying), cell in sorted(booked_cells.items()):
        stats = combo_stats(cell["scored_trades"])
        book["totals"]["booked"] += stats["trades"]
        last20 = sorted(cell["scored_trades"],
                        key=lambda t: _num(t.get("exited_at")) or 0)[-20:]
        last20_stats = combo_stats(last20)
        demotion = (len(last20) >= 20 and last20_stats["pf"] is not None
                    and last20_stats["pf"] < 1.0)
        book["cells"].append({
            "family": family, "underlying": underlying,
            "open": cell["open"], "data_gaps": cell["data_gaps"],
            "scored": stats["trades"], "wins": stats["wins"],
            "losses": stats["losses"], "win_rate": stats["win_rate"],
            "pf": stats["pf"], "net": stats["net"],
            "demotion_candidate": demotion,
        })
    return {
        "paper_seats": {fam: sorted(names) for fam, names in
                        sorted(seats.items())},
        "seats_source": seats_source(cfg),
        "promoted_families": sorted(promoted),
        "barred_families": sorted(BARRED_FAMILIES),
        "shadow_cells": out_cells,
        "book": book,
        "totals": totals,
        "graduation_bar": {"min_scored": GRADUATION_MIN_SCORED,
                           "min_pf": GRADUATION_MIN_PF},
        "note": ("Graduation is flagged for Josh's decision, never "
                 "auto-promoted. data_gap records are excluded from PF "
                 "(house rule)."),
    }
