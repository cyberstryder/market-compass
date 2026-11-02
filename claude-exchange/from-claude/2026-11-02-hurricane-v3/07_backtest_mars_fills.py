"""07_backtest_mars_fills.py — Mars with the honest-fill harness + placebo.

Runs the REAL strategy, then a PLACEBO at the same entry rate (random entries,
same bands / SPX gate / fill model / exits). Prints both. If the placebo is
inside a few WR points of the real run, the WR comes from the fill model.

Differences from 07_backtest_mars-richesse.py:
  - no yfinance (VIX for observation only; use --vix N)
  - no SPX remapping/rescaling; spx_5m.parquet is used only for the actual dates
  - exit-reason table splits profit-gated exits (CONE/FLOW/TRAIL can only be winners)
  - fill-delay / slippage columns reported
Usage: python3 07_backtest_mars_fills.py [--catalog DIR] [--vix 18] [--relaxed] [--exit-px close] [--slip 1] [--no-placebo]
"""
import sys, json, argparse
import pandas as pd, numpy as np
from pathlib import Path
from hurricane_v3_mars_fills import HurricaneV3, V3Config

ap=argparse.ArgumentParser()
ap.add_argument('--catalog', default='./catalog_may1/clean_0dte')
ap.add_argument('--vix', type=float, default=18.0)
ap.add_argument('--relaxed', action='store_true', help='buy fills on low<=lim with volume>=min_fill_volume; sell limit on high>=target')
ap.add_argument('--exit-px', default='low', choices=['low','close'])
ap.add_argument('--slip', type=int, default=0, help='extra ticks off every sell')
ap.add_argument('--no-placebo', action='store_true')
ap.add_argument('--placebo-seeds', type=int, default=3)
args=ap.parse_args()

CATALOG_DIR=Path(args.catalog)
sep='-'*70
print('='*70); print('Hurricane v3 — MARS, honest fills'); print('='*70)

bars1s=pd.read_parquet(CATALOG_DIR/'bars_1s.parquet')
if 'ts_event' in bars1s.columns: bars1s=bars1s.rename(columns={'ts_event':'timestamp'})
bars1s['timestamp']=pd.to_datetime(bars1s['timestamp'],utc=True)
for c in ['open','high','low']:
    if c not in bars1s.columns: bars1s[c]=bars1s['close']
if 'volume' not in bars1s.columns:
    print('  WARNING: no volume column in bars_1s — fill checks will be vacuous'); bars1s['volume']=0
print(f"  1s bars : {len(bars1s):,} rows, {bars1s['timestamp'].dt.date.nunique()} days")

p10=CATALOG_DIR/'bars_10s.parquet'
if p10.exists():
    bars10=pd.read_parquet(p10); bars10['timestamp']=pd.to_datetime(bars10['timestamp'],utc=True)
else:
    b=bars1s.set_index('timestamp').sort_index()
    bars10=b.groupby('instrument_id').resample('10s').agg(close=('close','last'),volume=('volume','sum')
            ).dropna(subset=['close']).reset_index()
oi_map=pd.read_parquet(CATALOG_DIR/'oi_map.parquet') if (CATALOG_DIR/'oi_map.parquet').exists() else None
with open(CATALOG_DIR/'atm_map.json') as f: atm_map=json.load(f)

def run(cfg, label):
    print(f"\n{sep}\nRUN: {label}\n{sep}")
    strat=HurricaneV3(config=cfg, bars1s_df=bars1s, bars10s_df=bars10, atm_map=atm_map,
                      oi_map_df=oi_map, catalog_dir=CATALOG_DIR, vix=args.vix)
    tr=strat.run_backtest()
    return strat, tr

