"""Whole-list 0DTE book gates (frozen respec, 2026-11-01).

One gate set, evaluated identically at contract selection
(engine.options, research path) and again at booking time
(zero_dte_paper._open_scanner) on the freshly consulted quote — the
alert-consumer path previously booked with NO spread re-check; that
gap is closed by both paths calling evaluate_candidate() here.

Gates (respec section 3/4):
- G1 premium cap: REMOVED 2026-11-12 (Josh's decision on that day's
  chain-lane diagnosis: the $0.90 cap was the largest single
  contract-level rejection among fully-fresh audits, and premium is
  his manual call when he looks at a published setup). Premium is
  still recorded everywhere it was (details, quote evidence,
  publications, diagnostics) — it is no longer an eligibility
  rejection at selection or at booking. Planned max loss at the
  book's +50% exit remains 50 x ask + $2.80 round-trip frictional
  cost, and the G4 daily planned-loss budget still prices it.
- G2 spread: ask - bid <= max(15% of ask, $0.05).
- G3 movement eligibility: the name's trailing-20-session median RTH
  range (daily bars) >= 1.50% of close, the name is present in the EOD
  chain archive (not missing from 5 consecutive snapshots), and the
  candidate's ask/spot <= range20 / 2. A name with < 20 sessions of
  daily history is movement-ineligible: no bookings, no shadow records
  (rejection evidence only).
- G4 caps (seated bookings): 1 open position per name, 2 bookings per
  name per day, 6 open positions book-wide, $250 daily planned-loss
  budget (sum of planned max losses).

Rejection reason tokens (respec section 4) are returned verbatim:
spread_too_wide, movement_ineligible, name_position_open,
name_daily_cap, book_open_cap, daily_risk_budget.
(premium_above_cap retired 2026-11-12 with G1.)
"""

from datetime import date, datetime, timedelta
from statistics import median

from .market import NY, day

SPREAD_PCT_OF_ASK = 0.15
SPREAD_FLOOR = 0.05
RANGE20_MIN = 0.015
RANGE20_SESSIONS = 20
CHAIN_MISSING_STREAK = 5
DAILY_RISK_BUDGET = 250.0
MAX_OPEN_BOOK = 6
MAX_PER_NAME_DAY = 2
COMMISSION_PER_LEG_SIDE = 0.65
SLIPPAGE_PER_LEG_SIDE = 0.75
ROUND_TRIP_COST = 2 * COMMISSION_PER_LEG_SIDE + 2 * SLIPPAGE_PER_LEG_SIDE  # 2.80

BOOK_PREFIX = "trade:0dte-scan-"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def planned_max_loss(ask):
    """Worst case at the -50% exit: half the premium plus round-trip
    frictional cost (respec section 3.1)."""
    ask = _num(ask)
    if ask is None:
        return None
    return round(50.0 * ask + ROUND_TRIP_COST, 2)


def quote_evidence(quote, now):
    """The Stage-1 fill stamp: the consulted quote's bid/ask/ts/source
    and its computed age at the decision moment. Stamped on every
    booking, shadow record, and gate rejection so no verdict ever
    rests on a stale fill unknowingly."""
    if not isinstance(quote, dict):
        return None
    ts = _num(quote.get("ts"))
    return {"bid": _num(quote.get("bid")), "ask": _num(quote.get("ask")),
            "ts": ts, "source": quote.get("source"),
            "age_s": round(now - ts, 3) if ts is not None else None}


def contract_dte(expiry, now):
    try:
        return (date.fromisoformat(str(expiry))
                - date.fromisoformat(day(now))).days
    except (ValueError, TypeError):
        return None


def dte_bucket(dte):
    if dte is None:
        return None
    return "0" if dte == 0 else "1-7"


def range20(db, c, symbol, now):
    """Trailing-20-session median RTH range ((high-low)/close) from
    daily bars, sessions strictly before today. None when fewer than
    20 sessions exist. Cached per symbol per day in state.

    The read goes through daily_history.completed_rows (latest
    revision per completed session): the daily archive is append-only
    and carries 20-55 revision rows per session, so the previous
    newest-80-rows read spanned only 2-3 distinct sessions and
    returned None for every symbol (2026-11-14 funnel diagnosis,
    files/odte-funnel-fix-design-2026-11-14.md Defect 3). The
    >= 20-session rule itself is unchanged."""
    key = "odte_book:range20:%s:%s" % (day(now), symbol)
    try:
        cached = db.get(c, key)
    except Exception:
        cached = None
    if isinstance(cached, dict) and "value" in cached:
        return cached["value"]
    value = None
    sessions = 0
    try:
        from .daily_history import completed_rows
        rows = completed_rows(db, c, symbol, now)
    except Exception:
        rows = []
    by_day = {}
    for r in rows:
        d = r.get("session_day")
        if not d:
            continue
        p = r.get("payload") or {}
        h, l, cl = _num(p.get("h")), _num(p.get("l")), _num(p.get("c"))
        if h is None or l is None or not cl:
            continue
        by_day[d] = (h - l) / cl
    days = sorted(by_day)[-RANGE20_SESSIONS:]
    sessions = len(days)
    if sessions >= RANGE20_SESSIONS:
        value = round(median([by_day[d] for d in days]), 6)
    try:
        db.put(c, key, {"value": value, "sessions": sessions, "at": now})
    except Exception:
        pass
    return value


