"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  MEDUSA-ORACLE  —  EOD PREDICTION ENGINE  v2.5-fix3                        ║
║  Standalone. Does NOT import or modify medusa_unified.py.                  ║
║                                                                              ║
║  USAGE:                                                                      ║
║    python medusa_oracle.py                          # today                 ║
║    python medusa_oracle.py --date 2026-04-02        # specific date         ║
║    python medusa_oracle.py --oi spx_quotedata.csv   # with real GEX         ║
║                                                                              ║
║  WHAT IT DOES:                                                               ║
║    1. Loads prior 10-day context from medusa_v2_results.csv                 ║
║    2. Loads today's 5-min + 60-min bars from medusa_intraday.xlsx           ║
║    3. Loads daily SPX/VIX from medusa_data.xlsx (with yfinance fallback)    ║
║    4. Parses CBOE option chain (--oi) for real GEX, PCR, max pain,          ║
║       gamma walls — falls back to synthetic sGEX if no chain provided       ║
║    5. Runs classify_periods() with 60-min MACD warm-up prepend              ║
║    6. Runs predict_shot_b() — pure DAES-B, Shot A removed                  ║
║    7. Outputs a formatted 15:15 decision brief to console + .txt file       ║
║                                                                              ║
║  FIXES (v2.5-fix3 / 2026-04-09):                                            ║
║    - vol_ema32_intra from daily totals EMA (not per-bar EMA)                ║
║    - 60-min bars loaded and prepended for MACD warm-up                      ║
║    - ATR context uses prior_row.name when today not in workbook             ║
║    - Shot A (daes_a blend) removed — pure DAES-B                           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import argparse
import datetime
import math
import sys
import warnings
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

WB_DAILY    = Path("medusa_data.xlsx")
WB_INTRADAY = Path("medusa_intraday.xlsx")
RESULTS_CSV = Path("medusa_v2_results.csv")
OI_DEFAULT  = Path("spx_quotedata.csv")

CONTEXT_DAYS  = 10
ES_WEIGHT     = 0.30
IV_PROXY_MULT = 0.95
SHOT_B_TIME   = "15:15"

PERIODS = {
    "p1": (0,   120),   # 09:30–11:30  120 min
    "p2": (120, 210),   # 11:30–13:00   90 min
    "p3": (210, 285),   # 13:00–14:15   75 min
    "p4": (285, 345),   # 14:15–15:15   60 min
    "p5": (345, 390),   # 15:15–16:00   45 min  ← prediction target
}
PERIOD_BETAS = {"p1": 0.18, "p2": 0.19, "p3": 0.20, "p4": 0.21, "p5": 0.24}

REGIME_OTM = {
    "BULL_3":              25,
    "BULL_2":              20,
    "BULL_1":              15,
    "NEUTRAL_BULL":        10,
    "NEUTRAL_BULL_CHOPPY":  5,
    "NEUTRAL":              5,
    "NEUTRAL_BEAR_CHOPPY":  5,
    "NEUTRAL_BEAR":        10,
    "BEAR_1":              15,
    "BEAR_2":              20,
    "BEAR_3":              25,
}

VOLUME_PROFILE_CUMFRAC = {
    "09:30": round(  0/390, 4), "09:35": round(  5/390, 4),
    "09:40": round( 10/390, 4), "09:45": round( 15/390, 4),
    "09:50": round( 20/390, 4), "09:55": round( 25/390, 4),
    "10:00": round( 30/390, 4), "10:05": round( 35/390, 4),
    "10:10": round( 40/390, 4), "10:15": round( 45/390, 4),
    "10:20": round( 50/390, 4), "10:25": round( 55/390, 4),
    "10:30": round( 60/390, 4), "10:35": round( 65/390, 4),
    "10:40": round( 70/390, 4), "10:45": round( 75/390, 4),
    "10:50": round( 80/390, 4), "10:55": round( 85/390, 4),
    "11:00": round( 90/390, 4), "11:05": round( 95/390, 4),
    "11:10": round(100/390, 4), "11:15": round(105/390, 4),
    "11:20": round(110/390, 4), "11:25": round(115/390, 4),
    "11:30": round(120/390, 4), "11:35": round(125/390, 4),
    "11:40": round(130/390, 4), "11:45": round(135/390, 4),
    "11:50": round(140/390, 4), "11:55": round(145/390, 4),
    "12:00": round(150/390, 4), "12:05": round(155/390, 4),
    "12:10": round(160/390, 4), "12:15": round(165/390, 4),
    "12:20": round(170/390, 4), "12:25": round(175/390, 4),
    "12:30": round(180/390, 4), "12:35": round(185/390, 4),
    "12:40": round(190/390, 4), "12:45": round(195/390, 4),
    "12:50": round(200/390, 4), "12:55": round(205/390, 4),
    "13:00": round(210/390, 4), "13:05": round(215/390, 4),
    "13:10": round(220/390, 4), "13:15": round(225/390, 4),
    "13:20": round(230/390, 4), "13:25": round(235/390, 4),
    "13:30": round(240/390, 4), "13:35": round(245/390, 4),
    "13:40": round(250/390, 4), "13:45": round(255/390, 4),
    "13:50": round(260/390, 4), "13:55": round(265/390, 4),
    "14:00": round(270/390, 4), "14:05": round(275/390, 4),
    "14:10": round(280/390, 4), "14:15": round(285/390, 4),
    "14:20": round(290/390, 4), "14:25": round(295/390, 4),
    "14:30": round(300/390, 4), "14:35": round(305/390, 4),
    "14:40": round(310/390, 4), "14:45": round(315/390, 4),
    "14:50": round(320/390, 4), "14:55": round(325/390, 4),
    "15:00": round(330/390, 4), "15:05": round(335/390, 4),
    "15:10": round(340/390, 4), "15:15": round(345/390, 4),
    "15:20": round(350/390, 4), "15:25": round(355/390, 4),
    "15:30": round(360/390, 4), "15:35": round(365/390, 4),
    "15:40": round(370/390, 4), "15:45": round(375/390, 4),
    "15:50": round(380/390, 4), "15:55": round(385/390, 4),
    "16:00": round(390/390, 4),
}


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — REGIME CLASSIFIER  (copied verbatim from medusa_unified.py)
# ═════════════════════════════════════════════════════════════════════════════

def wilder_ema(series, period):
    alpha = 1.0 / period
    result = [series[0]]
    for v in series[1:]:
        result.append(result[-1] * (1 - alpha) + v * alpha)
    return result


def compute_macd(prices, fast=12, slow=26, signal=9, signal_only_from=None):
    """
    MACD histogram. If signal_only_from is set, the EMA state is warmed up on
    prices[:signal_only_from] (60-min history) and the histogram is evaluated
    only on prices[signal_only_from:] (today's 5-min bars). This prevents
    60-min history from contaminating the intraday MACD signal.
    """
    if len(prices) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast  = pd.Series(prices).ewm(span=fast,   adjust=False).mean().values
    ema_slow  = pd.Series(prices).ewm(span=slow,   adjust=False).mean().values
    macd_line = ema_fast - ema_slow
    sig_line  = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().values
    histogram = macd_line - sig_line
    # If signal_only_from set, return values from the 5-min portion only
    if signal_only_from is not None and signal_only_from < len(histogram):
        return float(macd_line[signal_only_from:].mean()), \
               float(sig_line[-1]), \
               float(histogram[signal_only_from:].mean())
    return float(macd_line[-1]), float(sig_line[-1]), float(histogram[-1])


def compute_vwmacd(prices, volumes, fast=6, slow=15, signal=24, signal_only_from=None):
    """
    Volume-weighted MACD. signal_only_from separates 60-min warm-up from
    5-min intraday signal — same principle as compute_macd above.
    """
    if len(prices) < slow + signal or len(volumes) < slow:
        return 0.0, 0.0, 0.0, 1.0
    prices  = np.array(prices,  dtype=float)
    volumes = np.array(volumes, dtype=float)
    vol_avg  = volumes.mean()
    dv_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 1.0
    vw_prices = prices * (1 + (volumes / vol_avg - 1) * 0.3)
    ema_fast  = pd.Series(vw_prices).ewm(span=fast,   adjust=False).mean().values
    ema_slow  = pd.Series(vw_prices).ewm(span=slow,   adjust=False).mean().values
    macd_line = ema_fast - ema_slow
    sig_line  = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().values
    histogram = macd_line - sig_line
    if signal_only_from is not None and signal_only_from < len(histogram):
        return float(macd_line[signal_only_from:].mean()), \
               float(sig_line[-1]), \
               float(histogram[signal_only_from:].mean()), dv_ratio
    return float(macd_line[-1]), float(sig_line[-1]), float(histogram[-1]), dv_ratio


def delta_acceleration(prices, window=6):
    if len(prices) < window + 2:
        return 0.0
    p            = np.array(prices[-window:], dtype=float)
    first_deriv  = np.diff(p)
    second_deriv = np.diff(first_deriv)
    return float(np.mean(second_deriv[-3:])) if len(second_deriv) >= 3 else float(second_deriv[-1])


