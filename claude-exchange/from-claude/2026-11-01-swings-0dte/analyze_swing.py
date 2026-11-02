import sys, pathlib, numpy as np, pandas as pd
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("out")
d = pd.read_csv(OUT / "swing_trades.csv"); f = d[d.first_fire].copy()
pl = pd.read_csv(OUT / "placebo_draws.csv")
rng = np.random.default_rng(1)

def cl_t(g, col):
    x = g[col].dropna(); days = g.loc[x.index, "day"]
    s = x.groupby(days).sum(); n = x.groupby(days).size(); m = x.mean(); G = len(s)
    v = ((s - n * m) ** 2).sum() * G / (G - 1) / len(x) ** 2
    return m / np.sqrt(v) if v > 0 else np.nan

def placebo_pct(real_mean, draws, col, reps=2000):
    """draws: placebo rows for this cell, grouped by real trade (real_day+symbol). One draw per real
    trade per rep -> distribution of placebo cell means; percentile of the real mean."""
    keyed = draws.dropna(subset=[col]).groupby(["symbol", "real_day"])[col].apply(np.array)
    arrs = list(keyed.values)
    if len(arrs) < 20:
        return np.nan, np.nan, np.nan
    means = np.empty(reps)
    for r in range(reps):
        means[r] = np.mean([a[rng.integers(len(a))] for a in arrs])
    return (means < real_mean).mean() * 100, means.mean(), np.percentile(means, 97.5)

rows = []
for (rule, side), g in f.groupby(["rule", "side"]):
    p = pl[(pl.rule == rule) & (pl.side == side)]
    pR, pRm, pR975 = placebo_pct(g.R.mean(), p, "R")
    p5, p5m, p5_975 = placebo_pct(g.s5.mean(), p, "s5")
    rows.append(dict(rule=rule, side=side, n=len(g), WR=round((g.R > 0).mean(), 3), meanR=round(g.R.mean(), 3),
                     t_R=round(cl_t(g, "R"), 2), plac_meanR=round(pRm, 3), plac_pct_R=round(pR, 1),
                     excessR=round(g.R.mean() - pRm, 3),
                     s5_bps=round(g.s5.mean(), 1), t_s5=round(cl_t(g, "s5"), 2), plac_s5=round(p5m, 1), plac_pct_s5=round(p5, 1),
                     H1_R=round(g[g.day < "2025-11-01"].R.mean(), 3), H2_R=round(g[g.day >= "2025-11-01"].R.mean(), 3),
                     stop=round((g.reason == "stop").mean(), 2), tgt=round((g.reason == "target").mean(), 2),
                     top_sym_share=round(g.groupby("symbol").R.sum().max() / max(1e-9, abs(g.R.sum())), 2) if g.R.sum() != 0 else np.nan))
res = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(res.to_string(index=False))
res.to_csv(OUT / "swing_results.csv", index=False)

# daily_reversal long drill-down
g = f[(f.rule == "daily_reversal") & (f.side == "long")]
print("\n== daily_reversal long: by quarter ==")
q = g.assign(q=pd.to_datetime(g.day).dt.to_period("Q")).groupby("q").agg(n=("R", "size"), meanR=("R", "mean"), s5=("s5", "mean"))
print(q.round(3).to_string())
print("\nby weekly bias (1 bullish, -1 bearish, 0 neutral):")
print(g.groupby("bias").R.agg(["count", "mean"]).round(3).to_string())
print("\nby hour (CT):")
print(g.assign(hr=pd.to_datetime(g.signal_ct).dt.hour).groupby("hr").R.agg(["count", "mean"]).round(3).to_string())
print("\nper-symbol concentration: top 10 symbols by summed R")
print(g.groupby("symbol").R.agg(["count", "sum", "mean"]).sort_values("sum", ascending=False).head(10).round(2).to_string())
print("\nR distribution: p10/p50/p90", np.percentile(g.R, [10, 50, 90]).round(3), "mean excl. top 2% trades",
      round(g.R[g.R < g.R.quantile(.98)].mean(), 3))
# re-fire count
print("\nall fires / first fires:", len(d), len(f), "median fires per symbol-day:", d.groupby(["symbol", "day", "rule", "side"]).size().median())
