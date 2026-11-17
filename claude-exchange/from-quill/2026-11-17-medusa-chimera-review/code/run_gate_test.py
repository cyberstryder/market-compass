"""
GATED vs UNGATED — Medusa regime gate validation (desk standard, report §7).

Wiring (§7 step 1, defects fixed):
  - The gate is applied to the oracle's classify_periods() COMPOSITE at the
    real decision time for this version: 15:15 ET. Bars fed to the gate end
    at 15:15; its late window (last 12 x 5-min bars) is therefore
    14:15→15:15 — the window the report asked for, not the docstring's
    13:30→14:30.
  - apply_regime_gate is called with KEYWORD arguments (the docstring's
    positional call misbinds trend_clarity into the unused s0 slot).
  - trend_clarity passed is the RAW value: the oracle's components dict
    stores the weighted contribution (x0.10), so it is divided back out.
  - Two gate variants: as-uploaded (spacelike dead) and desk-calibrated
    (points velocity, limit 5 x ATR/sqrt(78)). Osmosis thresholds, ladder,
    immunity and WAIT are the author's in both.

Scoring (§7 step 3): decision→close residual ONLY (15:15→16:00).
  Direction from state via the gate's own _regime_direction mapping for
  both variants (NEUTRAL_BULL/BEAR families count as directional, NEUTRAL
  does not). WAIT days are stood down in the gated variant (direction 0)
  and the WAIT rate is reported — never silently dropped: the day-level
  totals count stood-down days as 0, so waiting can only help by dodging
  losses, and the fix/dull split shows what it cost on winning calls.
  Bear-side and bull-side fixes vs dulled correct calls reported separately.

Datasets:
  SPY — Erik's pinned Alpaca 1-min bars (499 sessions, 2024-10-28 →
        2026-10-26), aggregated to 5-min. Testbed scale (~SPX/10): the
        classifier and gate are ATR-relative, so states port; residual
        points are SPY points.
  SPX — yfinance ^GSPC 5-min, last 60 days (feed reach), real instrument,
        small n. ^GSPC volume is nominal; the feed convention (volume
        fallback) applies.

No threshold tuning is done anywhere in this run (§7 step 4): the
author's thresholds and the fixed 5x calibration are scored as-is;
first-half / second-half splits are reported for stability only.

Run:  ~/workspace/.venvs/marketdata/bin/python run_gate_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import medusa_oracle as mo  # noqa: E402  (the uploaded oracle, unmodified)
import gate_as_uploaded as gate_orig  # noqa: E402
import gate_calibrated as gate_cal  # noqa: E402


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
    daily_macd_slope = float(hist_macd.iloc[-1] - hist_macd.iloc[-2]) \
        if len(hist_macd) >= 2 else 0.0
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

ET = "America/New_York"
DECISION_MIN = 15 * 60 + 15  # 15:15 ET


def sanity_check_calibration():
    """Reproduce the calibration note's synthetic result before trusting it."""
    rng = np.random.default_rng(7)
    sigma = 80.0 / np.sqrt(78)
    closes = 6000 + np.cumsum(rng.normal(0, sigma, 78))
    bars = pd.DataFrame({"close": closes})
    w = gate_cal.compute_bar_weights(bars, 80.0)
    assert not np.any(w < 0.60), "normal session must flag nothing"
    closes2 = closes.copy(); closes2[40] += 54.0; closes2[50] += 36.0
    closes2[41:] += 54.0; closes2[51:] += 36.0
    w2 = gate_cal.compute_bar_weights(pd.DataFrame({"close": closes2}), 80.0)
    assert w2[40] < 0.60, "the -0.9% shock bar must be flagged"
    # NOTE: the +0.6% bar (36 pts) sits almost exactly on the gate's own
    # weight<0.60 line (0.8 x 45.3-pt limit = 36.2 pts) — whether it flags
    # depends on the underlying bar's own move. Knife-edge, not asserted.
    w0 = gate_orig.compute_bar_weights(bars, 80.0)
    assert np.all(w0 > 0.999), "uploaded filter should be dead on synthetic"
    print(f"sanity: calibrated weights OK (normal clean, -0.9% shock flagged "
          f"w={w2[40]:.3f}; +0.6% bar w={w2[50]:.3f}, on the 0.60 line); "
          f"uploaded weights dead as documented.")


def to_5m(day_1m: pd.DataFrame) -> pd.DataFrame:
    g = day_1m.resample("5min").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum")).dropna()
    g = g.reset_index().rename(columns={"ts": "datetime_est"})
    return g


