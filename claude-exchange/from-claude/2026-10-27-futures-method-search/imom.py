"""Intraday momentum (Gao-Han-Li-Zhou style): early RTH return predicts late-session return.
Gross = signed move from entry open to exit close in $/contract; net = gross - 2 ticks - $3. One trade/day.
Predictor windows x target windows; longs vs shorts separately; split halves."""
import numpy as np, pandas as pd, sim, feat
from multiprocessing import Pool
PRED={'f30':(8*60+30,9*60),'f60':(8*60+30,9*60+30),'on':( 'on',None),'f30+on':('f30on',None)}
TGT={'last45':(15*60,15*60+45),'last60':(14*60+45,15*60+45),'last90':(14*60+15,15*60+45),'pm':(13*60,15*60+45)}
SPLIT='2026-04-27'
def study(sym):
    df=feat.add_features(sim.load(sym)); tick=df.tick.iloc[0]; tv=df.tickval.iloc[0]
    g=df.groupby('sdate'); rows=[]
    def px_at(tod): return df[df.tod==tod].groupby('sdate').o.first()
    def cl_at(tod): return df[df.tod==tod].groupby('sdate').c.first()
    o830=px_at(8*60+30); c900=cl_at(8*60+59); c930=cl_at(9*60+29); pclose=g.p_rth_close.first()
    atr=g.atr.first()
    preds={'f30':(c900-o830),'f60':(c930-o830),'on':(o830-pclose),'f30+on':(c900-pclose)}
    for tn,(a,b) in TGT.items():
        eo=px_at(a); ec=px_at(15*60+45)  # exit at flatten bar open
        tret=ec-eo
        for pn,pr in preds.items():
            d=pd.concat([pr.rename('p'),tret.rename('r'),atr.rename('atr')],axis=1).dropna()
            d=d[d.p!=0]
            d['sgn']=np.sign(d.p); d['gross']=d.sgn*d.r/tick*tv; d['net']=d.gross-2*tv-3
            d['big']=(d.p.abs()/d.atr)>=0.15
            for side,m in (('long',d.sgn>0),('short',d.sgn<0),('both',d.sgn!=0)):
                for cond,mm in (('all',m),('big',m&d.big)):
                    x=d[mm]
                    if len(x)<30: continue
                    h1=x[x.index<SPLIT]; h2=x[x.index>=SPLIT]
                    rows.append(dict(sym=sym,pred=pn,tgt=tn,side=side,cond=cond,n=len(x),gross=round(x.gross.mean(),1),net=round(x.net.mean(),1),t=round(x.gross.mean()/(x.gross.std()/np.sqrt(len(x))),2),
                                     wr=round((x.gross>0).mean(),2),h1_net=round(h1.net.sum()),h2_net=round(h2.net.sum()),net_total=round(x.net.sum())))
    return pd.DataFrame(rows)
if __name__=='__main__':
    with Pool(5) as p: r=pd.concat(p.map(study,['MES','MNQ','MYM','MCL','MGC']))
    r.to_csv('imom.csv',index=False); pd.set_option('display.width',250)
    piv=r[(r.cond=='all')].pivot_table(index=['pred','tgt','side'],columns='sym',values='t').round(1)
    print(piv.to_string())
    print(r[(r.t>=1.8)].sort_values('t',ascending=False).to_string(index=False))