def compute_adx_dmi(highs, lows, closes, period=17):
    n = len(closes)
    if n < 2 * period + 1:
        return 0.0, 0.0, 0.0, 0.0
    highs  = np.array(highs,  dtype=float)
    lows   = np.array(lows,   dtype=float)
    closes = np.array(closes, dtype=float)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i]  - closes[i-1]),
                    abs(lows[i]   - closes[i-1]))
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up   = highs[i]  - highs[i-1]
        down = lows[i-1] - lows[i]
        if up > down and up > 0:   plus_dm[i]  = up
        if down > up and down > 0: minus_dm[i] = down

    def ws(arr):
        r = np.zeros(n)
        r[period] = arr[1:period+1].sum()
        for i in range(period + 1, n):
            r[i] = r[i-1] - r[i-1] / period + arr[i]
        return r

    tr14  = ws(tr)
    pdm14 = ws(plus_dm)
    mdm14 = ws(minus_dm)
    plus_di  = np.where(tr14 > 0, 100.0 * pdm14 / tr14, 0.0)
    minus_di = np.where(tr14 > 0, 100.0 * mdm14 / tr14, 0.0)
    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    adx = np.zeros(n)
    adx[2 * period] = dx[period:2 * period + 1].mean()
    for i in range(2 * period + 1, n):
        adx[i] = adx[i-1] - adx[i-1] / period + dx[i] / period
    adx_val      = float(adx[-1])
    plus_di_val  = float(plus_di[-1])
    minus_di_val = float(minus_di[-1])
    trend_clarity = ((adx_val - 25.0) / 25.0) * ((plus_di_val - minus_di_val) / 100.0)
    return float(np.clip(trend_clarity, -1.0, 1.0)), adx_val, plus_di_val, minus_di_val


def classify_regime(bars, s0, atr, vix,
                    trend_atr_mult=0.30, accel_threshold=0.0804,   # MC1
                    macd_fast=12, macd_slow=31, macd_sig=8,          # MC1
                    vwmacd_fast=4, vwmacd_slow=13, vwmacd_sig=25,    # MC1
                    adx_period=17, prior_score_avg=0.0,
                    es_weight=ES_WEIGHT,
                    daily_macd_hist=0.0, daily_macd_slope=0.0,
                    n_warmup=0):
    """
    n_warmup: number of bars at the start that are 60-min history (warm-up only).
    MACD and VWMACD signals are evaluated only on bars[n_warmup:] — the 5-min
    intraday bars. The 60-min history warms the EMA state but does not drive
    the signal direction.
    """
    if bars is None or len(bars) < 4:
        return {"state": "UNKNOWN", "score": 0.0, "effective_score": 0.0, "components": {}}

    bars = bars.copy()
    bars.columns = [c.lower() for c in bars.columns]
    for col in ["close", "volume", "high", "low"]:
        if col not in bars.columns:
            bars[col] = bars["close"] if col in ("high", "low") else 1.0

    prices  = bars["close"].ffill().values.tolist()
    volumes = bars["volume"].fillna(1.0).values.tolist()
    highs   = bars["high"].ffill().values.tolist()
    lows    = bars["low"].ffill().values.tolist()

    # For structural analysis (HH/LL, VWAP, price_struct): use 5-min bars only
    intra_start = n_warmup if n_warmup > 0 and n_warmup < len(prices) else 0
    prices_intra  = prices[intra_start:]
    volumes_intra = volumes[intra_start:]
    highs_intra   = highs[intra_start:]
    lows_intra    = lows[intra_start:]

    current = prices_intra[-1] if prices_intra else prices[-1]

    # VWAP computed on 5-min bars only
    bars_intra = bars.iloc[intra_start:] if intra_start > 0 else bars
    vwap       = float((bars_intra["close"] * bars_intra["volume"]).sum()
                       / bars_intra["volume"].sum()) if bars_intra["volume"].sum() > 0 else current
    move       = current - s0
    move_norm  = move / atr if atr > 0 else 0.0
    above_s0   = move > 0
    above_vwap = current > vwap

    if len(prices_intra) >= 12:
        higher_highs = max(highs_intra[-6:]) > max(highs_intra[-12:-6])
        lower_lows   = min(lows_intra[-6:])  < min(lows_intra[-12:-6])
    else:
        higher_highs = above_s0
        lower_lows   = not above_s0

    # MACD: warm on full series, signal on 5-min portion only
    signal_from = intra_start if intra_start > 0 else None
    _, _, macd_hist = compute_macd(prices, macd_fast, macd_slow, macd_sig,
                                   signal_only_from=signal_from)
    macd_bull  = macd_hist > 0
    if len(prices_intra) > 2:
        last_move = prices_intra[-1] - prices_intra[-2]
        prev_move = prices_intra[-2] - prices_intra[-3]
        macd_accel_bull = macd_bull     and last_move > prev_move
        macd_accel_bear = (not macd_bull) and last_move < prev_move
    else:
        macd_accel_bull = False
        macd_accel_bear = False

    _, _, vwm_hist, dv_ratio = compute_vwmacd(prices, volumes, vwmacd_fast, vwmacd_slow, vwmacd_sig,
                                               signal_only_from=signal_from)
    vwmacd_bull  = vwm_hist > 0
    volume_surge = dv_ratio > 1.3467  # MC1

    accel      = delta_acceleration(prices_intra)
    accel_norm = accel / atr if atr > 0 else 0.0
    accel_bull = accel_norm >  accel_threshold
    accel_bear = accel_norm < -accel_threshold

    trend_clarity, adx_val, plus_di_val, minus_di_val = compute_adx_dmi(
        highs_intra, lows_intra, prices_intra, adx_period)

    vwap_series = []
    for i in range(min(6, len(bars))):
        sub = bars.iloc[:len(bars) - i]
        if sub["volume"].sum() > 0:
            vwap_series.append((sub["close"] * sub["volume"]).sum() / sub["volume"].sum())
    vwap_rising  = len(vwap_series) >= 3 and vwap_series[0] > vwap_series[1] > vwap_series[2]
    vwap_falling = len(vwap_series) >= 3 and vwap_series[0] < vwap_series[1] < vwap_series[2]

    score  = 0.0
    score += np.sign(move_norm) * min(abs(move_norm) / 0.5, 1.0) * 0.20
    score += (0.08 if above_vwap else -0.08)
    score += (0.07 if higher_highs and above_s0 else -0.07 if lower_lows and not above_s0 else 0)
    score += (0.08 if vwap_rising else -0.08 if vwap_falling else 0)
    score += (0.0883 if macd_bull else -0.0883)                                    # macd — MC1
    score += (0.1057 if macd_accel_bull else -0.1057 if macd_accel_bear else 0)   # macd accel — MC1
    score += (0.1720 if vwmacd_bull else -0.1720)                                  # vwmacd — MC1
    score += (0.1525 if vwmacd_bull and volume_surge else -0.1525 if not vwmacd_bull and volume_surge else 0)  # vol surge — MC1
    score += (0.2231 if accel_bull else -0.2231 if accel_bear else 0)              # delta accel — MC1

    daily_hist_norm  = np.clip(daily_macd_hist  / 15.0, -1.0, 1.0)
    daily_slope_norm = np.clip(daily_macd_slope /  5.0, -1.0, 1.0)
    score += daily_hist_norm * 0.08
    score += daily_slope_norm * daily_hist_norm * 0.04
    score += (-0.05 if vix > 22.0 else 0.05 if vix < 16.0 else 0)
    score += trend_clarity * 0.10
    score  = max(-1.0, min(1.0, score))

    same_dir = (score > 0 and prior_score_avg > 0) or (score < 0 and prior_score_avg < 0)
    effective_score = ((1.0 - es_weight) * score + es_weight * prior_score_avg
                       if same_dir and es_weight > 0 else score)
    effective_score = max(-1.0, min(1.0, effective_score))

    if   effective_score >=  0.60: state = "BULL_3"
    elif effective_score >=  0.35: state = "BULL_2"
    elif effective_score >=  0.20: state = "BULL_1"
    elif effective_score >=  0.12: state = "NEUTRAL_BULL"
    elif effective_score >=  0.05: state = "NEUTRAL_BULL_CHOPPY"
    elif effective_score >= -0.05: state = "NEUTRAL"
    elif effective_score >= -0.12: state = "NEUTRAL_BEAR_CHOPPY"
    elif effective_score >= -0.20: state = "NEUTRAL_BEAR"
    elif effective_score >= -0.35: state = "BEAR_1"
    elif effective_score >= -0.60: state = "BEAR_2"
    else:                          state = "BEAR_3"

    dmacd_accel = (daily_macd_hist > 0 and daily_macd_slope > 0) or \
                  (daily_macd_hist < 0 and daily_macd_slope < 0)
    dmacd_decel = (daily_macd_hist > 0 and daily_macd_slope < 0) or \
                  (daily_macd_hist < 0 and daily_macd_slope > 0)

    return {
        "state":            state,
        "score":            round(score, 4),
        "effective_score":  round(effective_score, 4),
        "daily_macd_hist":  round(daily_macd_hist, 4),
        "daily_macd_slope": round(daily_macd_slope, 4),
        "daily_macd_accel": dmacd_accel,
        "daily_macd_decel": dmacd_decel,
        "move_norm":        round(move_norm, 3),
        "above_vwap":       above_vwap,
        "macd_hist":        round(macd_hist, 4),
        "vwmacd_hist":      round(vwm_hist,  4),
        "accel_norm":       round(accel_norm, 4),
        "dv_ratio":         round(dv_ratio,   3),
        "vwap":             round(vwap, 2),
        "components": {
            "price_struct":  round(np.sign(move_norm) * min(abs(move_norm) / 0.5, 1) * 0.20, 3),
            "vwap_pos":      round(0.08 if above_vwap else -0.08, 3),
            "hh_ll":         round(0.07 if (higher_highs and above_s0) else -0.07 if (lower_lows and not above_s0) else 0, 3),
            "vwap_slope":    round(0.08 if vwap_rising else -0.08 if vwap_falling else 0, 3),
            "macd":          round(0.0883 if macd_bull else -0.0883, 4),
            "macd_accel":    round(0.1057 if macd_accel_bull else -0.1057 if macd_accel_bear else 0, 4),
            "vwmacd":        round(0.1720 if vwmacd_bull else -0.1720, 4),
            "vol_surge":     round(0.1525 if (vwmacd_bull and volume_surge) else -0.1525 if (not vwmacd_bull and volume_surge) else 0, 4),
            "accel":         round(0.2231 if accel_bull else -0.2231 if accel_bear else 0, 4),
            "dmacd":         round(daily_hist_norm * 0.08 + daily_slope_norm * daily_hist_norm * 0.04, 3),
            "vix":           round(-0.05 if vix > 22.0 else 0.05 if vix < 16.0 else 0, 3),
            "trend_clarity": round(trend_clarity * 0.10, 3),
        },
        "adx":               round(adx_val, 2),
        "plus_di":           round(plus_di_val, 2),
        "minus_di":          round(minus_di_val, 2),
        "trend_clarity_raw": round(trend_clarity, 4),
    }


