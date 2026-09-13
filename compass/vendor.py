"""TraderMatrix's documented flow envelope and observed paid matrix schema.

Vendor units, inventory signs and flow classifications are preserved, not inferred.
Reference: https://www.tradermatrix.pro/developers (REST reference).
"""
from datetime import datetime
from .market import number


def source_time(value):
    if not isinstance(value, str): return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.timestamp() if date.tzinfo is not None else None
    except ValueError:
        return None


def observed_sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def matrix_summary(payload, received):
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("success") is False or not isinstance(data, dict):
        raise ValueError("TraderMatrix matrix envelope changed")
    expirations, rows = data.get("expirations"), data.get("rows")
    if not isinstance(expirations, list) or not 1 <= len(expirations) <= 60 or not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("TraderMatrix matrix dimensions invalid")
    if len(set(expirations)) != len(expirations):
        raise ValueError("TraderMatrix duplicate expirations")
    for expiry in expirations:
        if not isinstance(expiry, str): raise ValueError("TraderMatrix expiration invalid")
        datetime.strptime(expiry, "%Y-%m-%d")
    strikes, seen = [], set()
    for row in rows:
        strike = number(row.get("strike")) if isinstance(row, dict) else None
        if strike is None or strike <= 0 or strike in seen:
            raise ValueError("TraderMatrix strike invalid or repeated")
        seen.add(strike)
        item = {"strike": strike}
        for field in ("gex", "vex", "vexGross"):
            cells = row.get(field, [None] * len(expirations))
            if not isinstance(cells, list) or len(cells) != len(expirations):
                raise ValueError("TraderMatrix matrix cell alignment changed")
            if any(v is not None and number(v) is None for v in cells):
                raise ValueError("TraderMatrix nonnumeric exposure cell")
            item[field + "_cells"] = [number(v) for v in cells]
            item[field] = observed_sum(item[field + "_cells"])
        strikes.append(item)
    summaries = [{"expiry": expiry, **{
        field: observed_sum(row[field + "_cells"][i] for row in strikes)
        for field in ("gex", "vex", "vexGross")}}
        for i, expiry in enumerate(expirations)]
    count = len(strikes) * len(expirations)
    coverage = {field: sum(v is not None for row in strikes for v in row[field + "_cells"])
        for field in ("gex", "vex", "vexGross")}
    stamp = source_time(data.get("snapshotTime"))
    return {"symbol": data.get("underlyingSymbol"), "source": "tradermatrix",
        "source_ts": stamp, "received": received, "cached": payload.get("cached"),
        "spot": number(data.get("spotPrice")), "status": "available" if stamp and coverage["gex"] else "partial",
        "gex": observed_sum(row["gex"] for row in strikes),
        "vex": observed_sum(row["vex"] for row in strikes),
        "expirations": summaries, "strikes": sorted(strikes, key=lambda row: row["strike"]),
        "cells": count, "populated_cells": coverage,
        "methodology": "Vendor GEX/VEX units and position assumptions are unverified; VEX is not equated with the local vanna proxy. Totals sum only populated cells across returned expirations. Strike concentrations are not verified walls or flip levels."}


def flow_page(payload, received):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("TraderMatrix flow envelope changed")
    total, page, size = (number(payload.get(k)) for k in ("total", "page", "pageSize"))
    if any(n is None or n != int(n) for n in (total, page, size)) or total < 0 or page < 1 or not 1 <= size <= 100:
        raise ValueError("TraderMatrix pagination invalid")
    if len(payload["data"]) > size: raise ValueError("TraderMatrix page exceeds declared size")
    rows, rejected = [], 0
    for raw in payload["data"]:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        stamp, premium = source_time(raw.get("tradeTime")), number(raw.get("premium"))
        vendor_id, symbol = raw.get("id"), raw.get("ticker")
        if vendor_id is None or not isinstance(symbol, str) or not symbol or stamp is None or premium is None or premium < 0 or raw.get("isRedacted") is True:
            rejected += 1
            continue
        rows.append({"vendor_id": str(vendor_id), "symbol": symbol, "source_ts": stamp,
            "received": received, "premium": premium, "option_type": raw.get("type"),
            "strike": number(raw.get("strike")), "expiry": raw.get("expiry"),
            "volume": number(raw.get("volume")), "open_interest": number(raw.get("openInterest")),
            "classification": raw.get("tradeType"), "sentiment": raw.get("sentiment"),
            "score": number(raw.get("score")), "description": raw.get("flowDescription"),
            "is_repeat": raw.get("isRepeatFlow"), "oi_confirmation": raw.get("oiConfirmation"),
            "classification_source": "TraderMatrix; not independently verified"})
    aggregates = payload.get("aggregates") if isinstance(payload.get("aggregates"), dict) else {}
    return {"source": "tradermatrix", "received": received, "rows": rows,
        "total": int(total), "page": int(page), "page_size": int(size),
        "raw_count": len(payload["data"]), "rejected": rejected,
        "filters": payload.get("filters", {}),
        "aggregates": {key: number(aggregates.get(key)) for key in ("totalPremium", "bullishPremium", "bearishPremium")}}
