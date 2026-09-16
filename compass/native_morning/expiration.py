"""Forward-only expiration research. Quote measurements are not fills or recommendations."""
import math
from collections import Counter, defaultdict
from statistics import median

from sqlalchemy import select

VERSION = "atm_expiration_comparison_v1"
VALID = {"available", "wide_spread"}
HORIZONS = (5, 15, 30, 60)
COST_MODEL = {"version": "ask_bid_fees_v1", "fee_per_side": 0.65,
              "stress_slippage_per_share_per_side": 0.01}
LABELS = {"baseline": "Current contract", "zero_dte": "0DTE", "near_dte": "1–3 DTE"}
NOTES = ("Forward observations only; no historical initial quotes are backfilled. "
         "Closest standard CALL strike, lower strike on ties; earliest listed expiry in each window. "
         "DTE means calendar days. 1–3 DTE shares the baseline when eligible, not independent evidence. "
         "Entry ask to exit bid, $0.65/side assumed fees; stress adds $0.01/share/side slippage. "
         "One-contract dollars and premium-normalized returns are not equal-risk positions; inspect delta and moneyness. "
         "Missing observations are excluded, never losses. Fixed-exit comparisons use the same alerts across all four horizons; "
         "paired entry and exit quote timestamps must be within five seconds. "
         "Best observed exit is hindsight at sampled minutes, not a tradable exit rule. No strategy promotion or orders.")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quality(quote):
    if quote.get("status") not in VALID:
        return {"status": "unavailable", "reason": quote.get("reason", "quote_unavailable")}
    bid, ask = quote.get("bid"), quote.get("ask")
    if not (finite(bid) and finite(ask) and 0 < bid <= ask):
        return {"status": "unavailable", "reason": "invalid_quote"}
    model = quote.get("cost_model", COST_MODEL)
    fees = 2 * model["fee_per_side"]
    spread = ask - bid
    delta = (quote.get("greeks") or {}).get("delta")
    stock = quote.get("reference_stock_price")
    strike = (quote.get("contract") or {}).get("strike")
    return {"status": quote["status"], "premium_at_ask": ask * 100,
            "spread_per_contract": spread * 100, "spread_pct": spread / ((ask + bid) / 2) * 100,
            "round_trip_fees": fees, "spread_and_fees": spread * 100 + fees,
            "approx_stock_cost_hurdle": (spread + fees / 100) / delta if finite(delta) and 0 < delta <= 1 else None,
            "delta": delta if finite(delta) else None,
            "moneyness_pct": (strike / stock - 1) * 100 if finite(stock) and stock > 0 and finite(strike) else None,
            "hurdle_basis": "Constant-delta approximation; ignores changing IV, theta and gamma. Not a target."}


def net_exit(reference, quote):
    if reference.get("status") not in VALID or quote.get("status") not in VALID:
        return None
    ask, bid = reference.get("ask"), quote.get("bid")
    if not (finite(ask) and finite(bid) and ask > 0 and bid > 0):
        return None
    if quote.get("quote_at_ms", 0) <= reference.get("quote_at_ms", 0):
        return None
    if (quote.get("contract") or {}).get("symbol") != (reference.get("contract") or {}).get("symbol"):
        return None
    model = reference.get("cost_model", COST_MODEL)
    fee = model["fee_per_side"]
    gross = (bid - ask) * 100
    net = gross - 2 * fee
    return {"net_pnl": net, "gross_pnl": gross, "net_return_pct": net / (ask * 100 + fee) * 100,
            "stress_net_pnl": net - model["stress_slippage_per_share_per_side"] * 200,
            "entry_ask": ask, "exit_bid": bid, "quote_at_ms": quote["quote_at_ms"],
            "cost_model": dict(model)}


