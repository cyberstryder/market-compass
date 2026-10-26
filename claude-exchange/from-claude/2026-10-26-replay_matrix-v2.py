#!/usr/bin/env python3
"""Futures detector x ticker replay matrix.

Drives the REAL engine (compass.engine.Engine.tick) over historical minute
bars in a throwaway SQLite store: real detectors, real ict_paper fills,
real session/flatten rules, allowlists ignored. Bars are streamed in one at
a time (insert bar -> set quote -> tick), so no detector can see a bar that
has not happened yet (store.recent() has no upper time bound).

Input CSV columns (header required, any order):
    symbol, ts, open, high, low, close, volume
  symbol: contract root (MES, MNQ, MGC, MCL, MYM, M2K, M6A, M6E) or a
          Compass alias (MES.c.0 ...). Roots are mapped to the alias the
          detectors subscribe to.
  ts:     epoch seconds, or ISO-8601 (with offset or 'Z'); naive ISO is
          treated as America/Chicago.

Usage:
    python -m ops.replay_matrix bars.csv --out results.json [--cadence 1]
        [--days 30] [--symbols MNQ,MES] [--detectors bos_fvg,golden_zone]

Output: JSON with per (detector, symbol) stats and the full trade list, plus
a ranked text table on stdout.
"""
import argparse
import csv
import json
import math
import os
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CT = ZoneInfo("America/Chicago")
ALIASES = {
    "MES": "MES.c.0", "MNQ": "MNQ.c.0", "ES": "ES.c.0", "NQ": "NQ.c.0",
    "MGC": "MGC.v.0", "MCL": "MCL.v.0", "MYM": "MYM.c.0", "M2K": "M2K.c.0",
    "M6A": "M6A.c.0", "M6E": "M6E.c.0", "GC": "GC.v.0", "CL": "CL.v.0", "YM": "YM.v.0",
}
# Minimum tick per root, used to synthesise a one-tick bid/ask from the close.
TICKS = {"MES": 0.25, "MNQ": 0.25, "ES": 0.25, "NQ": 0.25, "MGC": 0.1, "GC": 0.1,
         "MCL": 0.01, "CL": 0.01, "MYM": 1.0, "YM": 1.0, "M2K": 0.1, "M6A": 0.0001, "M6E": 0.0001}
DETECTORS = ["golden_zone", "bos_fvg", "bos_gz_vwap", "turtle_soup", "smt_divergence",
             "aoi_zones", "continuation", "morning_drive", "icc", "rumers_box", "aoi_fade",
             "trend_rider"]


def parse_ts(v):
    v = str(v).strip()
    try:
        f = float(v)
        return f / 1000.0 if f > 1e11 else f
    except ValueError:
        pass
    s = v.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CT)
    return dt.timestamp()


def load_bars(path, symbols=None, days=None):
    rows = []
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in rd.fieldnames}
        def col(*names):
            for n in names:
                if n in cols: return cols[n]
            raise SystemExit("CSV missing column, need one of: %s (have %s)" % (names, list(cols)))
        c_sym, c_ts = col("symbol", "raw_symbol", "ticker", "root"), col("ts", "time", "timestamp", "datetime", "date")
        c_o, c_h, c_l, c_c = col("open", "o"), col("high", "h"), col("low", "l"), col("close", "c")
        c_v = cols.get("volume") or cols.get("v") or cols.get("vol")
        for r in rd:
            root = r[c_sym].strip().upper().split(".")[0]
            alias = ALIASES.get(root, r[c_sym].strip())
            if symbols and root not in symbols:
                continue
            try:
                rows.append((parse_ts(r[c_ts]), alias, root, float(r[c_o]), float(r[c_h]),
                             float(r[c_l]), float(r[c_c]), float(r[c_v]) if c_v and r[c_v] else 0.0))
            except (ValueError, KeyError):
                continue
    rows.sort()
    if days and rows:
        cutoff = rows[-1][0] - days * 86400
        rows = [r for r in rows if r[0] >= cutoff]
    return rows