def classify_gated(bars5: pd.DataFrame, s0, atr, vix, prior_score,
                   macd_h, macd_s):
    """The wired copy: oracle composite at 15:15, then each gate variant."""
    upto = bars5[bars5["datetime_est"].dt.hour * 60
                 + bars5["datetime_est"].dt.minute < DECISION_MIN]
    if len(upto) < 20:
        return None
    res = mo.classify_periods(upto, s0, atr, vix, prior_score_avg=prior_score,
                              daily_macd_hist=macd_h, daily_macd_slope=macd_s)
    if res is None:
        return None
    comp = res["composite"]
    state = comp["state"]
    eff = float(comp["effective_score"])
    tc_raw = float(np.clip(comp["components"].get("trend_clarity", 0.0) / 0.10,
                           -1.0, 1.0))
    out = {"state": state, "eff": eff, "trend_clarity": tc_raw, "bars": upto}
    for name, mod in (("orig", gate_orig), ("cal", gate_cal)):
        g = mod.apply_regime_gate(state=state, effective_score=eff,
                                  bars=upto, atr=atr, s0=s0,
                                  trend_clarity=tc_raw)
        out[name] = g
    return out


def run_dataset(label, days_iter):
    rows = []
    for day, bars5, s0, atr, ctx in days_iter:
        prior_score, macd_h, macd_s, vix = ctx
        cg = classify_gated(bars5, s0, atr, vix, prior_score, macd_h, macd_s)
        if cg is None:
            continue
        upto = cg["bars"]
        px_dec = float(upto["close"].iloc[-1])
        day_bars = bars5
        px_close = float(day_bars["close"].iloc[-1])
        residual = px_close - px_dec
        d0 = gate_orig._regime_direction(cg["state"])
        row = {"date": str(day), "state": cg["state"], "eff": cg["eff"],
               "tc": cg["trend_clarity"], "residual": residual,
               "dir_ungated": d0}
        for name in ("orig", "cal"):
            g = cg[name]
            stood_down = g["wait_signal"]
            row[f"state_{name}"] = g["state"]
            row[f"osmosis_{name}"] = g["osmosis_steps"]
            row[f"wait_{name}"] = stood_down
            row[f"pressure_{name}"] = g["contrarian_pressure"]
            row[f"spacelike_{name}"] = g["spacelike_detected"]
            row[f"dir_{name}"] = 0 if stood_down else \
                gate_orig._regime_direction(g["state"])
        rows.append(row)
    df = pd.DataFrame(rows)
    report(label, df)
    return df


def _stats(df, dircol):
    d = df[df[dircol] != 0]
    if len(d) == 0:
        return (0, float("nan"), float("nan"), 0.0)
    indir = d["residual"] * d[dircol]
    return (len(d), float((indir > 0).mean() * 100), float(indir.mean()),
            float(indir.sum()))


def _fix_dull(df, dircol, side_dir):
    """For one side: fixes = wrong ungated calls the gate stood down;
    dulled = right ungated calls the gate stood down."""
    side = df[df["dir_ungated"] == side_dir]
    wrong = side[side["residual"] * side_dir < 0]
    right = side[side["residual"] * side_dir > 0]
    fixed = wrong[wrong[dircol] == 0]
    dulled = right[right[dircol] == 0]
    return (len(side), len(wrong), len(fixed),
            float((fixed["residual"] * side_dir).sum()),
            len(right), len(dulled),
            float((dulled["residual"] * side_dir).sum()))


def report(label, df):
    print(f"\n{'=' * 74}\n  {label} — {len(df)} days classified at 15:15 ET\n{'=' * 74}")
    if df.empty:
        print("  no days"); return
    for name, col in (("UNGATED", "dir_ungated"), ("GATE as-uploaded", "dir_orig"),
                      ("GATE calibrated 5x", "dir_cal")):
        n, acc, mean_ind, tot = _stats(df, col)
        print(f"  {name:18s} traded {n:3d} d  sign {acc:5.1f}%  "
              f"mean in-dir {mean_ind:+6.2f} pts  total {tot:+8.1f} pts")
    cal = df
    print(f"  calibrated gate: osmosis on {(cal['osmosis_cal'] > 0).mean()*100:.0f}% of days, "
          f"WAIT {(cal['wait_cal']).mean()*100:.0f}%, "
          f"spacelike flagged {cal['spacelike_cal'].mean()*100:.0f}%")
    print(f"  uploaded gate:   osmosis on {(cal['osmosis_orig'] > 0).mean()*100:.0f}% of days, "
          f"WAIT {(cal['wait_orig']).mean()*100:.0f}%, "
          f"spacelike flagged {cal['spacelike_orig'].mean()*100:.0f}%")
    for side, nm in ((-1, "BEAR"), (1, "BULL")):
        ns, nw, nf, saved, nr, nd, lost = _fix_dull(df, "dir_cal", side)
        print(f"  {nm} side (calibrated): {ns} calls — wrong {nw}, stood down {nf} "
              f"(saved {saved:+.1f} pts); right {nr}, stood down {nd} (gave up {lost:+.1f} pts)")
    strong = df[df["state"].isin(["BULL_2", "BEAR_2", "BULL_1", "BEAR_1"])]
    if len(strong):
        print(f"  directional states only (BULL/BEAR 1-2, n={len(strong)}):")
        for name, col in (("UNGATED", "dir_ungated"), ("GATE cal", "dir_cal")):
            n, acc, mean_ind, tot = _stats(strong, col)
            print(f"    {name:10s} traded {n:3d} d  sign {acc:5.1f}%  "
                  f"mean in-dir {mean_ind:+6.2f}  total {tot:+8.1f}")
    half = len(df) // 2
    for nm, part in (("first half", df.iloc[:half]), ("second half", df.iloc[half:])):
        n0, a0, m0, t0 = _stats(part, "dir_ungated")
        n1, a1, m1, t1 = _stats(part, "dir_cal")
        print(f"  {nm:11s}: ungated total {t0:+7.1f} (n={n0}, {a0:.0f}%)  "
              f"gated-cal total {t1:+7.1f} (n={n1}, {a1:.0f}%)")