def classify_periods(bars_day, s0, atr, vix, prior_score_avg=0.0,
                     daily_macd_hist=0.0, daily_macd_slope=0.0, **kwargs):
    """p1–p4 independent regime classification → beta-weighted composite.
    **kwargs passed through to classify_regime for parameter tuning."""
    if bars_day is None or len(bars_day) < 4:
        return None

    bars_day = bars_day.copy().sort_values("datetime_est").reset_index(drop=True)
    open_time = bars_day["datetime_est"].iloc[0].replace(
        hour=9, minute=30, second=0, microsecond=0)
    bars_day["_mins"] = (bars_day["datetime_est"] - open_time).dt.total_seconds() / 60.0

    period_regimes = {}
    available      = []

    for pname in ["p1", "p2", "p3", "p4"]:
        start_min, end_min = PERIODS[pname]
        pbars = bars_day[
            (bars_day["_mins"] >= start_min) &
            (bars_day["_mins"] <  end_min)
        ].drop(columns=["_mins"])
        if len(pbars) < 4:
            continue
        reg = classify_regime(
            pbars, s0, atr, vix,
            prior_score_avg=prior_score_avg,
            daily_macd_hist=daily_macd_hist,
            daily_macd_slope=daily_macd_slope,
            **kwargs)
        period_regimes[pname] = reg
        available.append(pname)

    if not available:
        return None

    raw_weights = {p: PERIOD_BETAS[p] for p in available}
    total_w     = sum(raw_weights.values())
    norm_w      = {p: w / total_w for p, w in raw_weights.items()}

    comp_score     = float(np.clip(sum(period_regimes[p]["score"]           * norm_w[p] for p in available), -1.0, 1.0))
    comp_eff_score = float(np.clip(sum(period_regimes[p]["effective_score"] * norm_w[p] for p in available), -1.0, 1.0))

    all_comp_keys = list(period_regimes[available[0]]["components"].keys())
    comp_components = {
        key: round(sum(period_regimes[p]["components"].get(key, 0.0) * norm_w[p] for p in available), 3)
        for key in all_comp_keys
    }

    if   comp_eff_score >=  0.60: comp_state = "BULL_3"
    elif comp_eff_score >=  0.35: comp_state = "BULL_2"
    elif comp_eff_score >=  0.20: comp_state = "BULL_1"
    elif comp_eff_score >=  0.12: comp_state = "NEUTRAL_BULL"
    elif comp_eff_score >=  0.05: comp_state = "NEUTRAL_BULL_CHOPPY"
    elif comp_eff_score >= -0.05: comp_state = "NEUTRAL"
    elif comp_eff_score >= -0.12: comp_state = "NEUTRAL_BEAR_CHOPPY"
    elif comp_eff_score >= -0.20: comp_state = "NEUTRAL_BEAR"
    elif comp_eff_score >= -0.35: comp_state = "BEAR_1"
    elif comp_eff_score >= -0.60: comp_state = "BEAR_2"
    else:                         comp_state = "BEAR_3"

    latest = period_regimes[available[-1]]
    composite = {
        "state":            comp_state,
        "score":            round(comp_score, 4),
        "effective_score":  round(comp_eff_score, 4),
        "daily_macd_hist":  latest["daily_macd_hist"],
        "daily_macd_slope": latest["daily_macd_slope"],
        "daily_macd_accel": latest["daily_macd_accel"],
        "daily_macd_decel": latest["daily_macd_decel"],
        "move_norm":        latest["move_norm"],
        "above_vwap":       latest["above_vwap"],
        "macd_hist":        latest["macd_hist"],
        "vwmacd_hist":      latest["vwmacd_hist"],
        "accel_norm":       latest["accel_norm"],
        "dv_ratio":         latest["dv_ratio"],
        "vwap":             latest["vwap"],
        "components":       comp_components,
        "adx":              latest.get("adx", 0.0),
        "plus_di":          latest.get("plus_di", 0.0),
        "minus_di":         latest.get("minus_di", 0.0),
        "trend_clarity_raw":latest.get("trend_clarity_raw", 0.0),
    }
    return {"per_period": period_regimes, "composite": composite,
            "available": available, "weights": norm_w}

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — DERIVED SERIES
# ═════════════════════════════════════════════════════════════════════════════

def compute_atr_series(spx_df, period=17):
    """
    Composite ATR = (ATR17 + ATR33 + ATR51) / 3
    Equal weight across three timeframes. Stored as 'atr17' for compatibility.
    """
    hi, lo, cl = spx_df["high"], spx_df["low"], spx_df["close"]
    prev_cl = cl.shift(1)
    tr = pd.concat([hi - lo, (hi - prev_cl).abs(), (lo - prev_cl).abs()], axis=1).max(axis=1)
    atr17 = tr.ewm(alpha=1/17, adjust=False).mean()
    atr33 = tr.ewm(alpha=1/33, adjust=False).mean()
    atr51 = tr.ewm(alpha=1/51, adjust=False).mean()
    return (atr17 + atr33 + atr51) / 3


def compute_vol_ema(spx_df, short=8, long=32):
    vol = spx_df["volume"].copy()
    return vol.ewm(span=short, adjust=False).mean(), vol.ewm(span=long, adjust=False).mean()


def compute_daily_macd_series(close_series, fast=12, slow=26, signal=9):
    prices   = close_series.ffill()
    ema_fast = prices.ewm(span=fast,   adjust=False).mean()
    ema_slow = prices.ewm(span=slow,   adjust=False).mean()
    macd_ln  = ema_fast - ema_slow
    sig_ln   = macd_ln.ewm(span=signal, adjust=False).mean()
    return macd_ln - sig_ln


def get_daily_macd_context(spx_all, date):
    if "daily_macd_hist" not in spx_all.columns:
        return {"hist": 0.0, "slope": 0.0, "bull": False,
                "accel": False, "decel": False, "label": "unknown"}
    try:
        idx = spx_all.index.get_loc(date)
    except KeyError:
        return {"hist": 0.0, "slope": 0.0, "bull": False,
                "accel": False, "decel": False, "label": "unknown"}
    if idx < 2:
        return {"hist": 0.0, "slope": 0.0, "bull": False,
                "accel": False, "decel": False, "label": "unknown"}
    hist      = float(spx_all["daily_macd_hist"].iloc[idx - 1])
    hist_prev = float(spx_all["daily_macd_hist"].iloc[idx - 2])
    slope     = hist - hist_prev
    bull      = hist > 0
    accel     = (bull and slope > 0) or (not bull and slope < 0)
    decel     = (bull and slope < 0) or (not bull and slope > 0)
    if bull and slope > 0:       label = "bull_accel"
    elif bull and slope < 0:     label = "bull_decel"
    elif not bull and slope < 0: label = "bear_accel"
    elif not bull and slope > 0: label = "bear_decel"
    else:                        label = "flat"
    return {"hist": round(hist, 4), "slope": round(slope, 4),
            "bull": bull, "accel": accel, "decel": decel, "label": label}


def compute_exponent(atr, vix=None, base=1.48):
    """ATR-only exponent. VIX removed."""
    atr_norm = np.clip((atr - 40) / 150, 0, 1)
    return base - 0.15 * atr_norm


def compute_atr_context(spx_all, date, lookback=6):
    """
    ATR context — pure volatility structure, no VIX.
    Returns: atr_now, atr_slope, atr_regime, atr_momentum, atr_trend.
    """
    try:
        idx = spx_all.index.get_loc(date)
    except KeyError:
        return {"atr_now": 0.0, "atr_6d_ago": 0.0, "atr_slope": 0.0,
                "atr_regime": "unknown", "atr_momentum": 0.0, "atr_trend": "unknown"}
    if idx < lookback + 2:
        atr_now = float(spx_all["atr17"].iloc[idx])
        return {"atr_now": round(atr_now, 2), "atr_6d_ago": round(atr_now, 2),
                "atr_slope": 0.0, "atr_regime": "unknown",
                "atr_momentum": 0.0, "atr_trend": "unknown"}
    atr_series = spx_all["atr17"].iloc[max(0, idx - lookback - 2):idx + 1]
    atr_now    = float(atr_series.iloc[-1])
    atr_6d_ago = float(atr_series.iloc[-lookback - 1])
    atr_3d_ago = float(atr_series.iloc[-3])
    atr_slope  = (atr_now - atr_6d_ago) / atr_6d_ago if atr_6d_ago > 0 else 0.0
    slope_recent = (atr_now - atr_3d_ago) / atr_3d_ago if atr_3d_ago > 0 else 0.0
    slope_prior  = (atr_3d_ago - atr_6d_ago) / atr_6d_ago if atr_6d_ago > 0 else 0.0
    atr_momentum = slope_recent - slope_prior
    coiling = False
    if idx >= lookback:
        atr_window = [float(spx_all["atr17"].iloc[idx - i]) for i in range(lookback)]
        coiling = all(atr_window[i] < atr_window[i+1] for i in range(len(atr_window)-1))
    if coiling:            atr_regime = atr_trend = "coiling"
    elif atr_slope > 0.08: atr_regime = atr_trend = "expanding"
    elif atr_slope < -0.08:atr_regime = atr_trend = "compressing"
    else:                  atr_regime = atr_trend = "stable"
    return {"atr_now": round(atr_now, 2), "atr_6d_ago": round(atr_6d_ago, 2),
            "atr_slope": round(atr_slope, 4), "atr_regime": atr_regime,
            "atr_momentum": round(atr_momentum, 4), "atr_trend": atr_trend}


