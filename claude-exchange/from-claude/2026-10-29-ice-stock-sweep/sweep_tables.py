#!/usr/bin/env python3
"""Stage 2: 240-cell sweep + pooled session-matched placebo from build_tables.py output.

    python3 sweep_tables.py --tables ./tables --out ./results --draws 200
"""
import argparse, itertools, time
from pathlib import Path
import numpy as np, pandas as pd
from build_tables import COMBOS

EXITS = ["h15", "h30", "h60", "h120", "brk"]
SIDES = ["long", "short"]
THRS = [3, 4]
PLACEBO_CAP = 50_000   # per cell: subsample real trades to this many for the placebo comparison


def load(tables, symbols=None):
    data = {}
    for f in sorted(Path(tables).glob("*.npz")):
        if symbols and f.stem not in symbols: continue
        z = np.load(f)
        pool = z["pool"]
        # pool offsets for O(1) sampling: sort bars by pool id
        order = np.argsort(pool, kind="stable")
        starts = np.searchsorted(pool[order], np.arange(pool.max() + 1))
        lens = np.diff(np.append(starts, len(pool)))
        hour = (z["minute"].astype(np.int64) // 60) - 8          # 0..6 hour-of-day pool (any day)
        order_h = np.argsort(hour, kind="stable")
        starts_h = np.searchsorted(hour[order_h], np.arange(hour.max() + 1))
        lens_h = np.diff(np.append(starts_h, len(hour)))
        data[f.stem] = dict(votes=z["votes"], out=z["out"], day=z["day"],
                            pools={"dh": (pool, order, starts, lens), "h": (hour, order_h, starts_h, lens_h)})
    return data


def stats(bps, day):
    n = len(bps)
    if n == 0: return dict(n=0)
    mu = bps.mean(); sd = bps.std(ddof=1) if n > 1 else np.nan
    t = mu / (sd / np.sqrt(n)) if sd and sd > 0 else np.nan
    w = bps[bps > 0].sum(); l = -bps[bps < 0].sum()
    mid = np.median(day)
    h1 = bps[day < mid].mean() if (day < mid).any() else np.nan
    h2 = bps[day >= mid].mean() if (day >= mid).any() else np.nan
    return dict(n=int(n), mean_bps=round(float(mu), 3), median_bps=round(float(np.median(bps)), 3),
                t=round(float(t), 2), win=round(float((bps > 0).mean()), 3),
                pf=round(float(w / l), 3) if l > 0 else np.inf,
                h1_bps=round(float(h1), 3), h2_bps=round(float(h2), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--draws", type=int, default=200); ap.add_argument("--symbols", default=None)
    ap.add_argument("--min_n", type=int, default=50)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    data = load(a.tables, a.symbols.split(",") if a.symbols else None)
    print(len(data), "symbols", flush=True)
    rng = np.random.default_rng(20261029)

    rows, per_sym = [], []
    t0 = time.time()
    for ci, (v, dm, vw) in enumerate(COMBOS):
        for thr, (si, side) in itertools.product(THRS, enumerate(SIDES)):
            # collect fires once per (combo, thr, side)
            fires = {s: np.flatnonzero(d["votes"][:, ci, si] >= thr) for s, d in data.items()}
            for ei, ex in enumerate(EXITS):
                bps_l, day_l, sym_l = [], [], []
                for s, d in data.items():
                    idx = fires[s]
                    if len(idx) == 0: continue
                    b = d["out"][idx, ei, si]; ok = ~np.isnan(b)
                    bps_l.append(b[ok]); day_l.append(d["day"][idx[ok]]); sym_l.append(np.full(ok.sum(), s))
                if bps_l:
                    bps = np.concatenate(bps_l).astype(np.float64); day = np.concatenate(day_l); sym = np.concatenate(sym_l)
                else:
                    bps = np.array([]); day = np.array([]); sym = np.array([])
                st = stats(bps, day)
                cell = dict(variant=v, dmi=dm, vote_window=vw, thr=thr, side=side, exit=ex, **st)
                # placebo for everything with n >= min_n (not just gate candidates) so the full table is comparable
                if st.get("n", 0) >= a.min_n:
                    for tag in ("h", "dh"):
                        cell.update({f"{k}_{tag}": v2 for k, v2 in
                                     placebo_cell(data, fires, ei, si, bps, sym, a.draws, rng, tag).items()})
                rows.append(cell)
                if st.get("n", 0):
                    ps = pd.DataFrame({"symbol": sym, "bps": bps}).groupby("symbol")["bps"].agg(["count", "mean"]).reset_index()
                    per_sym.append(ps.assign(variant=v, dmi=dm, vote_window=vw, thr=thr, side=side, exit=ex))
                print(f"{v:9s} {dm:5s} vw{vw} thr{thr} {side:5s} {ex:4s} n={st.get('n',0):8d} "
                      f"mean={st.get('mean_bps',np.nan):8.3f} t={st.get('t',np.nan):6.2f} "
                      f"pl_h={cell.get('placebo_pct_h',np.nan):5.1f} pl_dh={cell.get('placebo_pct_dh',np.nan):5.1f}  {time.time()-t0:.0f}s", flush=True)
    res = pd.DataFrame(rows); res.to_csv(out / "sweep.csv", index=False)
    pd.concat(per_sym).to_csv(out / "per_symbol.csv", index=False)
    gate = res[(res["n"] >= a.min_n) & (res["h1_bps"] > 0) & (res["h2_bps"] > 0) & (res["t"] >= 1.5)]
    gate.to_csv(out / "gate.csv", index=False)
    print("\ngate (n>=50, both halves>0, t>=1.5):", len(gate), " of which placebo>=97.5:",
          int((gate["placebo_pct_h"] >= 97.5).sum()), " (hour-matched); day-hour-matched:",
          int((gate["placebo_pct_dh"] >= 97.5).sum()))


def placebo_cell(data, fires, ei, si, real_bps, real_sym, draws, rng, tag="h"):
    """Pooled matched placebo. tag="h": each real trade replaced by a random bar of the same symbol in
    the same clock hour on ANY day (tests day selection + timing against time-of-day drift).
    tag="dh": same symbol, same day, same hour (tests intra-hour timing only; the null inherits the
    day's realised direction, so it is the harsher, leak-prone check). Returns pct of draws below
    the real mean (same subsample), placebo mean, excess."""
    syms = list(data.keys())
    # subsample real trades for tractability
    n = len(real_bps)
    if n > PLACEBO_CAP:
        pick = rng.choice(n, PLACEBO_CAP, replace=False)
    else:
        pick = np.arange(n)
    real_mean = real_bps[pick].mean()
    sub_sym = real_sym[pick]
    # map subsampled trades back to bar indices per symbol: we need pool ids. Rebuild from fires in order.
    # fires[s] with valid outcomes, in the same order as real_bps was concatenated.
    sums = np.zeros(draws); cnts = np.zeros(draws)
    offset = 0
    for s in syms:
        idx = fires[s]
        if len(idx) == 0: continue
        d = data[s]
        b = d["out"][idx, ei, si]; ok = ~np.isnan(b); idx_ok = idx[ok]
        m = len(idx_ok)
        sel = pick[(pick >= offset) & (pick < offset + m)] - offset
        offset += m
        if len(sel) == 0: continue
        pool, order, starts, lens = d["pools"][tag]
        pools = pool[idx_ok[sel]]
        st = starts[pools]; ln = lens[pools]
        outc = d["out"][:, ei, si]
        for k in range(draws):
            r = st + (rng.random(len(sel)) * ln).astype(np.int64)
            bars = order[r]
            pb = outc[bars]; okb = ~np.isnan(pb)
            sums[k] += pb[okb].sum(); cnts[k] += okb.sum()
    means = sums / np.where(cnts > 0, cnts, np.nan)
    means = means[~np.isnan(means)]
    if len(means) == 0: return {}
    return dict(placebo_pct=round(float((means < real_mean).mean() * 100), 1),
                placebo_mean_bps=round(float(means.mean()), 3),
                excess_bps=round(float(real_mean - means.mean()), 3),
                placebo_n=int(len(pick)))


if __name__ == "__main__":
    main()
