#!/usr/bin/env python3
"""ICE × stock universe sweep.

    python3 run_sweep.py --bars ./bars_1m --out ./results [--symbols AAPL,MSFT] [--draws 200]

Cells: variant {v222, max6, max6_rsi} × dmi {cum, plain} × vote_window {1, 3} × threshold {3, 4}
       × side {long, short} × exit {h15, h30, h60, h120, bracket 1x/2x ATR}.
Per cell: n, mean bps, t, win%, PF, H1/H2 mean, $ per 100 sh total, placebo percentile.
Pooled across the universe (bps is size-neutral); per-symbol CSV also written for the top cells.
Gate (same spirit as the futures search): n ≥ 50, H1 & H2 > 0, t ≥ 1.5, placebo ≥ 97.5 pct.
"""
import argparse, itertools, json, sys, time
from pathlib import Path
import numpy as np
import pandas as pd
from ice_features import ice_votes, VARIANTS
from stock_sim import rth, simulate, summarize, placebo

EXITS = [("h15", "horizon", 15), ("h30", "horizon", 30), ("h60", "horizon", 60),
         ("h120", "horizon", 120), ("brk", "bracket", None)]

def load(bars_dir, symbols=None):
    out = {}
    for f in sorted(Path(bars_dir).glob("*.parquet")):
        s = f.stem
        if symbols and s not in symbols:
            continue
        df = pd.read_parquet(f)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = rth(df.sort_values("ts"))
        if len(df) > 5000:
            out[s] = df
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", default=None); ap.add_argument("--draws", type=int, default=200)
    ap.add_argument("--placebo_min_n", type=int, default=50)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    syms = a.symbols.upper().split(",") if a.symbols else None
    data = load(a.bars, syms)
    print(f"{len(data)} symbols loaded", flush=True)

    # 1) signals per symbol per (variant, dmi, window) — cache
    sig = {}
    t0 = time.time()
    for v, dmi_cum, vw in itertools.product(VARIANTS, [True, False], [1, 3]):
        for s, df in data.items():
            votes = ice_votes(df[["open", "high", "low", "close"]], v, dmi_cumulative=dmi_cum, vote_window=vw)
            sig[(v, dmi_cum, vw, s)] = votes[["bullX", "bearX"]].values
        print(f"signals {v} dmi_cum={dmi_cum} vw={vw}  {time.time()-t0:.0f}s", flush=True)

    # 2) sweep
    rows = []; per_sym = []
    for v, dmi_cum, vw, thr, side_name, (ex_name, ex_kind, H) in itertools.product(
            VARIANTS, [True, False], [1, 3], [3, 4], ["long", "short"], EXITS):
        side = 1 if side_name == "long" else -1
        col = 0 if side == 1 else 1
        trades = []
        for s, df in data.items():
            x = sig[(v, dmi_cum, vw, s)][:, col]
            idx = np.flatnonzero(x >= thr)
            if len(idx) == 0:
                continue
            tr = simulate(df, idx, side, ex_kind, horizon=H or 30)
            if len(tr):
                tr["symbol"] = s; trades.append(tr)
        tr = pd.concat(trades) if trades else pd.DataFrame(columns=["bps", "day", "minute", "usd100"])
        summ = summarize(tr)
        cell = dict(variant=v, dmi="cum" if dmi_cum else "plain", vote_window=vw, thr=thr,
                    side=side_name, exit=ex_name, **summ)
        rows.append(cell)
        if summ.get("n", 0):
            ps = tr.groupby("symbol")["bps"].agg(["count", "mean"]).reset_index()
            ps = ps.assign(variant=v, dmi=cell["dmi"], vote_window=vw, thr=thr, side=side_name, exit=ex_name)
            per_sym.append(ps)
        print(f"{v:9s} {cell['dmi']:5s} vw{vw} thr{thr} {side_name:5s} {ex_name:5s} "
              f"n={summ.get('n',0):6d} mean={summ.get('mean_bps',np.nan):7.2f} t={summ.get('t',np.nan):5.2f}",
              flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(out / "sweep.csv", index=False)
    if per_sym:
        pd.concat(per_sym).to_csv(out / "per_symbol.csv", index=False)

    # 3) placebo on candidates (n ≥ min, both halves > 0, t ≥ 1.5)
    cand = res[(res["n"] >= a.placebo_min_n) & (res["h1_bps"] > 0) & (res["h2_bps"] > 0) & (res["t"] >= 1.5)]
    print(f"\n{len(cand)} candidate cells for placebo", flush=True)
    pl = []
    for _, r in cand.iterrows():
        v, dmi_cum, vw, thr = r["variant"], r["dmi"] == "cum", int(r["vote_window"]), int(r["thr"])
        side = 1 if r["side"] == "long" else -1; col = 0 if side == 1 else 1
        ex = next(e for e in EXITS if e[0] == r["exit"])
        # pooled placebo: per draw, weighted mean across symbols (weights = real trade counts)
        draw_sum = np.zeros(a.draws); draw_n = np.zeros(a.draws)
        for s, df in data.items():
            x = sig[(v, dmi_cum, vw, s)][:, col]
            idx = np.flatnonzero(x >= thr)
            if len(idx) == 0:
                continue
            tr = simulate(df, idx, side, ex[1], horizon=ex[2] or 30)
            if len(tr) == 0:
                continue
            _, _, means = placebo(df, tr, side, ex[1], draws=a.draws, horizon=ex[2] or 30)
            ok = ~np.isnan(means)
            draw_sum[ok] += means[ok] * len(tr); draw_n[ok] += len(tr)
        pooled = draw_sum / np.where(draw_n > 0, draw_n, np.nan)
        pooled = pooled[~np.isnan(pooled)]
        pct = float((pooled < r["mean_bps"]).mean() * 100) if len(pooled) else np.nan
        placebo_mean = float(pooled.mean()) if len(pooled) else np.nan
        pl.append(dict(**r.to_dict(), placebo_pct=round(pct, 1),
                       placebo_mean_bps=round(placebo_mean, 2),
                       excess_bps=round(r["mean_bps"] - placebo_mean, 2)))
        print(f"placebo {v} {r['dmi']} vw{vw} thr{thr} {r['side']} {r['exit']}: "
              f"real {r['mean_bps']} vs placebo {placebo_mean:.2f} → {pct:.0f}th pct", flush=True)
    pd.DataFrame(pl).to_csv(out / "placebo.csv", index=False)
    print("done")

if __name__ == "__main__":
    main()
