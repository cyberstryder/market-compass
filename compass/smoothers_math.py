# Source: cyberstryder/smoothers-new shared/smoothers.py; preserved for migration parity.
"""Pine-compatible smoother math.

Computes the triple-EMA z-score smoother chain that the v3 Pine indicator uses.
Output: a direction series (+1 light blue / -1 dark blue) at every bar.
"""

import numpy as np
import pandas as pd


def _ema(arr: np.ndarray, length: int) -> np.ndarray:
    """Pine-equivalent EMA. Waits for `length` valid (non-NaN) values to
    accumulate in the input before producing the first output. Seeds with
    the SMA of those values. After seeding, NaN inputs carry forward the
    previous EMA value (this matches Pine's behavior in the smoother chain
    where rk5 has NaN at the start because it depends on rk3 which is na
    for the first `length-1` bars).

    Without this NaN-aware seeding, the second EMA in the chain
    (rk6 = ema(rk5, length)) would seed to NaN and propagate NaN forever,
    which was the bug that prevented any signals from firing.
    """
    n = len(arr)
    out = np.full(n, np.nan)
    if n < length:
        return out
    alpha = 2.0 / (length + 1)

    valid_mask = ~np.isnan(arr)
    if valid_mask.sum() < length:
        return out

    valid_indices = np.where(valid_mask)[0]
    seed_idx = valid_indices[length - 1]
    out[seed_idx] = np.mean(arr[valid_indices[:length]])

    for i in range(seed_idx + 1, n):
        cur = arr[i]
        prev = out[i - 1]
        if np.isnan(cur):
            out[i] = prev
        else:
            out[i] = alpha * cur + (1 - alpha) * prev

    return out


def _stdev(arr: np.ndarray, length: int) -> np.ndarray:
    """Rolling sample stdev matching Pine's ta.stdev (population stdev, ddof=0)."""
    s = pd.Series(arr)
    return s.rolling(window=length, min_periods=length).std(ddof=0).values


def compute_smoother(bars: pd.DataFrame, length: int) -> pd.Series:
    """Compute the direction series (+1 / -1) for a given smoother length.

    Mirrors the Pine indicator block:
        ys1  = (high + low + close*2) / 4
        rk3  = ta.ema(ys1, length)
        rk4  = ta.stdev(ys1, length)
        rk5  = (ys1 - rk3) * 200 / rk4
        rk6  = ta.ema(rk5, length)
        up   = ta.ema(rk6, length)
        down = ta.ema(up,  length)
        direction = (up > down) ? +1 : -1
    """
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    ys1 = (h + l + 2 * c) / 4.0

    rk3 = _ema(ys1, length)
    rk4 = _stdev(ys1, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        rk5 = np.where(rk4 != 0, (ys1 - rk3) * 200.0 / rk4, np.nan)
    rk6 = _ema(rk5, length)
    up = _ema(rk6, length)
    down = _ema(up, length)

    direction = np.where(up > down, 1, -1).astype(float)
    direction[np.isnan(up) | np.isnan(down)] = np.nan
    return pd.Series(direction, index=bars.index)

