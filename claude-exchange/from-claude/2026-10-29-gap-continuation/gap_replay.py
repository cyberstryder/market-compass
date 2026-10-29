#!/usr/bin/env python3
"""Post-gap continuation replay on data-foundation-v2-stocks (no earnings calendar in the data: |gap| is the proxy).

Per symbol-day: gap = RTH open (08:30 CT) / prior RTH close (15:00 CT) − 1.
Outcomes in the GAP DIRECTION (sign-adjusted so + = continuation), bps of the entry price:
  open→+15m, open→+60m, open→close
  Earnings-play rule as written: enter 08:45 CT (after 15-min OR) in gap direction, stop = OR extreme against,
    target 2R, flatten 14:55 CT → R-multiple and bps. Also the mis-timed 09:45 CT entry with the same stop logic.
Buckets: |gap| ≥ 3 / 5 / 8 / 12 %, up vs down, H1/H2. Baseline: |gap| < 1 %.
"""
import glob, sys
import numpy as np, pandas as pd
CT = "America/Chicago"

def day_table(f):
    df = pd.read_parquet(f, columns=["ts", "open", "high", "low", "close"])
    df["ct"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(CT).dt.tz_localize(None)
    df = df.sort_values("ct").drop_duplicates("ct")
    m = df["ct"].dt.hour * 60 + df["ct"].dt.minute
    r = df[(m >= 510) & (m < 900)].copy(); r["m"] = m[(m >= 510) & (m < 900)].values; r["d"] = r["ct"].dt.normalize()
    rows = []
    prev_close = None; prev_d = None
    for d, g in r.groupby("d"):
        if d.weekday() >= 5 or len(g) < 300: prev_close = g["close"].iloc[-1]; continue
        o = g["open"].iloc[0]
        if prev_close is None: prev_close = g["close"].iloc[-1]; continue
        gap = o / prev_close - 1
        mm = g["m"].values; op = g["open"].values; hi = g["high"].values; lo = g["low"].values; cl = g["close"].values
        def px_at(minute):
            i = np.searchsorted(mm, minute); return op[i] if i < len(op) else np.nan
        orh = hi[mm < 525].max(); orl = lo[mm < 525].min()
        rec = dict(symbol=f.split("/")[-1][:-8], day=d, gap=gap, open=o, p15=px_at(525), p60=px_at(570), p945=px_at(585),
                   close=cl[-1], orh=orh, orl=orl, prev_close=prev_close)
        # earnings-play sim
        for lab, ent_min in (("e845", 525), ("e945", 585)):
            s = 1 if gap > 0 else -1
            i0 = np.searchsorted(mm, ent_min)
            if i0 >= len(op) or abs(gap) < 0.005: rec[f"{lab}_R"] = np.nan; rec[f"{lab}_bps"] = np.nan; continue
            entry = op[i0]
            stop = orl if s == 1 else orh
            risk = s * (entry - stop)
            if risk <= 0: rec[f"{lab}_R"] = np.nan; rec[f"{lab}_bps"] = np.nan; continue
            tgt = entry + s * 2 * risk
            px = None
            for j in range(i0, len(op)):
                if mm[j] >= 895: px = op[j]; break
                if s == 1:
                    if lo[j] <= stop: px = stop; break
                    if hi[j] >= tgt: px = tgt; break
                else:
                    if hi[j] >= stop: px = stop; break
                    if lo[j] <= tgt: px = tgt; break
            if px is None: px = cl[-1]
            rec[f"{lab}_R"] = s * (px - entry) / risk; rec[f"{lab}_bps"] = 1e4 * s * (px - entry) / entry
        rows.append(rec); prev_close = cl[-1]
    return rows

def main():
    rows = []
    for k, f in enumerate(sorted(glob.glob("/home/claude/bars_v2/*.parquet")), 1):
        rows += day_table(f)
        if k % 30 == 0: print(k, flush=True)
    R = pd.DataFrame(rows); R.to_parquet("/home/claude/results_gap/gap_days.parquet", index=False)
    s = np.sign(R["gap"])
    for lab, col in (("c15", "p15"), ("c60", "p60"), ("cclose", "close")):
        R[lab] = 1e4 * s * (R[col] / R["open"] - 1)
    R["c945_close"] = 1e4 * s * (R["close"] / R["p945"] - 1)
    mid = R["day"].median()
    out = ["# Post-gap continuation replay — 163 symbols, " + f"{R.day.min().date()} → {R.day.max().date()}, {len(R):,} symbol-days",
           "Outcomes sign-adjusted to the gap direction (+ = continuation), bps unless noted. e845 = earnings-play rule as written "
           "(enter 08:45 CT, stop at 15-min OR extreme, 2R target, flatten 14:55); e945 = same with the code's actual 09:45 CT entry.",
           "", "| bucket | n | open→15m | open→60m | open→close | P(close beyond open) | e845 R mean | e845 win | e845 bps | e945 R mean | e945 bps | H1 e845 R | H2 e845 R |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    buckets = [("|gap|<1% (baseline)", R.gap.abs() < 0.01)]
    for th in (0.03, 0.05, 0.08, 0.12):
        buckets += [(f"|gap|≥{th*100:.0f}%", R.gap.abs() >= th), (f"  up ≥{th*100:.0f}%", R.gap >= th), (f"  down ≤−{th*100:.0f}%", R.gap <= -th)]
    for lab, m in buckets:
        g = R[m]
        if len(g) == 0: continue
        h1 = g[g.day < mid]["e845_R"].mean(); h2 = g[g.day >= mid]["e845_R"].mean()
        out.append(f"| {lab} | {len(g):,} | {g.c15.mean():.0f} | {g.c60.mean():.0f} | {g.cclose.mean():.0f} | {(g.cclose > 0).mean():.3f} | "
                   f"{g.e845_R.mean():.3f} | {(g.e845_R > 0).mean():.3f} | {g.e845_bps.mean():.0f} | {g.e945_R.mean():.3f} | {g.e945_bps.mean():.0f} | {h1:.3f} | {h2:.3f} |")
    # fade check: from 08:45 to close for |gap|>=3
    g = R[R.gap.abs() >= 0.03]
    out += ["", f"From 08:45 to close, |gap| ≥ 3%: mean {1e4*(np.sign(g.gap)*(g.close/g.p15-1)).mean():.0f} bps in gap direction, "
            f"median {1e4*(np.sign(g.gap)*(g.close/g.p15-1)).median():.0f}; from 09:45: mean {g.c945_close.mean():.0f}, median {g.c945_close.median():.0f}."]
    txt = "\n".join(out); print(txt); open("/home/claude/results_gap/gap_report.md", "w").write(txt)

if __name__ == "__main__":
    import os; os.makedirs("/home/claude/results_gap", exist_ok=True); main()
