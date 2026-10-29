#!/usr/bin/env python3
"""Pull N years of 1-minute stock bars from Alpaca historical REST → one parquet per symbol.

Run on the VM where ALPACA keys are already set:
    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python3 pull_stock_bars.py \
        --watchlist watchlist.txt --out ./bars_1m --years 2

Resumable: a symbol whose parquet already exists and ends within 2 days of --end is skipped.
Output schema (per symbol parquet): ts (UTC, ns), open, high, low, close, volume, vwap, trades.
Feed: 'sip' (requires the paid Algo Trader Plus plan for full history); falls back to 'iex' with --feed iex.
"""
import argparse, os, sys, time, json, datetime as dt
from pathlib import Path
import urllib.request, urllib.parse, urllib.error
import pandas as pd

BASE = "https://data.alpaca.markets/v2/stocks/bars"

def parse_watchlist(path):
    txt = Path(path).read_text().strip()
    syms = []
    for tok in txt.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        syms.append(tok.split(":")[-1].upper())
    return sorted(set(syms))

def fetch(symbol, start, end, feed, key, secret, retries=6):
    rows, page = [], None
    while True:
        q = {"symbols": symbol, "timeframe": "1Min", "start": start, "end": end,
             "limit": 10000, "adjustment": "all", "feed": feed, "sort": "asc"}
        if page:
            q["page_token"] = page
        url = BASE + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"{symbol}: gave up after {retries} retries")
        bars = data.get("bars", {}).get(symbol, []) or []
        rows.extend(bars)
        page = data.get("next_page_token")
        if not page:
            return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--end", default=None, help="ISO date, default = today")
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--only", default=None, help="comma list of symbols (debug)")
    a = ap.parse_args()

    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("set APCA_API_KEY_ID / APCA_API_SECRET_KEY")

    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today()
    start = end - dt.timedelta(days=int(365.25 * a.years) + 7)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    syms = parse_watchlist(a.watchlist)
    if a.only:
        syms = [s for s in syms if s in a.only.upper().split(",")]

    manifest = []
    for i, s in enumerate(syms, 1):
        f = out / f"{s}.parquet"
        if f.exists():
            last = pd.read_parquet(f, columns=["ts"])["ts"].max()
            if pd.Timestamp(end, tz="UTC") - last < pd.Timedelta(days=2):
                print(f"[{i}/{len(syms)}] {s} up to date", flush=True); continue
        t0 = time.time()
        try:
            rows = fetch(s, start.isoformat() + "T00:00:00Z", end.isoformat() + "T23:59:59Z",
                         a.feed, key, secret)
        except Exception as e:
            print(f"[{i}/{len(syms)}] {s} FAILED {e}", flush=True)
            manifest.append({"symbol": s, "rows": 0, "error": str(e)}); continue
        if not rows:
            print(f"[{i}/{len(syms)}] {s} no bars", flush=True)
            manifest.append({"symbol": s, "rows": 0}); continue
        df = pd.DataFrame(rows).rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low",
                                                "c": "close", "v": "volume", "vw": "vwap", "n": "trades"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df[["ts", "open", "high", "low", "close", "volume", "vwap", "trades"]].sort_values("ts")
        df = df.drop_duplicates("ts")
        df.to_parquet(f, index=False, compression="zstd")
        manifest.append({"symbol": s, "rows": len(df), "first": str(df.ts.iloc[0]), "last": str(df.ts.iloc[-1])})
        print(f"[{i}/{len(syms)}] {s} {len(df):,} bars {df.ts.iloc[0].date()}→{df.ts.iloc[-1].date()} "
              f"{time.time()-t0:.0f}s", flush=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print("done:", sum(m["rows"] for m in manifest), "bars")

if __name__ == "__main__":
    main()
