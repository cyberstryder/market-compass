#!/usr/bin/env python3
"""Level-preserving re-simulation of OUR harness trades + close-only counterfactual
+ Claude's placebo distribution (his Part B construction, verbatim logic).

Real arm: entry/stop/target LEVELS from our position records (what our harness
and production actually booked). Walk 1-min bars after the entry bar; flatten
15:45 CT at the last completed bar's close.
  - detection='extremes': stop if bar extreme pierces (stop checked first),
    stop fills at stop -2tk adverse, target at level  == our harness + desk rule.
  - detection='close':    same fills, but the hit test uses the bar CLOSE only
    (isolates the pure detection-semantics delta on our entries).
Placebo arm: Claude's placebo_mfe.py Part B exactly — same per-session-bucket
counts as the real trades, random bar in bucket, random side (and same-side
variant), real trades' stop/target distances, his close-anchored simulate().
"""
import bisect, glob, json, os, random, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ops.replay_matrix import load_bars, TICKS
from ops.placebo_mfe import Bars, simulate, summarize, session_bucket, flatten_ts

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bars_merged")
PAIRS = [("aoi_fade", "MCL"), ("aoi_fade", "MES"), ("turtle_soup", "MCL"),
         ("bos_fvg", "MNQ"), ("rumers_box", "MYM"), ("golden_zone", "MNQ")]


def sim_levels(B, i0, side, entry, stop, target, tick, mult, qty, fee,
               detection="extremes", stop_ticks=2):
    sgn = 1 if side == "long" else -1
    flat = flatten_ts(B.ts[i0])
    risk = abs(entry - stop)
    mfe = mae = 0.0
    j = i0 + 1
    n = len(B.ts)
    px = None; reason = None
    while j < n:
        if B.ts[j] >= flat:
            px = B.c[j - 1] if j > i0 + 1 else B.o[j]
            reason = "session_flatten"; break
        hi, lo, cl = B.h[j], B.l[j], B.c[j]
        fav = (hi - entry) * sgn if sgn == 1 else (entry - lo)
        adv = (entry - lo) * sgn if sgn == 1 else (hi - entry)
        mfe = max(mfe, fav); mae = max(mae, adv)
        if detection == "extremes":
            hit_stop = lo <= stop if sgn == 1 else hi >= stop
            hit_tgt = (hi >= target if sgn == 1 else lo <= target) if target else False
        else:  # close-only hit test
            hit_stop = cl <= stop if sgn == 1 else cl >= stop
            hit_tgt = (cl >= target if sgn == 1 else cl <= target) if target else False
        if hit_stop:
            px = stop - sgn * stop_ticks * tick; reason = "stop"; break
        if hit_tgt:
            px = target; reason = "target"; break
        j += 1
    if px is None:
        px = B.c[n - 1]; reason = "eod"; j = n - 1
    pnl = (px - entry) * sgn * mult * qty - fee * 2 * qty
    return dict(pnl=pnl, exit_reason=reason,
                mfe_r=mfe / risk if risk else 0, mae_r=mae / risk if risk else 0)


