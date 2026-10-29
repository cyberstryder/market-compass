"""Python port of the three ICE v2.2.2 thinkScript variants.

Indicator definitions follow thinkorswim's built-ins where documented; two are reconstructions
and are flagged ASSUMPTION — confirm on a TOS chart before trusting a result that hinges on them:

  * TTM_Squeeze().Histogram is proprietary. Reconstructed as the usual linreg of
    (close - mean(midpoint(Donchian), SMA(close))) over `length` (LazyBear form).
  * DMI_Oscillator(length, isCumulative=yes) is read as TotalSum(DI+ - DI-) — the script
    passes `yes`, which makes a running sum. Set `dmi_cumulative=False` to use plain DI+ - DI-.

All inputs are per-symbol OHLCV DataFrames (sorted, contiguous 1-min RTH bars). Signals are
computed within each symbol and are NaN-safe. Returns bull/bear integer vote counts.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


# ───────────── primitives (thinkScript semantics) ─────────────
def ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()

def wilders(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()

def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()

def crosses_above(x: pd.Series, level) -> pd.Series:
    lv = level if np.isscalar(level) else level
    prev = x.shift(1)
    lv_prev = lv if np.isscalar(lv) else lv.shift(1)
    return (prev <= lv_prev) & (x > lv)

def crosses_below(x: pd.Series, level) -> pd.Series:
    prev = x.shift(1)
    lv_prev = level if np.isscalar(level) else level.shift(1)
    return (prev >= lv_prev) & (x < level)

def within(flag: pd.Series, bars: int) -> pd.Series:
    return flag.astype(float).rolling(bars, min_periods=1).max().fillna(0).astype(bool)


# ───────────── TOS built-ins ─────────────
def true_strength_index(close, long_len=25, short_len=13, signal_len=8):
    d = close.diff()
    num = ema(ema(d, long_len), short_len)
    den = ema(ema(d.abs(), long_len), short_len)
    return 100.0 * num / den.replace(0, np.nan)

def dmi(df, n, avg=wilders):
    h, l, c = df["high"], df["low"], df["close"]
    hi_diff = h.diff()
    lo_diff = -l.diff()
    plus_dm = np.where((hi_diff > lo_diff) & (hi_diff > 0), hi_diff, 0.0)
    minus_dm = np.where((lo_diff > hi_diff) & (lo_diff > 0), lo_diff, 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = avg(tr, n)
    di_plus = 100.0 * avg(pd.Series(plus_dm, index=df.index), n) / atr.replace(0, np.nan)
    di_minus = 100.0 * avg(pd.Series(minus_dm, index=df.index), n) / atr.replace(0, np.nan)
    return di_plus, di_minus

def dmi_oscillator(df, n, cumulative=True):
    p, m = dmi(df, n)
    osc = p - m
    return osc.fillna(0).cumsum() if cumulative else osc   # ASSUMPTION: isCumulative=yes → TotalSum

def ttm_squeeze_hist(df, length=20):
    # ASSUMPTION: LazyBear reconstruction of the TTM histogram.
    c = df["close"]
    mid = (df["high"].rolling(length).max() + df["low"].rolling(length).min()) / 2.0
    basis = (mid + sma(c, length)) / 2.0
    dev = c - basis
    # linear regression value at the last point of each window
    x = np.arange(length, dtype=float)
    x_mean = x.mean()
    denom = ((x - x_mean) ** 2).sum()
    def _lr(w):
        if np.isnan(w).any():
            return np.nan
        slope = ((x - x_mean) * (w - w.mean())).sum() / denom
        return w.mean() + slope * (x[-1] - x_mean)
    return dev.rolling(length).apply(_lr, raw=True)

def aroon(df, length=25):
    # TOS: Up = 100*(length - bars since highest high over length+1 bars)/length
    w = length + 1
    h, l = df["high"].values, df["low"].values
    n = len(h)
    up = np.full(n, np.nan); dn = np.full(n, np.nan)
    if n >= w:
        from numpy.lib.stride_tricks import sliding_window_view as swv
        hw = swv(h, w); lw = swv(l, w)
        # argmax of reversed gives bars-since for the most recent extreme on ties
        since_h = (w - 1) - np.argmax(hw[:, ::-1] == hw.max(axis=1, keepdims=True), axis=1)
        since_h = (w - 1) - since_h  # bars since
        since_l = (w - 1) - np.argmax(lw[:, ::-1] == lw.min(axis=1, keepdims=True), axis=1)
        since_l = (w - 1) - since_l
        up[w - 1:] = 100.0 * (length - since_h) / length
        dn[w - 1:] = 100.0 * (length - since_l) / length
    return pd.Series(up, index=df.index), pd.Series(dn, index=df.index)

def qstick(df, n):
    return sma(df["close"] - df["open"], n)

def rsi(close, n=14):
    d = close.diff()
    up = wilders(d.clip(lower=0), n)
    dn = wilders((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ───────────── the three variants ─────────────
VARIANTS = {
    # name: parameters
    "v222":      dict(tsi=(22, 13, 8), dmi_len=22, dmi_thr=0.0, qstick=50, squeeze=20,
                      aroon_len=25, aroon_mode="cross3", macd=None, rsi=None),
    "max6":      dict(tsi=(22, 13, 8), dmi_len=22, dmi_thr=0.0, qstick=24, squeeze=32,
                      aroon_len=48, aroon_mode="state70", macd=(6, 24), rsi=None),
    "max6_rsi":  dict(tsi=(22, 13, 8), dmi_len=22, dmi_thr=0.75, qstick=24, squeeze=32,
                      aroon_len=48, aroon_mode="state70", macd=(6, 24), rsi=(18, 64, 46)),
}


def ice_votes(df: pd.DataFrame, variant: str, dmi_cumulative: bool = True,
              vote_window: int = 1) -> pd.DataFrame:
    """Return DataFrame with columns bullX, bearX (int) and the individual flags.

    vote_window > 1 ORs each *crossover* flag over the last `vote_window` bars before summing
    (state flags are left as-is). vote_window=1 reproduces the thinkScript exactly.
    """
    p = VARIANTS[variant]
    c = df["close"]
    flags_bull, flags_bear = {}, {}

    tsi = true_strength_index(c, *p["tsi"])
    flags_bull["tsi"] = crosses_above(tsi, 0.0); flags_bear["tsi"] = crosses_below(tsi, 0.0)

    osc = dmi_oscillator(df, p["dmi_len"], cumulative=dmi_cumulative)
    flags_bull["dmi"] = crosses_above(osc, p["dmi_thr"]); flags_bear["dmi"] = crosses_below(osc, -p["dmi_thr"])

    sq = ttm_squeeze_hist(df, p["squeeze"])
    flags_bull["squeeze"] = (sq.shift(1) < 0) & (sq > 0)
    flags_bear["squeeze"] = (sq.shift(1) > 0) & (sq < 0)

    au, ad = aroon(df, p["aroon_len"])
    if p["aroon_mode"] == "cross3":
        flags_bull["aroon"] = within(crosses_above(au, ad), 3)
        flags_bear["aroon"] = within(crosses_above(ad, au), 3)
        state_keys = set()
    else:
        flags_bull["aroon"] = au >= 70; flags_bear["aroon"] = ad >= 70
        state_keys = {"aroon"}

    q = qstick(df, p["qstick"])
    flags_bull["qstick"] = crosses_above(q, 0.0); flags_bear["qstick"] = crosses_below(q, 0.0)

    if p["macd"]:
        m = wilders(c, p["macd"][0]) - wilders(c, p["macd"][1])
        flags_bull["macd"] = crosses_above(m, 0.0); flags_bear["macd"] = crosses_below(m, 0.0)

    if p["rsi"]:
        r = rsi(c, p["rsi"][0])
        flags_bull["rsi"] = r >= p["rsi"][1]; flags_bear["rsi"] = r <= p["rsi"][2]
        state_keys.add("rsi")

    if vote_window > 1:
        for k in flags_bull:
            if k not in state_keys:
                flags_bull[k] = within(flags_bull[k], vote_window)
                flags_bear[k] = within(flags_bear[k], vote_window)

    out = pd.DataFrame(index=df.index)
    for k in flags_bull:
        out[f"b_{k}"] = flags_bull[k].fillna(False).astype(np.int8)
        out[f"s_{k}"] = flags_bear[k].fillna(False).astype(np.int8)
    out["bullX"] = out[[f"b_{k}" for k in flags_bull]].sum(axis=1)
    out["bearX"] = out[[f"s_{k}" for k in flags_bear]].sum(axis=1)
    return out
