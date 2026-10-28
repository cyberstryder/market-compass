"""Daily-trend filtered intraday pullback: dir = sign(prev close - SMA_N of session closes) known at session start.
Entries: (a) pullback to session VWAP in trend direction (price crosses back through VWAP toward trend side after being
on the wrong side >= 0.1 ATR); (b) 60-min channel breakout in trend direction; (c) fixed-time entry at 08:30/02:00 in trend dir.
Both directions reported; mirror (counter-trend) shown as control. Stop 0.25 ATR, target 0.5 ATR / none; -2tk stop fills."""
import numpy as np, pandas as pd, sim, feat
from multiprocessing import Pool
SPLIT='2026-04-27'
def study(sym):
    df=feat.add_features(sim.load(sym)); b=sim.Book(df)
    daily=df.groupby('sdate').c.last(); 
    out=[]
    for N in (10,20,50):
        sma=daily.rolling(N).mean(); tr=np.sign(daily.shift()-sma.shift()).rename('tr')
        trend=df.sdate.map(tr).values
        c=b.c; tod=b.tod; atr=df.atr_t.values; ok=~np.isnan(atr)&~np.isnan(trend)&(trend!=0)
        vw=df.s_vwap.values; dev=(c-vw)/b.tick/np.maximum(atr,1)
        # (a) vwap pullback: trend up, price was below vwap by >=0.1 ATR within last 30 bars, now closes above vwap
        below=pd.Series(dev<=-0.1).rolling(30).max().values==1; above=pd.Series(dev>=0.1).rolling(30).max().values==1
        win=((tod>=2*60)&(tod<15*60))
        upx=ok&win&(dev>0)&(np.roll(dev,1)<=0)&np.roll(below,1); dnx=ok&win&(dev<0)&(np.roll(dev,1)>=0)&np.roll(above,1)
        r60h=df.r60_hi.values; r60l=df.r60_lo.values
        bu=ok&win&(c>r60h)&(np.roll(c,1)<=np.roll(r60h,1)); bd=ok&win&(c<r60l)&(np.roll(c,1)>=np.roll(r60l,1))
        sigs={'vwap_pb_long':(upx,1),'vwap_pb_short':(dnx,-1),'chan_long':(bu,1),'chan_short':(bd,-1),
              't0830_long':(ok&(tod==8*60+30),1),'t0830_short':(ok&(tod==8*60+30),-1),'t0200_long':(ok&(tod==2*60),1),'t0200_short':(ok&(tod==2*60),-1)}
        for nm,(mask,d) in sigs.items():
            for align in ('with','against'):
                m=mask&((trend==d) if align=='with' else (trend==-d))
                idx=np.flatnonzero(m)
                if len(idx)<40: continue
                for stopa,tgta in ((0.25,0.5),(0.25,None),(0.15,0.3)):
                    t=b.run(idx,np.full(len(idx),d),atr[idx]*stopa,None if tgta is None else atr[idx]*tgta)
                    keep=[];last=-1
                    for i,j in zip(t.i.values,t.j.values):
                        keep.append(i>last); 
                        if i>last: last=j
                    t=t[np.array(keep)]
                    if len(t)<40: continue
                    st=sim.stats(t); h1=sim.stats(t[t.sdate<SPLIT]); h2=sim.stats(t[t.sdate>=SPLIT])
                    out.append(dict(sym=sym,N=N,sig=nm,align=align,stop=stopa,tgt=tgta,**st,h1=h1['net'],h2=h2['net']))
    return pd.DataFrame(out)
if __name__=='__main__':
    with Pool(5) as p: r=pd.concat(p.map(study,['MES','MNQ','MYM','MCL','MGC']))
    r.to_csv('trend_pb.csv',index=False); pd.set_option('display.width',250)
    print(r.groupby(['align','sig']).agg(cells=('t','size'),t_mean=('t','mean'),net_mean=('net','mean')).round(2).to_string())
    print(r.sort_values('t',ascending=False).head(25).to_string(index=False))
    print('cells',len(r),'t>=2',(r.t>=2).sum(),'t<=-2',(r.t<=-2).sum())
