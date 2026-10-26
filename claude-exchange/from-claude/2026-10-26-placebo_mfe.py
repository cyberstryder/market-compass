#!/usr/bin/env python3
"""Placebo control + MFE/MAE study for replay trades.

Inputs: replay result JSONs (ops/replay_matrix.py output) and the 1-min bar CSVs they were run on.

Part A — pure bracket simulator over 1-min bars (shared by real and placebo):
    entry at the signal bar's close +1 tick adverse; stop filled at stop -2 ticks adverse (Task 002
    rule); target at the level; both-touched bar -> stop first (conservative); flatten 15:45 CT.
    First the REAL trades are re-simulated (sanity: should land near the engine's numbers).
Part B — placebo: for each pair, N draws of the same number of trades with entry bars drawn
    uniformly from the SAME session windows the detector traded in, random direction, and the
    real trades' stop/target distances (in points) re-used. Reports where the real net sits in the
    placebo distribution (percentile) and the placebo's own mean/PF.
Part C — MFE/MAE in R for every real trade (bars from entry to exit), and an exit sweep from the
    real entries: fixed targets 1R/1.5R/2R/3R, breakeven-at-1R, and trailing stops.

Usage: python -m ops.placebo_mfe --bars DIR --results 'replay/result_*.json' --pairs aoi_fade:MCL,aoi_fade:MES,turtle_soup:MCL --draws 500
"""
import argparse, bisect, glob, json, math, random, sys, os
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ops.replay_matrix import load_bars, TICKS

CT = ZoneInfo("America/Chicago")
STOP_TICKS = 2
FLATTEN = (15, 45)
HALT = ((15, 15), (15, 30))   # CME equity halt; also 16:00-17:00 maintenance handled by session gap


def ct(ts): return datetime.fromtimestamp(ts, CT)


def flatten_ts(ts):
    d = ct(ts)
    f = d.replace(hour=FLATTEN[0], minute=FLATTEN[1], second=0, microsecond=0)
    if d >= f:  # after today's flatten: next session's flatten (tomorrow 15:45)
        f = f.replace(day=f.day) ; f = datetime.fromtimestamp(f.timestamp() + 86400, CT)
    return f.timestamp()


class Bars:
    def __init__(self, rows):
        self.ts = [r[0] for r in rows]; self.o = [r[3] for r in rows]; self.h = [r[4] for r in rows]
        self.l = [r[5] for r in rows]; self.c = [r[6] for r in rows]
    def idx(self, ts):
        i = bisect.bisect_left(self.ts, ts); return i if i < len(self.ts) and self.ts[i] == ts else None
    def idx_at_or_after(self, ts):
        i = bisect.bisect_left(self.ts, ts); return i if i < len(self.ts) else None


def simulate(bars, i_entry, side, stop_pts, target_pts, tick, mult, qty, fee, stop_ticks=STOP_TICKS,
             mode="fixed", trail_r=None, be_at_r=None, max_bars=None, ambiguous="stop"):
    """Enter at close of bar i_entry (+1 tick adverse). Returns dict(pnl, exit_reason, bars, mfe_r, mae_r)."""
    sgn = 1 if side == "long" else -1
    entry = bars.c[i_entry] + sgn * tick
    stop = entry - sgn * stop_pts
    target = entry + sgn * target_pts
    risk = stop_pts
    flat = flatten_ts(bars.ts[i_entry])
    mfe = mae = 0.0
    best = entry
    n = len(bars.ts)
    j = i_entry + 1
    while j < n:
        ts = bars.ts[j]
        if ts >= flat:
            px = bars.o[j]; reason = "session_flatten"; break
        hi, lo = bars.h[j], bars.l[j]
        fav = (hi - entry) * sgn if sgn == 1 else (entry - lo)
        adv = (entry - lo) * sgn if sgn == 1 else (hi - entry)
        mfe = max(mfe, fav); mae = max(mae, adv)
        # trailing / breakeven adjustments use the previous bar's extreme (no lookahead within bar)
        if mode == "trail" and trail_r is not None:
            if sgn == 1: best = max(best, hi); cand = best - trail_r * risk
            else: best = min(best, lo); cand = best + trail_r * risk
            if mfe >= risk: stop = max(stop, cand) if sgn == 1 else min(stop, cand)
        if be_at_r is not None and mfe >= be_at_r * risk:
            stop = max(stop, entry) if sgn == 1 else min(stop, entry)
        hit_stop = lo <= stop if sgn == 1 else hi >= stop
        hit_tgt = (hi >= target if sgn == 1 else lo <= target) if target_pts else False
        if hit_stop and hit_tgt and ambiguous == "target":
            px = target; reason = "target"; break
        if hit_stop:
            px = stop - sgn * stop_ticks * tick; reason = "stop"; break
        if hit_tgt:
            px = target; reason = "target"; break
        if max_bars and j - i_entry >= max_bars:
            px = bars.c[j]; reason = "time"; break
        j += 1
    else:
        px = bars.c[n - 1]; reason = "eod"
    pnl = (px - entry) * sgn * mult * qty - fee * 2 * qty
    return dict(pnl=pnl, exit_reason=reason, bars=j - i_entry, mfe_r=mfe / risk if risk else 0, mae_r=mae / risk if risk else 0,
                r=(px - entry) * sgn / risk if risk else 0)


