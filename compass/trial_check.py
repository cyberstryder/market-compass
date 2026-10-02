#!/usr/bin/env python3
"""Read-only validation: did Compass generate trials / flow-study receipts /
tape confirmations for HOOD and COIN on the days before their 2026-10-02 moves?

Usage (Railway engine shell, where DATABASE_URL exists):
    python3 /tmp/trial_check.py HOOD COIN --days 2026-09-30,2026-10-01

SELECTs only. Answers: would our flow-detection pipeline have caught the
institutional call flow that preceded the HOOD/COIN rips?
"""
import os, sys, json
from datetime import datetime, timezone
from sqlalchemy import create_engine, text

def day_bounds(day):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp(), d.timestamp() + 86400

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = ["2026-09-30", "2026-10-01"]
    for a in sys.argv[1:]:
        if a.startswith("--days"):
            days = a.split("=", 1)[1].split(",") if "=" in a else sys.argv[sys.argv.index(a)+1].split(",")
    symbols = [s.upper() for s in args] or ["HOOD", "COIN"]

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    url = url.replace("postgres://", "postgresql+psycopg://", 1).replace(
        "postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, connect_args={"connect_timeout": 10})

    lo = min(day_bounds(d)[0] for d in days)
    hi = max(day_bounds(d)[1] for d in days)
    syms = tuple(symbols)

    with engine.connect() as c:
        print("== discovery_trials_v1 (flow-based discovery cohorts) ==")
        rows = c.execute(text("""
            SELECT symbol, to_timestamp(created) AT TIME ZONE 'America/Chicago' AS ct,
                   status,
                   payload->>'side' AS side, payload->>'kind' AS kind,
                   payload->>'score' AS score
            FROM discovery_trials_v1
            WHERE symbol IN :syms AND created >= :lo AND created < :hi
            ORDER BY created"""),
            {"syms": syms, "lo": lo, "hi": hi}).mappings().all()
        print(f"rows: {len(rows)}")
        for r in rows[:30]:
            print(f"  {r['ct']:%m-%d %H:%M} {r['symbol']:5} {r['status']:10} side={r['side']} kind={r['kind']} score={r['score']}")

        print("== setup_trials_v1 (setup-study trials -> tape-confirmed input) ==")
        rows = c.execute(text("""
            SELECT symbol, side, status,
                   to_timestamp(started) AT TIME ZONE 'America/Chicago' AS ct,
                   payload->>'strategy' AS strategy
            FROM setup_trials_v1
            WHERE symbol IN :syms AND started >= :lo AND started < :hi
            ORDER BY started"""),
            {"syms": syms, "lo": lo, "hi": hi}).mappings().all()
        print(f"rows: {len(rows)}")
        for r in rows[:30]:
            print(f"  {r['ct']:%m-%d %H:%M} {r['symbol']:5} {r['side']:6} {r['status']:10} {r['strategy']}")

        print("== tm_flow_study_v1 (per-print flow assessments) ==")
        rows = c.execute(text("""
            SELECT symbol, day, direction, tm_score, compass_score, status, origin
            FROM tm_flow_study_v1
            WHERE symbol IN :syms AND day = ANY(:days)
            ORDER BY day, tm_score DESC NULLS LAST"""),
            {"syms": syms, "days": days}).mappings().all()
        print(f"rows: {len(rows)}")
        for r in rows[:30]:
            print(f"  {r['day']} {r['symbol']:5} {r['direction']:8} tm={r['tm_score']} compass={r['compass_score']} {r['status']:10} {r['origin']}")

        print("== tape_confirm:* state keys (confirmation verdicts) ==")
        rows = c.execute(text("""
            SELECT key, value->>'verdict' AS verdict, value->>'symbol' AS sym,
                   value->>'side' AS side, (value->>'max_score')::float AS max_score,
                   to_timestamp((value->>'evaluated_at')::float) AT TIME ZONE 'America/Chicago' AS ct
            FROM state
            WHERE key LIKE 'tape_confirm:%'
              AND (value->>'symbol') IN :syms
              AND (value->>'evaluated_at')::float >= :lo
              AND (value->>'evaluated_at')::float < :hi
            ORDER BY max_score DESC NULLS LAST"""),
            {"syms": syms, "lo": lo, "hi": hi}).mappings().all()
        print(f"rows: {len(rows)}")
        for r in rows[:30]:
            print(f"  {r['ct']:%m-%d %H:%M} {r['sym']:5} {r['side']:6} max_score={r['max_score']} verdict={r['verdict']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
