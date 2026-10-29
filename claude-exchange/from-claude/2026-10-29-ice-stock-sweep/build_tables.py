#!/usr/bin/env python3
"""Stage 1: per symbol, compute ICE vote counts (12 combos) and per-bar outcomes (5 exits × 2 sides).

    python3 build_tables.py --bars ../bars_v2 --out ./tables

Writes tables/<SYM>.npz with
  votes  int8 [n, 12, 2]   combos = product(variant[v222,max6,max6_rsi], dmi[cum,plain], vw[1,3]); last axis bull/bear
  out    float32 [n, 5, 2] exits  = h15,h30,h60,h120,brk ; last axis long/short ; NaN = not tradable. Net bps.
  pool   int32 [n]         symbol-day-hour group id (placebo pool)
  day    int32 [n]         ordinal day
Same conventions as stock_sim.py (RTH 08:30–15:00 CT, next-bar open ± $0.01, $0.01/sh RT fee,
stop fills 1¢ through, no entry after 14:30 CT, flatten 14:55 CT, ATR14 Wilder).
"""
import argparse, itertools, sys, time
from pathlib import Path
import numpy as np, pandas as pd
from numba import njit
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, str(Path(__file__).parent))
from ice_features import (ema, wilders, sma, crosses_above, crosses_below, within,
                          true_strength_index, dmi, qstick, rsi)

CT = "America/Chicago"
SLIP, STOP_SLIP, FEE = 0.01, 0.01, 0.01
LAST_ENTRY, FLATTEN = 14 * 60 + 30, 14 * 60 + 55
HORIZONS = [15, 30, 60, 120]
VARIANTS = ["v222", "max6", "max6_rsi"]
COMBOS = list(itertools.product(VARIANTS, ["cum", "plain"], [1, 3]))