def build_engine(tmpdir, aliases, detectors):
    from compass.store import Store
    from compass.config import Config
    from compass.engine import Engine
    from compass import ict_paper
    store = Store("sqlite:///" + os.path.join(tmpdir, "replay.db"))
    store.initialize()
    kw = dict(local=True, role="all", db=store_url(store), stocks=(), futures=(),
              alpaca_key="", alpaca_secret="", massive="", databento="", matrix="", discord="",
              paper_trading=True, ict_futures_paper=True, setup_study=False, scanner=False,
              option_ideas=False, swing_ideas=False, day_trading_board=False, apex_magnet=False,
              tape_confirmed=False, flow_pulse=False, zero_dte_paper=False, darkpool=False,
              iv_rank=False, squeeze=False, gap_continuation=False, breakouts=False,
              mu_scalp=False, vix_regime_enabled=False, traderspost_enabled=False,
              crosstrade_enabled=False, gap_fade_enabled=False, vwap_fade_enabled=False,
              ict_brake_shadow=False, ict_htf_tradingview=False, orb_setups=False,
              morning_enabled=False, secondary=False, research=False, discovery=False,
              ict_symbols=tuple(aliases),
              ict_smt_pairs=("MNQ.c.0:MES.c.0",) if {"MNQ.c.0", "MES.c.0"} <= set(aliases) else ())
    for d in DETECTORS:
        kw[ict_paper.DETECTOR_FLAGS[d]] = d in detectors
    cfg = Config(**kw)
    # Allowlists ignored: every detector may trade every ticker.
    ict_paper.symbol_allowed = lambda detector, symbol: True
    # Never block on the TraderMatrix intel / news-pause lookups.
    try:
        from compass import tradermatrix_intel
        tradermatrix_intel.refresh_intel = lambda db, c=None: None
    except Exception:
        pass
    engine = Engine(store, cfg, clock=None)
    return store, cfg, engine


def patch_bar_access(windows):
    """Serve ict_bars()/resolve_bar_key() from the harness's in-memory windows.

    Same rows the detectors would read from bar_window:<alias>, minus ~200
    JSON parses of an 1800-row list per tick. Every detector module imports
    these names directly, so patch each module's binding, not just
    ict_common."""
    import sys as _sys
    from compass import ict_common
    cache = {}

    def ict_bars(db, c, alias, limit=1800):
        w = windows.get(alias)
        if not w:
            return []
        key = (alias, len(w), w[-1][0])
        hit = cache.get(alias)
        if hit and hit[0] == key:
            bars = hit[1]
        else:
            bars = [{'ts': r[0], 'o': r[1], 'h': r[2], 'l': r[3], 'c': r[4], 'v': r[5]} for r in w]
            cache[alias] = (key, bars)
        return bars[-limit:] if limit else bars

    def resolve_bar_key(db, c, alias):
        return alias if windows.get(alias) else None

    def _is_orig(obj, fname):
        return callable(obj) and getattr(obj, '__module__', '') == 'compass.ict_common' and getattr(obj, '__name__', '') == fname
    for name, mod in list(_sys.modules.items()):
        if not name.startswith('compass'):
            continue
        if _is_orig(getattr(mod, 'ict_bars', None), 'ict_bars'):
            mod.ict_bars = ict_bars
        if _is_orig(getattr(mod, 'resolve_bar_key', None), 'resolve_bar_key'):
            mod.resolve_bar_key = resolve_bar_key
    ict_common.ict_bars = ict_bars
    ict_common.resolve_bar_key = resolve_bar_key
    # Memoize the pure structure helpers per (bars object, args). ict_bars
    # hands every detector the SAME list object for a symbol within a tick,
    # so bos_fvg / bos_gz_vwap / golden_zone / trend_rider stop recomputing
    # identical fractals, ATR, BOS and VWAP. Results are byte-identical.
    import functools
    def memo(fn):
        store_ = {}
        @functools.wraps(fn)
        def wrapped(bars, *a, **k):
            try:
                key = (id(bars), len(bars), bars[-1]['ts'] if bars else None, a, tuple(sorted(k.items())))
                hit = store_.get(key)
                if hit is not None:
                    return hit
                out = fn(bars, *a, **k)
                if len(store_) > 4096: store_.clear()
                store_[key] = out
                return out
            except TypeError:
                return fn(bars, *a, **k)
        return wrapped
    for fname in ("detect_bos", "fractal_swings", "atr", "session_vwap", "detect_fvgs"):
        orig = getattr(ict_common, fname, None)
        if orig is None: continue
        wrapped = memo(orig)
        for name, mod in list(_sys.modules.items()):
            if name.startswith('compass') and _is_orig(getattr(mod, fname, None), fname):
                setattr(mod, fname, wrapped)
        setattr(ict_common, fname, wrapped)


