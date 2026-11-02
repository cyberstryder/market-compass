"""Replay Compass swing-ideas-v1 technical rules on data-foundation-v2-stocks, scored on the underlying.

Mirrors compass/swing_signals.py (daily_context + technical_setups) exactly:
  * daily context from completed RTH sessions strictly before the signal day; 60 contiguous
    sessions and 10 complete weeks required; SMA20/50, weekly SMA10 (last close of each of the
    last 10 complete weeks), Wilder RSI14 and ATR14 seeded over the whole contiguous window
    (<=160 calendar days), high20/low20, high5/low5.
  * trigger = a completed 1-min RTH bar whose close crosses a daily level versus the previous
    minute's close (sign*(prev-level) <= 0 < sign*(price-level)); signal bar must close before
    14:30 CT (close-1800).
  * rules: daily_breakout (aligned+trend, cross of high20/low20, stop = level -/+ 0.5 ATR);
    pullback_reclaim (aligned+trend, cross of SMA20, stop = low5-0.1ATR / high5+0.1ATR);
    daily_reversal (close vs SMA20 and SMA20 vs SMA50 both against the side, RSI 25-55 long /
    45-75 short, cross of SMA20, same stop). 0 < risk <= 3 ATR; target = 2R.
Outcome on the underlying (production buys an option; here the signal itself is judged):
  * entry = next 1-min bar open +/- $0.01; stop/target on bar extremes from the entry bar on,
    stop first if both touched in one bar; stop fills $0.01 through; max hold = 10 sessions
    including the entry session, exit at the 10th session's close - 15 min (hold_deadline).
  * fixed horizons: directional return at close of entry day and at the 1st/3rd/5th/9th later
    session close (the swing-flow-study checkpoints), also at +60 min.
Primary sample = first fire per (symbol, day, rule, side); all fires kept as a secondary count.
"""
from __future__ import annotations
import sys, json, pathlib
from datetime import date, timedelta
import numpy as np, pandas as pd
import exchange_calendars as xc

CT = "America/Chicago"
DATA = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("/home/claude/data/v2stocks")
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/home/claude/swingtest/out")
OUT.mkdir(parents=True, exist_ok=True)
HOLD_SESSIONS = 10
SLIP = 0.01

cal = xc.get_calendar("XNYS")
sched = cal.schedule.loc["2024-10-01":"2026-11-01"]
SESS = [d.date() for d in sched.index]
SESS_IDX = {d: i for i, d in enumerate(SESS)}
OPEN_CT = {d.date(): o.tz_convert(CT).tz_localize(None) for d, o in sched["open"].items()}
CLOSE_CT = {d.date(): c.tz_convert(CT).tz_localize(None) for d, c in sched["close"].items()}


def wilder(values):
    values = list(values)
    if len(values) < 14:
        return float("nan")
    v = sum(values[:14]) / 14
    for x in values[14:]:
        v = (v * 13 + x) / 14
    return v


def daily_context(daily: dict, today: date):
    """daily: {date: (o,h,l,c)} completed sessions. Returns dict or None (warming_up)."""
    expected = [d for d in SESS if today - timedelta(days=160) <= d <= today - timedelta(days=1)]
    if len(expected) < 60:
        return None
    required = expected[-60:]
    if any(d not in daily for d in required):
        return None
    dates = []
    for d in reversed(expected):
        if d not in daily:
            break
        dates.insert(0, d)
    bars = [daily[d] for d in dates]
    weeks = {}
    this_week = today - timedelta(days=today.weekday())
    for d in expected:
        monday = d - timedelta(days=d.weekday())
        if monday < this_week:
            weeks.setdefault(monday, []).append(d)
    complete = sorted(weeks)[-10:]
    if len(complete) < 10 or any(d not in daily for w in complete for d in weeks[w]):
        return None
    weekly = [daily[weeks[w][-1]][3] for w in complete]
    closes = [b[3] for b in bars]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = wilder([max(0, x) for x in changes]); loss = wilder([max(0, -x) for x in changes])
    rsi = 100 - 100 / (1 + gain / loss) if loss else 100 if gain else 50
    tr = [max(b[1] - b[2], abs(b[1] - a[3]), abs(b[2] - a[3])) for a, b in zip(bars, bars[1:])]
    return dict(close=closes[-1], sma20=sum(closes[-20:]) / 20, sma50=sum(closes[-50:]) / 50,
                rsi14=rsi, atr14=wilder(tr), high20=max(b[1] for b in bars[-20:]),
                low20=min(b[2] for b in bars[-20:]), high5=max(b[1] for b in bars[-5:]),
                low5=min(b[2] for b in bars[-5:]),
                weekly_bias=("bullish" if weekly[-1] > sum(weekly) / 10 else
                             "bearish" if weekly[-1] < sum(weekly) / 10 else "neutral"))


