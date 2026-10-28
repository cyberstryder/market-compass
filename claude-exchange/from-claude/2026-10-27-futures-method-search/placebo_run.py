import numpy as np, pandas as pd, sys, time
import sim, feat, families, sweep
from multiprocessing import Pool
def one(sym):
    res,trades,book=sweep.run_sym(sym)
    cand=res[(res.t>=1.3)&(res.h1_net>0)&(res.h2_net>0)&(res.fam!='tod_drift')]
    out=[]
    for _,r in cand.iterrows():
        tr=trades[(sym,r['name'])]
        if r.fam in ('vwap_fade','mr','gap') and r['name'].endswith(('_h60','_h30','_h120')):
            # horizon placebo: same day/hour random entries, same direction, same hold
            hold=int(r['name'].split('_h')[-1]); rng=np.random.default_rng(0); nets=[]
            sd=book.sdate; tod=book.tod
            by_day={d:np.flatnonzero(sd==d) for d in np.unique(tr.sdate.values)}
            for _ in range(200):
                idx=[]
                for d,tdd in zip(tr.sdate.values,tr.tod.values):
                    c=by_day[d]; c2=c[(tod[c]//60)==(tdd//60)]; idx.append(rng.choice(c2 if len(c2) else c))
                mv=book.horizon(np.array(idx)-1,tr.dir.values,hold); nets.append(np.nansum(mv*book.tv-sim.FEES))
            nets=np.array(nets); real=tr.pnl.sum()
            pl=dict(pct=round((nets<real).mean()*100,1),plc_mean=round(nets.mean()),plc_sd=round(nets.std()))
        else:
            # need target ticks per trade: recompute from variant
            v=[v for v in families.variants(book.df) if v['name']==r['name']][0]
            tgt=None if v['target'] is None else np.asarray(v['target'])[tr.k.values]
            pl=sim.placebo(book,tr,n_draws=200,target_ticks=tgt)
        out.append(dict(sym=sym,name=r['name'],**pl))
        print(sym,r['name'],round(r.t,2),pl,flush=True)
    return pd.DataFrame(out)
if __name__=='__main__':
    with Pool(5) as p: dfs=p.map(one,['MES','MNQ','MYM','MCL','MGC'])
    pd.concat(dfs).to_csv('placebo.csv',index=False)
