"""Read-only unusual-flow lookup for arbitrary symbols/days.

Usage (Railway engine shell, where DATABASE_URL exists):
    python3 /tmp/flow_lookup.py HOOD COIN --days 2026-09-30,2026-10-01

Prints the largest-premium TraderMatrix flow prints per symbol/day.
Read-only: SELECTs only, no writes.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

CT = ZoneInfo("America/Chicago")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+", help="tickers, e.g. HOOD COIN")
    ap.add_argument("--days", default="2026-09-30,2026-10-01",
                    help="comma-separated YYYY-MM-DD day labels")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--min-premium", type=float, default=100000)
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    engine = create_engine(url, connect_args={"connect_timeout": 10})

    symbols = [s.upper() for s in args.symbols]
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    out = {"symbols": symbols, "days": days, "rows": []}

    with engine.connect() as c:
        for day in days:
            rows = c.execute(text("""
                SELECT source_ts, payload FROM flow_records
                WHERE day = :day
                  AND (payload->>'premium')::float >= :minp
                ORDER BY (payload->>'premium')::float DESC
            """), {"day": day, "minp": args.min_premium}).mappings().all()
            # filter symbols in Python (payload JSON; avoids PG json operator quirks)
            per = {}
            for r in rows:
                p = r["payload"] or {}
                if str(p.get("symbol", "")).upper() not in symbols:
                    continue
                per.setdefault(str(p.get("symbol")).upper(), []).append((r["source_ts"], p))
            for sym in symbols:
                for ts, p in per.get(sym, [])[:args.limit]:
                    t = datetime.fromtimestamp(ts, tz=CT).strftime("%m-%d %H:%M CT")
                    out["rows"].append({
                        "day": day, "time": t, "symbol": sym,
                        "sentiment": p.get("sentiment"),
                        "type": p.get("option_type"),
                        "strike": p.get("strike"), "expiry": p.get("expiry"),
                        "premium": round(p.get("premium") or 0),
                        "score": p.get("score"),
                        "vol": p.get("volume"), "oi": p.get("open_interest"),
                        "repeat": p.get("is_repeat"),
                        "desc": (p.get("description") or "")[:120],
                    })
    engine.dispose()
    # human-readable table to stdout
    print("time            sym  sent      type   strike  expiry      premium  score  vol/oi  desc")
    for r in out["rows"]:
        print("%-15s %-4s %-9s %-6s %-7s %-10s %9d  %-5s %s  %s" % (
            r["time"], r["symbol"], str(r["sentiment"])[:9],
            str(r["type"])[:6], r["strike"], str(r["expiry"])[:10],
            r["premium"], r["score"],
            "%s/%s" % (r["vol"], r["oi"]), r["desc"]))
    if not out["rows"]:
        print("(no prints >= $%d for %s on %s)" % (
            args.min_premium, ",".join(symbols), ",".join(days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
