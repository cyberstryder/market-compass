# ICE × stock universe — harness (2026-10-29)

1. On the VM (Alpaca keys in env):
   python3 pull_stock_bars.py --watchlist watchlist.txt --out ./bars_1m --years 2
   (~165 symbols, 2y 1-min SIP, adjusted, ~2 GB parquet, resumable; --feed iex if SIP history is refused)
2. Send ./bars_1m (zip/tar, or push to R2) back.
3. Sweep:  python3 run_sweep.py --bars ./bars_1m --out ./results --draws 200
   → results/sweep.csv (all cells), per_symbol.csv, placebo.csv (gated candidates).

Files: ice_features.py (thinkScript port; two reconstructions flagged ASSUMPTION — TTM histogram,
DMI_Oscillator isCumulative), stock_sim.py (RTH fills/exits/fees/placebo), run_sweep.py.
Fill rule: next-bar open ±$0.01, $0.01/sh RT fee, stop fills 1¢ through, no entries after 14:30 CT,
flatten 14:55 CT. Gate: n≥50, H1&H2>0, t≥1.5, pooled session-matched placebo ≥97.5th pct.
