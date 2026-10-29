#!/usr/bin/env python3
"""Replicate the Option Hacker scan 'Bull-CortesTreasure' on the underlying and score it as a lottery-ticket filter.

Scan conditions (all with EXT sessions), evaluated at each RTH close (15:00 CT) per symbol-day:
  D   : ICE-RSI bullishCross true within 4 daily bars (including today's)
  1h  : within 72 one-hour bars
  15m : within 96 fifteen-minute bars
  5m  : within 336 five-minute bars
ICE-RSI = the Max6+RSI variant (TSI 22/13/8, DMI 22 ±0.75, TTM 32, Aroon 48 ≥70, Q-Stick 24, Wilder-MACD 6/24,
RSI 18 ≥64/≤46), bullX ≥ 3. DMI reading: plain (ratio-equivalent zero crossings) by default; --dmi cum for the other.

Outcome per symbol-day: entry next RTH open; close-to-entry return at +1..+5 trading days; max favorable excursion
(high) over 5 days; hits of ≥1σ / ≥2σ / ≥3σ up where σ = trailing-20-day RTH close-to-close std × sqrt(h).
Baseline: all other symbol-days of the same symbols. Also the bear mirror (puts) and the D+1h-only subset.
"""
import argparse, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from ice_features import ice_votes

CT = "America/Chicago"

def resample(df, rule, origin):
    g = df.set_index("ct").resample(rule, origin=origin, label="left", closed="left")
    o = g["open"].first(); h = g["high"].max(); l = g["low"].min(); c = g["close"].last()
    out = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}).dropna()
    return out

def fire_times(bars, variant, dmi_cum):
    v = ice_votes(bars[["open", "high", "low", "close"]], variant, dmi_cumulative=dmi_cum)
    bull = bars.index[v["bullX"].values >= 3]; bear = bars.index[v["bearX"].values >= 3]
    return bull, bear

