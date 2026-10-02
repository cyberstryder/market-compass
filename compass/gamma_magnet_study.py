"""Gamma-magnet hit-rate study (read-only).

Question: when the morning GEX snapshot shows a dominant positive-gamma
strike near spot, does price get drawn to it before the close?

Method, per symbol-day:
  1. Take the exposure snapshot closest to 10:00 CT (must fall 09:30-11:00 CT).
  2. magnet = strike with the largest positive GEX; concentration = its share
     of total positive GEX.
  3. Outcome: did any minute bar between the snapshot time and the 15:00 CT
     close touch the magnet strike?
  4. Placebo: the mirror-image price level on the opposite side of spot
     (same distance). Controls for baseline drift / volatility.

Run (Railway engine shell, where DATABASE_URL already exists):
    python3 scripts/gamma_magnet_study.py [--symbols SPY,QQQ] [--days 30]

Prints one compact JSON object to stdout. Makes no writes.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

CDT = timezone(timedelta(hours=-5))  # all study dates fall in daylight time
UTC = timezone.utc

DEFAULT_SYMBOLS = [
    "SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "MU", "AMD", "META",
    "AMZN", "MSFT", "GOOGL", "NFLX", "AVGO", "PLTR", "HOOD", "COIN",
]
FIRST_DAY = datetime(2026, 7, 6, tzinfo=UTC)
LAST_DAY = datetime(2026, 9, 28, tzinfo=UTC)  # inclusive

# Morning read: snapshot closest to 10:00 CT, accepted 09:30-11:00 CT.
SAMPLE_HOUR, SAMPLE_MIN = 10, 0
WINDOW_LO_MIN, WINDOW_HI_MIN = 9 * 60 + 30, 11 * 60
CLOSE_HOUR, CLOSE_MIN = 15, 0  # regular-session close, CT

MIN_DIST, MAX_DIST = 0.002, 0.05   # magnet 0.2%-5% from spot
MIN_CONC = 0.10                     # magnet holds >=10% of positive GEX

DIST_BUCKETS = [
    ("<0.5%", 0.0, 0.005),
    ("0.5-1%", 0.005, 0.01),
    ("1-2%", 0.01, 0.02),
    ("2-3%", 0.02, 0.03),
    ("3-5%", 0.03, 0.050001),
]
CONC_BUCKETS = [
    ("10-20%", 0.10, 0.20),
    ("20-35%", 0.20, 0.35),
    ("35%+", 0.35, 1.000001),
]


def day_windows(day):
    """Epoch bounds for one trading day.

    `day` is a CDT-midnight datetime. Returns (sample_target, window_lo,
    window_hi, close) as UTC epoch seconds.
    """
    target = (day + timedelta(hours=SAMPLE_HOUR, minutes=SAMPLE_MIN)).timestamp()
    lo = (day + timedelta(minutes=WINDOW_LO_MIN)).timestamp()
    hi = (day + timedelta(minutes=WINDOW_HI_MIN)).timestamp()
    close = (day + timedelta(hours=CLOSE_HOUR, minutes=CLOSE_MIN)).timestamp()
    return target, lo, hi, close


def extract_magnet(payload):
    """Return (spot, magnet_strike, distance, concentration) or None."""
    try:
        spot = float(payload.get("spot") or 0)
    except (TypeError, ValueError):
        return None
    if not spot or spot <= 0:
        return None
    strikes = payload.get("strikes") or []
    best, pos_total = None, 0.0
    for s in strikes:
        try:
            gex = float(s.get("gex") or 0)
            strike = float(s.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if gex > 0:
            pos_total += gex
            if best is None or gex > best[1]:
                best = (strike, gex)
    if best is None or pos_total <= 0:
        return None
    magnet, mgex = best
    dist = abs(magnet - spot) / spot
    conc = mgex / pos_total
    return spot, magnet, dist, conc


def bucket(value, buckets):
    for name, lo, hi in buckets:
        if lo <= value < hi:
            return name
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=0,
                    help="limit to the last N days of the window (0 = all)")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    url = os.environ["DATABASE_URL"].replace(
        "postgres://", "postgresql+psycopg://", 1).replace(
        "postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 10})
    t0 = time.time()

    days = []
    d = datetime(2026, 7, 6, tzinfo=CDT)
    last = datetime(2026, 9, 28, tzinfo=CDT)
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    if args.days > 0:
        days = days[-args.days:]

    SNAP_Q = text("""
        SELECT ts, payload FROM events
        WHERE kind='exposure' AND symbol=:s AND ts>=:lo AND ts<:hi
        ORDER BY abs(ts - :target) LIMIT 1""")
    RANGE_Q = text("""
        SELECT max((payload->>'h')::float) AS hi, min((payload->>'l')::float) AS lo,
               count(*) AS n
        FROM events
        WHERE kind='bar' AND symbol=:s AND ts>=:lo AND ts<:hi""")

    rows = []  # one dict per valid symbol-day
    coverage = {}
    with engine.connect() as c:
        for sym in symbols:
            cov = {"days": 0, "with_snapshot": 0, "valid_magnet": 0}
            for day in days:
                # skip weekends (CDT weekday; holidays skip naturally via no data)
                if day.weekday() >= 5:
                    continue
                cov["days"] += 1
                target, lo, hi, close = day_windows(day)
                snap = c.execute(SNAP_Q, {"s": sym, "lo": lo, "hi": hi,
                                          "target": target}).mappings().first()
                if not snap:
                    continue
                cov["with_snapshot"] += 1
                payload = snap["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                mag = extract_magnet(payload)
                if not mag:
                    continue
                spot, magnet, dist, conc = mag
                if not (MIN_DIST <= dist <= MAX_DIST and conc >= MIN_CONC):
                    continue
                rng = c.execute(RANGE_Q, {"s": sym, "lo": snap["ts"],
                                          "hi": close}).mappings().first()
                if not rng or not rng["n"]:
                    continue
                touched = rng["lo"] <= magnet <= rng["hi"]
                placebo = 2 * spot - magnet
                placebo_touched = rng["lo"] <= placebo <= rng["hi"]
                cov["valid_magnet"] += 1
                rows.append({
                    "symbol": sym,
                    "day": day.strftime("%Y-%m-%d"),
                    "spot": round(spot, 2),
                    "magnet": magnet,
                    "dist": round(dist, 4),
                    "conc": round(conc, 3),
                    "direction": "above" if magnet > spot else "below",
                    "dist_bucket": bucket(dist, DIST_BUCKETS),
                    "conc_bucket": bucket(conc, CONC_BUCKETS),
                    "touched": bool(touched),
                    "placebo_touched": bool(placebo_touched),
                    "bars": rng["n"],
                })
            coverage[sym] = cov
    engine.dispose()

    def agg(rs):
        n = len(rs)
        if not n:
            return {"n": 0}
        mh = sum(1 for r in rs if r["touched"])
        ph = sum(1 for r in rs if r["placebo_touched"])
        return {"n": n,
                "magnet_hit_rate": round(mh / n, 3),
                "placebo_hit_rate": round(ph / n, 3),
                "lift": round(mh / n - ph / n, 3)}

    out = {
        "window": [FIRST_DAY.strftime("%Y-%m-%d"),
                   LAST_DAY.strftime("%Y-%m-%d")],
        "symbols": symbols,
        "filters": {"dist": [MIN_DIST, MAX_DIST], "min_concentration": MIN_CONC,
                    "sample": "snapshot closest to 10:00 CT within 09:30-11:00 CT",
                    "outcome_window": "snapshot time to 15:00 CT close"},
        "coverage": coverage,
        "overall": agg(rows),
        "by_distance": {b[0]: agg([r for r in rows
                                   if r["dist_bucket"] == b[0]])
                        for b in DIST_BUCKETS},
        "by_direction": {d: agg([r for r in rows if r["direction"] == d])
                         for d in ("above", "below")},
        "by_concentration": {b[0]: agg([r for r in rows
                                        if r["conc_bucket"] == b[0]])
                             for b in CONC_BUCKETS},
        "by_symbol": {s: agg([r for r in rows if r["symbol"] == s])
                      for s in symbols},
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(out, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
