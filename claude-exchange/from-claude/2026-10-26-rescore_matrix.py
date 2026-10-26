#!/usr/bin/env python3
"""Re-score every replay pair with the bar-extreme simulator (entries from the engine replay,
exits from ops.placebo_mfe.simulate). Reports stop-first and target-first ambiguous-bar bounds."""
import glob, json, os, sys, math
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ops.placebo_mfe import Bars, simulate, summarize
from ops.replay_matrix import load_bars, TICKS
D = sys.argv[1]; results = sys.argv[2]; out = sys.argv[3] if len(sys.argv) > 3 else "rescore.json"
trades = []
for f in glob.glob(results): trades += json.load(open(f))["trades"]
trades = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
bars = {}
rows = []
by = defaultdict(list)
for t in trades: by[(t["detector"], t["symbol"].split(".")[0])].append(t)
for (det, root), sel in by.items():
    if root not in bars:
        bars[root] = Bars(load_bars(glob.glob(os.path.join(D, "bars_%s.*.csv" % root))[0]))
    B = bars[root]; tick = TICKS[root]
    res = {"stop": [], "target": [], "engine": [t["pnl"] for t in sel]}
    for t in sel:
        i = B.idx(t["entered_at"] - 60)
        if i is None: i = B.idx_at_or_after(t["entered_at"] - 60)
        if i is None: continue
        sp = abs(t["entry"] - t["stop"]); tp = abs(t["target"] - t["entry"]) if t.get("target") else 0
        for amb in ("stop", "target"):
            res[amb].append(simulate(B, i, t["side"], sp, tp, tick, t["multiplier"], t["qty"], t["fee"], ambiguous=amb)["pnl"])
    rows.append(dict(detector=det, symbol=root, engine=summarize(res["engine"]), stop_first=summarize(res["stop"]), target_first=summarize(res["target"])))
rows.sort(key=lambda r: -(r["stop_first"]["net"]))
json.dump(rows, open(out, "w"), indent=1)
print("%-14s %-5s %4s | %9s %5s | %9s %5s %5s | %9s %5s %5s" % ("detector", "sym", "n", "engine$", "PF", "stopfirst$", "PF", "win", "tgtfirst$", "PF", "win"))
for r in rows:
    e, s, g = r["engine"], r["stop_first"], r["target_first"]
    print("%-14s %-5s %4d | %9.0f %5s | %9.0f %5s %5s | %9.0f %5s %5s" % (r["detector"], r["symbol"], e["n"], e["net"], e["pf"], s["net"], s["pf"], s["win"], g["net"], g["pf"], g["win"]))
