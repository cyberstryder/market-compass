"""Pinned pure stock research calculations from Morning source f459740a452a3cdf17fbb62af3d38edc1fc63c4d."""

METHOD = "research_v1"

def stock_coverage(signal, bars, expected, now):
    bars = [b for b in bars if signal['signal_at_ms'] <= b['open_at_ms'] < signal['signal_at_ms'] + 60 * 60000
            and b['open_at_ms'] + 60000 <= now]
    elapsed = max(0, min(60, (now - signal["signal_at_ms"]) // 60000))
    present = {(b["open_at_ms"] - signal["signal_at_ms"]) // 60000 + 1 for b in bars}
    missing = sorted(set(range(1, elapsed + 1)) - present)
    status = ("not_recorded" if not expected else "disabled" if not signal["settings"]["checkpoints"]
              else "complete" if len(present) == 60 else "incomplete" if now > signal["signal_at_ms"] + 3720000 else "collecting")
    return {"status": status, "recorded": len(present), "expected": 60 if expected else 0,
            "elapsed_minutes": elapsed, "unreceived_elapsed_minutes": missing,
            "volume_missing": sum(b["volume"] is None for b in bars)}

def stock_exit(signal, bars, horizon=60, target=None, stop=None):
    """Bracket ambiguity is retained. Opening gaps precede the candle's high/low."""
    entry = signal["price"]
    by_minute = {(b["open_at_ms"] - signal["signal_at_ms"]) // 60000 + 1: b for b in bars}
    base = {"asset": "stock", "rule": f"close_{horizon}m" if target is None else f"target_{target:g}_stop_{stop:g}_60m",
            "horizon_min": horizon, "target_pct": target, "stop_pct": stop, "method": METHOD}
    upper, lower = entry * (1 + (target or 0) / 100), entry * (1 - (stop or 0) / 100)
    def result(b, minute, price, reason):
        return dict(base, status="modeled", exit_reason=reason, exit_minute=minute,
                    exit_bar_open_ms=b["open_at_ms"], exit_bar_close_ms=b["open_at_ms"] + 60000,
                    price=price, return_pct=(price / entry - 1) * 100)
    for minute in range(1, horizon + 1):
        b = by_minute.get(minute)
        if not b:
            return dict(base, status="incomplete", missing_minute=minute)
        if target is not None:
            if b["open"] <= lower:
                return result(b, minute, b["open"], "stop_at_open")
            if b["open"] >= upper:
                return result(b, minute, upper, "target_at_open")  # Limit price, no favorable gap credit.
            hit_target, hit_stop = b["high"] >= upper, b["low"] <= lower
            if hit_target and hit_stop:
                return dict(base, status="ambiguous", exit_minute=minute, exit_bar_open_ms=b["open_at_ms"],
                            return_lower_pct=-stop, return_upper_pct=target, reason="both_levels_in_same_candle")
            if hit_target or hit_stop:
                return result(b, minute, upper if hit_target else lower, "target" if hit_target else "stop")
        if minute == horizon:
            return result(b, minute, b["close"], "time_close")

def path_outcome(price, at_ms, bars, horizon, now):
    by_time = {b["open_at_ms"]: b for b in bars if at_ms <= b["open_at_ms"] < at_ms + horizon*60000
               and b["open_at_ms"]+60000 <= now and (b["open_at_ms"]-at_ms) % 60000 == 0}
    selected = [by_time[t] for t in sorted(by_time)]
    elapsed = max(0, min(horizon, (now-at_ms)//60000))
    missing = [i+1 for i in range(elapsed) if at_ms+i*60000 not in by_time]
    complete = len(by_time) == horizon
    peak = max(selected, key=lambda b: b["high"], default=None)
    dip = min(selected, key=lambda b: b["low"], default=None)
    last = by_time.get(at_ms+(horizon-1)*60000)
    return {"horizon_min": horizon, "status": "complete" if complete else "incomplete" if now > at_ms+(horizon+7)*60000 else "collecting",
        "recorded": len(selected), "expected": horizon, "missing_elapsed_minutes": missing,
        "return_pct": (last["close"]/price-1)*100 if complete else None,
        "best_pct": (max(price, peak["high"])/price-1)*100 if peak else None,
        "worst_pct": (min(price, dip["low"])/price-1)*100 if dip else None,
        "peak_bar_open_ms": peak["open_at_ms"] if peak and peak["high"] > price else None,
        "dip_bar_open_ms": dip["open_at_ms"] if dip and dip["low"] < price else None,
        "peak_price": max(price, peak["high"]) if peak else None,
        "dip_price": min(price, dip["low"]) if dip else None,
        "volume_missing": sum(b["volume"] is None for b in selected)}

def extend_baseline(signal, original, link, tapes, now):
    empty = {"status": "not_recorded", "recorded": 0, "expected": 0}
    if not link:
        return original, empty, {}
    r, session = link["record"], link["session"]
    match = (r["at_ms"] == signal["signal_at_ms"] and r["price"] == signal["price"] and r["baseline_setup"] == signal["setup"]
             and r["features"] == signal["features"]
             and all(session[k] == signal[k] for k in ("ticker", "stream_id", "script_version", "logic_mode", "timeframe_min", "settings", "is_test")))
    if not match:
        return original, dict(empty, status="identity_conflict"), {}
    by_time = {b["open_at_ms"]: b for b in original}
    conflicts = []
    fields = ("open", "high", "low", "close", "volume")
    for b in tapes.get(session["session_id"], []):
        t = b["open_at_ms"]
        if not signal["signal_at_ms"] <= t < signal["signal_at_ms"]+180*60000:
            continue
        if t in by_time and any(by_time[t][k] != b[k] for k in fields):
            conflicts.append(t)
        else:
            by_time[t] = b
    # Keep conflicting raw checkpoint candles in their original table, but do
    # not silently choose a price for extended statistics or their history export.
    for t in conflicts:
        by_time.pop(t, None)
    merged = [by_time[t] for t in sorted(by_time)]
    outcomes = {str(h): path_outcome(signal["price"], signal["signal_at_ms"], merged, h, now) for h in (120, 180)}
    coverage = dict(outcomes["180"], record_id=r["record_id"], conflicting_bar_open_ms=conflicts)
    return merged, coverage, outcomes
