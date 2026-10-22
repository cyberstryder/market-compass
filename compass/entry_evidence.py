"""Compass evidence snapshot for 0DTE scanner entries (audit #8, 2026-10-21).

The pre-enrichment scanner book fired on raw volume breakouts with no Compass
context at all. At entry time this module snapshots the context that already
exists in the store — nothing here fetches or computes a new feed:

- gamma-flip regime      <- gamma_flip:<symbol> (gamma_flip.compute_flip,
                            written by providers on each matrix refresh)
- nearest Apex/GEX walls <- gamma_levels:<symbol> (call/put wall,
                            concentration), matrix_levels:<symbol>
                            (gex/vex concentrations), apex:<symbol> levels
- IV rank                <- iv_rank:<symbol> (iv_rank.get; /api/iv-rank data)
- price vs session VWAP  <- scanner_features:<symbol> / levels:<symbol>,
                            spot from quote:<symbol>
- squeeze state          <- squeeze:latest (squeeze.scan states)
- earnings / news flags  <- intel:earnings:<day> / intel:economic:<day>
                            (TraderMatrix intel calendars, CT day keys)

Evidence only: never a trigger, never blocks an entry. Every section
degrades to None when its source is unavailable, so a snapshot dict is
always returned and its mere presence on a trade record marks the trade
as post-enrichment (see simple.py's 0DTE stats reset).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
VERSION = "0dte-evidence-v1"
NEWS_WINDOW_SEC = 900  # mirrors tradermatrix_intel.should_pause_for_news


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _get(db, c, key, default=None):
    try:
        value = db.get(c, key, default)
    except Exception:
        return default
    return default if value is None else value


def _fmt(value):
    n = _num(value)
    if n is None:
        return "?"
    return ("%f" % n).rstrip("0").rstrip(".") if n % 1 else "%g" % n


def _spot(db, c, underlying, now, price):
    p = _num(price)
    if p and p > 0:
        return p
    q = _get(db, c, "quote:" + underlying, {}) or {}
    if isinstance(q, dict):
        ts = _num(q.get("ts"))
        bid, ask = _num(q.get("bid")), _num(q.get("ask"))
        if bid and ask and bid > 0 and ask > 0 and (ts is None or now - ts <= 300):
            return round((bid + ask) / 2, 4)
    for key in ("gamma_levels:" + underlying, "gamma_flip:" + underlying):
        item = _get(db, c, key, {}) or {}
        p = _num(item.get("spot")) if isinstance(item, dict) else None
        if p and p > 0:
            return p
    return None


def _gamma(db, c, underlying, spot):
    item = _get(db, c, "gamma_flip:" + underlying, {}) or {}
    if not isinstance(item, dict):
        return None
    flip = _num(item.get("flip"))
    if not flip:
        return None
    if spot:
        regime = ("above_flip" if spot > flip else
                  "below_flip" if spot < flip else "at_flip")
        distance_pct = round((spot - flip) / flip * 100, 3)
    else:
        regime = item.get("regime")
        distance_pct = _num(item.get("distance_pct"))
    return {"flip": flip, "regime": regime, "distance_pct": distance_pct}


def _walls(db, c, underlying, spot):
    """Nearest wall above/below spot across GEX walls and Apex levels."""
    if not spot:
        return None
    candidates = []
    levels_item = _get(db, c, "gamma_levels:" + underlying, {}) or {}
    if isinstance(levels_item, dict) and levels_item.get("status") == "ok":
        lv = levels_item.get("levels") or {}
        for field, kind in (("call_wall", "gex_call_wall"),
                            ("put_wall", "gex_put_wall"),
                            ("concentration", "gex_concentration")):
            p = _num(lv.get(field))
            if p:
                candidates.append((p, kind))
    for key in ("matrix_levels:" + underlying, "apex:" + underlying):
        item = _get(db, c, key, {}) or {}
        if not isinstance(item, dict):
            continue
        for level in item.get("levels") or []:
            if isinstance(level, dict):
                p = _num(level.get("price"))
                if p:
                    candidates.append((p, str(level.get("kind") or "level")))
    if not candidates:
        return None

    def pick(target):
        if target is None:
            return None
        kind = next(k for p, k in candidates if p == target)
        return {"price": target, "kind": kind,
                "distance": round(abs(target - spot), 2)}

    above = min((p for p, _ in candidates if p > spot), default=None)
    below = max((p for p, _ in candidates if p < spot), default=None)
    return {"above": pick(above), "below": pick(below)}


def _iv_rank(db, c, underlying):
    try:
        from .iv_rank import get as stored_iv_rank
        row = stored_iv_rank(db, c, underlying)
    except Exception:
        row = {}
    rank = _num((row or {}).get("iv_rank"))
    if rank is None:
        return None
    return {"rank": rank, "iv": _num(row.get("iv"))}


def _vwap(db, c, underlying, spot):
    if not spot:
        return None
    vwap = None
    for key in ("scanner_features:" + underlying, "levels:" + underlying):
        item = _get(db, c, key, {}) or {}
        if isinstance(item, dict):
            vwap = _num(item.get("vwap"))
        if vwap:
            break
    if not vwap:
        return None
    return {"vwap": vwap,
            "position": ("above" if spot > vwap else
                         "below" if spot < vwap else "at"),
            "distance": round(spot - vwap, 2)}


def _squeeze(db, c, underlying):
    latest = _get(db, c, "squeeze:latest", {}) or {}
    if not isinstance(latest, dict):
        return None
    state = (latest.get("states") or {}).get(underlying)
    if not isinstance(state, dict) or state.get("state") is None:
        return None
    return {"state": state.get("state"), "direction": state.get("direction"),
            "bars": state.get("squeeze_bars")}


def _calendar(db, c, underlying, now):
    day_str = datetime.fromtimestamp(now, CT).strftime("%Y-%m-%d")
    earnings_rec = _get(db, c, "intel:earnings:" + day_str)
    economic_rec = _get(db, c, "intel:economic:" + day_str)
    if earnings_rec is None and economic_rec is None:
        return None
    out = {"earnings_today": False, "earnings_date": None,
           "earnings_time": None, "high_impact_news_today": False,
           "news_window_now": False}
    if isinstance(earnings_rec, dict):
        events = [ev for ev in earnings_rec.get("opra_earnings") or []
                  if isinstance(ev, dict) and ev.get("symbol") == underlying]
        today_ev = next((ev for ev in events if ev.get("date") == day_str),
                        None)
        chosen = today_ev or (events[0] if events else None)
        if chosen:
            out["earnings_today"] = today_ev is not None
            out["earnings_date"] = chosen.get("date")
            out["earnings_time"] = chosen.get("time")
    if isinstance(economic_rec, dict):
        for ev in economic_rec.get("high_impact") or []:
            if not isinstance(ev, dict) or ev.get("date") != day_str:
                continue
            out["high_impact_news_today"] = True
            t = str(ev.get("time") or "")
            # Same time convention as tradermatrix_intel.should_pause_for_news:
            # bare "HH:MM" 24h strings only.
            if ":" in t and "AM" not in t and "PM" not in t:
                try:
                    hh, mm = map(int, t.split(":")[:2])
                    event_ts = datetime.fromtimestamp(now, CT).replace(
                        hour=hh, minute=mm, second=0,
                        microsecond=0).timestamp()
                except (TypeError, ValueError):
                    continue
                if abs(now - event_ts) <= NEWS_WINDOW_SEC:
                    out["news_window_now"] = True
    return out


def build(db, c, underlying, now, price=None):
    """Evidence snapshot for `underlying` at `now`. Never raises.

    `price` is the underlying reference price when the caller already has
    one (the scanner's signal price); otherwise the spot is resolved from
    the freshest stored quote / gamma spot.
    """
    snap = {"version": VERSION, "at": now, "underlying": underlying,
            "price": None, "gamma": None, "walls": None, "iv_rank": None,
            "vwap": None, "squeeze": None, "calendar": None}
    if not underlying:
        return snap
    try:
        spot = _spot(db, c, underlying, now, price)
    except Exception:
        spot = None
    snap["price"] = spot
    for field, fn, args in (
            ("gamma", _gamma, (db, c, underlying, spot)),
            ("walls", _walls, (db, c, underlying, spot)),
            ("vwap", _vwap, (db, c, underlying, spot)),
            ("iv_rank", _iv_rank, (db, c, underlying)),
            ("squeeze", _squeeze, (db, c, underlying)),
            ("calendar", _calendar, (db, c, underlying, now))):
        try:
            snap[field] = fn(*args)
        except Exception:
            snap[field] = None
    return snap


def alert_lines(snap):
    """Compact Discord lines rendering a snapshot for the alert text."""
    if not isinstance(snap, dict):
        return []
    lines = []
    bits = []
    gamma = snap.get("gamma") or {}
    if gamma.get("regime"):
        label = {"above_flip": "above flip (dampened)",
                 "below_flip": "below flip (amplified)",
                 "at_flip": "at the flip"}.get(
                     gamma["regime"],
                     str(gamma["regime"]).replace("_", " "))
        bits.append("Gamma %s · flip %s" % (label, _fmt(gamma.get("flip"))))
    iv = snap.get("iv_rank") or {}
    if iv.get("rank") is not None:
        bits.append("IV rank %.0f" % iv["rank"])
    vw = snap.get("vwap") or {}
    if vw.get("vwap") is not None and vw.get("position"):
        bits.append("Price %s VWAP (%+.2f)" % (vw["position"],
                                               vw.get("distance") or 0))
    if bits:
        lines.append("Evidence: " + " · ".join(bits))
    walls = snap.get("walls") or {}
    wbits = []
    for side in ("above", "below"):
        w = walls.get(side)
        if w:
            wbits.append("%s %s %s (%.2f away)" % (
                side, str(w.get("kind") or "wall").replace("_", " "),
                _fmt(w.get("price")), w.get("distance") or 0))
    if wbits:
        lines.append("Walls: " + " · ".join(wbits))
    notes = []
    sq = snap.get("squeeze") or {}
    if sq.get("state") and sq["state"] != "none":
        notes.append("Squeeze %s%s" % (
            sq["state"],
            (" " + str(sq["direction"])) if sq.get("direction") else ""))
    cal = snap.get("calendar") or {}
    if cal.get("earnings_today"):
        notes.append("Earnings today" + (
            " (%s)" % cal["earnings_time"] if cal.get("earnings_time")
            else ""))
    if cal.get("news_window_now"):
        notes.append("High-impact news window now")
    elif cal.get("high_impact_news_today"):
        notes.append("High-impact news today")
    if notes:
        lines.append("Flags: " + " · ".join(notes))
    if not lines:
        lines.append("Evidence: no Compass context available at entry")
    return lines
