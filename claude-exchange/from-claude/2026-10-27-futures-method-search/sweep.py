import sys, json, numpy as np, pandas as pd, time
import sim, feat, families
SPLIT='2026-04-27'
def run_sym(sym, stop_adv=2, placebo_draws=0):
    df=feat.add_features(sim.load(sym)); book=sim.Book(df, stop_adverse_ticks=stop_adv)
    rows=[]; trades={}
    for v in families.variants(df):
        if len(v['idx'])<20: continue
        if v['stop'] is None:   # horizon
            idx=v['idx']; d=v['dir']
            mv=book.horizon(idx,d,v['hold'])
            pnl=mv*book.tv-sim.FEES
            i1=np.minimum(idx+1,len(df)-1); j1=np.minimum(i1+v['hold'],book.flat_idx[i1])
            tr=pd.DataFrame(dict(pnl=pnl,i=i1,j=j1,sdate=df.sdate.values[i1],dir=d,tod=df.tod.values[i1],stop_ticks=0)).dropna()
        else:
            tr=book.run(v['idx'],v['dir'],v['stop'],v['target'],max_bars=v['hold'])
        if len(tr)<40: continue
        # dedupe: no overlapping trades for bracket variants (one position at a time)
        if len(tr):
            keep=[]; last_exit=-1
            for i,j in zip(tr.i.values,tr.j.values):
                if i>last_exit: keep.append(True); last_exit=j
                else: keep.append(False)
            tr=tr[np.array(keep)]
        if len(tr)<40: continue
        st=sim.stats(tr); h1=sim.stats(tr[tr.sdate<SPLIT]); h2=sim.stats(tr[tr.sdate>=SPLIT])
        q=pd.to_datetime(tr.sdate).dt.to_period('Q').astype(str); qn=tr.groupby(q.values).pnl.sum()
        row=dict(sym=sym,fam=v['fam'],name=v['name'],**st,h1_net=h1.get('net'),h1_pf=h1.get('pf'),h1_n=h1.get('n'),h2_net=h2.get('net'),h2_pf=h2.get('pf'),h2_n=h2.get('n'),q_pos=int((qn>0).sum()),q_n=len(qn))
        rows.append(row); trades[(sym,v['name'])]=tr
    res=pd.DataFrame(rows)
    return res, trades, book
if __name__=='__main__':
    allres=[]
    for sym in ['MES','MNQ','MYM','MCL','MGC']:
        t=time.time(); res,tr,_=run_sym(sym); allres.append(res); print(sym,len(res),'variants',round(time.time()-t),'s',flush=True)
    res=pd.concat(allres); res.to_csv('sweep_stop2.csv',index=False)
    print(res.sort_values('t',ascending=False).head(40).to_string())