def compute_charm_vanna(s0, K, T, sigma, direction="call", r=0.05):
    """Charm (delta decay) and Vanna (delta/IV sensitivity) for sweet-zone timing."""
    from scipy.stats import norm
    if T <= 1e-8 or sigma <= 0 or s0 <= 0 or K <= 0:
        return {"charm": 0.0, "vanna": 0.0,
                "charm_signal": "neutral", "vanna_signal": "neutral"}
    sqrt_T = np.sqrt(T)
    d1 = (np.log(s0 / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm.pdf(d1)
    charm_raw = -pdf_d1 * (r / (sigma * sqrt_T) - d2 / (2 * T))
    charm = charm_raw if direction == "call" else -charm_raw
    vanna = -pdf_d1 * d2 / sigma
    if direction == "put": vanna = -vanna
    charm_signal = ("exit_soon" if charm < -0.05 else "watch" if charm < -0.02 else "hold")
    vanna_signal = ("favorable"   if (direction == "call" and vanna > 0) or
                                     (direction == "put"  and vanna < 0) else
                    "unfavorable" if abs(vanna) > 0.02 else "neutral")
    return {"charm": round(charm, 5), "vanna": round(vanna, 5),
            "charm_signal": charm_signal, "vanna_signal": vanna_signal}


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — INTRADAY FEATURES
# ═════════════════════════════════════════════════════════════════════════════

def vol_pace_at(cumvol, time_str, vol_ema32):
    keys = sorted(VOLUME_PROFILE_CUMFRAC.keys())
    frac = VOLUME_PROFILE_CUMFRAC.get(
        time_str,
        next((VOLUME_PROFILE_CUMFRAC[k] for k in reversed(keys) if k <= time_str),
             VOLUME_PROFILE_CUMFRAC[keys[0]]))
    expected = vol_ema32 * frac * 0.80
    return cumvol / expected if expected > 0 else 1.0


def compute_vol_ema32_intraday(bars_5m, span=32):
    """EMA(32) of daily-total 5-min bar volumes — correct baseline for pace calc."""
    if bars_5m is None or bars_5m.empty:
        return None
    daily_totals = (bars_5m.groupby(bars_5m["datetime_est"].dt.date)["volume"]
                    .sum().sort_index())
    if len(daily_totals) < 3:
        return None
    val = float(daily_totals.ewm(span=span, adjust=False).mean().iloc[-1])
    return val if val > 0 else None


def build_volume_profile_cumfrac(bars, min_days=5):
    """
    Option A: real U-shaped cumfrac from bar data, anchored to 345/390 at 15:15.
    Falls back to linear VOLUME_PROFILE_CUMFRAC if insufficient data.
    """
    if bars is None or bars.empty:
        return None
    if "datetime_est" not in bars.columns or "volume" not in bars.columns:
        return None
    df = bars.copy()
    df["date"] = df["datetime_est"].dt.date
    df["time"] = df["datetime_est"].dt.strftime("%H:%M")
    daily_totals = df.groupby("date")["volume"].sum()
    if len(daily_totals) < min_days:
        return None
    cumfrac = {}
    for t in sorted(df["time"].unique()):
        day_cumvol = (df[df["time"] <= t]
                      .groupby("date")["volume"].sum()
                      .reindex(daily_totals.index).dropna())
        eod_vol = daily_totals.reindex(day_cumvol.index)
        valid   = eod_vol[eod_vol > 0]
        if len(valid) < min_days:
            continue
        cumfrac[t] = float((day_cumvol.reindex(valid.index) / valid).median())
    if not cumfrac:
        return None
    # Normalize: anchor last trading timestamp to minutes_elapsed/390
    anchor_time   = "15:15"   # oracle cuts at 15:15
    anchor_target = 345 / 390
    if anchor_time in cumfrac and cumfrac[anchor_time] > 0:
        scale   = anchor_target / cumfrac[anchor_time]
        cumfrac = {t: round(v * scale, 4) for t, v in cumfrac.items()}
    return cumfrac


def get_intraday_features(bars_5m, date, vol_ema32, exponent, cutoff_time=None,
                          vol_ema32_intra=None, cumfrac_table=None):
    day = bars_5m[bars_5m["datetime_est"].dt.date == date].sort_values("datetime_est")
    h, m = map(int, SHOT_B_TIME.split(":"))
    shot_b = datetime.time(h, m)
    effective_cutoff = min(cutoff_time, shot_b) if cutoff_time is not None else shot_b
    day_b = day[day["datetime_est"].dt.time <= effective_cutoff]
    if len(day_b) < 6:
        day_b = day
    if len(day_b) < 1:
        return None

    min_bars = min(6, len(day_b))
    cumvol   = float(day_b["volume"].sum())
    price_b  = float(day_b["close"].iloc[-1])
    vwap     = ((day_b["close"] * day_b["volume"]).sum() / day_b["volume"].sum()
                if day_b["volume"].sum() > 0 else price_b)
    last_t   = day_b["datetime_est"].iloc[-1].strftime("%H:%M")
    mom      = float(day_b["close"].iloc[-1] - day_b["close"].iloc[-min_bars])

    # ── Vol pace: regression-conditioned p5 projection ───────────────────────
    # From 40 days of 5-min data, the p5/p4 per-minute rate ratio follows:
    #   r45 = 3.2913 - 0.7059 * r34   (r=-0.33, p=0.037)
    # where r34 = p4_rate / p3_rate (afternoon acceleration into 15:15)
    # When r34 = 1.12 (typical), r45 = 2.50 — matches the flat constant.
    # On fast-accelerating days (r34 > 1.3), p5 burst is slightly smaller.
    # On decelerating days (r34 < 1.0), p5 burst is slightly larger.
    # Residual std = 0.228 — uncertainty band for pace confidence.
    #
    # Fallback to flat 2.50 when p3 or p4 data unavailable.
    P5_ACCEL_INTERCEPT = 3.2913
    P5_ACCEL_SLOPE     = -0.7059
    P5_ACCEL_FLAT      = 2.50    # fallback constant

    p3_bars = day[(day["datetime_est"].dt.time >= datetime.time(13,  0)) &
                  (day["datetime_est"].dt.time <  datetime.time(14, 15))]
    p4_bars = day[(day["datetime_est"].dt.time >= datetime.time(14, 15)) &
                  (day["datetime_est"].dt.time <= datetime.time(15, 15))]

    _vol_baseline = vol_ema32_intra if (vol_ema32_intra and vol_ema32_intra > 0) else vol_ema32

    if len(p4_bars) >= 4 and _vol_baseline and _vol_baseline > 0:
        vol_p4   = float(p4_bars["volume"].sum())
        rate_p4  = vol_p4 / 60.0

        if len(p3_bars) >= 4:
            vol_p3  = float(p3_bars["volume"].sum())
            rate_p3 = vol_p3 / 75.0
            r34     = rate_p4 / rate_p3 if rate_p3 > 0 else 1.12
            # Condition p5 ratio on p4/p3 acceleration
            r45     = P5_ACCEL_INTERCEPT + P5_ACCEL_SLOPE * r34
            r45     = float(np.clip(r45, 2.00, 3.40))   # empirical bounds
        else:
            r45 = P5_ACCEL_FLAT

        vol_p5_proj  = rate_p4 * r45 * 45.0
        vol_eod_proj = cumvol + vol_p5_proj
        pace = vol_eod_proj / _vol_baseline
    else:
        # Fallback: linear cumfrac (insufficient p4 bars)
        _table   = cumfrac_table if cumfrac_table else VOLUME_PROFILE_CUMFRAC
        _cumfrac = _table.get(
            last_t,
            next((_table[k] for k in sorted(_table.keys(), reverse=True)
                  if k <= last_t), 0.01))
        if _vol_baseline > 0 and _cumfrac > 0:
            pace = cumvol / (_vol_baseline * _cumfrac)
        else:
            pace = 1.0

    pace = float(np.clip(pace, 0.0, 5.0))
    w_t  = 1.03 * (min(pace, 2.0) ** exponent)

    if pace > 1.3:   pace_regime = "hot"
    elif pace > 1.1: pace_regime = "above_avg"
    elif pace > 0.9: pace_regime = "on_pace"
    elif pace > 0.7: pace_regime = "light"
    else:            pace_regime = "thin"

    return {
        "price_b": price_b, "vwap": vwap, "momentum": mom,
        "pace": round(pace, 3), "pace_regime": pace_regime, "w_t": w_t,
        "cumvol": cumvol, "total_bars": len(day),
        "last_time": last_t, "bars": day_b,
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — PREDICTIONS
# ═════════════════════════════════════════════════════════════════════════════

def predict_shot_b(s0, vix, atr, exponent, intra, regime,
                   daily_macd_hist=0.0, sgex_disp=0.0):
    """Pure DAES-B. Shot A removed — was s0 * tiny_constant, dead weight."""
    if intra is None:
        # No intraday bars — ATR-based drift fallback
        atr_norm = np.clip((atr - 40) / 150, 0, 1)
        return s0 + atr * 0.02 * (1 - atr_norm)

    price_b = intra["price_b"]
    vwap    = intra["vwap"]
    mom     = intra["momentum"]
    w_t     = intra["w_t"]

    reg_state = regime.get("state", "NEUTRAL") if regime else "NEUTRAL"
    reg_score = regime.get("score", 0.0)       if regime else 0.0

    if reg_state in ("BULL_3","BULL_2","BEAR_3","BEAR_2"):
        mom_weight, vwap_weight = 0.55, 0.15
    elif reg_state in ("BULL_1","BEAR_1","NEUTRAL_BULL","NEUTRAL_BEAR"):
        mom_weight, vwap_weight = 0.35, 0.25
    elif reg_state in ("NEUTRAL_BULL_CHOPPY","NEUTRAL_BEAR_CHOPPY","NEUTRAL"):
        mom_weight, vwap_weight = 0.20, 0.40
    else:
        mom_weight, vwap_weight = 0.40, 0.25

    vwap_pull = (vwap - price_b) * w_t * vwap_weight
    mom_carry = mom  * (1 - w_t) * mom_weight
    reg_amp   = 1.0 + abs(reg_score) * 0.20

    if regime and regime.get("daily_macd_decel"):
        reg_amp *= 0.80
    elif regime and regime.get("daily_macd_accel"):
        reg_amp *= 1.10

    macd_mag   = abs(daily_macd_hist)
    gex_wt     = (1.0 / (1.0 + 0.3 * macd_mag)) * (0.4 + 0.6 * w_t)
    sgex_nudge = sgex_disp * gex_wt

    return price_b + (vwap_pull + mom_carry) * reg_amp + sgex_nudge


def compute_synthetic_gex(s0, price_b, atr, vix, daily_macd_hist, regime_score=0.0):
    """Fallback sGEX when no real CBOE chain is provided."""
    p        = float(price_b) if price_b else float(s0)
    atr_ref  = max(float(atr), 20.0)
    pull     = (round(p / 25) * 25 - p) * 0.50 + \
               (round(p / 50) * 50 - p) * 0.30 + \
               (round(p / 100) * 100 - p) * 0.20
    calm_atr  = float(np.clip(1.0 - (atr_ref - 40) / 60, 0.0, 1.0))
    calm_vix  = float(np.clip(1.0 - (vix - 15) / 20, 0.0, 1.0))
    dampener  = 1.0 / (1.0 + 0.3 * abs(float(daily_macd_hist)))
    rn_grav   = float(np.clip(pull * calm_atr * calm_vix * dampener, -8.0, 8.0))
    is_panic  = vix > 28.0 and abs(p - s0) > 1.5 * atr_ref
    panic_disp = float(np.sign(s0 - p) * min(abs(p - s0) * 0.15, 12.0)) if is_panic else 0.0
    disp      = rn_grav + panic_disp
    label     = ("gex_panic" if is_panic else
                 "gex_bull" if disp > 2 else "gex_bear" if disp < -2 else "gex_neutral")
    return {"displacement": round(disp, 2), "label": label, "source": "synthetic",
            "net_gex": None, "pcr": None, "max_pain": None,
            "call_walls": [], "put_walls": []}


def is_vol_heavy(vol_ratio, vix=15.0):
    if vol_ratio >= 1.50: return "heavy"
    if vix > 20.0 and vol_ratio >= 1.20: return "heavy"
    if vix > 18.0 and vol_ratio >= 1.10: return "caution"
    return "normal"


def direction_score(s0, pred_b, vix, w_t, intra, vol_ratio, regime=None, vix_gate=None):
    _vix     = vix_gate if vix_gate is not None else vix
    vol_flag = is_vol_heavy(vol_ratio, _vix)
    score = 0.0
    score += (1 if pred_b > s0 else -1) * 0.25
    if intra:
        vwap_diff = intra["price_b"] - intra["vwap"]
        score += np.sign(vwap_diff) * min(abs(vwap_diff) / 10.0, 1.0) * 0.20
        score += np.sign(intra["momentum"]) * min(abs(intra["momentum"]) / 15.0, 1.0) * 0.15
    if regime:
        rs = regime.get("score", 0.0)
        rt = regime.get("state", "NEUTRAL")
        if any(x in rt for x in ("BULL_3","BULL_2","BEAR_3","BEAR_2")):
            score += rs * 0.25
        elif any(x in rt for x in ("BULL_1","BEAR_1","NEUTRAL_BULL","NEUTRAL_BEAR")):
            score += rs * 0.15
        else:
            score += rs * 0.08
    if 0.80 <= w_t <= 1.10:
        score += np.sign(score) * 0.10
    elif w_t > 1.10:
        score += np.sign(score) * 0.06
    score += (-0.05 if vix >= 22 else -0.03 if vix >= 20 else 0.03 if vix < 16 else 0)
    if vol_flag == "caution":
        score *= 0.70
    elif vol_flag == "heavy":
        score *= 0.50
    score = max(-1.0, min(1.0, score))

    bull_prob  = 1.0 / (1.0 + math.exp(-score * 3.5))
    bear_prob  = 1.0 - bull_prob
    edge       = abs(bull_prob - 0.50)
    confidence = ("strong" if edge >= 0.25 else "moderate" if edge >= 0.15
                  else "weak" if edge >= 0.08 else "neutral")
    signal     = ("BULL" if confidence == "strong" and bull_prob > 0.50 else
                  "BEAR" if confidence == "strong" else "NEUTRAL")
    note = f"score={score:+.3f}"
    if vol_flag != "normal": note += f"  vol={vol_flag}"
    if vix >= 22: note += f"  VIX={vix:.0f}"
    return {"bull_prob": round(bull_prob, 3), "bear_prob": round(bear_prob, 3),
            "confidence": confidence, "signal": signal,
            "vol_flag": vol_flag, "vix_level": round(_vix, 1), "note": note}


def iv_from_vix(vix):
    return (vix / 100.0) * IV_PROXY_MULT


def bs_call(S, K, T, sigma, r=0.05):
    if T <= 1e-8: return max(S - K, 0.0)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma * np.sqrt(T))
    return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d1 - sigma*np.sqrt(T))


def bs_put(S, K, T, sigma, r=0.05):
    if T <= 1e-8: return max(K - S, 0.0)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)