def summarize(pnls):
    w = [p for p in pnls if p > 0]; l = [-p for p in pnls if p <= 0]
    return dict(n=len(pnls), net=round(sum(pnls), 2), win=round(100 * len(w) / len(pnls), 1) if pnls else None,
                pf=round(sum(w) / sum(l), 2) if l and sum(l) else None, avg=round(sum(pnls) / len(pnls), 2) if pnls else None)


def session_bucket(ts):
    h = ct(ts).hour
    return "asia" if (h >= 17 or h < 2) else "london" if h < 8 else "ny_am" if h < 11 else "ny_pm"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True); ap.add_argument("--results", required=True)
    ap.add_argument("--pairs", required=True); ap.add_argument("--draws", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7); ap.add_argument("--out", default="placebo_mfe.json")
    a = ap.parse_args()
    random.seed(a.seed)
    pairs = [tuple(p.split(":")) for p in a.pairs.split(",")]
    trades = []
    for f in glob.glob(a.results): trades += json.load(open(f))["trades"]
    trades = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    bars_by = {}
    out = {}
    for det, root in pairs:
        sel = [t for t in trades if t["detector"] == det and t["symbol"].split(".")[0] == root]
        if not sel: print("no trades for", det, root); continue
        alias = sel[0]["symbol"]
        if root not in bars_by:
            rows = load_bars(glob.glob(os.path.join(a.bars, "bars_%s.*.csv" % root))[0])
            bars_by[root] = Bars(rows)
        B = bars_by[root]
        tick = TICKS[root]; mult = sel[0]["multiplier"]; qty = sel[0]["qty"]; fee = sel[0]["fee"]
        # --- A: re-simulate real trades with the pure simulator
        real = []
        for t in sel:
            # entered_at = decision time = completion of the triggering 1-min bar (bar ts + 60).
            # (signal_time is the detector's own bar timestamp — hourly for aoi_fade — so do not use it.)
            i = B.idx(t["entered_at"] - 60)
            if i is None: i = B.idx_at_or_after(t["entered_at"] - 60)
            if i is None: continue
            sgn = 1 if t["side"] == "long" else -1
            stop_pts = abs(t["entry"] - t["stop"]); tgt_pts = abs(t["target"] - t["entry"]) if t.get("target") else 0
            r = simulate(B, i, t["side"], stop_pts, tgt_pts, tick, mult, qty, fee)
            r.update(i=i, side=t["side"], stop_pts=stop_pts, tgt_pts=tgt_pts, session=session_bucket(B.ts[i]), engine_pnl=t["pnl"])
            real.append(r)
        pair = "%s|%s" % (det, root)
        res = {"real_engine": summarize([t["pnl"] for t in sel]), "real_resim": summarize([r["pnl"] for r in real])}
        # --- B: placebo — same session mix, same count, random bar in that session, random side, real geometry
        by_sess = defaultdict(list)
        for i, ts in enumerate(B.ts): by_sess[session_bucket(ts)].append(i)
        sess_counts = defaultdict(int)
        for r in real: sess_counts[r["session"]] += 1
        geoms = [(r["stop_pts"], r["tgt_pts"]) for r in real]
        nets, pfs, wins = [], [], []
        for d in range(a.draws):
            pn = []
            for sess, cnt in sess_counts.items():
                pool = by_sess[sess]
                for _ in range(cnt):
                    i = random.choice(pool); side = random.choice(("long", "short")); sp, tp = random.choice(geoms)
                    pn.append(simulate(B, i, side, sp, tp, tick, mult, qty, fee)["pnl"])
            s = summarize(pn); nets.append(s["net"]); pfs.append(s["pf"] or 0); wins.append(s["win"])
        nets.sort()
        real_net = res["real_resim"]["net"]
        pct = 100.0 * bisect.bisect_left(nets, real_net) / len(nets)
        res["placebo"] = dict(draws=a.draws, mean_net=round(sum(nets) / len(nets), 2), p5=nets[int(.05 * len(nets))], p50=nets[len(nets) // 2],
                              p95=nets[int(.95 * len(nets))], mean_pf=round(sum(pfs) / len(pfs), 2), mean_win=round(sum(wins) / len(wins), 1),
                              real_net_percentile=round(pct, 1), sessions=dict(sess_counts))
        # same-direction placebo: random bar but the detector's actual side mix (tests "direction" vs "timing")
        sides = [r["side"] for r in real]
        nets2 = []
        for d in range(a.draws):
            pn = []
            k = 0
            for sess, cnt in sess_counts.items():
                pool = by_sess[sess]
                for _ in range(cnt):
                    i = random.choice(pool); sp, tp = random.choice(geoms)
                    pn.append(simulate(B, i, sides[k % len(sides)], sp, tp, tick, mult, qty, fee)["pnl"]); k += 1
            nets2.append(summarize(pn)["net"])
        nets2.sort()
        res["placebo_same_sides"] = dict(mean_net=round(sum(nets2) / len(nets2), 2), real_net_percentile=round(100.0 * bisect.bisect_left(nets2, real_net) / len(nets2), 1))
        # --- C: MFE/MAE + exit sweep from the real entries
        mfe = sorted(r["mfe_r"] for r in real); mae = sorted(r["mae_r"] for r in real)
        q = lambda v, p: round(v[int(p * (len(v) - 1))], 2)
        res["mfe_r"] = dict(p25=q(mfe, .25), p50=q(mfe, .5), p75=q(mfe, .75), p90=q(mfe, .9), share_ge_1R=round(100 * sum(1 for x in mfe if x >= 1) / len(mfe), 1), share_ge_2R=round(100 * sum(1 for x in mfe if x >= 2) / len(mfe), 1))
        res["mae_r"] = dict(p25=q(mae, .25), p50=q(mae, .5), p75=q(mae, .75), p90=q(mae, .9))
        # winners vs losers MAE: how much heat do eventual winners take?
        wl = [r["mae_r"] for r in real if r["pnl"] > 0]; ll = [r["mae_r"] for r in real if r["pnl"] <= 0]
        res["mae_r_winners_p75"] = q(sorted(wl), .75) if wl else None
        sweep = {}
        for name, kw in {"target_1R": dict(mode="fixed", tgt=1.0), "target_1.5R": dict(mode="fixed", tgt=1.5), "target_2R": dict(mode="fixed", tgt=2.0),
                         "target_3R": dict(mode="fixed", tgt=3.0), "as_run": dict(mode="fixed", tgt=None),
                         "2R_be_at_1R": dict(mode="fixed", tgt=2.0, be=1.0), "trail_1R_no_target": dict(mode="trail", tgt=None, trail=1.0, notarget=True),
                         "trail_1.5R_target_3R": dict(mode="trail", tgt=3.0, trail=1.5), "stop_0.5x_target_1R": dict(mode="fixed", tgt=1.0, stopx=0.5),
                         "stop_1.5x_target_2R": dict(mode="fixed", tgt=2.0, stopx=1.5)}.items():
            pn = []
            for r in real:
                sp = r["stop_pts"] * kw.get("stopx", 1.0)
                tp = (kw["tgt"] * sp) if kw.get("tgt") else (0 if kw.get("notarget") else r["tgt_pts"])
                pn.append(simulate(B, r["i"], r["side"], sp, tp, tick, mult, qty, fee, mode=kw["mode"], trail_r=kw.get("trail"), be_at_r=kw.get("be"))["pnl"])
            sweep[name] = summarize(pn)
        res["exit_sweep"] = sweep
        # by session (resim)
        res["by_session"] = {s: summarize([r["pnl"] for r in real if r["session"] == s]) for s in sorted(set(r["session"] for r in real))}
        out[pair] = res
        print("\n=== %s ===" % pair)
        print(" engine:", res["real_engine"], "\n resim :", res["real_resim"])
        print(" placebo(random side): mean %.0f p5 %.0f p50 %.0f p95 %.0f meanPF %.2f meanWin %.1f  -> real at %.1f pct" % (res["placebo"]["mean_net"], res["placebo"]["p5"], res["placebo"]["p50"], res["placebo"]["p95"], res["placebo"]["mean_pf"], res["placebo"]["mean_win"], pct))
        print(" placebo(same sides) : mean %.0f -> real at %.1f pct" % (res["placebo_same_sides"]["mean_net"], res["placebo_same_sides"]["real_net_percentile"]))
        print(" MFE R:", res["mfe_r"], "\n MAE R:", res["mae_r"], " winners' MAE p75:", res["mae_r_winners_p75"])
        for k, v in sweep.items(): print("   %-22s n=%3d net %8.0f win %5s PF %s" % (k, v["n"], v["net"], v["win"], v["pf"]))
        print(" by session:", {k: (v["n"], v["net"], v["pf"]) for k, v in res["by_session"].items()})
    json.dump(out, open(a.out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