def within(fires, bar_index, eval_times, nbars):
    """For each eval time, True if a fire occurred in the last nbars bars (bar containing eval time counts)."""
    pos_eval = np.searchsorted(bar_index.values, eval_times.values, side="right") - 1   # index of bar containing eval
    pos_fire = np.searchsorted(bar_index.values, fires.values, side="right") - 1
    pos_fire = np.unique(pos_fire)
    out = np.zeros(len(eval_times), bool)
    if len(pos_fire) == 0: return out
    # last fire at or before each eval pos
    k = np.searchsorted(pos_fire, pos_eval, side="right") - 1
    ok = k >= 0
    out[ok] = (pos_eval[ok] - pos_fire[k[ok]]) < nbars
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--bars", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--dmi", default="plain"); ap.add_argument("--variant", default="max6_rsi"); ap.add_argument("--only", default=None)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    dmi_cum = a.dmi == "cum"
    files = sorted(Path(a.bars).glob("*.parquet"))
    if a.only: files = [f for f in files if f.stem in a.only.split(",")]
    rows = []; t0 = time.time()
    for k, f in enumerate(files, 1):
        df = pd.read_parquet(f, columns=["ts", "open", "high", "low", "close"])
        df["ct"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(CT).dt.tz_localize(None)
        df = df.sort_values("ct").drop_duplicates("ct")
        m = df["ct"].dt.hour * 60 + df["ct"].dt.minute
        ext = df[(m >= 180) & (m < 1140)]                          # TOS EXT session 03:00–19:00 CT
        rth = df[(m >= 510) & (m < 900)]
        # daily RTH table
        d = rth.groupby(rth["ct"].dt.normalize()).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        d = d[d.index.weekday < 5]
        if len(d) < 60: continue
        # timeframes (EXT)
        tf = {"5m": resample(ext, "5min", "start_day"), "15m": resample(ext, "15min", "start_day"),
              "1h": resample(ext, "1h", "start_day"), "D": resample(ext, "1D", "start_day")}
        tf["D"] = tf["D"][tf["D"].index.weekday < 5]
        eval_t = d.index + pd.Timedelta(hours=14, minutes=59)      # at RTH close
        cond = {}
        for name, nb in (("D", 4), ("1h", 72), ("15m", 96), ("5m", 336)):
            bull, bear = fire_times(tf[name], a.variant, dmi_cum)
            cond[f"bull_{name}"] = within(bull, tf[name].index, eval_t, nb)
            cond[f"bear_{name}"] = within(bear, tf[name].index, eval_t, nb)
        # outcomes
        c = d["close"].values; o = d["open"].values; h = d["high"].values; l = d["low"].values; n = len(d)
        sig = pd.Series(c).pct_change().rolling(20).std().shift(0).values     # trailing 20d daily σ (as of today)
        rec = pd.DataFrame({"symbol": f.stem, "day": d.index, "sigma": sig, **cond})
        entry = np.r_[o[1:], np.nan]                                           # next day open
        rec["entry"] = entry
        for hz in (1, 2, 3, 4, 5):
            x = np.r_[c[hz:], [np.nan] * hz]                                   # close hz days after today = hz-th day after entry day... entry day is day+1, so close at day+hz
            rec[f"ret{hz}"] = x / entry - 1
            hh = pd.Series(h).shift(-1)[::-1].rolling(hz, min_periods=1).max()[::-1].values  # max high over days +1..+hz
            ll = pd.Series(l).shift(-1)[::-1].rolling(hz, min_periods=1).min()[::-1].values
            rec[f"mfe{hz}"] = hh / entry - 1; rec[f"mae{hz}"] = ll / entry - 1
        rows.append(rec)
        if k % 20 == 0: print(f"[{k}/{len(files)}] {time.time()-t0:.0f}s", flush=True)
    R = pd.concat(rows, ignore_index=True)
    R = R.dropna(subset=["entry", "ret5", "sigma"])
    R.to_parquet(out / f"scan_days_{a.variant}_{a.dmi}.parquet", index=False)
    report(R, out / f"scan_report_{a.variant}_{a.dmi}.md", a)

def block(R, mask, label, side):
    s = 1 if side == "bull" else -1
    sub = R[mask]; base = R[~mask]
    def stats(X):
        res = {"n": len(X)}
        for hz in (1, 3, 5):
            r = s * X[f"ret{hz}"]; sg = X["sigma"] * np.sqrt(hz)
            fe = (X[f"mfe{hz}"] if s == 1 else -X[f"mae{hz}"])
            res[f"mean{hz}"] = r.mean() * 1e4; res[f"med{hz}"] = r.median() * 1e4
            res[f"p1s{hz}"] = (fe >= 1 * sg).mean(); res[f"p2s{hz}"] = (fe >= 2 * sg).mean(); res[f"p3s{hz}"] = (fe >= 3 * sg).mean()
            res[f"lotto{hz}"] = np.maximum(0, fe - 1.5 * sg).mean() * 1e4   # payoff-like: excess over 1.5σ, bps
        return res
    a, b = stats(sub), stats(base)
    lines = [f"\n### {label} ({side}) — n={a['n']:,} vs baseline {b['n']:,}  ({a['n']/(a['n']+b['n'])*100:.1f}% of symbol-days)",
             "| horizon | mean bps | base | median bps | base | P(≥1σ MFE) | base | P(≥2σ) | base | P(≥3σ) | base | lotto bps | base |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for hz in (1, 3, 5):
        lines.append(f"| +{hz}d | {a[f'mean{hz}']:.1f} | {b[f'mean{hz}']:.1f} | {a[f'med{hz}']:.1f} | {b[f'med{hz}']:.1f} | "
                     f"{a[f'p1s{hz}']:.3f} | {b[f'p1s{hz}']:.3f} | {a[f'p2s{hz}']:.3f} | {b[f'p2s{hz}']:.3f} | "
                     f"{a[f'p3s{hz}']:.3f} | {b[f'p3s{hz}']:.3f} | {a[f'lotto{hz}']:.1f} | {b[f'lotto{hz}']:.1f} |")
    return "\n".join(lines)

def report(R, path, a):
    out = [f"# Bull-CortesTreasure scan replay — {a.variant}, DMI {a.dmi}", f"symbol-days scored: {len(R):,}, symbols {R.symbol.nunique()}, {R.day.min().date()} → {R.day.max().date()}"]
    for side in ("bull", "bear"):
        full = R[f"{side}_D"] & R[f"{side}_1h"] & R[f"{side}_15m"] & R[f"{side}_5m"]
        out.append(block(R, full, "All four timeframes (the scan)", side))
        out.append(block(R, R[f"{side}_D"] & R[f"{side}_1h"], "D + 1h only", side))
        out.append(block(R, R[f"{side}_D"], "D only (within 4 bars)", side))
        out.append(block(R, R[f"{side}_5m"] & R[f"{side}_15m"] & R[f"{side}_1h"] & ~R[f"{side}_D"], "5m+15m+1h without D", side))
        # halves
        mid = R.day.median()
        for lab, m in (("H1", R.day < mid), ("H2", R.day >= mid)):
            out.append(block(R[m], full[m], f"All four, {lab}", side))
    # condition frequencies
    out.append("\n### Condition frequencies (share of symbol-days)")
    for c in [c for c in R.columns if c.startswith("bull_") or c.startswith("bear_")]:
        out.append(f"- {c}: {R[c].mean():.3f}")
    Path(path).write_text("\n".join(out)); print("\n".join(out))

if __name__ == "__main__":
    main()
