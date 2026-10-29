"""Stock-side simulator for 1-min RTH signals: fixed horizons + ATR bracket, fees, placebo.

Conventions
  * Bars: per-symbol DataFrame with ts (UTC), open, high, low, close, volume. RTH only
    (08:30–15:00 CT) is enforced here; signals are evaluated on bar close at index i.
  * Entry: next bar's open ± slip (default $0.01) in the trade direction. No entries after
    `last_entry` (14:30 CT default) so every horizon can complete before the flatten.
  * Exits: horizon H → exit at open of bar i+1+H (next tradable bar after H minutes).
           bracket → stop = entry ∓ k_stop·ATR14, target = entry ± k_tgt·ATR14, evaluated on
           bar extremes from bar i+1 onward; stop assumed hit first if both in one bar;
           stop fills `stop_slip` cents through; time stop at `flatten` (14:55 CT).
  * Return: net $ per 100 shares after `fee_rt` ($/share round trip) and slip; also bps.
  * Placebo: for each real trade, draw a random bar from the same symbol-day within the same
    clock-hour and run the same exit; `draws` repetitions → percentile of the real mean.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

CT = "America/Chicago"

def rth(df: pd.DataFrame) -> pd.DataFrame:
    t = df["ts"].dt.tz_convert(CT)
    m = (t.dt.hour * 60 + t.dt.minute)
    keep = (m >= 8 * 60 + 30) & (m < 15 * 60) & (t.dt.weekday < 5)
    out = df.loc[keep].reset_index(drop=True).copy()
    out["ct"] = t[keep].dt.tz_localize(None).reset_index(drop=True)  # wall-clock CT
    out["day"] = out["ct"].dt.date
    out["minute"] = (out["ct"].dt.hour * 60 + out["ct"].dt.minute).astype(int)
    return out

def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def simulate(df: pd.DataFrame, sig_idx: np.ndarray, side: int, exit_kind: str,
             horizon: int = 30, k_stop: float = 1.0, k_tgt: float = 2.0,
             slip: float = 0.01, stop_slip: float = 0.01, fee_rt: float = 0.01,
             last_entry: int = 14 * 60 + 30, flatten: int = 14 * 60 + 55) -> pd.DataFrame:
    """Vectorised where possible. side=+1 long, -1 short. Returns one row per trade."""
    o, h, l = df["open"].values, df["high"].values, df["low"].values
    day = df["day"].values; minute = df["minute"].values
    a = atr(df).values
    n = len(df)
    rows = []
    for i in sig_idx:
        e = i + 1
        if e >= n or day[e] != day[i] or minute[i] > last_entry or np.isnan(a[i]):
            continue
        entry = o[e] + side * slip
        if exit_kind == "horizon":
            x = e + horizon
            if x >= n or day[x] != day[e]:
                continue
            exit_px = o[x]; reason = "h"
        else:
            stop = entry - side * k_stop * a[i]
            tgt = entry + side * k_tgt * a[i]
            j = e; exit_px = None; reason = "time"
            while j < n and day[j] == day[e]:
                if minute[j] >= flatten:
                    exit_px = o[j]; reason = "time"; break
                if side == 1:
                    if l[j] <= stop: exit_px = stop - stop_slip; reason = "stop"; break
                    if h[j] >= tgt:  exit_px = tgt; reason = "tgt"; break
                else:
                    if h[j] >= stop: exit_px = stop + stop_slip; reason = "stop"; break
                    if l[j] <= tgt:  exit_px = tgt; reason = "tgt"; break
                j += 1
            if exit_px is None:
                continue
        gross = side * (exit_px - entry)
        net_sh = gross - fee_rt
        rows.append((i, day[i], minute[i], entry, exit_px, reason,
                     net_sh * 100.0, 1e4 * net_sh / entry))
    return pd.DataFrame(rows, columns=["i", "day", "minute", "entry", "exit", "reason", "usd100", "bps"])


def summarize(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return dict(n=0)
    x = tr["bps"].values
    mu = x.mean(); sd = x.std(ddof=1) if len(x) > 1 else np.nan
    t = mu / (sd / np.sqrt(len(x))) if sd and sd > 0 else np.nan
    wins = x[x > 0].sum(); losses = -x[x < 0].sum()
    days = pd.to_datetime(tr["day"]).sort_values()
    mid = days.iloc[len(days) // 2]
    h1 = tr.loc[pd.to_datetime(tr["day"]) < mid, "bps"].mean()
    h2 = tr.loc[pd.to_datetime(tr["day"]) >= mid, "bps"].mean()
    return dict(n=len(x), mean_bps=round(mu, 2), t=round(t, 2), win=round((x > 0).mean(), 3),
                pf=round(wins / losses, 2) if losses > 0 else np.inf,
                usd100_total=round(tr["usd100"].sum(), 0), h1_bps=round(h1, 2), h2_bps=round(h2, 2))


def placebo(df: pd.DataFrame, tr: pd.DataFrame, side: int, exit_kind: str, draws: int = 200,
            seed: int = 0, **kw) -> tuple[float, float]:
    """Percentile of the real mean bps within session-matched random entries. Returns (pct, placebo_mean)."""
    if len(tr) == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    key = pd.Series(np.arange(len(df)), index=pd.MultiIndex.from_arrays(
        [df["day"].values, (df["minute"].values // 60)]))
    groups = {k: v.values for k, v in key.groupby(level=[0, 1])}
    real = tr["bps"].mean()
    means = []
    for _ in range(draws):
        idx = []
        for d, m in zip(tr["day"].values, tr["minute"].values):
            pool = groups.get((d, m // 60))
            if pool is not None and len(pool):
                idx.append(rng.choice(pool))
        p = simulate(df, np.array(idx, dtype=int), side, exit_kind, **kw)
        means.append(p["bps"].mean() if len(p) else np.nan)
    means = np.array(means, dtype=float)
    ok = means[~np.isnan(means)]
    if len(ok) == 0:
        return np.nan, np.nan, means
    return float((ok < real).mean() * 100), float(ok.mean()), means
