"""Replay the only seated 0DTE family — scanner `session_sweep_reclaim` — on the underlying.

Mirrors compass/scanner.py technical_candidates + market.levels:
  levels: prior_high/low = prior RTH session extremes (session must be complete); overnight_high/low =
  bars in [open-15.5h, open) (extended-hours bars). Long: bar low < level - tick AND close > level AND
  close > previous bar high (level = prior_low or overnight_low). Short mirrored on prior_high /
  overnight_high. Stop (invalidation) = bar low - tick / bar high + tick. Signal bars close before
  14:30 CT (session_open: now < close-1800).
Outcome on the underlying (production buys a 0DTE option, exits +/-50% premium or 14:55 flatten):
  entry = next bar open +/- $0.01; fixed horizons +30/+60/+120 min (open of that bar) and the
  14:55 CT flatten bar; bracket stop=invalidation, target=2R, bar extremes, stop-first, 14:55 time stop.
Primary sample: first fire per (symbol, day, side); all fires counted.
"""
from __future__ import annotations
import sys, pathlib
import numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import swing_replay as sr

DATA = pathlib.Path(sys.argv[1]); OUT = pathlib.Path(sys.argv[2]); OUT.mkdir(exist_ok=True, parents=True)
sr.DATA = DATA
TICK = 0.01; SLIP = 0.01
CT = "America/Chicago"


def load_all(sym):
    df = pd.read_parquet(DATA / f"{sym}.parquet", columns=["ts", "open", "high", "low", "close", "volume"])
    t = df["ts"].dt.tz_convert(CT).dt.tz_localize(None)
    df["ct"] = t; df["day"] = t.dt.date
    df["open_ct"] = df["day"].map(sr.OPEN_CT); df["close_ct"] = df["day"].map(sr.CLOSE_CT)
    return df[df["open_ct"].notna()].reset_index(drop=True)


def run_symbol(sym):
    df = load_all(sym)
    rth = (df["ct"] >= df["open_ct"]) & (df["ct"] < df["close_ct"])
    r = df[rth].reset_index(drop=True)
    if len(r) < 1000:
        return []
    # prior-session levels (complete sessions only: bar count == session minutes)
    g = r.groupby("day")
    agg = g.agg(h=("high", "max"), l=("low", "min"), n=("close", "size"), oc=("open_ct", "first"), cc=("close_ct", "first"))
    agg["minutes"] = ((agg["cc"] - agg["oc"]).dt.total_seconds() // 60).astype(int)
    agg["complete"] = agg["n"] == agg["minutes"]
    days = list(agg.index)
    prior = {}
    for k in range(1, len(days)):
        d, pd_ = days[k], days[k - 1]
        if agg.loc[pd_, "complete"]:
            prior[d] = (agg.loc[pd_, "h"], agg.loc[pd_, "l"])
    # overnight levels from all bars in [open-15.5h, open)
    ext = df[~rth]
    on = {}
    for d in days:
        o = sr.OPEN_CT[d]
        w = ext[(ext["ct"] >= o - pd.Timedelta(hours=15.5)) & (ext["ct"] < o)]
        if len(w):
            on[d] = (w["high"].max(), w["low"].min())
    o = r["open"].values; h = r["high"].values; l = r["low"].values; c = r["close"].values
    day = r["day"].values; n = len(r)
    m2c = ((r["close_ct"] - r["ct"]).dt.total_seconds() // 60).astype(int).values
    didx = pd.Series(range(n)).groupby(r["day"]).agg(["min", "max"])
    flat_idx = {}
    for d, (a, b) in didx.iterrows():
        cand = np.flatnonzero(m2c[a:b + 1] >= 5)  # 14:55 CT flatten bar
        flat_idx[d] = a + cand[-1] if len(cand) else b
    # ATR14 on 1-min (scanner's atr14) for reporting
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    atr1 = pd.Series(tr).rolling(14).mean().values
    trades = []
    prevh = np.roll(h, 1); prevl = np.roll(l, 1)
    same = np.roll(day, 1) == day; same[0] = False
    for d, (a, b) in didx.iterrows():
        refs = []
        if d in prior:
            refs += [("prior_high", prior[d][0], -1), ("prior_low", prior[d][1], 1)]
        if d in on:
            refs += [("overnight_high", on[d][0], -1), ("overnight_low", on[d][1], 1)]
        if not refs:
            continue
        seen = set()
        for i in range(a + 1, b + 1):
            if m2c[i] <= 30 or i + 1 >= n or day[i + 1] != d:
                continue
            for name, level, sign in refs:
                if sign == 1:
                    swept = l[i] < level - TICK and c[i] > level and c[i] > prevh[i]
                    stop = l[i] - TICK
                else:
                    swept = h[i] > level + TICK and c[i] < level and c[i] < prevl[i]
                    stop = h[i] + TICK
                if not swept:
                    continue
                e = i + 1
                entry = o[e] + sign * SLIP
                risk = sign * (entry - stop)
                if risk <= 0:
                    continue
                target = entry + sign * 2 * risk
                j_end = flat_idx[d]
                if e > j_end:
                    continue
                hh = h[e:j_end + 1]; ll = l[e:j_end + 1]
                if sign == 1:
                    sh = np.flatnonzero(ll <= stop); th = np.flatnonzero(hh >= target)
                else:
                    sh = np.flatnonzero(hh >= stop); th = np.flatnonzero(ll <= target)
                ks = sh[0] if len(sh) else None; kt = th[0] if len(th) else None
                if ks is not None and (kt is None or ks <= kt):
                    x = e + ks; px = stop - sign * SLIP; reason = "stop"
                    if (sign == 1 and o[x] < stop) or (sign == -1 and o[x] > stop): px = o[x] - sign * SLIP
                elif kt is not None:
                    x = e + kt; px = target - sign * SLIP; reason = "target"
                else:
                    x = j_end; px = c[x] - sign * SLIP; reason = "flatten"
                def hz(k):
                    return sign * (o[e + k] / entry - 1) * 1e4 if e + k <= j_end else np.nan
                first = (d, sign) not in seen; seen.add((d, sign))
                trades.append(dict(symbol=sym, side="long" if sign == 1 else "short", level=name, day=str(d),
                                   signal_ct=str(r["ct"].iat[i]), first_fire=first, entry=entry, stop=stop, risk=risk,
                                   risk_bps=risk / entry * 1e4, risk_atr1=risk / atr1[i] if atr1[i] > 0 else np.nan,
                                   R=sign * (px - entry) / risk, bps=sign * (px - entry) / entry * 1e4, reason=reason,
                                   h30=hz(30), h60=hz(60), h120=hz(120),
                                   close=sign * (c[j_end] / entry - 1) * 1e4, min_to_close=int(m2c[i])))
    return trades


if __name__ == "__main__":
    syms = [s.strip() for s in open(DATA / "symbols.txt") if s.strip()]
    if len(sys.argv) > 3: syms = sys.argv[3].split(",")
    rows = []
    for k, s in enumerate(syms):
        t = run_symbol(s); rows.extend(t)
        print(f"{k+1}/{len(syms)} {s} fires={len(t)}", file=sys.stderr, flush=True)
    pd.DataFrame(rows).to_csv(OUT / "sweep_trades.csv", index=False)
    print(len(rows))
