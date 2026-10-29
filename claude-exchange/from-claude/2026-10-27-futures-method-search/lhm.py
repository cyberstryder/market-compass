"""Last-period momentum, Gao-Han-Li-Zhou form, adapted to no-carry micros.
Predictors known at entry time: f30 (08:30->09:00), d1430 (08:30->14:30), d1500 (08:30->15:00), pc1500 (prev RTH close->15:00).
Entry at 15:00 open (or 15:15), exit at 15:45 flatten open. One trade/day/instrument, 1 contract.
P&L in $ net of 1 tick adverse entry + 1 tick exit (2 ticks) + $3 fees. Also basket (sum over instruments, same day)."""
import numpy as np, pandas as pd, sim, feat
from multiprocessing import Pool
SPLIT='2026-04-27'
def per_sym(sym):
    df=feat.add_features(sim.load(sym)); tick=df.tick.iloc[0]; tv=df.tickval.iloc[0]
    g=df.groupby('sdate'); px=lambda t: df[df.tod==t].groupby('sdate').o.first()
    o830=px(510); c900=px(540); p1430=px(870); p1500=px(900); p1515=px(915); x=px(945); pclose=g.p_rth_close.first(); atr=g.atr.first()
    preds={'f30':c900-o830,'d1430':p1430-o830,'d1500':p1500-o830,'pc1500':p1500-pclose,'f30+d1500':None}
    out={}
    for en_name,en in (('1500',p1500),('1515',p1515)):
        for pn,pr in preds.items():
            if pr is None:
                s=np.sign(c900-o830); s2=np.sign(p1500-o830); pr=pd.Series(np.where(s==s2,s,0),index=s.index)  # agree
            d=pd.concat([pr.rename('p'),(x-en).rename('r'),atr.rename('atr')],axis=1).dropna(); d=d[d.p!=0]
            sg=np.sign(d.p); gross=sg*d.r/tick*tv; net=gross-2*tv-3
            d2=pd.DataFrame(dict(sgn=sg,gross=gross,net=net,big=(d.p.abs()/d.atr)>=0.2))
            out[(en_name,pn)]=d2
    return sym,out
if __name__=='__main__':
    with Pool(5) as p: res=dict(p.map(per_sym,['MES','MNQ','MYM','MCL','MGC']))
    rows=[]
    for key in res['MES']:
        for cond in ('all','big'):
            basket=None
            for sym,o in res.items():
                d=o[key]; d=d if cond=='all' else d[d.big]
                if len(d)<30: continue
                for side,m in (('both',d.sgn!=0),('long',d.sgn>0),('short',d.sgn<0)):
                    xx=d[m]
                    if len(xx)<20: continue
                    h1=xx[xx.index<SPLIT]; h2=xx[xx.index>=SPLIT]
                    rows.append(dict(entry=key[0],pred=key[1],cond=cond,sym=sym,side=side,n=len(xx),gross=round(xx.gross.mean(),1),net=round(xx.net.mean(),1),t=round(xx.gross.mean()/(xx.gross.std()/np.sqrt(len(xx))),2),wr=round((xx.net>0).mean(),2),h1=round(h1.net.sum()),h2=round(h2.net.sum()),net_total=round(xx.net.sum()),maxdd=round((xx.net.cumsum()-xx.net.cumsum().cummax()).min())))
                basket=d.net if basket is None else basket.add(d.net,fill_value=0)
            if basket is not None and len(basket)>=50:
                b=basket; h1=b[b.index<SPLIT]; h2=b[b.index>=SPLIT]
                rows.append(dict(entry=key[0],pred=key[1],cond=cond,sym='BASKET5',side='both',n=len(b),gross=np.nan,net=round(b.mean(),1),t=round(b.mean()/(b.std()/np.sqrt(len(b))),2),wr=round((b>0).mean(),2),h1=round(h1.sum()),h2=round(h2.sum()),net_total=round(b.sum()),maxdd=round((b.cumsum()-b.cumsum().cummax()).min())))
    r=pd.DataFrame(rows); r.to_csv('lhm.csv',index=False); pd.set_option('display.width',250)
    print(r[(r.side=='both')].pivot_table(index=['entry','pred','cond'],columns='sym',values=['net','t']).round(1).to_string())
    print(r[r.sym=='BASKET5'].sort_values('t',ascending=False).to_string(index=False))
    print(r[(r.sym!='BASKET5')&(r.t>=2)].sort_values('t',ascending=False).to_string(index=False))
