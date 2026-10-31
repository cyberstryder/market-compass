import numpy as np, pandas as pd, sim, feat, families, sweep, warnings
warnings.filterwarnings('ignore')
pd.set_option('display.width',200)
out=[]
for sym in ['MES','MNQ','MYM','MCL','MGC']:
    df=feat.add_features(sim.load(sym)); book=sim.Book(df)
    V={v['name']:v for v in families.variants(df) if v['fam']=='gap'}
    tod=df.tod.values; atr=df.atr_t.values; tick=df.tick.iloc[0]
    on=(df.open_0830.values-df.p_rth_close.values)/tick
    idx=np.flatnonzero((tod==8*60+30)&~np.isnan(atr)&~np.isnan(on))
    g=on[idx]/atr[idx]
    for thr in (0.5,):
        big=np.abs(g)>=thr; d=-np.sign(on[idx]).astype(int)
        tr=book.run(idx[big],d[big],atr[idx[big]]*0.5,np.abs(on[idx[big]]))
        real=tr.pnl.sum(); p=tr.pnl.values
        # 1. sign-shuffle null: same days, random direction
        rng=np.random.default_rng(0); nets=[]
        for _ in range(500):
            dd=rng.choice([-1,1],size=big.sum())
            t2=book.run(idx[big],dd,atr[idx[big]]*0.5,np.abs(on[idx[big]])); nets.append(t2.pnl.sum())
        nets=np.array(nets); pct_sign=(nets<real).mean()*100
        # 2. complement control: fade on small-gap days (0.1..0.5 ATR), same geometry (target=|gap|, stop 0.5 atr)
        sm=(np.abs(g)>=0.1)&(np.abs(g)<thr)
        t3=book.run(idx[sm],d[sm],atr[idx[sm]]*0.5,np.abs(on[idx[sm]]))
        # 3. concentration
        srt=np.sort(p)[::-1]; top3=srt[:3].sum()
        # 4. gross (pre-fee) per trade, in ticks, and target/stop hit rates
        out.append(dict(sym=sym,n=len(p),net=round(real),avg=round(p.mean(),1),t=sim.stats(tr)['t'],
            sign_shuffle_pct=round(pct_sign,1),shuffle_mean=round(nets.mean()),
            smallgap_n=len(t3),smallgap_avg=round(t3.pnl.mean(),1),
            top3_share=round(top3/real,2) if real>0 else None,
            tgt_rate=round((tr.reason=='target').mean(),2),stop_rate=round((tr.reason=='stop').mean(),2),flat_rate=round((tr.reason=='flatten').mean(),2),
            avg_ticks=round(tr.ticks.mean(),1),med_hold_min=int((tr.j-tr.i).median())))
print(pd.DataFrame(out).to_string())
