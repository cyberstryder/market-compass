"""
decide_today.py — Medusa regime state + calibrated gate, for TODAY.

Fetches today's SPX 5-minute bars from yfinance, classifies the regime
with the uploaded oracle (classify_periods composite), then applies the
desk-calibrated regime gate. Prints a signal only — it places no orders
and talks to no broker.

    python decide_today.py

The oracle's decision is defined at 15:15 ET (its p4 period ends then).
Run before 15:15 ET and you get a PRELIMINARY read on completed periods
only, clearly labelled — the state can and does change into 15:15.
Run at/after 15:15 ET for the final decision.

Conventions match the desk's gate test (run_gate_test.py): S0 = today's
open, ATR = oracle ATR(17) on prior daily bars, prior context from prior
daily closes. (The author's own build_brief anchors S0 at the prior
close instead; states can differ between the two anchors.)

DESK TEST CONTEXT (2026-11-17, decision→close residual, 499 SPY days):
the ungated classifier's 15:15 sign accuracy was 49.2% and the gate made
it worse (45.7%, net −24 pts vs ungated). There is no measured edge here.
This script exists so you can watch the machinery run live, not because
the desk endorses trading it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import medusa_oracle as mo          # noqa: E402
import gate_calibrated as gate      # noqa: E402

ET = "America/New_York"


def _flat(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.columns = [str(c).lower() for c in df.columns]
    return df


def prior_context(spx_d, vix_d, current_date):
    """Prior-day context, strictly before current_date (desk harness math)."""
    hist = spx_d[spx_d.index.date < current_date]
    if len(hist) < 5:
        return 0.0, 0.0, 0.0, 18.0
    closes = hist["close"].dropna()
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist_macd = macd - signal
    rets = closes.pct_change().dropna().tail(3)
    prior_score = float(np.clip(rets.mean() / 0.008, -1, 1))
    daily_macd_hist = float(hist_macd.iloc[-1])
    daily_macd_slope = float(hist_macd.iloc[-1] - hist_macd.iloc[-2])
    vix_level = 18.0
    vix_hist = vix_d[vix_d.index.date < current_date]
    if len(vix_hist) > 0:
        vix_level = float(vix_hist["close"].iloc[-1])
    return prior_score, daily_macd_hist, daily_macd_slope, vix_level


def daily_atr(spx_d, current_date):
    """Oracle ATR(17) from daily bars strictly before the day."""
    hist = spx_d[spx_d.index.date < current_date]
    try:
        vals = pd.Series(mo.compute_atr_series(hist, period=17)).dropna()
        if len(vals) > 0:
            return float(vals.iloc[-1])
    except Exception:
        pass
    h, l, c = hist["high"], hist["low"], hist["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def main():
    import yfinance as yf
    now_et = pd.Timestamp.now(tz=ET)
    today = now_et.date()
    print(f"Medusa decision runner — {today}  (now {now_et:%H:%M} ET)")

    intra = _flat(yf.download("^GSPC", period="5d", interval="5m",
                              auto_adjust=False, progress=False))
    intra.index = pd.to_datetime(intra.index)
    if intra.index.tz is not None:
        intra = intra.tz_convert(ET)
    day = intra[intra.index.date == today].between_time("09:30", "15:59")
    if day.empty:
        print("No SPX bars for today yet (market closed or holiday).")
        return
    # completed 5-min bars only — drop the bar still forming
    day = day[day.index + pd.Timedelta(minutes=5) <= now_et]
    if len(day) < 8:
        print(f"Only {len(day)} completed bars — too early to classify.")
        return
    bars = day.reset_index().rename(columns={"index": "datetime_est",
                                             "Datetime": "datetime_est"})
    if "datetime_est" not in bars.columns:
        bars = bars.rename(columns={bars.columns[0]: "datetime_est"})
    bars["volume"] = bars.get("volume", 1.0).fillna(1.0)

    daily = _flat(yf.download("^SPX", period="2y", interval="1d",
                              auto_adjust=False, progress=False))
    daily.index = pd.to_datetime(daily.index).tz_localize(ET)
    vixd = _flat(yf.download("^VIX", period="2y", interval="1d",
                             auto_adjust=False, progress=False))
    vixd.index = pd.to_datetime(vixd.index).tz_localize(ET)

    s0 = float(day["open"].iloc[0])
    atr = daily_atr(daily, today)
    prior_score, macd_h, macd_s, vix = prior_context(daily, vixd, today)

    res = mo.classify_periods(bars, s0, atr, vix, prior_score_avg=prior_score,
                              daily_macd_hist=macd_h, daily_macd_slope=macd_s)
    if res is None:
        print("Classifier returned nothing (not enough completed periods).")
        return
    comp = res["composite"]
    state = comp["state"]
    eff = float(comp["effective_score"])
    tc_raw = float(np.clip(comp["components"].get("trend_clarity", 0.0) / 0.10,
                           -1.0, 1.0))
    g = gate.apply_regime_gate(state=state, effective_score=eff, bars=bars,
                               atr=atr, s0=s0, trend_clarity=tc_raw)

    final = (now_et.hour * 60 + now_et.minute) >= 15 * 60 + 15
    spot = float(day["close"].iloc[-1])
    d0 = gate._regime_direction(state)
    d1 = 0 if g["wait_signal"] else gate._regime_direction(g["state"])

    print(f"SPX {spot:,.2f}  (open {s0:,.2f}, ATR {atr:.1f}, VIX {vix:.1f})")
    print(f"Status: {'FINAL 15:15 decision' if final else 'PRELIMINARY — decision finalizes 15:15 ET'}")
    print(f"Ungated:  {state}  (effective score {eff:+.3f})  "
          f"direction {'LONG' if d0 > 0 else 'SHORT' if d0 < 0 else 'NONE'}")
    print(f"Gate:     {g['state']}  osmosis {g['osmosis_steps']} step(s), "
          f"pressure {g['contrarian_pressure']:.3f}, "
          f"spacelike {'FLAGGED' if g['spacelike_detected'] else 'no'}, "
          f"WAIT {'YES' if g['wait_signal'] else 'no'}")
    if g["wait_signal"]:
        print("SIGNAL:   STAND DOWN (gate says WAIT)")
    elif d1 > 0:
        print("SIGNAL:   LONG bias into the close")
    elif d1 < 0:
        print("SIGNAL:   SHORT bias into the close")
    else:
        print("SIGNAL:   NONE (neutral)")
    print("\nDesk test context: this signal's 15:15→close sign accuracy "
          "measured 49.2% over 499 days ungated, 45.7% gated. No edge "
          "measured. Signal only — no orders are placed by this script.")


if __name__ == "__main__":
    main()