def store_url(store):
    return str(store.engine.url)


def run(bars, cadence, detectors, progress=True, dailies=None):
    aliases = sorted({b[1] for b in bars})
    tmpdir = tempfile.mkdtemp(prefix="compass-replay-")
    store, cfg, engine = build_engine(tmpdir, aliases, detectors)
    # Import every detector module (so their ict_bars bindings exist), then patch.
    for m in ("golden_zone", "bos_fvg", "bos_gz_vwap", "turtle_soup", "smt_divergence", "aoi_zones",
              "continuation", "morning_drive", "icc", "rumers_box", "aoi_fade", "trend_rider",
              "session_liquidity", "htf_levels", "ict_paper", "tier_a_b", "vwap_fade", "gap_fade"):
        __import__("compass." + m)
    windows = {a: [] for a in aliases}  # alias -> rolling [ts,o,h,l,c,v] (what providers.bars writes)
    patch_bar_access(windows)
    dailies = sorted(dailies or [])  # (ts, alias, o, h, l, c, v); released once the day is complete
    di = 0
    import logging
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    logging.getLogger().setLevel(logging.CRITICAL)
    i, n = 0, len(bars)
    last_tick = 0
    t0 = time.time()
    ticks = 0
    while i < n:
        ts = bars[i][0]
        touched = set()
        with store.tx() as c:
            while di < len(dailies) and dailies[di][0] + 86400 <= ts + 60:
                dts, dalias, o, h, l, cl, v = dailies[di]
                store.append(c, "daily", "replay", dalias, dts, {"o": o, "h": h, "l": l, "c": cl, "v": v})
                di += 1
            while i < n and bars[i][0] == ts:
                _, alias, root, o, h, l, cl, v = bars[i]
                w = windows[alias]
                w.append([ts, o, h, l, cl, v])
                if len(w) > 1800: del w[:-1800]
                touched.add(alias)
                tick = TICKS.get(root, 0.25)
                store.put(c, "latestbar:" + alias, ts)
                store.put(c, "quote:" + alias, {"ts": ts + 60, "bid": round(cl - tick, 6), "ask": round(cl, 6),
                                               "bid_size": 10, "ask_size": 10, "source": "replay"})
                i += 1
            for alias in touched:
                store.put(c, "bar_window:" + alias, windows[alias])
        now = ts + 60  # the bar is complete one minute after its open
        if now - last_tick >= cadence * 60 - 1:
            engine.tick(now)
            last_tick = now
            ticks += 1
            if progress and ticks % 500 == 0:
                print("  %5.1f%%  %s  ticks=%d  %.0fs" % (100.0 * i / n, datetime.fromtimestamp(now, CT).strftime("%Y-%m-%d %H:%M"), ticks, time.time() - t0), file=sys.stderr, flush=True)
    # Flatten anything still open at the end of data.
    engine.tick(bars[-1][0] + 60)
    with store.tx() as c:
        trades = [v for k, v in store.prefix(c, "trade:").items() if str(v.get("id", "")).startswith(("ict-", "yt-"))]
    return trades, aliases, tmpdir