def rth(df):
    t = df["ts"].dt.tz_convert(CT)
    m = t.dt.hour * 60 + t.dt.minute
    keep = (m >= 510) & (m < 900) & (t.dt.weekday < 5)
    out = df.loc[keep].reset_index(drop=True).copy()
    tt = t[keep].reset_index(drop=True)
    out["minute"] = m[keep].values.astype(np.int32)
    out["day"] = (tt.dt.tz_localize(None).dt.normalize().astype("int64") // 86_400_000_000_000).astype(np.int32)
    return out


def squeeze_hist(df, length):
    """Vectorised linreg(dev, length) endpoint value."""
    c = df["close"]
    mid = (df["high"].rolling(length).max() + df["low"].rolling(length).min()) / 2.0
    basis = (mid + sma(c, length)) / 2.0
    dev = (c - basis).values
    x = np.arange(length, dtype=float); xm = x.mean(); denom = ((x - xm) ** 2).sum()
    w = (x - xm) / denom                                 # slope weights
    n = len(dev); outv = np.full(n, np.nan)
    if n >= length:
        win = swv(dev, length)
        mean = win.mean(axis=1)
        slope = win @ w
        outv[length - 1:] = mean + slope * (x[-1] - xm)
    return pd.Series(outv, index=df.index)


def aroon(df, length):
    w = length + 1
    h, l = df["high"].values, df["low"].values
    n = len(h); up = np.full(n, np.nan); dn = np.full(n, np.nan)
    if n >= w:
        hw = swv(h, w); lw = swv(l, w)
        # bars since most recent max/min: argmax over reversed window
        since_h = np.argmax(hw[:, ::-1] == hw.max(axis=1, keepdims=True), axis=1)
        since_l = np.argmax(lw[:, ::-1] == lw.min(axis=1, keepdims=True), axis=1)
        up[w - 1:] = 100.0 * (length - since_h) / length
        dn[w - 1:] = 100.0 * (length - since_l) / length
    return pd.Series(up, index=df.index), pd.Series(dn, index=df.index)


def votes_all(df):
    c = df["close"]
    n = len(df)
    B, S = {}, {}   # flag dicts keyed by name
    tsi = true_strength_index(c, 22, 13, 8)
    B["tsi"], S["tsi"] = crosses_above(tsi, 0.0), crosses_below(tsi, 0.0)
    p, m = dmi(df, 22)
    osc_plain = (p - m)
    osc_cum = osc_plain.fillna(0).cumsum()
    for tag, osc in (("cum", osc_cum), ("plain", osc_plain)):
        B[f"dmi0_{tag}"], S[f"dmi0_{tag}"] = crosses_above(osc, 0.0), crosses_below(osc, 0.0)
        B[f"dmi75_{tag}"], S[f"dmi75_{tag}"] = crosses_above(osc, 0.75), crosses_below(osc, -0.75)
    for L in (20, 32):
        sq = squeeze_hist(df, L)
        B[f"sq{L}"] = (sq.shift(1) < 0) & (sq > 0); S[f"sq{L}"] = (sq.shift(1) > 0) & (sq < 0)
    au, ad = aroon(df, 25)
    B["ar25"], S["ar25"] = within(crosses_above(au, ad), 3), within(crosses_above(ad, au), 3)
    au, ad = aroon(df, 48)
    B["ar48"], S["ar48"] = au >= 70, ad >= 70
    for L in (50, 24):
        q = qstick(df, L)
        B[f"q{L}"], S[f"q{L}"] = crosses_above(q, 0.0), crosses_below(q, 0.0)
    macd = wilders(c, 6) - wilders(c, 24)
    B["macd"], S["macd"] = crosses_above(macd, 0.0), crosses_below(macd, 0.0)
    r = rsi(c, 18)
    B["rsi"], S["rsi"] = r >= 64, r <= 46
    B = {k: v.fillna(False).to_numpy(dtype=bool) for k, v in B.items()}
    S = {k: v.fillna(False).to_numpy(dtype=bool) for k, v in S.items()}

    def w3(a):  # OR over last 3 bars
        out = a.copy(); out[1:] |= a[:-1]; out[2:] |= a[:-2]; return out

    spec = {
        "v222":     (["tsi", "dmi0_{d}", "sq20", "ar25", "q50"], []),
        "max6":     (["tsi", "dmi0_{d}", "sq32", "q24", "macd"], ["ar48"]),
        "max6_rsi": (["tsi", "dmi75_{d}", "sq32", "q24", "macd"], ["ar48", "rsi"]),
    }
    votes = np.zeros((n, len(COMBOS), 2), dtype=np.int8)
    for ci, (v, d, vw) in enumerate(COMBOS):
        cross, state = spec[v]
        for side, F in ((0, B), (1, S)):
            tot = np.zeros(n, dtype=np.int8)
            for k in cross:
                a = F[k.format(d=d)]
                tot += (w3(a) if vw == 3 else a)
            for k in state:
                tot += F[k]
            votes[:, ci, side] = tot
    return votes


@njit(cache=True)
def bracket_all(o, h, l, atr, day, minute, side, k_stop, k_tgt, slip, stop_slip, fee, last_entry, flatten):
    n = len(o); out = np.full(n, np.nan, dtype=np.float32)
    for i in range(n - 1):
        e = i + 1
        if day[e] != day[i] or minute[i] > last_entry or np.isnan(atr[i]):
            continue
        entry = o[e] + side * slip
        stop = entry - side * k_stop * atr[i]
        tgt = entry + side * k_tgt * atr[i]
        j = e; px = np.nan
        while j < n and day[j] == day[e]:
            if minute[j] >= flatten:
                px = o[j]; break
            if side == 1:
                if l[j] <= stop: px = stop - stop_slip; break
                if h[j] >= tgt:  px = tgt; break
            else:
                if h[j] >= stop: px = stop + stop_slip; break
                if l[j] <= tgt:  px = tgt; break
            j += 1
        if np.isnan(px):
            continue
        out[i] = 1e4 * (side * (px - entry) - fee) / entry
    return out


def outcomes_all(df):
    o = df["open"].values.astype(np.float64); h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64); c = df["close"].values
    day = df["day"].values; minute = df["minute"].values
    n = len(o)
    tr = np.maximum.reduce([h - l, np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))]); tr[0] = h[0] - l[0]
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().values
    out = np.full((n, 5, 2), np.nan, dtype=np.float32)
    ok_entry = np.zeros(n, dtype=bool); ok_entry[:-1] = (day[1:] == day[:-1]) & (minute[:-1] <= LAST_ENTRY)
    for hi, H in enumerate(HORIZONS):
        for si, side in enumerate((1, -1)):
            x = np.arange(n) + 1 + H
            valid = ok_entry & (x < n)
            xv = np.minimum(x, n - 1)
            valid &= (day[xv] == day)
            entry = o[np.minimum(np.arange(n) + 1, n - 1)] + side * SLIP
            bps = 1e4 * (side * (o[xv] - entry) - FEE) / entry
            out[:, hi, si] = np.where(valid, bps, np.nan)
    for si, side in enumerate((1, -1)):
        out[:, 4, si] = bracket_all(o, h, l, atr, day, minute, side, 1.0, 2.0, SLIP, STOP_SLIP, FEE, LAST_ENTRY, FLATTEN)
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--bars", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    files = sorted(Path(a.bars).glob("*.parquet"))
    if a.only: files = [f for f in files if f.stem in a.only.split(",")]
    t0 = time.time()
    for k, f in enumerate(files, 1):
        dst = out / f"{f.stem}.npz"
        if dst.exists(): continue
        df = pd.read_parquet(f); df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = rth(df.sort_values("ts").drop_duplicates("ts"))
        if len(df) < 5000: print(f.stem, "skipped (short)"); continue
        v = votes_all(df); o = outcomes_all(df)
        pool = (df["day"].values.astype(np.int64) * 24 + df["minute"].values // 60).astype(np.int64)
        _, pool_id = np.unique(pool, return_inverse=True)
        np.savez(dst, votes=v, out=o, pool=pool_id.astype(np.int32), day=df["day"].values.astype(np.int32),
                 minute=df["minute"].values.astype(np.int16))
        print(f"[{k}/{len(files)}] {f.stem} n={len(df)} {time.time()-t0:.0f}s", flush=True)

if __name__ == "__main__":
    main()
