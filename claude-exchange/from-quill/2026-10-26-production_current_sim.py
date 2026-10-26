#!/usr/bin/env python3
"""Production-current rescore (model D in the verification report).

Entry model = production since the 2026-10-26 entry-quote booking ship
(compass/ict_paper.py:354-363): entry repriced to the market price a market
order would meet (here: close of the booking bar + 1 tick adverse), while
stop/target remain the detector's absolute structural levels (:410-420).
If the entry already sits beyond a level, production's quote path exits on
the first fresh quote -> modeled as an immediate exit at that level.
Otherwise: extremes walk over 1-min bars, stop-first, stop fills at
stop -2 ticks, target at level, flatten 15:45 CT at the last completed close.
"""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ops.replay_matrix import load_bars, TICKS
from ops.placebo_mfe import Bars, summarize, flatten_ts
from collections import Counter

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bars_merged")
PAIRS = [("aoi_fade", "MCL"), ("aoi_fade", "MES"), ("turtle_soup", "MCL"),
         ("bos_fvg", "MNQ"), ("rumers_box", "MYM"), ("golden_zone", "MNQ")]


def sim_prod(B, i0, side, stop, target, tick, mult, qty, fee):
    sgn = 1 if side == "long" else -1
    entry = B.c[i0] + sgn * tick
    flat = flatten_ts(B.ts[i0])

    def pnl(px):
        return (px - entry) * sgn * mult * qty - fee * 2 * qty

    if target:
        if sgn == 1 and entry >= target:
            return dict(pnl=pnl(target), exit_reason="target_immediate", entry=entry)
        if sgn == -1 and entry <= target:
            return dict(pnl=pnl(target), exit_reason="target_immediate", entry=entry)
    if sgn == 1 and entry <= stop:
        return dict(pnl=pnl(stop - 2 * tick), exit_reason="stop_immediate", entry=entry)
    if sgn == -1 and entry >= stop:
        return dict(pnl=pnl(stop + 2 * tick), exit_reason="stop_immediate", entry=entry)
    j = i0 + 1
    n = len(B.ts)
    while j < n:
        if B.ts[j] >= flat:
            return dict(pnl=pnl(B.c[j - 1]), exit_reason="session_flatten", entry=entry)
        hs = B.l[j] <= stop if sgn == 1 else B.h[j] >= stop
        ht = (B.h[j] >= target if sgn == 1 else B.l[j] <= target) if target else False
        if hs:
            return dict(pnl=pnl(stop - sgn * 2 * tick), exit_reason="stop", entry=entry)
        if ht:
            return dict(pnl=pnl(target), exit_reason="target", entry=entry)
        j += 1
    return dict(pnl=pnl(B.c[n - 1]), exit_reason="eod", entry=entry)


def main():
    trades = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "our_trades.json")))["trades"]
    out = {}
    for det, root in PAIRS:
        B = Bars(load_bars(glob.glob(os.path.join(D, "bars_%s.*.csv" % root))[0]))
        sel = [t for t in trades if t["detector"] == det and t["symbol"].split(".")[0] == root]
        rs = []
        for t in sel:
            i = B.idx(t["entered_at"] - 60)
            if i is None:
                i = B.idx_at_or_after(t["entered_at"] - 60)
            rs.append(sim_prod(B, i, t["side"], t["stop"], t["target"], TICKS[root],
                               t["multiplier"], t["qty"], t["fee"]))
        s = summarize([r["pnl"] for r in rs])
        out[f"{det}|{root}"] = dict(production_current=s,
                                    reasons=dict(Counter(r["exit_reason"] for r in rs)))
        print(det, root, "production-current:", s, out[f"{det}|{root}"]["reasons"])
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "production_current_results.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
