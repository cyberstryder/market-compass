"""Load Databento year-1 micro futures bars → one parquet per symbol with CT fields.
Session date: CME trade date (17:00 CT open belongs to next calendar day). Weekend/holiday handled by data.
"""
import pandas as pd, numpy as np, json, sys
from pathlib import Path
RAW = Path('/home/claude/data/year1/databento_futures_1y')
OUT = Path('/home/claude/fsearch/parq'); OUT.mkdir(exist_ok=True)
DEGRADED = {'2025-11-28','2026-01-31','2026-03-15','2026-03-16','2026-03-21','2026-04-10','2026-05-24','2026-08-29','2026-10-24'}
SPEC = {  # tick size, $ per tick
 'MES':(0.25,1.25),'MNQ':(0.25,0.50),'MYM':(1.0,0.50),'MCL':(0.01,1.00),'MGC':(0.10,1.00)}
for f in sorted(RAW.glob('bars_*.csv')):
    sym = f.name[5:8]
    df = pd.read_csv(f)
    df['ts'] = df['ts'].astype('int64')
    t = pd.to_datetime(df['ts'], unit='s', utc=True).dt.tz_convert('America/Chicago')
    df['t'] = t
    df['hour'] = t.dt.hour; df['minute']=t.dt.minute
    df['tod'] = df['hour']*60+df['minute']            # minutes since midnight CT
    df['sdate'] = (t + pd.Timedelta(hours=7)).dt.date.astype(str)
    df['dow'] = pd.to_datetime(df['sdate']).dt.dayofweek
    df = df.sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    roll = df['instrument_id'] != df['instrument_id'].shift()
    roll.iloc[0]=False
    df['roll_day'] = df['sdate'].isin(set(df.loc[roll,'sdate']))
    df['degraded'] = df['sdate'].isin(DEGRADED)
    df['gap_prev'] = df['ts'].diff().fillna(60)
    tick,tv = SPEC[sym]; df['tick']=tick; df['tickval']=tv
    df.to_parquet(OUT/f'{sym}.parquet')
    n_sess = df['sdate'].nunique()
    big = df[(df.gap_prev>600)&~((df.hour==17)&(df.minute==0))]
    print(f"{sym}: {len(df):,} bars  sessions={n_sess}  first={df.t.iloc[0]}  last={df.t.iloc[-1]}  rolls={int(roll.sum())}")
    print(f"   intra-session gaps>10min (excl. 17:00 reopen): {len(big)};  bars/session median={df.groupby('sdate').size().median()}")
    for i in df.index[roll]:
        print(f"   roll {df.t[i]} {df.raw_symbol[i-1]}→{df.raw_symbol[i]} jump {(df.o[i]-df.c[i-1])/tick:+.0f} ticks")