def entry_prem(s0, K, direction, sigma):
    T = 90.0 / (252 * 390)
    return bs_call(s0, K, T, sigma) if direction == "call" else bs_put(s0, K, T, sigma)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — REAL GEX (from CBOE option chain CSV)
# ═════════════════════════════════════════════════════════════════════════════

def parse_real_gex(oi_path, s0, vix, atr):
    """
    Parse CBOE spx_quotedata.csv and compute:
      - Net GEX (real dealer gamma exposure)
      - PCR (put/call OI ratio, 0DTE)
      - Max pain strike
      - GEX-weighted nudge toward gravity center
      - Call/put OI walls
      - ATM IV from chain

    Returns dict compatible with the sGEX fallback dict shape.
    Returns None on parse failure (caller falls back to synthetic GEX).
    """
    path = Path(oi_path)
    if not path.exists():
        return None

    try:
        df = pd.read_csv(path, skiprows=3, header=0)
        df.columns = [c.strip() for c in df.columns]
    except Exception as e:
        print(f"  [GEX] parse error: {e}")
        return None

    def sf(x):
        try: return float(str(x).replace(',', ''))
        except: return 0.0

    df['strike']   = df['Strike'].apply(sf)
    df['call_oi']  = df['Open Interest'].apply(sf)
    df['call_gam'] = df['Gamma'].apply(sf)
    df['call_iv']  = df['IV'].apply(sf)
    df['call_bid'] = df['Bid'].apply(sf)
    df['put_oi']   = df['Open Interest.1'].apply(sf)
    df['put_gam']  = df['Gamma.1'].apply(sf)
    df['put_iv']   = df['IV.1'].apply(sf)
    df['put_bid']  = df['Bid.1'].apply(sf)

    # Identify today's 0DTE expiry — first row's expiry date
    expiry_col = df.columns[0]
    df['expiry'] = df[expiry_col].astype(str).str.strip()
    today_expiry = df['expiry'].iloc[0]

    today = df[df['expiry'] == today_expiry].copy()
    atm   = today[(today['strike'] >= s0 - 400) & (today['strike'] <= s0 + 400)].copy()

    if atm.empty:
        return None

    # ── Real GEX ─────────────────────────────────────────────────────────────
    atm['call_gex'] = atm['call_gam'] * atm['call_oi'] * 100 * s0
    atm['put_gex']  = atm['put_gam']  * atm['put_oi']  * 100 * s0 * -1
    atm['net_gex']  = atm['call_gex'] + atm['put_gex']

    total_gex    = float(atm['net_gex'].sum())
    call_gex_sum = float(atm['call_gex'].sum())
    put_gex_sum  = float(atm['put_gex'].sum())

    # ── PCR (full chain, not just ATM window) ─────────────────────────────────
    total_call_oi = float(today['call_oi'].sum())
    total_put_oi  = float(today['put_oi'].sum())
    pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

    # ── GEX gravity center → nudge ────────────────────────────────────────────
    denom = atm['call_gex'].sum() + atm['put_gex'].abs().sum()
    gex_center = (
        float((atm['call_gex'] * atm['strike']).sum() +
              (atm['put_gex'].abs() * atm['strike']).sum())
        / denom) if denom > 0 else s0

    calm_vix  = float(np.clip(1.0 - (vix - 15) / 20, 0.0, 1.0))
    calm_atr  = float(np.clip(1.0 - (atr  - 40) / 150, 0.0, 1.0))
    gex_pull_raw = (gex_center - s0) * 0.15
    gex_nudge    = float(np.clip(gex_pull_raw * calm_atr * calm_vix, -20, 20))

    # ── OI walls ─────────────────────────────────────────────────────────────
    call_walls = list(
        atm[atm['strike'] > s0].nlargest(3, 'call_oi')['strike'].values)
    put_walls  = list(
        atm[atm['strike'] < s0].nlargest(3, 'put_oi')['strike'].values)

    # ── Max pain (simplified: minimize total intrinsic value across chain) ─────
    all_strikes = sorted(atm['strike'].unique())
    pain_vals   = []
    for K in all_strikes:
        cp = float(((K - atm[atm['strike'] <= K]['strike']) * atm[atm['strike'] <= K]['call_oi']).sum())
        pp = float(((atm[atm['strike'] >= K]['strike'] - K) * atm[atm['strike'] >= K]['put_oi']).sum())
        pain_vals.append((K, cp + pp))
    max_pain = min(pain_vals, key=lambda x: x[1])[0] if pain_vals else round(s0 / 5) * 5

    # ── ATM IV from chain ─────────────────────────────────────────────────────
    near = atm[(atm['call_iv'] > 0) & (atm['strike'].between(s0 - 100, s0 + 100))]
    chain_iv = float(near['call_iv'].mean()) if not near.empty else vix / 100.0 * IV_PROXY_MULT

    # ── GEX label ─────────────────────────────────────────────────────────────
    is_panic = vix > 28.0 and abs(gex_nudge) > 10
    label    = ("gex_panic"   if is_panic else
                "gex_bull"    if gex_nudge > 2 else
                "gex_bear"    if gex_nudge < -2 else "gex_neutral")

    return {
        "displacement": round(gex_nudge, 2),
        "label":        label,
        "source":       "real_cboe",
        "net_gex":      round(total_gex),
        "call_gex":     round(call_gex_sum),
        "put_gex":      round(put_gex_sum),
        "gex_center":   round(gex_center, 1),
        "pcr":          round(pcr, 3),
        "max_pain":     max_pain,
        "call_walls":   [round(k) for k in sorted(call_walls)],
        "put_walls":    [round(k) for k in sorted(put_walls, reverse=True)],
        "chain_iv":     round(chain_iv, 4),
        "total_call_oi": int(total_call_oi),
        "total_put_oi":  int(total_put_oi),
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — DATA LOADING
# ═════════════════════════════════════════════════════════════════════════════

def _ws_to_df(wb, sheet):
    if sheet not in wb.sheetnames:
        return pd.DataFrame()
    ws   = wb[sheet]
    rows = list(ws.values)
    if len(rows) < 2:
        return pd.DataFrame()
    return pd.DataFrame(rows[1:], columns=rows[0])


def load_daily_data():
    """Load SPX daily + VIX from medusa_data.xlsx, fallback to yfinance."""
    if WB_DAILY.exists():
        try:
            from openpyxl import load_workbook
            wb   = load_workbook(WB_DAILY, read_only=True)
            spx  = _ws_to_df(wb, "RAW_SPX_DAILY")
            vix  = _ws_to_df(wb, "RAW_VIX_DAILY")
            calc = _ws_to_df(wb, "CALC_DAILY")
            wb.close()

            for df in [spx, vix, calc]:
                df.columns = [str(c).lower() for c in df.columns]

            for col in ["open", "high", "low", "close", "volume"]:
                if col in spx.columns:
                    spx[col] = pd.to_numeric(spx[col], errors="coerce")
            spx["date"] = pd.to_datetime(spx["date"], errors="coerce")
            spx = spx.dropna(subset=["date", "close"]).set_index("date").sort_index()

            vix_close = pd.Series(dtype=float)
            if "close" in vix.columns and "date" in vix.columns:
                vix["date"]  = pd.to_datetime(vix["date"], errors="coerce")
                vix["close"] = pd.to_numeric(vix["close"], errors="coerce")
                vix_close    = vix.dropna(subset=["date", "close"]).set_index("date")["close"].sort_index()

            if not calc.empty and "date" in calc.columns:
                calc["date"] = pd.to_datetime(calc["date"], errors="coerce")
                for col in ["exponent", "atr17", "vol_ema8", "vol_ema32"]:
                    if col in calc.columns:
                        calc[col] = pd.to_numeric(calc[col], errors="coerce")
                calc = calc.dropna(subset=["date"]).set_index("date").sort_index()
                for col in calc.columns:
                    if col not in spx.columns:
                        spx[col] = calc[col]

            spx["atr17"]           = compute_atr_series(spx)
            spx["vol_ema8"], spx["vol_ema32"] = compute_vol_ema(spx)
            spx["daily_macd_hist"] = compute_daily_macd_series(spx["close"])

            print(f"  [daily] {len(spx)} rows  ({spx.index[0].date()} → {spx.index[-1].date()})")
            return spx, vix_close
        except Exception as e:
            print(f"  [WARN] medusa_data.xlsx: {e}")

    # yfinance fallback
    print("  [daily] medusa_data.xlsx not found — falling back to yfinance...")
    try:
        import yfinance as yf
        spx_yf = yf.download("^GSPC", period="180d", interval="1d",
                              auto_adjust=True, progress=False)
        vix_yf = yf.download("^VIX",  period="60d",  interval="1d",
                              auto_adjust=True, progress=False)
        for df in [spx_yf, vix_yf]:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
        spx_yf.index = pd.to_datetime(spx_yf.index)
        vix_yf.index = pd.to_datetime(vix_yf.index)
        spx_yf["atr17"]           = compute_atr_series(spx_yf)
        spx_yf["vol_ema8"], spx_yf["vol_ema32"] = compute_vol_ema(spx_yf)
        spx_yf["daily_macd_hist"] = compute_daily_macd_series(spx_yf["close"])
        vix_close_yf = vix_yf["close"] if "close" in vix_yf.columns else pd.Series(dtype=float)
        return spx_yf, vix_close_yf
    except Exception as e:
        print(f"  [ERROR] yfinance fallback failed: {e}")
        return pd.DataFrame(), pd.Series(dtype=float)


def load_intraday_bars():
    """Load 5-min SPX bars from medusa_intraday.xlsx (RAW_SPX_INTRADAY)."""
    if not WB_INTRADAY.exists():
        return pd.DataFrame()
    try:
        from openpyxl import load_workbook
        wb = load_workbook(WB_INTRADAY, read_only=True)
        df = _ws_to_df(wb, "RAW_SPX_INTRADAY")
        wb.close()
        if df.empty:
            return pd.DataFrame()
        df.columns = [str(c).lower() for c in df.columns]
        est_col = next((c for c in df.columns if "est" in c or "datetime" in c), None)
        if est_col:
            df["datetime_est"] = pd.to_datetime(df[est_col], errors="coerce")
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["datetime_est", "close"])
        df = (df[["datetime_est", "open", "high", "low", "close", "volume"]]
              .drop_duplicates(subset=["datetime_est"])
              .sort_values("datetime_est")
              .reset_index(drop=True))
        print(f"  [5-min] {len(df)} bars  "
              f"({df['datetime_est'].dt.date.min()} → {df['datetime_est'].dt.date.max()})")
        return df
    except Exception as e:
        print(f"  [WARN] medusa_intraday.xlsx: {e}")
        return pd.DataFrame()


