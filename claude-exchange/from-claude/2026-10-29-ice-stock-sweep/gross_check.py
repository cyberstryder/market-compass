"""Frictionless cross-check vs Quill's frozen study: signed underlying move at +30/+60/+120 from next-bar open,
in $/share and bps, pooled, per variant/dmi/vw1/thr3/side. Also per-day fire counts."""
import numpy as np, pandas as pd, glob, itertools, sys
sys.path.insert(0,'.')
from build_tables import rth, COMBOS
rows=[]; H=[30,60,120]
acc={}  # (ci,thr,side,H) -> lists
for f in sorted(glob.glob('/home/claude/bars_v2/*.parquet')):
    s=f.split('/')[-1][:-8]
    df=pd.read_parquet(f); df['ts']=pd.to_datetime(df['ts'],utc=True); d=rth(df.sort_values('ts').drop_duplicates('ts'))
    z=np.load(f'/home/claude/tables/{s}.npz'); V=z['votes']; o=d['open'].values; day=d['day'].values; mn=d['minute'].values; n=len(o)
    e=np.minimum(np.arange(n)+1,n-1); ok_e=np.zeros(n,bool); ok_e[:-1]=(day[1:]==day[:-1])&(mn[:-1]<=870)
    for h in H:
        x=np.minimum(np.arange(n)+1+h,n-1); ok=ok_e&(np.arange(n)+1+h<n)&(day[x]==day)
        mv=o[x]-o[e]
        for ci,(v,dm,vw) in enumerate(COMBOS):
            if vw!=1: continue
            for thr in (3,4):
                for si,side in enumerate((1,-1)):
                    idx=np.flatnonzero((V[:,ci,si]>=thr)&ok)
                    if len(idx)==0: continue
                    k=(v,dm,thr,si,h); a=acc.setdefault(k,[[],[],[]])
                    a[0].append(side*mv[idx]); a[1].append(1e4*side*mv[idx]/o[e][idx]); a[2].append(day[idx])
out=[]
for (v,dm,thr,si,h),(usd,bps,dy) in acc.items():
    usd=np.concatenate(usd); bps=np.concatenate(bps); dy=np.concatenate(dy); mid=np.median(dy)
    out.append(dict(variant=v,dmi=dm,thr=thr,side='long' if si==0 else 'short',H=h,n=len(usd),
        gross_usd_sh=round(usd.mean(),4),gross_bps=round(bps.mean(),3),median_bps=round(np.median(bps),3),
        t=round(bps.mean()/(bps.std(ddof=1)/np.sqrt(len(bps))),2),win=round((bps>0).mean(),3),
        h1=round(bps[dy<mid].mean(),3),h2=round(bps[dy>=mid].mean(),3),fires_per_day=round(len(usd)/len(np.unique(dy)),1)))
r=pd.DataFrame(out).sort_values(['variant','dmi','thr','side','H']); r.to_csv('/home/claude/results/gross_check.csv',index=False)
pd.set_option('display.width',250); print(r[(r.H==60)].to_string()); print(); print(r[(r.thr==3)&(r.dmi=='plain')].to_string())