def spy_days():
    DATA = HERE / "data"
    if not (DATA / "SPY_1min.parquet").exists():
        DATA = Path.home() / "workspace" / "chimera" / "spy-data"
    if not (DATA / "SPY_1min.parquet").exists():
        print("SPY dataset not found (expected data/SPY_1min.parquet) — "
              "skipping the 499-day testbed run.")
        return
    m = pd.read_parquet(DATA / "SPY_1min.parquet")
    m["ts"] = m["ts"].dt.tz_convert(ET)
    m = m.set_index("ts").sort_index().between_time("09:30", "15:59")
    m = m[["open", "high", "low", "close", "volume"]].astype(float)
    d = pd.read_parquet(DATA / "SPY_daily.parquet")
    d["ts"] = d["ts"].dt.tz_convert(ET)
    d = d.set_index("ts").sort_index()
    d = d[["open", "high", "low", "close", "volume"]].astype(float)
    import yfinance as yf
    vix = yf.download("^VIX", start="2024-06-01", end="2026-10-28",
                      auto_adjust=False, progress=False)
    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = [c[0].lower() for c in vix.columns]
    else:
        vix.columns = [str(c).lower() for c in vix.columns]
    vix.index = pd.to_datetime(vix.index).tz_localize(ET)
    for day, day_1m in m.groupby(m.index.date):
        if len(day_1m) < 200:
            continue
        bars5 = to_5m(day_1m)
        ctx = prior_context(d, vix, day)
        yield day, bars5, float(day_1m["open"].iloc[0]), daily_atr(d, day), ctx


def spx_days():
    import yfinance as yf
    h = yf.download("^GSPC", period="60d", interval="5m",
                    auto_adjust=False, progress=False)
    if isinstance(h.columns, pd.MultiIndex):
        h.columns = [c[0] for c in h.columns]
    h.columns = [str(c).lower() for c in h.columns]
    h.index = pd.to_datetime(h.index)
    if h.index.tz is not None:
        h = h.tz_convert(ET)
    h = h.between_time("09:30", "15:59")
    d = yf.download("^SPX", period="2y", interval="1d",
                    auto_adjust=False, progress=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0] for c in d.columns]
    d.columns = [str(c).lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(ET)
    vix = yf.download("^VIX", period="2y", interval="1d",
                      auto_adjust=False, progress=False)
    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = [c[0].lower() for c in vix.columns]
    else:
        vix.columns = [str(c).lower() for c in vix.columns]
    vix.index = pd.to_datetime(vix.index).tz_localize(ET)
    for day, day_5m in h.groupby(h.index.date):
        if len(day_5m) < 60:
            continue
        bars5 = day_5m.reset_index().rename(columns={"index": "datetime_est",
                                                     "Datetime": "datetime_est"})
        if "datetime_est" not in bars5.columns:
            bars5 = bars5.rename(columns={bars5.columns[0]: "datetime_est"})
        bars5["volume"] = bars5.get("volume", 1.0).fillna(1.0)
        ctx = prior_context(d, vix, day)
        yield day, bars5, float(day_5m["open"].iloc[0]), daily_atr(d, day), ctx


if __name__ == "__main__":
    sanity_check_calibration()
    df_spy = run_dataset("SPY pinned 1-min (testbed, SPY points)", spy_days())
    if not df_spy.empty:
        df_spy.to_csv(HERE / "gate-test-spy-daily.csv", index=False)
    df_spx = run_dataset("SPX yfinance 5-min (real instrument)", spx_days())
    if not df_spx.empty:
        df_spx.to_csv(HERE / "gate-test-spx-daily.csv", index=False)
    print("\nWrote gate-test daily CSVs next to this script.")