def load(sym):
    df = pd.read_parquet(DATA / f"{sym}.parquet", columns=["ts", "open", "high", "low", "close", "volume"])
    t = df["ts"].dt.tz_convert(CT).dt.tz_localize(None)
    df["ct"] = t
    df["day"] = t.dt.date
    op = df["day"].map(OPEN_CT); cl = df["day"].map(CLOSE_CT)
    keep = op.notna() & (t >= op) & (t < cl)
    df = df.loc[keep].reset_index(drop=True)
    df["min_to_close"] = ((cl[keep].reset_index(drop=True) - df["ct"]).dt.total_seconds() // 60).astype(int)
    return df


def hold_deadline_idx(day_arr, days_sorted_pos, entry_day):
    """index of the last bar at or before (10th session close - 15 min)."""
    i = SESS_IDX[entry_day]
    last_day = SESS[min(i + HOLD_SESSIONS - 1, len(SESS) - 1)]
    return last_day


def run_symbol(sym):
    df = load(sym)
    if len(df) < 1000:
        return []
    # daily aggregates
    g = df.groupby("day", sort=True)
    agg = g.agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), n=("close", "size"))
    agg = agg[agg["n"] >= 300]  # a real full session
    daily = {d: (r.o, r.h, r.l, r.c) for d, r in agg.iterrows()}
    ctx = {}
    for d in agg.index:
        cx = daily_context(daily, d)
        if cx:
            ctx[d] = cx
    if not ctx:
        return []
    o = df["open"].values; h = df["high"].values; l = df["low"].values; c = df["close"].values
    day = df["day"].values; m2c = df["min_to_close"].values
    n = len(df)
    # per-bar daily levels
    lv = {k: np.full(n, np.nan) for k in ("sma20", "sma50", "rsi14", "atr14", "high20", "low20", "high5", "low5", "close")}
    bias = np.zeros(n, dtype=np.int8)
    day_idx = pd.Series(range(n)).groupby(df["day"]).agg(["min", "max"])
    for d, (a, b) in day_idx.iterrows():
        cx = ctx.get(d)
        if not cx:
            continue
        for k in lv:
            lv[k][a:b + 1] = cx[k]
        bias[a:b + 1] = 1 if cx["weekly_bias"] == "bullish" else -1 if cx["weekly_bias"] == "bearish" else 0
    prev = np.roll(c, 1); prev[0] = np.nan
    same_day = np.roll(day, 1) == day; same_day[0] = False
    valid = same_day & ~np.isnan(lv["atr14"]) & (m2c > 30) & (lv["atr14"] > 0)  # signal bar closes before close-30min
    # day-first-bar close rows: every bar first index of day: session open <= bar_start -> all RTH bars qualify
    trades = []
    sess_close_idx = {d: b for d, (a, b) in day_idx.iterrows()}
    sess_open_idx = {d: a for d, (a, b) in day_idx.iterrows()}

    def cross(level, sign):
        return (sign * (prev - level) <= 0) & (0 < sign * (c - level))

    for side, sign in (("long", 1), ("short", -1)):
        aligned = bias == sign
        trend = (sign * (c - lv["sma20"]) > 0) & (sign * (lv["sma20"] - lv["sma50"]) >= 0)
        extreme = lv["high20"] if sign == 1 else lv["low20"]
        reversing = (sign * (lv["close"] - lv["sma20"]) < 0) & (sign * (lv["sma20"] - lv["sma50"]) < 0)
        rsi_ok = ((lv["rsi14"] >= 25) & (lv["rsi14"] <= 55)) if sign == 1 else ((lv["rsi14"] >= 45) & (lv["rsi14"] <= 75))
        stop_pb = (lv["low5"] - .1 * lv["atr14"]) if sign == 1 else (lv["high5"] + .1 * lv["atr14"])
        rules = [
            ("daily_breakout", valid & aligned & trend & cross(extreme, sign), extreme - sign * .5 * lv["atr14"]),
            ("pullback_reclaim", valid & aligned & trend & cross(lv["sma20"], sign), stop_pb),
            ("daily_reversal", valid & reversing & rsi_ok & cross(lv["sma20"], sign), stop_pb),
        ]
        for rule, mask, stop_arr in rules:
            idx = np.flatnonzero(mask)
            seen_day = set()
            for i in idx:
                price, stop, atr = c[i], stop_arr[i], lv["atr14"][i]
                risk = sign * (price - stop)
                if not (0 < risk <= 3 * atr) or stop <= 0 or price + sign * 2 * risk <= 0:
                    continue
                e = i + 1
                if e >= n or day[e] != day[i]:
                    continue
                first = day[i] not in seen_day
                seen_day.add(day[i])
                entry = o[e] + sign * SLIP
                target = price + sign * 2 * risk
                # bracket walk through hold window
                d0 = day[i]
                last_day = SESS[min(SESS_IDX[d0] + HOLD_SESSIONS - 1, len(SESS) - 1)]
                # last tradable index: last bar of last_day with min_to_close >= 15, else last bar of data
                j_end = None
                if last_day in sess_close_idx:
                    b = sess_close_idx[last_day]
                    a = sess_open_idx[last_day]
                    cand = np.flatnonzero(m2c[a:b + 1] >= 15)
                    j_end = a + cand[-1] if len(cand) else b
                else:
                    # data ends before deadline -> incomplete
                    j_end = n - 1
                    complete = False
                hh = h[e:j_end + 1]; ll = l[e:j_end + 1]
                if sign == 1:
                    stop_hit = np.flatnonzero(ll <= stop); tgt_hit = np.flatnonzero(hh >= target)
                else:
                    stop_hit = np.flatnonzero(hh >= stop); tgt_hit = np.flatnonzero(ll <= target)
                ks = stop_hit[0] if len(stop_hit) else None
                kt = tgt_hit[0] if len(tgt_hit) else None
                if ks is not None and (kt is None or ks <= kt):
                    x = e + ks; exit_px = stop - sign * SLIP; reason = "stop"
                    # gap through stop: fill at open if open is beyond
                    if (sign == 1 and o[x] < stop) or (sign == -1 and o[x] > stop):
                        exit_px = o[x] - sign * SLIP
                elif kt is not None:
                    x = e + kt; exit_px = target - sign * SLIP; reason = "target"
                    if (sign == 1 and o[x] > target) or (sign == -1 and o[x] < target):
                        exit_px = o[x] - sign * SLIP
                else:
                    x = j_end; exit_px = c[x] - sign * SLIP; reason = "hold" if last_day in sess_close_idx else "incomplete"
                pnl_sh = sign * (exit_px - entry)
                R = pnl_sh / risk
                sessions_held = SESS_IDX[day[x]] - SESS_IDX[d0] + 1
                # fixed horizons (directional bps vs entry, gross)
                def hz(k):
                    di = SESS_IDX[d0] + k
                    if di >= len(SESS) or SESS[di] not in sess_close_idx:
                        return np.nan
                    return sign * (c[sess_close_idx[SESS[di]]] / entry - 1) * 1e4
                h60 = sign * (o[e + 60] / entry - 1) * 1e4 if e + 60 < n and day[e + 60] == d0 else np.nan
                trades.append(dict(symbol=sym, side=side, rule=rule, day=str(d0), signal_ct=str(df["ct"].iat[i]),
                                   first_fire=first, signal_price=price, entry=entry, stop=stop, target=target,
                                   risk=risk, risk_atr=risk / atr, atr=atr, exit=exit_px, exit_ct=str(df["ct"].iat[x]),
                                   reason=reason, sessions_held=sessions_held, R=R, bps=pnl_sh / entry * 1e4,
                                   h60=h60, close0=hz(0), s1=hz(1), s3=hz(3), s5=hz(5), s9=hz(9),
                                   bias=int(bias[i]), rsi14=lv["rsi14"][i], min_to_close=int(m2c[i])))
    return trades


if __name__ == "__main__":
    syms = [s.strip() for s in open(DATA / "symbols.txt") if s.strip()]
    if len(sys.argv) > 3:
        syms = sys.argv[3].split(",")
    rows = []
    for k, s in enumerate(syms):
        try:
            t = run_symbol(s)
        except Exception as ex:  # keep going, report
            print("ERR", s, ex, file=sys.stderr); t = []
        rows.extend(t)
        print(f"{k+1}/{len(syms)} {s} fires={len(t)}", file=sys.stderr, flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "swing_trades.csv", index=False)
    print(len(df), "trades written")