def load_intraday_bars_60m():
    """Load 60-min SPX bars from medusa_intraday.xlsx (RAW_SPX_60MIN).
    Provides deep MACD history for period classifier warm-up.
    """
    if not WB_INTRADAY.exists():
        return pd.DataFrame()
    try:
        from openpyxl import load_workbook
        wb = load_workbook(WB_INTRADAY, read_only=True)
        if "RAW_SPX_60MIN" not in wb.sheetnames:
            wb.close()
            return pd.DataFrame()
        df = _ws_to_df(wb, "RAW_SPX_60MIN")
        wb.close()
        if df.empty:
            return pd.DataFrame()
        df.columns = [str(c).lower() for c in df.columns]
        est_col = next((c for c in df.columns if "est" in c or "datetime" in c), None)
        if est_col:
            df["datetime_est"] = pd.to_datetime(df[est_col], errors="coerce")
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["datetime_est", "close"])
        df = (df[["datetime_est", "open", "high", "low", "close", "volume"]]
              .drop_duplicates(subset=["datetime_est"])
              .sort_values("datetime_est")
              .reset_index(drop=True))
        # Regular session only
        df = df[(df["datetime_est"].dt.time >= datetime.time(9, 30)) &
                (df["datetime_est"].dt.time <= datetime.time(16, 0))]
        print(f"  [60-min] {len(df)} bars  "
              f"({df['datetime_est'].dt.date.min()} → {df['datetime_est'].dt.date.max()})")
        return df
    except Exception as e:
        print(f"  [WARN] RAW_SPX_60MIN: {e}")
        return pd.DataFrame()


def load_prior_scores(date):
    """Load last 3 regime scores from medusa_v2_results.csv for prior_avg blend."""
    if not RESULTS_CSV.exists():
        return 0.0, []
    try:
        res = pd.read_csv(RESULTS_CSV, parse_dates=["date"])
        past = res[res["date"] < pd.Timestamp(date)].sort_values("date").tail(3)
        scores = [float(r["regime_score"]) for _, r in past.iterrows()
                  if float(r.get("regime_score", 0.0)) != 0.0]
        prior_avg = float(np.mean(scores)) if scores else 0.0
        return prior_avg, scores
    except Exception:
        return 0.0, []


def load_prior_context(date):
    """Load last 10 days of regime/signal context for the brief header."""
    if not RESULTS_CSV.exists():
        return pd.DataFrame()
    try:
        res = pd.read_csv(RESULTS_CSV, parse_dates=["date"])
        past = res[res["date"] < pd.Timestamp(date)].sort_values("date").tail(10)
        return past
    except Exception:
        return pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — BUILD BRIEF
# ═════════════════════════════════════════════════════════════════════════════