def detector_of(trade):
    d = trade.get("detector")
    if d:
        return d
    tid = str(trade.get("id", ""))
    from compass.ict_paper import STRATEGY_TAGS
    for name, tag in STRATEGY_TAGS.items():
        if tid.startswith(tag):
            return name
    return trade.get("strategy") or "unknown"


STOP_FILL_TICKS_DEFAULT = 2  # shared rule (claude-exchange Task 002, 2026-10-25): live bracket stops fill
                             # a median +1 / p75 +2 ticks past the stop; both harnesses fill at stop - 2 ticks.


def reprice_stop(t, ticks=STOP_FILL_TICKS_DEFAULT):
    """Return (pnl, exit) with a stop exit re-priced to stop +/- `ticks` adverse.

    The engine fills stops at the synthesized quote (the breaching bar's close),
    which Task 002 measured as a pessimistic bound; targets are left at the level
    (broker targets filled on their limits). None = leave the trade as recorded."""
    if ticks is None or not str(t.get("exit_reason", "")).startswith("stop"):
        return t.get("pnl"), t.get("exit")
    try:
        sgn = 1 if t["side"] == "long" else -1
        ex = float(t["stop"]) - sgn * ticks * float(t["tick"])
        fees = float(t.get("fee", 0)) * 2 * float(t.get("qty", 1))
        pnl = (ex - float(t["entry"])) * sgn * float(t["multiplier"]) * float(t.get("qty", 1)) - fees
        return pnl, ex
    except (KeyError, TypeError, ValueError):
        return t.get("pnl"), t.get("exit")


def stats(trades, stop_fill_ticks=STOP_FILL_TICKS_DEFAULT):
    by = defaultdict(list)
    for t in trades:
        if t.get("status") != "closed":
            continue
        if stop_fill_ticks is not None:
            p2, e2 = reprice_stop(t, stop_fill_ticks)
            if p2 is not None:
                t = {**t, "pnl": p2, "exit": e2, "pnl_minute_close": t.get("pnl"), "exit_minute_close": t.get("exit")}
        pnl = t.get("pnl")
        if pnl is None:
            continue
        by[(detector_of(t), str(t.get("symbol", "")).split(".")[0])].append(t)
    out = []
    for (det, sym), ts in by.items():
        ts.sort(key=lambda t: t.get("exited_at", 0))
        pn = [float(t["pnl"]) for t in ts]
        wins = [p for p in pn if p > 0]; losses = [p for p in pn if p <= 0]
        gross_w, gross_l = sum(wins), -sum(losses)
        eq, peak, dd = 0.0, 0.0, 0.0
        for p in pn:
            eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
        # initial_risk is the position's total planned $ risk (ict_paper); R = pnl / that.
        r = [float(t["pnl"]) / float(t["initial_risk"]) for t in ts if t.get("initial_risk")]
        reasons = defaultdict(int)
        for t in ts: reasons[t.get("exit_reason", "?")] += 1
        out.append({
            "detector": det, "symbol": sym, "trades": len(pn),
            "win_rate": round(len(wins) / len(pn), 3) if pn else None,
            "net": round(sum(pn), 2),
            "avg": round(sum(pn) / len(pn), 2) if pn else None,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else (math.inf if gross_w else None),
            "max_drawdown": round(dd, 2),
            "avg_R": round(sum(r) / len(r), 2) if r else None,
            "exit_reasons": dict(reasons),
            "first": datetime.fromtimestamp(ts[0].get("entered_at", ts[0].get("opened_at", 0)), CT).strftime("%Y-%m-%d") if ts else None,
            "last": datetime.fromtimestamp(ts[-1].get("exited_at", 0), CT).strftime("%Y-%m-%d") if ts else None,
        })
    out.sort(key=lambda s: (-(s["net"] or 0)))
    return out