def chain_coverage_ok(db, c, symbol, now):
    """Respec section 2 exclusion: a name missing from 5 consecutive
    completed EOD chain snapshots is out of the universe. Fewer than 5
    completed snapshots on record is no evidence either way (covered).
    Cached per symbol per day."""
    key = "odte_book:chain_ok:%s:%s" % (day(now), symbol)
    try:
        cached = db.get(c, key)
    except Exception:
        cached = None
    if isinstance(cached, dict) and "ok" in cached:
        return bool(cached["ok"])
    ok = True
    try:
        from .chain_eod import manifest_key
        today = datetime.fromtimestamp(now, NY).date()
        streak = 0
        completed = 0
        for i in range(1, 13):
            dstr = (today - timedelta(days=i)).isoformat()
            m = db.get(c, manifest_key(dstr))
            if not isinstance(m, dict) or not m.get("finished_at") \
                    or m.get("running"):
                continue
            completed += 1
            entry = (m.get("symbols") or {}).get(symbol) or {}
            if entry.get("status") == "ok":
                break
            streak += 1
            if streak >= CHAIN_MISSING_STREAK:
                ok = False
                break
        if completed < CHAIN_MISSING_STREAK:
            ok = True
    except Exception:
        ok = True
    try:
        db.put(c, key, {"ok": ok, "at": now})
    except Exception:
        pass
    return ok


def evaluate_candidate(db, c, underlying, quote, spot, now):
    """G2-G3 for one candidate contract on its consulted quote (G1,
    the premium cap, removed 2026-11-12 — see module docstring).
    Returns (ok, reason_token_or_None, details). Quote must already
    have passed the freshness standard; this set is price/geometry.
    Details carry the values behind each decision — ask (the
    premium), spread, range20, ask/spot ratio — so rejection
    diagnostics record them (2026-11-12 instrumentation)."""
    ask = _num((quote or {}).get("ask"))
    bid = _num((quote or {}).get("bid"))
    details = {"ask": ask, "bid": bid, "range20": None,
               "spread": None, "ask_spot_ratio": None}
    if ask is None or bid is None:
        return False, "movement_ineligible", details
    details["spread"] = round(ask - bid, 4)
    # G2 — quoted spread, relative with a nickel floor.
    if ask - bid > max(SPREAD_PCT_OF_ASK * ask, SPREAD_FLOOR):
        return False, "spread_too_wide", details
    # G3 — movement eligibility: history, chain coverage, range, and
    # the premium-to-movement sanity ratio.
    r20 = range20(db, c, underlying, now)
    details["range20"] = r20
    if r20 is None or r20 < RANGE20_MIN:
        return False, "movement_ineligible", details
    if not chain_coverage_ok(db, c, underlying, now):
        return False, "movement_ineligible", details
    spot = _num(spot)
    if spot is not None and spot > 0:
        details["ask_spot_ratio"] = round(ask / spot, 6)
    if spot is None or spot <= 0 or ask / spot > r20 / 2.0:
        return False, "movement_ineligible", details
    details["planned_max_loss"] = planned_max_loss(ask)
    return True, None, details


def _record_planned(t):
    planned = _num(t.get("planned_max_loss"))
    if planned is not None:
        return planned
    entry = _num(t.get("entry"))
    if entry is not None:
        # Legacy rows predate the stamp: entry is ask + 1c slippage.
        return planned_max_loss(max(entry - 0.01, 0.0)) or 0.0
    return 0.0


def check_caps(db, c, underlying, planned, now):
    """G4 book caps for a seated booking. Returns (ok, token)."""
    try:
        trades = db.prefix(c, BOOK_PREFIX)
    except Exception:
        trades = {}
    today = day(now)
    name = str(underlying or "").upper()
    open_book = 0
    name_today = 0
    spent = 0.0
    for t in trades.values():
        if not isinstance(t, dict) or t.get("kind") != "scanner":
            continue
        if t.get("status") == "open":
            open_book += 1
            if str(t.get("underlying") or "").upper() == name:
                return False, "name_position_open"
        entered = t.get("entered_at")
        if t.get("entry") is not None and entered \
                and day(entered) == today:
            if str(t.get("underlying") or "").upper() == name:
                name_today += 1
            spent += _record_planned(t)
    if name_today >= MAX_PER_NAME_DAY:
        return False, "name_daily_cap"
    if open_book >= MAX_OPEN_BOOK:
        return False, "book_open_cap"
    if spent + (_num(planned) or 0.0) > DAILY_RISK_BUDGET:
        return False, "daily_risk_budget"
    return True, None