def build_brief(date_str, oi_path=None):
    """
    Build and return the 15:15 decision brief string.
    date_str: 'YYYY-MM-DD'
    oi_path:  path to CBOE spx_quotedata.csv (optional)
    """
    date   = pd.Timestamp(date_str)
    lines  = []
    W      = 62

    def line(s=""): lines.append(s)
    def rule(c="─"): lines.append(c * W)
    def hdr(s):  lines.append(f"  {s}")
    def kv(k, v): lines.append(f"  {k:<28}{v}")

    print("\n  [1/5] Loading daily data...")
    spx_all, vix_all = load_daily_data()
    if spx_all.empty:
        return "ERROR: No daily SPX data. Run medusa_fetch.py first."

    print("  [2/5] Loading intraday bars...")
    bars_5m = load_intraday_bars()
    bars_1h = load_intraday_bars_60m()
    if bars_5m.empty:
        return "ERROR: No intraday bars. Run: python medusa_fetch.py --mode intraday"

    # ── Locate today in spx_all ───────────────────────────────────────────────
    # TODAY is often not in the daily workbook (it only goes to yesterday's close).
    # Strategy: use the most recent available row as "prior" regardless.
    target_date = pd.Timestamp(date_str)
    today_in_wb = target_date in spx_all.index

    if today_in_wb:
        try:
            full_idx = spx_all.index.get_loc(target_date)
        except Exception:
            full_idx = len(spx_all) - 1
    else:
        # today not in workbook — treat last row as prior (yesterday's close)
        full_idx = len(spx_all)   # conceptually "after" last row

    if full_idx == 0:
        return f"ERROR: Not enough history for {date_str}."

    prior_row  = spx_all.iloc[full_idx - 1]
    atr        = float(prior_row.get("atr17", 50.0))

    # ── S0: prior close (the anchor for the day's prediction) ────────────────
    # When today is not yet in the daily workbook, prior_row IS yesterday's close.
    # That is the correct S0 — what the market closed at before today opened.
    s0 = float(prior_row["close"])

    # ── vol_ema32: derive from 5-min bars using daily totals (correct scale) ──
    # compute_vol_ema32_intraday sums volumes per day then takes EMA(32).
    # This matches the same scale as cumvol (sum of today's bars).
    _vol_ema32_intra = compute_vol_ema32_intraday(bars_5m) if not bars_5m.empty else None
    if _vol_ema32_intra and _vol_ema32_intra > 0:
        vol_ema32 = _vol_ema32_intra
        print(f"  [vol] ema32_intra={vol_ema32:,.0f}  (from 5-min daily totals)")
    else:
        _vol_wb = float(prior_row.get("vol_ema32", 0))
        vol_ema32 = _vol_wb if 0 < _vol_wb < 1e10 else 1e9
        print(f"  [vol] ema32_intra=None → using workbook: {vol_ema32:,.0f}")

    # ── VIX: use last available if today not in workbook ─────────────────────
    vix = float(vix_all.get(target_date,
                vix_all.iloc[-1] if len(vix_all) else 20.0))

    exponent = compute_exponent(atr, vix)

    # ── Daily MACD: use prior_row's index date, not target_date ──────────────
    # get_daily_macd_context looks up target_date in spx_all; if it's missing
    # (today not in workbook) it returns zeros. Use prior row's date instead.
    _dmacd_date = prior_row.name  # prior_row's actual date
    dmacd = get_daily_macd_context(spx_all, _dmacd_date)
    # Shift by one: we want hist at close of prior day, slope = hist[t-1] - hist[t-2]
    # get_daily_macd_context already returns iloc[idx-1] when called with today's date
    # but when called with prior_row.name it returns iloc[idx-2]. Correct by calling
    # with target_date and falling back gracefully when missing:
    if today_in_wb:
        dmacd = get_daily_macd_context(spx_all, target_date)
    else:
        # Manually extract from last two rows
        if len(spx_all) >= 2 and "daily_macd_hist" in spx_all.columns:
            hist_now  = float(spx_all["daily_macd_hist"].iloc[-1])
            hist_prev = float(spx_all["daily_macd_hist"].iloc[-2])
            slope     = hist_now - hist_prev
            bull      = hist_now > 0
            accel     = (bull and slope > 0) or (not bull and slope < 0)
            decel     = not accel and slope != 0
            if bull and slope > 0:       label = "bull_accel"
            elif bull and slope < 0:     label = "bull_decel"
            elif not bull and slope < 0: label = "bear_accel"
            elif not bull and slope > 0: label = "bear_decel"
            else:                        label = "flat"
            dmacd = {"hist": round(hist_now, 4), "slope": round(slope, 4),
                     "bull": bull, "accel": accel, "decel": decel, "label": label}
        else:
            dmacd = {"hist": 0.0, "slope": 0.0, "bull": False,
                     "accel": False, "decel": False, "label": "unknown"}

    # ATR context: use prior_row.name when today not yet in workbook
    _actx_date = target_date if today_in_wb else prior_row.name
    actx = compute_atr_context(spx_all, _actx_date)

    # ── Intraday features ─────────────────────────────────────────────────────
    _now = datetime.datetime.now().time()

    # vol_ema32_intra: computed from PURE 5-min bars (daily totals EMA).
    # Must be computed BEFORE any 60-min prepend — mixing bar sizes corrupts daily totals.
    _vol_ema32_intra = compute_vol_ema32_intraday(bars_5m) if not bars_5m.empty else None

    _cumfrac_live = build_volume_profile_cumfrac(bars_5m) if not bars_5m.empty else None
    intra = None
    if not bars_5m.empty and target_date.date() in set(bars_5m["datetime_est"].dt.date):
        intra = get_intraday_features(bars_5m, target_date.date(),
                                      vol_ema32, exponent, cutoff_time=_now,
                                      vol_ema32_intra=_vol_ema32_intra,
                                      cumfrac_table=_cumfrac_live)
    if intra is None:
        return ("ERROR: No intraday bars found for today.\n"
                "Run: python medusa_fetch.py --mode intraday")

    intra_bars = intra.pop("bars")

    # ── Prepend 60-min history for MACD warm-up ───────────────────────────────
    # Only intra_bars gets the prepend — bars_5m stays pure for vol_ema32.
    # n_warmup tracks how many bars are 60-min history so classify_regime
    # can evaluate MACD signal on 5-min bars only.
    n_warmup = 0
    if not bars_1h.empty and intra_bars is not None:
        prior_60m = bars_1h[bars_1h["datetime_est"].dt.date < target_date.date()].tail(60)
        if not prior_60m.empty:
            n_warmup = len(prior_60m)
            shared = [c for c in prior_60m.columns if c in intra_bars.columns]
            intra_bars = (pd.concat([prior_60m[shared], intra_bars[shared]], ignore_index=True)
                          .drop_duplicates(subset=["datetime_est"])
                          .sort_values("datetime_est")
                          .reset_index(drop=True))

    # ── Prior context ─────────────────────────────────────────────────────────
    prior_avg, prior_scores = load_prior_scores(date_str)
    context = load_prior_context(date_str)

    bull_days = bear_days = 0
    streak_dir = streak_n = None
    if not context.empty:
        signals = context["dir_signal"].tolist()
        bull_days = signals.count("BULL")
        bear_days = signals.count("BEAR")
        streak_dir = signals[-1]
        streak_n   = 1
        for s in reversed(signals[:-1]):
            if s == streak_dir: streak_n += 1
            else: break

    # ── Regime classification — per-period weighted composite ─────────────────
    print("  [3/5] Running regime classifier...")
    period_result = classify_periods(
        intra_bars, s0, atr, vix,
        prior_score_avg=prior_avg,
        daily_macd_hist=dmacd["hist"],
        daily_macd_slope=dmacd["slope"],
        n_warmup=n_warmup)

    if period_result is not None:
        regime        = period_result["composite"]
        per_period    = period_result["per_period"]
        avail_periods = period_result["available"]
        period_weights= period_result["weights"]
    else:
        regime = classify_regime(
            intra_bars, s0, atr, vix,
            prior_score_avg=prior_avg,
            daily_macd_hist=dmacd["hist"],
            daily_macd_slope=dmacd["slope"],
            n_warmup=n_warmup)
        per_period    = {}
        avail_periods = []
        period_weights= {}

    _last_t    = intra.get("last_time", "15:15")
    _cumfrac   = VOLUME_PROFILE_CUMFRAC.get(
        _last_t,
        next((VOLUME_PROFILE_CUMFRAC[k] for k in sorted(VOLUME_PROFILE_CUMFRAC.keys(), reverse=True)
              if k <= _last_t), 0.01))
    _daily_equiv = float(intra.get("cumvol", 0)) / _cumfrac if _cumfrac > 0 else float(intra.get("cumvol", 0))
    vol_ratio  = float(np.clip(_daily_equiv / vol_ema32 if vol_ema32 > 0 else 1.0, 0.0, 5.0))

    # ── GEX — real if chain provided, else synthetic ──────────────────────────
    print("  [4/5] Computing GEX...")
    gex = None
    oi_used = oi_path or (str(OI_DEFAULT) if OI_DEFAULT.exists() else None)
    if oi_used:
        gex = parse_real_gex(oi_used, intra["price_b"], vix, atr)
        if gex:
            print(f"         Real GEX from {oi_used}  (net={gex['net_gex']:+,.0f}  PCR={gex['pcr']:.3f})")
    if gex is None:
        gex = compute_synthetic_gex(s0, intra["price_b"], atr, vix,
                                    dmacd["hist"], regime["score"])
        print("         sGEX (synthetic) — pass --oi spx_quotedata.csv for real GEX")

    # ── Prediction ────────────────────────────────────────────────────────────
    print("  [5/5] Building prediction...")
    pred_b = predict_shot_b(s0, vix, atr, exponent, intra, regime,
                            daily_macd_hist=dmacd["hist"],
                            sgex_disp=gex["displacement"])

    _rs = regime["state"]
    if _rs in ("BULL_3","BEAR_3"):               atr_residual = atr * 0.35
    elif _rs in ("BULL_2","BEAR_2"):             atr_residual = atr * 0.40
    elif _rs in ("BULL_1","BEAR_1"):             atr_residual = atr * 0.45
    elif _rs in ("NEUTRAL_BULL","NEUTRAL_BEAR"): atr_residual = atr * 0.50
    else:                                        atr_residual = atr * 0.60
    eod_lo = pred_b - atr_residual
    eod_hi = pred_b + atr_residual

    dir_res = direction_score(s0, pred_b, vix, intra["w_t"], intra,
                              vol_ratio, regime)
    bear_alpha = (dir_res["signal"] == "BEAR" and
                  dir_res["confidence"] == "strong" and
                  vix < 30.0 and
                  dir_res.get("vol_flag", "normal") in ("normal", "caution"))

    # ════════════════════════════════════════════════════════════════════════
    # FORMAT BRIEF
    # ════════════════════════════════════════════════════════════════════════

    rule("═")
    hdr(f"MEDUSA-ORACLE  |  {date_str}  |  {intra['last_time']} EST")
    rule("═")
    line()
    kv("S0 (prior close):",    f"{s0:,.2f}")
    kv("Price at snap:",       f"{intra['price_b']:,.2f}")
    kv("VWAP (session):",      f"{intra['vwap']:,.2f}")
    kv("ATR(17):",             f"{atr:.1f} pts")
    kv("ATR(composite):",         f"{atr:.1f} pts  (regime={actx['atr_regime']}  slope={actx['atr_slope']:+.3f})")
    kv("VIX:",                    f"{vix:.1f}")
    kv("Vol pace:",            f"{intra['pace_regime']}  ({intra['pace']:.2f}x)")
    line()

    rule()
    hdr("PRIOR 10-DAY CONTEXT")
    rule()
    if not context.empty:
        kv("Bull/Bear days:", f"{bull_days}B / {bear_days}bear  (of {len(context)})")
        if streak_dir:
            kv("Current streak:", f"{streak_n}x {streak_dir}")
        kv("Daily MACD:",
           f"{dmacd['label']}  (hist={dmacd['hist']:+.3f}  slope={dmacd['slope']:+.3f})")
        kv("Prior avg score:", f"{prior_avg:+.3f}  (last {len(prior_scores)} scores)")
    else:
        kv("Prior context:", "medusa_v2_results.csv not found — no context loaded")
    line()

    # ── Regime block ──────────────────────────────────────────────────────────
    rule()
    src_regime = "per-period weighted composite" if avail_periods else "single-pass fallback"
    hdr(f"REGIME CLASSIFICATION  [{src_regime}]")
    rule()
    kv("State:",           f"{regime['state']}")
    kv("Raw score:",       f"{regime['score']:+.4f}")
    kv("Effective score:", f"{regime['effective_score']:+.4f}  (prior_avg: {prior_avg:+.3f})")
    line()
    if avail_periods:
        hdr("  Period trajectory:")
        period_times = {"p1":"09:30–11:30","p2":"11:30–13:00",
                        "p3":"13:00–14:15","p4":"14:15–15:15"}
        for p in ["p1","p2","p3","p4"]:
            if p in per_period:
                pr = per_period[p]
                w  = period_weights.get(p, 0.0)
                arrow = "▲" if pr["score"] > 0.05 else "▼" if pr["score"] < -0.05 else "·"
                lines.append(f"    {p} {period_times[p]}  {arrow}  "
                             f"{pr['state']:<22}  score={pr['score']:+.3f}  w={w:.2f}")
            else:
                lines.append(f"    {p}  — (insufficient bars)")
        line()
    hdr("  Composite components:")
    for comp, val in regime["components"].items():
        arrow = "▲" if val > 0 else "▼" if val < 0 else "·"
        lines.append(f"    {comp:<16} {arrow}  {val:+.3f}")
    lines.append(
        f"    {'[ADX raw]':<16}    {regime.get('adx', 0.0):.1f}  "
        f"(+DI={regime.get('plus_di', 0.0):.1f}  "
        f"-DI={regime.get('minus_di', 0.0):.1f}  "
        f"clarity={regime.get('trend_clarity_raw', 0.0):+.3f})")
    line()

    # ── GEX block ─────────────────────────────────────────────────────────────
    rule()
    src_tag = "REAL GEX (CBOE chain)" if gex["source"] == "real_cboe" else "sGEX (synthetic)"
    hdr(f"GEX  [{src_tag}]")
    rule()
    if gex["source"] == "real_cboe":
        kv("Net GEX:",    f"{gex['net_gex']:+,.0f}  "
                         f"({'+ dealer long γ → dampening' if gex['net_gex'] > 0 else '- dealer short γ → amplifying'})")
        kv("Call GEX:",   f"{gex['call_gex']:+,.0f}")
        kv("Put GEX:",    f"{gex['put_gex']:+,.0f}")
        kv("GEX center:", f"{gex['gex_center']:.1f}  "
                         f"({gex['gex_center'] - intra['price_b']:+.1f} vs snap)")
        kv("GEX nudge:",  f"{gex['displacement']:+.2f} pts  [{gex['label']}]")
        kv("PCR (0DTE):", f"{gex['pcr']:.3f}  "
                         f"({'put-heavy' if gex['pcr'] > 1.1 else 'call-heavy' if gex['pcr'] < 0.9 else 'neutral'})")
        kv("Max pain:",   f"{gex['max_pain']:.0f}  "
                         f"({gex['max_pain'] - intra['price_b']:+.1f} vs snap)")
        kv("Call walls:", "  ".join(f"{k:.0f}" for k in gex['call_walls']) or "none")
        kv("Put walls:",  "  ".join(f"{k:.0f}" for k in gex['put_walls']) or "none")
    else:
        kv("sGEX disp:",  f"{gex['displacement']:+.2f} pts  [{gex['label']}]")
        kv("Note:",       "Run with --oi spx_quotedata.csv for real GEX")
    line()

    rule()
    hdr("EOD ESTIMATE")
    rule()
    kv("Predicted close:", f"{pred_b:,.2f}")
    kv("Probable range:",  f"{eod_lo:,.1f}  –  {eod_hi:,.1f}")
    kv("Range width:",     f"± {atr_residual:.1f} pts  ({atr_residual/s0*100:.2f}%)")
    line()

    rule()
    hdr("SIGNAL")
    rule()
    kv("Direction:",   f"{dir_res['signal']}")
    kv("Confidence:",  f"{dir_res['confidence']}  "
                      f"(bull_p={dir_res['bull_prob']:.3f}  bear_p={dir_res['bear_prob']:.3f})")
    kv("Note:",        dir_res["note"])
    line()

    if dir_res["signal"] in ("BULL", "BEAR"):
        direction = "call" if dir_res["signal"] == "BULL" else "put"
        otm_base  = REGIME_OTM.get(regime["state"], 10)

        rule()
        hdr(f"STRIKE SELECTION  ({direction.upper()}S)")
        rule()
        kv("Regime OTM rule:", f"ATM + {otm_base} pts  ({regime['state']})")
        if bear_alpha:
            otm_base_adj = max(otm_base - 3, 5)
            kv("Bear Alpha:", f"YES ★  → OTM tightened to {otm_base_adj} pts")
        else:
            kv("Bear Alpha:", "NO")

        sigma = (gex["chain_iv"] if gex.get("chain_iv") else iv_from_vix(vix))
        price_ref = intra["price_b"]

        for label_s, delta_otm in [("conservative", -3), ("standard", 0), ("aggressive", +3)]:
            otm_final = max((otm_base - 3 if bear_alpha else otm_base) + delta_otm, 5)
            if direction == "put":
                K = round((price_ref - otm_final) / 5) * 5
            else:
                K = round((price_ref + otm_final) / 5) * 5
            prem = entry_prem(price_ref, K, direction, sigma)

            # Look up real bid from chain if available
            real_bid = ""
            if gex["source"] == "real_cboe" and oi_used:
                try:
                    _df = pd.read_csv(oi_used, skiprows=3, header=0)
                    _df.columns = [c.strip() for c in _df.columns]
                    _expiry_col = _df.columns[0]
                    _today_exp  = _df[_expiry_col].astype(str).str.strip().iloc[0]
                    _td = _df[_df[_expiry_col].astype(str).str.strip() == _today_exp].copy()
                    def _sf(x):
                        try: return float(str(x).replace(',',''))
                        except: return 0.0
                    _td['strike'] = _td['Strike'].apply(_sf)
                    _row = _td[_td['strike'] == K]
                    if not _row.empty:
                        bid_col = 'Bid' if direction == 'call' else 'Bid.1'
                        _bid = _sf(_row[bid_col].iloc[0])
                        if _bid > 0:
                            real_bid = f"  real_bid=${_bid:.2f}"
                except Exception:
                    pass

            mark = " ◄" if label_s == "standard" else ""
            lines.append(f"    {label_s:<14} K={K:>6}  OTM={otm_final:>3}pts  "
                         f"BS≈${prem:.2f}{real_bid}{mark}")
        line()
        kv("SEZ watch:", "ARMED — watch 15:55" if vix < 20 else
                        f"CAUTION — VIX {vix:.0f} (elevated)")
    else:
        line()
        lines.append(f"  NO TRADE — {dir_res['note']}")

    line()
    rule("═")
    lines.append(f"  ▶ HUMAN DECISION REQUIRED  |  Hold to 16:00 expiry")
    rule("═")
    line()

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Medusa-Oracle — standalone EOD prediction engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python medusa_oracle.py
  python medusa_oracle.py --date 2026-04-03
  python medusa_oracle.py --oi spx_quotedata.csv
  python medusa_oracle.py --date 2026-04-03 --oi spx_quotedata.csv
        """)
    parser.add_argument("--date", default=str(datetime.date.today()),
                        help="Trade date YYYY-MM-DD (default: today)")
    parser.add_argument("--oi", default=None,
                        help="Path to CBOE OI CSV (default: spx_quotedata.csv if present)")
    parser.add_argument("--save", action="store_true",
                        help="Save brief to medusa_oracle_YYYY-MM-DD.txt")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║  MEDUSA-ORACLE  v2.5-fix3  —  EOD Prediction Engine         ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"\n  Date: {args.date}")
    if args.oi:
        print(f"  OI file: {args.oi}")
    elif OI_DEFAULT.exists():
        print(f"  OI file: {OI_DEFAULT}  (auto-detected)")

    brief = build_brief(args.date, oi_path=args.oi)

    print()
    print(brief)

    if args.save or True:   # always save — useful log
        out = Path(f"medusa_oracle_{args.date}.txt")
        out.write_text(brief, encoding="utf-8")
        print(f"\n  [SAVED] {out}")


if __name__ == "__main__":
    main()
