"""Hour-matched other-day placebo for swing_trades.csv.

For every first-fire real trade: draw K other sessions of the same symbol (uniform over sessions with a
full RTH day and a rolling daily ATR14), enter at the same clock minute, same side, same stop
distance as a multiple of that day's ATR14, same 2R target, same 10-session hold. Record R and the
fixed-horizon bps. Output placebo_draws.csv (one row per real trade per draw).
"""
from __future__ import annotations
import sys, pathlib
import numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import swing_replay as sr

K = int(sys.argv[3]) if len(sys.argv) > 3 else 20
rng = np.random.default_rng(7)
DATA = pathlib.Path(sys.argv[1]); OUT = pathlib.Path(sys.argv[2])
sr.DATA = DATA
tr = pd.read_csv(OUT / "swing_trades.csv")
tr = tr[tr.first_fire].copy()
tr["ct"] = pd.to_datetime(tr["signal_ct"])
tr["minute"] = tr["ct"].dt.hour * 60 + tr["ct"].dt.minute

rows = []
for sym, grp in tr.groupby("symbol"):
    df = sr.load(sym)
    o = df["open"].values; h = df["high"].values; l = df["low"].values; c = df["close"].values
    day = df["day"].values; m2c = df["min_to_close"].values; n = len(df)
    ct = df["ct"]; minute = (ct.dt.hour * 60 + ct.dt.minute).values
    g = pd.Series(range(n)).groupby(df["day"]).agg(["min", "max", "size"])
    full = g[g["size"] >= 300]
    agg = df.groupby("day").agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
    tr_ = pd.concat([agg.h - agg.l, (agg.h - agg.c.shift()).abs(), (agg.l - agg.c.shift()).abs()], axis=1).max(axis=1)
    atr_d = tr_.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().shift(1)  # prior sessions only
    days = [d for d in full.index if not np.isnan(atr_d.get(d, np.nan)) and sr.SESS_IDX.get(d, 10**9) + sr.HOLD_SESSIONS - 1 < len(sr.SESS)]
    days = [d for d in days if sr.SESS[sr.SESS_IDX[d] + sr.HOLD_SESSIONS - 1] in full.index]
    if len(days) < 50:
        continue
    open_idx = {d: int(full.loc[d, "min"]) for d in full.index}; close_idx = {d: int(full.loc[d, "max"]) for d in full.index}
    for t in grp.itertuples():
        sign = 1 if t.side == "long" else -1
        picks = rng.choice(len(days), size=K, replace=True)
        for pk in picks:
            d0 = days[pk]
            if str(d0) == t.day:
                continue
            a, b = open_idx[d0], close_idx[d0]
            seg = np.flatnonzero(minute[a:b + 1] == t.minute)
            if not len(seg):
                continue
            i = a + seg[0]
            if m2c[i] <= 30 or i + 1 >= n or day[i + 1] != d0:
                continue
            e = i + 1
            atr = atr_d[d0]
            risk = t.risk_atr * atr
            entry = o[e] + sign * sr.SLIP
            stop = entry - sign * risk; target = entry + sign * 2 * risk
            last_day = sr.SESS[sr.SESS_IDX[d0] + sr.HOLD_SESSIONS - 1]
            la, lb = open_idx[last_day], close_idx[last_day]
            cand = np.flatnonzero(m2c[la:lb + 1] >= 15); j_end = la + cand[-1] if len(cand) else lb
            hh = h[e:j_end + 1]; ll = l[e:j_end + 1]
            if sign == 1:
                sh = np.flatnonzero(ll <= stop); th = np.flatnonzero(hh >= target)
            else:
                sh = np.flatnonzero(hh >= stop); th = np.flatnonzero(ll <= target)
            ks = sh[0] if len(sh) else None; kt = th[0] if len(th) else None
            if ks is not None and (kt is None or ks <= kt):
                x = e + ks; px = stop - sign * sr.SLIP
                if (sign == 1 and o[x] < stop) or (sign == -1 and o[x] > stop): px = o[x] - sign * sr.SLIP
            elif kt is not None:
                x = e + kt; px = target - sign * sr.SLIP
                if (sign == 1 and o[x] > target) or (sign == -1 and o[x] < target): px = o[x] - sign * sr.SLIP
            else:
                x = j_end; px = c[x] - sign * sr.SLIP
            R = sign * (px - entry) / risk
            def hz(k):
                di = sr.SESS_IDX[d0] + k
                if di >= len(sr.SESS) or sr.SESS[di] not in close_idx: return np.nan
                return sign * (c[close_idx[sr.SESS[di]]] / entry - 1) * 1e4
            rows.append(dict(symbol=sym, side=t.side, rule=t.rule, real_day=t.day, R=R,
                             bps=sign * (px - entry) / entry * 1e4, s1=hz(1), s3=hz(3), s5=hz(5), s9=hz(9)))
    print(sym, len(rows), file=sys.stderr, flush=True)
pd.DataFrame(rows).to_csv(OUT / "placebo_draws.csv", index=False)
print("draws", len(rows))
