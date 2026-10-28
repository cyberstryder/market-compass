"""Event study: reversion toward VWAP after an extension of z ATR-units. Day-clustered t-stats.
Move measured from next-bar open to close at horizon H, signed toward VWAP, in ticks, minus 2 ticks friction, in $.
Placebo: random bars same day/hour, direction toward VWAP at that bar (tests 'extension' vs 'any time')."""
import numpy as np, pandas as pd, sim, feat
from multiprocessing import Pool
H=(15,30,60,120)
def study(sym):
    df=feat.add_features(sim.load(sym)); b=sim.Book(df)
    c=b.c; tod=b.tod; atr=df.atr_t.values; tick=b.tick; ok=~np.isnan(atr)
    rows=[]
    for base,vw in (('rvwap',df.rth_vwap.values),('svwap',df.s_vwap.values)):
        dev=(c-vw)/tick; z=dev/np.maximum(atr,1)   # extension in ATR units
        win=(tod>=9*60)&(tod<14*60+30) if base=='rvwap' else ((tod>=19*60)|(tod<14*60+30))
        for zt in (0.15,0.25,0.35,0.5):
            for side,mask,d in (('above',win&ok&(z>=zt)&(np.roll(z,1)<zt),-1),('below',win&ok&(z<=-zt)&(np.roll(z,1)>-zt),1)):
                idx=np.flatnonzero(mask)
                if len(idx)<40: continue
                rng=np.random.default_rng(1)
                sd=b.sdate; by_day={dd:np.flatnonzero(sd==dd) for dd in np.unique(sd[idx])}
                pidx=np.array([rng.choice((lambda cc:cc[(tod[cc]//60)==(tod[i]//60)] if len(cc[(tod[cc]//60)==(tod[i]//60)]) else cc)(by_day[sd[i]])) for i in idx])
                pdir=-np.sign(dev[pidx]).astype(int); pdir[pdir==0]=1
                for hh in H:
                    mv=b.horizon(idx,np.full(len(idx),d),hh)*b.tv-sim.FEES
                    pm=b.horizon(pidx,pdir,hh)*b.tv-sim.FEES
                    s=pd.DataFrame(dict(d=sd[np.minimum(idx+1,len(c)-1)],v=mv)).dropna().groupby('d').v.mean()
                    p=pd.DataFrame(dict(d=sd[np.minimum(pidx+1,len(c)-1)],v=pm)).dropna().groupby('d').v.mean()
                    t=s.mean()/(s.std()/np.sqrt(len(s))); tp=p.mean()/(p.std()/np.sqrt(len(p)))
                    diff=(s-p.reindex(s.index)).dropna(); td=diff.mean()/(diff.std()/np.sqrt(len(diff)))
                    rows.append(dict(sym=sym,base=base,z=zt,side=side,H=hh,n=int((~np.isnan(mv)).sum()),days=len(s),avg=round(np.nanmean(mv),1),t_day=round(t,2),plc_avg=round(np.nanmean(pm),1),t_plc=round(tp,2),t_diff=round(td,2)))
    return pd.DataFrame(rows)
if __name__=='__main__':
    with Pool(5) as p: r=pd.concat(p.map(study,['MES','MNQ','MYM','MCL','MGC']))
    r.to_csv('vwap_event.csv',index=False)
    pd.set_option('display.width',250)
    print(r[r.H==60].pivot_table(index=['base','z','side'],columns='sym',values=['avg','t_day','t_diff']).round(1).to_string())
    print(); print(r[(r.z==0.35)].sort_values(['sym','base','side','H']).to_string())