def print_table(rows):
    print("%-14s %-5s %6s %6s %9s %8s %7s %9s %6s  %s" % ("detector", "sym", "n", "win%", "net$", "avg$", "PF", "maxDD$", "avgR", "exits"))
    for s in rows:
        pf = "inf" if s["profit_factor"] == math.inf else ("%.2f" % s["profit_factor"] if s["profit_factor"] is not None else "-")
        print("%-14s %-5s %6d %6s %9.2f %8.2f %7s %9.2f %6s  %s" % (
            s["detector"], s["symbol"], s["trades"], ("%.0f" % (100 * s["win_rate"])) if s["win_rate"] is not None else "-",
            s["net"], s["avg"], pf, s["max_drawdown"], s["avg_R"] if s["avg_R"] is not None else "-",
            ",".join("%s:%d" % kv for kv in sorted(s["exit_reasons"].items()))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default="replay_results.json")
    ap.add_argument("--cadence", type=int, default=1, help="engine tick every N minutes (default 1)")
    ap.add_argument("--days", type=int, default=None, help="only the last N days of data")
    ap.add_argument("--symbols", default=None, help="comma list of roots to include")
    ap.add_argument("--detectors", default=",".join(d for d in DETECTORS if d != "continuation"),
                    help="default: all except continuation (disabled by Josh 2026-10-08; quadratic ATR makes it 70%% of tick time)")
    ap.add_argument("--daily", nargs="*", default=[], help="daily bar CSVs (same columns) for HTF levels")
    ap.add_argument("--stop-fill-ticks", type=float, default=STOP_FILL_TICKS_DEFAULT,
                    help="re-price stop exits at stop +/- N ticks adverse (shared rule = 2); -1 keeps the engine's minute-close fill")
    args = ap.parse_args()
    symbols = set(s.strip().upper() for s in args.symbols.split(",")) if args.symbols else None
    detectors = [d.strip() for d in args.detectors.split(",") if d.strip()]
    bars = []
    for path in args.csv:
        bars.extend(load_bars(path, symbols, args.days))
    bars.sort()
    if args.days and bars:
        cutoff = bars[-1][0] - args.days * 86400
        bars = [b for b in bars if b[0] >= cutoff]
    if not bars:
        raise SystemExit("no bars loaded")
    print("bars=%d symbols=%s span=%s -> %s detectors=%s" % (
        len(bars), sorted({b[2] for b in bars}),
        datetime.fromtimestamp(bars[0][0], CT), datetime.fromtimestamp(bars[-1][0], CT), detectors), file=sys.stderr)
    dailies = []
    for path in args.daily:
        dailies.extend(load_bars(path, symbols, None))
    trades, aliases, tmpdir = run(bars, args.cadence, detectors, dailies=[(d[0], d[1], d[3], d[4], d[5], d[6], d[7]) for d in dailies])
    sft = None if args.stop_fill_ticks is not None and args.stop_fill_ticks < 0 else args.stop_fill_ticks
    rows = stats(trades, sft)
    with open(args.out, "w") as fh:
        json.dump({"generated": time.time(), "bars": len(bars), "symbols": aliases, "detectors": detectors,
                   "cadence_min": args.cadence, "stop_fill_ticks": sft,
                   "clock": "bar evaluated at open+60s (complete bar); production sees it ~2s after close",
                   "summary": rows, "trades": trades}, fh, indent=1, default=str)
    print("stop fill rule: %s" % ("engine minute-close" if sft is None else "stop -/+ %g ticks adverse" % sft), file=sys.stderr)
    print_table(rows)
    print("\n%d closed trades; results -> %s" % (sum(r["trades"] for r in rows), args.out), file=sys.stderr)


if __name__ == "__main__":
    main()