def report(tr, label):
    print(f"\n{sep}\nRESULTS — {label}\n{sep}")
    if tr.empty: print('  No trades.'); return
    n=len(tr); tp=tr['pnl_usd'].sum(); wr=(tr['pnl_usd']>0).mean()*100
    ng=tr[~tr['profit_gated']]
    print(f"  Trades {n}  WR {wr:.1f}%  net ${tp:+,.2f}  avg ${tp/n:+.2f}/trade  "
          f"avg hold {tr['hold_secs'].mean():.0f}s")
    if len(ng):
        print(f"  Excl. profit-gated exits (CONE/FLOW/TRAIL): {len(ng)} trades  "
              f"WR {(ng['pnl_usd']>0).mean()*100:.1f}%  net ${ng['pnl_usd'].sum():+,.2f}")
    print(f"  Entry fill delay: median {tr['entry_fill_delay'].median():.0f}s  p90 {tr['entry_fill_delay'].quantile(.9):.0f}s")
    print(f"  Exit  fill delay: median {tr['exit_fill_delay'].median():.0f}s  "
          f"exit slip vs signal px: mean {tr['exit_slip_ticks'].mean():+.2f} ticks")
    gross_ticks=((tr['exit_price']-tr['entry_price'])/0.05)
    print(f"  Gross per trade: mean {gross_ticks.mean():+.2f} ticks  median {gross_ticks.median():+.2f} ticks  "
          f"(1 tick = $5; fees ${2*0.65:.2f}/RT)")
    print(f"\n  Exit reason            n    WR     avg$      total$   gated")
    for r,g in tr.groupby('exit_reason'):
        print(f"  {r:<18} {len(g):>5}  {(g['pnl_usd']>0).mean()*100:>5.1f}%  {g['pnl_usd'].mean():>+8.2f}  "
              f"{g['pnl_usd'].sum():>+10.2f}   {'Y' if g['profit_gated'].iloc[0] else ''}")
    if 'premium_band' in tr.columns:
        print(f"\n  Band   n    WR     total$")
        for b,g in tr.groupby('premium_band'):
            print(f"  {b:<5} {len(g):>4}  {(g['pnl_usd']>0).mean()*100:>5.1f}%  {g['pnl_usd'].sum():>+10.2f}")
    print(f"\n  SPX source: {tr['spx_source'].value_counts().to_dict()}")
    daily=tr.groupby('date')['pnl_usd'].sum()
    print(f"  Days {len(daily)}  positive {int((daily>0).sum())}  best ${daily.max():+.2f}  worst ${daily.min():+.2f}")

base=dict(fill_strict=not args.relaxed, exit_market_px=args.exit_px, extra_slippage_ticks=args.slip)
cfg=V3Config(**base)
strat,tr=run(cfg,'REAL')
report(tr,'REAL')
tr.to_csv('mars_fills_real.csv',index=False)

if not args.no_placebo and strat.stats['checked']>0:
    # match entry rate: orders placed / entry checks that reached the signal gates
    rate=strat.stats['orders_placed']/max(strat.stats['checked'],1)
    rows=[]
    for seed in range(args.placebo_seeds):
        pcfg=V3Config(placebo_rate=rate, placebo_seed=seed, **base)
        ps,ptr=run(pcfg,f'PLACEBO seed={seed} p={rate:.4f}')
        if not ptr.empty:
            rows.append(dict(seed=seed,n=len(ptr),wr=(ptr['pnl_usd']>0).mean()*100,net=ptr['pnl_usd'].sum(),
                             avg=ptr['pnl_usd'].mean()))
        ptr.to_csv(f'mars_fills_placebo_{seed}.csv',index=False)
    print(f"\n{sep}\nREAL vs PLACEBO\n{sep}")
    if not tr.empty:
        print(f"  REAL      n={len(tr):>5}  WR {(tr['pnl_usd']>0).mean()*100:5.1f}%  net ${tr['pnl_usd'].sum():+10.2f}  avg ${tr['pnl_usd'].mean():+.2f}")
    for r in rows:
        print(f"  PLACEBO{r['seed']}  n={r['n']:>5}  WR {r['wr']:5.1f}%  net ${r['net']:+10.2f}  avg ${r['avg']:+.2f}")
    if rows and not tr.empty:
        pm=np.mean([r['wr'] for r in rows]); print(f"\n  WR gap real−placebo: {(tr['pnl_usd']>0).mean()*100-pm:+.1f} pts  "
              f"(inside ±3 pts ⇒ the WR is the fill model, not the signal)")
print(f"\n[Saved] mars_fills_real.csv" + ('' if args.no_placebo else f" + mars_fills_placebo_*.csv"))
