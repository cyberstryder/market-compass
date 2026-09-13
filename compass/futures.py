"""MES/MNQ session clock, customary quarterly roll and versioned research ranges."""
from datetime import datetime, date, time as wall, timedelta
from functools import lru_cache
import pandas as pd
from .market import CT, calendar, session, dedup


def at(d, hour, minute=0):
    return datetime.combine(d, wall(hour, minute), CT).timestamp()


@lru_cache(maxsize=100)
def hours_for(d):
    cal = calendar("CMES")
    label = cal.date_to_session(pd.Timestamp(d), direction="next")
    trading_date = label.date()
    start = cal.session_open(label).timestamp()
    # CMES's general calendar closes at 17 CT on regular days. MES/MNQ close at 16.
    end = min(cal.session_close(label).timestamp(), at(trading_date, 16))
    flatten = min(end - 60, at(trading_date, 15, 45))
    return {"day": trading_date.isoformat(), "open": start, "close": end,
        "flatten_at": flatten, "entry_end": min(flatten - 900, at(trading_date, 15, 15)),
        "halt_start": at(trading_date, 15, 15), "halt_end": at(trading_date, 15, 30)}


def futures_session(now):
    local = datetime.fromtimestamp(now, CT)
    requested = local.date() + timedelta(days=local.hour >= 17)
    result = dict(hours_for(requested.isoformat()))
    result["is_open"] = result["open"] <= now < result["close"] and not result["halt_start"] <= now < result["halt_end"]
    result["entry_open"] = result["is_open"] and now < result["entry_end"]
    return result


def risk_day(now):
    return futures_session(now)["day"]


def lead_contract(root, trading_day):
    d = date.fromisoformat(trading_day)
    for year in (d.year, d.year + 1):
        for month, code in ((3, "H"), (6, "M"), (9, "U"), (12, "Z")):
            first = date(year, month, 1)
            third_friday = first + timedelta(days=(4-first.weekday()) % 7 + 14)
            roll = third_friday - timedelta(days=4)
            if d < roll:
                return {"root": root, "raw_symbol": f"{root}{code}{year%10}",
                    "chart_symbol": f"{root}{code}{year}", "contract_year": year,
                    "contract_month": month, "next_roll_date": roll.isoformat()}
    raise ValueError("No quarterly contract selected")


def selection(aliases, now):
    session_info = futures_session(now)
    return [{**lead_contract(alias.split(".")[0], session_info["day"]),
        "configured_symbol": alias, "trading_day": session_info["day"],
        "policy": "CME customary quarterly roll, effective for the Monday trading session; explicit raw contract, no price adjustment"}
        for alias in aliases]


def prior_rth(trading_day):
    cal = calendar("XNYS")
    d = pd.Timestamp(trading_day) - pd.Timedelta(days=1)
    label = cal.date_to_session(d, direction="previous")
    return label.date().isoformat(), cal.session_open(label).timestamp(), cal.session_close(label).timestamp()


def future_levels(rows, asof, coverage=None):
    rows = dedup(rows, asof)
    h = futures_session(asof)
    d = date.fromisoformat(h["day"])
    rth = session(h["day"])
    use_rth = bool(rth and asof >= rth[0])
    range_start = rth[0] if use_rth else h["open"]
    current = [r for r in rows if h["open"] <= r["ts"] < min(h["close"], asof)]
    opening = [r for r in current if range_start <= r["ts"] < range_start+900]
    prior_day, previous_start, previous_end = prior_rth(h["day"])
    prior = [r for r in rows if previous_start <= r["ts"] < previous_end]
    overnight_end = rth[0] if rth else at(d, 8, 30)
    overnight = [r for r in current if r["ts"] < overnight_end]
    expected = set(range(int(range_start), int(range_start+900), 60))
    out = {"asof": asof, "day": h["day"], "session_open": range_start,
        "session_close": h["entry_end"]+1800, "flatten_at": h["flatten_at"],
        "futures_session_open": h["open"], "futures_session_close": h["close"],
        "entry_end": h["entry_end"], "range_name": "RTH" if use_rth else "Globex",
        "rule_version": "futures-orb15-v2", "bars": len(current),
        "or_complete": {int(r["ts"]) for r in opening} == expected and asof >= range_start+900,
        "prior_day": prior_day, "prior_bars": len(prior),
        "prior_complete": len(prior) == round((previous_end-previous_start)/60)}
    if coverage and coverage.get("verified") and coverage.get("day")==prior_day and len(prior)==coverage.get("record_count"):
        out.update(prior_complete=True,prior_history_basis="Provider record count verified; zero-trade intervals omitted")
    for label, group in (("prior", prior), ("overnight", overnight), ("or", opening)):
        if group:
            out[label+"_high"] = max(r["payload"]["h"] for r in group)
            out[label+"_low"] = min(r["payload"]["l"] for r in group)
    if prior: out["prior_close"] = prior[-1]["payload"]["c"]
    if current:
        volume = sum(r["payload"]["v"] for r in current)
        out.update(price=current[-1]["payload"]["c"], price_asof=current[-1]["ts"]+60,
            vwap=sum(r["payload"]["c"]*r["payload"]["v"] for r in current)/volume if volume else None,
            vwap_method="Volume-weighted minute-close approximation")
    recent = current[-15:]
    if len(recent) == 15 and all(b["ts"]-a["ts"] == 60 for a,b in zip(recent,recent[1:])):
        out["atr14"] = sum(max(b["payload"]["h"]-b["payload"]["l"],abs(b["payload"]["h"]-a["payload"]["c"]),
            abs(b["payload"]["l"]-a["payload"]["c"])) for a,b in zip(recent,recent[1:]))/14
    return out
