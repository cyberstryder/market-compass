"""Separate source freshness, process liveness, and exchange session state."""
from datetime import datetime
from .market import fresh, CT


STREAMS = {"alpaca_stocks": "equities", "databento_futures": "futures", "option_stream": "equities"}
COLLECTORS = set(STREAMS) | {"alpaca_history", "futures_history", "option_chain", "tradermatrix", "tradermatrix_flow",
    "project_morning", "project_smoothers", "research", "secondary_data"}


def clock(value):
    return datetime.fromtimestamp(value, CT).isoformat() if value is not None else None


def decorate_health(items, workers, markets, now):
    result = []
    for item in items:
        h = {**item}
        if h["name"] in {"option_chain", "tradermatrix"}:
            h["source_ts"] = None  # Per-symbol/row clocks are displayed separately from HTTP checks.
        h["source_asof"] = clock(h.get("source_ts"))
        h["age"] = round(now - h["source_ts"], 1) if h.get("source_ts") is not None else None
        h["check_age"] = round(now - h.get("checked_at", 0), 1)
        role = "collector" if h["name"] in COLLECTORS or h["name"].startswith("databento_") else "engine" if h["name"] in {"engine", "discord", "secondary"} else "web"
        worker = workers.get("worker:" + role) or workers.get("worker:all")
        h["heartbeat_age"] = round(now - worker["at"], 1) if worker else None
        h["recorded_status"] = h["status"]
        if h["status"] in {"not_configured", "disabled"}:
            result.append(h)
            continue
        if not worker or h["heartbeat_age"] > 20:
            h.update(status="stale", detail=role + " worker heartbeat missing or older than 20s; " + h["detail"])
        elif h["status"] == "error":
            pass  # Exchange closure must never hide an actual provider rejection.
        elif h["name"] in STREAMS or h["name"].startswith("databento_"):
            if not markets[STREAMS.get(h["name"], "futures")]:
                h.update(status="market_closed", detail="Session closed; live quote validation resumes when it opens. " + h["detail"])
            elif h["age"] is None:
                h.update(status="waiting", detail="Session open; no source event observed yet. " + h["detail"])
            elif h["age"] > 20 or h["age"] < -1:
                h.update(status="stale", detail="Session open; source events are not current. " + h["detail"])
        elif h["name"] in {"option_chain", "tradermatrix", "tradermatrix_flow", "alpaca_history", "futures_history", "engine", "secondary", "secondary_data", "project_morning", "project_smoothers"}:
            limit = {"alpaca_history": 3900,"futures_history":3900, "option_chain": 300, "tradermatrix": 180,"tradermatrix_flow":180, "engine": 20,
                "secondary": 20, "secondary_data": 45, "project_morning": 25, "project_smoothers": 25}[h["name"]]
            if h["check_age"] > limit:
                h.update(status="stale", detail="Worker is alive but this task has stopped reporting. " + h["detail"])
        result.append(h)
    return result


def quote_checks(stocks, futures, quotes, mappings, markets, now,selected=None):
    rows = []
    for alias in (*stocks, *futures):
        future = alias in futures
        target=next((t for t in selected or [] if t["configured_symbol"]==alias),None)
        ids = {m["payload"]["instrument_id"] for m in mappings if m["payload"].get("input") == alias}
        matches = [(symbol, q) for symbol, q in quotes.items()
            if symbol == alias or symbol.startswith(alias + "@") or (future and q.get("instrument_id") in ids)]
        if target:
            matches=[(symbol,q) for symbol,q in quotes.items() if symbol.split("@")[0]==target["raw_symbol"]]
        symbol, quote = max(matches, key=lambda x: x[1].get("ts", 0)) if matches else (target["raw_symbol"] if target else alias, None)
        valid = fresh(quote, now)
        opened = markets["futures" if future else "equities"]
        status = "market_closed" if not opened else "ready" if valid else "missing" if quote is None else "blocked"
        rows.append({"symbol": symbol, "configured_symbol": alias, "status": status,
            "source": quote.get("source") if quote else None,
            "source_ts": quote.get("ts") if quote else None,
            "source_asof": clock(quote.get("ts")) if quote else None,
            "age": round(now - quote["ts"], 1) if quote else None,
            "quote_ready": bool(opened and valid),
            "detail": "Fresh two-sided quote required for a simulated fill; spread, setup and risk gates also apply."})
    return rows