def summarize_signal(reference, baseline_samples, zero_samples, now):
    comparison = reference.get("expiration_comparison")
    if not comparison:
        return {"status": "not_collected", "variants": {}}
    variants = comparison["variants"]
    result = {"status": "observing", "version": comparison["version"], "variants": {}}
    for key in LABELS:
        ref = variants[key]
        samples = zero_samples if key == "zero_dte" else baseline_samples if ref.get("contract") else []
        measured, states, slots = {}, Counter(), {}
        for s in samples:
            q = s.get("quote") or {}
            states[q.get("status", s["status"])] += 1
            slots[s["minute"]] = s
            if s["due_at_ms"] <= now and q.get("sampled_at_ms", now + 1) <= now:
                value = net_exit(ref, q)
                if value is not None:
                    measured[s["minute"]] = value
        exits = {}
        for h in HORIZONS:
            if h in measured:
                exits[str(h)] = dict(measured[h], status="measured")
            else:
                s = slots.get(h, {})
                q = s.get("quote") or {}
                exits[str(h)] = {"status": "unavailable", "reason": ref.get("reason") if ref.get("status") not in VALID
                    else q.get("return_unavailable_reason", q.get("reason", s.get("status", "not_scheduled")))}
        best = max(measured, key=lambda h: measured[h]["net_pnl"]) if measured else None
        result["variants"][key] = {"label": LABELS[key], "reference": ref, "quality": quality(ref),
            "alias_of": ref.get("alias_of"), "samples": dict(states), "valid_minutes": len(measured),
            "fixed_exits": exits, "best_observed": dict(measured[best], minute=best, hindsight_only=True) if best else None,
            "giveback_to_60m": measured[best]["net_pnl"] - measured[60]["net_pnl"] if best and 60 in measured else None}
    return result


def aggregates(reports):
    reports = list(reports)
    pairs = []
    for left, right in (("baseline", "zero_dte"), ("near_dte", "zero_dte")):
        complete = [r for r in reports if all(
            r.get("variants", {}).get(k, {}).get("fixed_exits", {}).get(str(h), {}).get("status") == "measured"
            for k in (left, right) for h in HORIZONS)]
        shared = [r for r in complete if
            abs(r["variants"][left]["reference"]["quote_at_ms"] - r["variants"][right]["reference"]["quote_at_ms"]) <= 5000
            and all(abs(r["variants"][left]["fixed_exits"][str(h)]["quote_at_ms"] -
                        r["variants"][right]["fixed_exits"][str(h)]["quote_at_ms"]) <= 5000 for h in HORIZONS)]
        exits = []
        for h in HORIZONS:
            stats = {}
            for k in (left, right):
                values = [r["variants"][k]["fixed_exits"][str(h)] for r in shared]
                stats[k] = {"n": len(values), "median_net_pnl": median(v["net_pnl"] for v in values) if values else None,
                    "mean_net_pnl": sum(v["net_pnl"] for v in values) / len(values) if values else None,
                    "median_net_return_pct": median(v["net_return_pct"] for v in values) if values else None,
                    "positive_pct": sum(v["net_pnl"] > 0 for v in values) / len(values) * 100 if values else None}
            exits.append({"minute": h, "variants": stats})
        pairs.append({"left": left, "right": right, "paired": len(shared), "excluded": len(reports) - len(shared),
                      "timestamp_skew_excluded": len(complete) - len(shared), "max_quote_skew_ms": 5000,
                      "fixed_exits": exits})
    return {"version": VERSION, "signals": len(reports), "collected": sum(bool(r.get("variants")) for r in reports),
            "pairs": pairs, "notes": NOTES, "execution_eligible": False}


def load_zero_samples(conn, ids):
    import json
    from .store import expiration_samples
    samples = defaultdict(list)
    if ids:
        for r in conn.execute(select(expiration_samples).where(expiration_samples.c.signal_id.in_(ids))
                              .order_by(expiration_samples.c.minute)).mappings():
            samples[r["signal_id"]].append({"minute": r["minute"], "status": r["status"], "due_at_ms": r["due_at_ms"],
                                           "quote": json.loads(r["quote_json"]) if r["quote_json"] else None})
    return samples
