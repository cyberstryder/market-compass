"""VWAP-side persistence: at each 30-min mark, direction = sign(close - VWAP); hold H minutes (or flatten). Day-clustered t.
Longs and shorts reported separately. Benchmark: unconditional long at same times (beta), and random direction."""
import numpy as np, pandas as pd, sim, feat
from multiprocessing import Pool
def study(sym):
    df=feat.add_features(sim.load(sym)); b=sim.Book(df)
    c=b.c; tod=b.tod; atr=df.atr_t.values; ok=~np.isnan(atr); rows=[]
    for base,vw in (('rvwap',df.rth_vwap.values),('svwap',df.s_vwap.values)):
        dev=(c-vw)/b.tick/np.maximum(atr,1)
        for slot in range(0,24*60,30):
            if base=='rvwap' and not (9*60<=slot<15*60): continue
            if slot>=15*60 and slot<17*60: continue
            idx=np.flatnonzero((tod==slot)&ok&~np.isnan(dev))
            if len(idx)<100: continue
            for minz in (0.0,0.1,0.25):
                sel=idx[np.abs(dev[idx])>=minz]
                if len(sel)<60: continue
                d=np.sign(dev[sel]).astype(int)
                for H in (30,60,120,480):
                    mv=(b.horizon(sel,d,H)+2)*b.tv   # GROSS (friction added back)
                    for side,m in (('long',d>0),('short',d<0),('both',d!=0)):
                        v=mv[m]; dd=b.sdate[np.minimum(sel[m]+1,len(c)-1)]
                        s=pd.DataFrame(dict(d=dd,v=v)).dropna().groupby('d').v.mean()
                        if len(s)<30: continue
                        rows.append(dict(sym=sym,base=base,slot=f'{slot//60:02d}{slot%60:02d}',minz=minz,H=H,side=side,n=int(np.isfinite(v).sum()),avg=round(np.nanmean(v),1),net=round(np.nansum(v)),t=round(s.mean()/(s.std()/np.sqrt(len(s))),2),wr=round(np.nanmean(v>0),2)))
    return pd.DataFrame(rows)
if __name__=='__main__':
    with Pool(5) as p: r=pd.concat(p.map(study,['MES','MNQ','MYM','MCL','MGC']))
    r.to_csv('vwap_side.csv',index=False); pd.set_option('display.width',250)
    # summary: pooled by (sym, base, side, H) across slots -> mean t, count of slots t>1
    g=r[r.minz==0.1].groupby(['sym','base','side','H']).agg(slots=('t','size'),mean_avg=('avg','mean'),mean_t=('t','mean'),pos=('t',lambda x:(x>1).sum()),neg=('t',lambda x:(x<-1).sum())).round(2)
    print(g.to_string())
    print(r[(r.t.abs()>=2.5)].sort_values('t',ascending=False).to_string())