def main():
    trades = json.load(open("our_trades.json"))["trades"]
    bars_by = {}
    random.seed(7)
    out = {}
    for det, root in PAIRS:
        sel = [t for t in trades if t["detector"] == det and t["symbol"].split(".")[0] == root]
        if not sel:
            continue
        if root not in bars_by:
            bars_by[root] = Bars(load_bars(glob.glob(os.path.join(D, "bars_%s.*.csv" % root))[0]))
        B = bars_by[root]
        tick = TICKS[root]
        real_ext, real_close, real_lvl, real_sig = [], [], [], []
        agree = 0
        skipped = 0
        for t in sel:
            i = B.idx(t["entered_at"] - 60)
            if i is None:
                i = B.idx_at_or_after(t["entered_at"] - 60)
            if i is None:
                skipped += 1; continue
            r_lvl = sim_levels(B, i, t["side"], t["entry"], t["stop"], t["target"],
                                       tick, t["multiplier"], t["qty"], t["fee"], "extremes")
            real_lvl.append(r_lvl)
            if r_lvl["exit_reason"] == t.get("exit_reason"):
                agree += 1
            if t.get("signal_time"):
                i_sig = B.idx(t["signal_time"])
                if i_sig is None:
                    i_sig = B.idx_at_or_after(t["signal_time"])
                if i_sig is not None:
                    real_sig.append(sim_levels(B, i_sig, t["side"], t["entry"], t["stop"], t["target"],
                                               tick, t["multiplier"], t["qty"], t["fee"], "extremes"))
            real_close.append(sim_levels(B, i, t["side"], t["entry"], t["stop"], t["target"],
                                         tick, t["multiplier"], t["qty"], t["fee"], "close"))
            sp = abs(t["entry"] - t["stop"]); tp = abs(t["target"] - t["entry"]) if t.get("target") else 0
            real_ext.append((t, i, sp, tp))
        lvl = summarize([r["pnl"] for r in real_lvl])
        sig = summarize([r["pnl"] for r in real_sig]) if real_sig else None
        clo = summarize([r["pnl"] for r in real_close])
        eng = summarize([t["pnl"] for (_, _, _, _), t in zip(real_ext, [x[0] for x in real_ext])]) if False else summarize([x[0]["pnl"] for x in real_ext])
        # --- placebo: his Part B construction verbatim (close-anchored simulate)
        by_sess = defaultdict(list)
        for i2, ts in enumerate(B.ts):
            by_sess[session_bucket(ts)].append(i2)
        sess_counts = defaultdict(int)
        geoms = []
        sides = []
        mult = sel[0]["multiplier"]; qty = sel[0]["qty"]; fee = sel[0]["fee"]
        for t, i, sp, tp in real_ext:
            sess_counts[session_bucket(B.ts[i])] += 1
            geoms.append((sp, tp)); sides.append(t["side"])
        def draw_nets(same_sides):
            nets = []
            for _ in range(500):
                pn = []
                k = 0
                for sess, cnt in sess_counts.items():
                    pool = by_sess[sess]
                    for _ in range(cnt):
                        i2 = random.choice(pool)
                        sp, tp = random.choice(geoms)
                        side = sides[k % len(sides)] if same_sides else random.choice(("long", "short"))
                        k += 1
                        pn.append(simulate(B, i2, side, sp, tp, tick, mult, qty, fee)["pnl"])
                nets.append(sum(pn))
            nets.sort(); return nets
        nets = draw_nets(False); nets2 = draw_nets(True)
        def pct(nets_, v): return round(100.0 * bisect.bisect_left(nets_, v) / len(nets_), 1)
        mfe = sorted(r["mfe_r"] for r in real_lvl); mae = sorted(r["mae_r"] for r in real_lvl)
        q = lambda v, p: round(v[int(p * (len(v) - 1))], 2)
        wl = [r["mae_r"] for r in real_lvl if r["pnl"] > 0]
        out[f"{det}|{root}"] = dict(
            n=len(real_lvl), skipped=skipped, recorded_engine=eng, level_extremes=lvl, level_close_only=clo,
            placebo=dict(mean=round(sum(nets) / len(nets), 2), p5=nets[25], p95=nets[475],
                         pct_of_level_real=pct(nets, lvl["net"])),
            placebo_same_sides=dict(mean=round(sum(nets2) / len(nets2), 2),
                                    pct_of_level_real=pct(nets2, lvl["net"])),
            agree_reasons=agree, signal_anchor_extremes=sig,
            mfe_r=dict(p50=q(mfe, .5), share_ge_1R=round(100 * sum(1 for x in mfe if x >= 1) / len(mfe), 1),
                       share_ge_2R=round(100 * sum(1 for x in mfe if x >= 2) / len(mfe), 1)),
            mae_r=dict(p50=q(mae, .5)), mae_winners_p75=q(sorted(wl), .75) if wl else None,
            sessions=dict(sess_counts))
        print(f"=== {det}|{root}  n={lvl['n']} skipped={skipped}")
        print("  recorded(engine):", eng)
        print("  reason agreement: %d/%d   signal-anchor extremes:" % (agree, lvl['n']), sig)
        print("  level-extremes  :", lvl)
        print("  level-close-only:", clo)
        print("  placebo mean %.0f p5 %.0f p95 %.0f -> level-real at %.1f pct (same-sides %.1f)" % (
            out[f"{det}|{root}"]["placebo"]["mean"], out[f"{det}|{root}"]["placebo"]["p5"],
            out[f"{det}|{root}"]["placebo"]["p95"], out[f"{det}|{root}"]["placebo"]["pct_of_level_real"],
            out[f"{det}|{root}"]["placebo_same_sides"]["pct_of_level_real"]))
    json.dump(out, open("level_sim_results.json", "w"), indent=1)


if __name__ == "__main__":
    main()

def sim_attainable(B, i0, side, entry, stop, target, tick, mult, qty, fee, stop_ticks=2):
    """Limit-fill semantics: the position activates on the first bar at/after
    booking whose range touches the booked entry level; exits walk from the
    fill bar (stop-first if the fill bar also pierces the stop). If 15:45 CT
    flatten arrives first, the trade never happens."""
    sgn = 1 if side == "long" else -1
    flat = flatten_ts(B.ts[i0])
    j = i0 + 1
    n = len(B.ts)
    fill = None
    while j < n:
        if B.ts[j] >= flat:
            return None  # never filled before flatten
        if B.l[j] <= entry <= B.h[j]:
            fill = j; break
        j += 1
    if fill is None:
        return None
    # exit walk from the fill bar onward
    k = fill
    while k < n:
        if B.ts[k] >= flat:
            px = B.c[k - 1] if k > fill else B.o[k]; reason = "session_flatten"; break
        hit_stop = B.l[k] <= stop if sgn == 1 else B.h[k] >= stop
        hit_tgt = (B.h[k] >= target if sgn == 1 else B.l[k] <= target) if target else False
        if hit_stop:
            px = stop - sgn * stop_ticks * tick; reason = "stop"; break
        if hit_tgt:
            px = target; reason = "target"; break
        k += 1
    else:
        px = B.c[n - 1]; reason = "eod"
    pnl = (px - entry) * sgn * mult * qty - fee * 2 * qty
    return dict(pnl=pnl, exit_reason=reason)
