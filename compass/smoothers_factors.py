# Source-equivalent math, not a guarantee of future option value.
"""Pure factor math — pandas/numpy only, no network, no side effects.

Imported by BOTH the retro enrichment script (analysis/enrich_signals.py) and
the live entry-context helper (shared/entry_context.py) so the numbers computed
when analysing the live cohort are identical to the numbers logged on the signal
row at fire time. If a factor proves out in backtest it is then already being
recorded the same way going forward — no second implementation to drift.

All factors are daily-bar based (ATR, realized vol, SMA trend), so the
session-aligned 1H aggregation used for the *signal* engine does not apply here;
plain split-adjusted daily bars are the right input.
"""

import numpy as np
import pandas as pd


def atr(daily: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR over daily OHLC. `daily` needs high/low/close columns."""
    high, low, close = daily["high"], daily["low"], daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing == EWM with alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def realized_vol(daily: pd.DataFrame, window: int = 20, annualize: bool = True) -> pd.Series:
    """Rolling stdev of daily log returns. Annualized (×√252) by default."""
    logret = np.log(daily["close"] / daily["close"].shift(1))
    vol = logret.rolling(window).std()
    return vol * np.sqrt(252) if annualize else vol


def vol_percentile(vol_series: pd.Series, asof_value: float, lookback: int = 252) -> float:
    """Percentile (0-100) of `asof_value` within the trailing `lookback` readings
    of `vol_series`. Answers 'is this an unusually volatile regime for this name?'
    Returns NaN if there isn't enough history."""
    hist = vol_series.dropna().iloc[-lookback:]
    if len(hist) < 20 or pd.isna(asof_value):
        return float("nan")
    return float((hist < asof_value).mean() * 100.0)


def daily_trend(daily: pd.DataFrame, fast: int = 50, slow: int = 200) -> int:
    """+1 uptrend (close > fastSMA > slowSMA), -1 downtrend (close < fastSMA <
    slowSMA), 0 mixed/insufficient history. Evaluated on the last row."""
    close = daily["close"]
    if len(close) < slow:
        return 0
    c = close.iloc[-1]
    f = close.rolling(fast).mean().iloc[-1]
    s = close.rolling(slow).mean().iloc[-1]
    if pd.isna(f) or pd.isna(s):
        return 0
    if c > f > s:
        return 1
    if c < f < s:
        return -1
    return 0


def trend_aligned(direction: str, trend: int) -> bool:
    """True when the signal direction agrees with the daily trend."""
    d = (direction or "").strip().upper()
    if d == "CALL":
        return trend > 0
    if d == "PUT":
        return trend < 0
    return False

