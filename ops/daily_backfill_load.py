#!/usr/bin/env python3
"""Client driver for the fill-only daily-bar backfill (2026-11-03).

Reads a load plan produced by the offline derivation/audit step —
  plan:   {symbol: {date: {o, h, l, c, v}}}   (already splice-scaled)
  factors:{symbol: {factor, anchor}}          (provenance, stamped
                                               into each payload)
— and POSTs one symbol per call to /api/admin/daily-backfill
(cron-token gated). The server enforces fill-only / completed-session
/ validity rules; this client only transports. Safe to re-run: the
server inserts nothing twice.

Usage:
  python3 ops/daily_backfill_load.py --plan load_plan.json \
      --factors splice_factors.json [--symbols AAPL,MSFT] [--dry-run]
  python3 ops/daily_backfill_load.py --verify AAPL,MSFT

Base URL from COMPASS_BASE (default the production dashboard); token
from COMPASS_CRON_TOKEN or ~/.config/compass/cron_token. The token is
read in-process and never printed.
"""
import argparse, json, os, pathlib, subprocess, sys

DEFAULT_BASE = "https://marketcompass.up.railway.app"


def token():
    t = os.environ.get("COMPASS_CRON_TOKEN")
    if t:
        return t.strip()
    return pathlib.Path.home().joinpath(
        ".config/compass/cron_token").read_text().strip()


def call(base, method, path, body=None):
    cmd = ["curl", "-s", "--max-time", "120", "-X", method,
           "-H", f"Authorization: Bearer {token()}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json",
                "--data-binary", json.dumps(body)]
    cmd.append(base + path)
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except Exception:
        raise RuntimeError(f"bad response from {path}: {out.stdout[:200]!r} "
                           f"stderr={out.stderr[:200]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan")
    ap.add_argument("--factors")
    ap.add_argument("--symbols", help="comma list; default: all in plan")
    ap.add_argument("--base", default=os.environ.get("COMPASS_BASE",
                                                     DEFAULT_BASE))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", help="comma list: GET stored counts instead")
    args = ap.parse_args()

    if args.verify:
        for sym in [s.strip().upper() for s in args.verify.split(",") if s.strip()]:
            r = call(args.base, "GET", f"/api/admin/daily-backfill?symbol={sym}")
            fills = r.get("fills") or []
            tot = sum(f.get("inserted", 0) for f in fills)
            print(f"{sym}: stored_sessions={r.get('stored_sessions')} "
                  f"fill_calls={len(fills)} inserted_total={tot}")
        return

    plan = json.loads(pathlib.Path(args.plan).read_text())
    factors = json.loads(pathlib.Path(args.factors).read_text()) if args.factors else {}
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else sorted(plan))
    grand = {"inserted": 0, "skipped_existing": 0, "refused": 0}
    # The app middleware caps POST bodies at 16 KiB; a symbol's bars go
    # in chunks of 120 (~12 KB). The endpoint is idempotent and each
    # call is its own transaction, so chunking is safe.
    CHUNK = 120
    for sym in symbols:
        bars = [{"date": d, **b} for d, b in sorted(plan.get(sym, {}).items())]
        if not bars:
            print(f"{sym}: no bars in plan")
            continue
        factor = (factors.get(sym) or {}).get("factor")
        if args.dry_run:
            print(f"{sym}: would post {len(bars)} bars "
                  f"({bars[0]['date']}..{bars[-1]['date']}) factor={factor}")
            continue
        tot = {"inserted": 0, "skipped_existing": 0, "refused": 0}
        for i in range(0, len(bars), CHUNK):
            r = call(args.base, "POST", "/api/admin/daily-backfill",
                     {"symbol": sym, "factor": factor,
                      "bars": bars[i:i + CHUNK]})
            if not r.get("ok"):
                print(f"{sym}: FAILED at chunk {i // CHUNK}: {r}")
                sys.exit(1)
            for k in tot:
                tot[k] += r.get(k, 0)
            if r.get("refused_detail"):
                print(f"   refused_detail: {r['refused_detail']}")
        for k in grand:
            grand[k] += tot[k]
        print(f"{sym}: inserted={tot['inserted']} "
              f"skipped={tot['skipped_existing']} refused={tot['refused']} "
              f"({bars[0]['date']}..{bars[-1]['date']})", flush=True)
    print("TOTAL:", grand)


if __name__ == "__main__":
    main()
