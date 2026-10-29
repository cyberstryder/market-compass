import numpy as np, pandas as pd, sim, feat
from multiprocessing import Pool
from lhm import per_sym
# full-size: tick value ×10 vs micro; friction = 2 ticks full + $4.50 fees RT; 5 micros: 5×(2 ticks micro) + 5×$3
FULL={'MES':('ES',12.5),'MNQ':('NQ',5.0),'MYM':('YM',5.0),'MCL':('CL',10.0),'MGC':('GC',10.0)}
MICRO={'MES':1.25,'MNQ':0.5,'MYM':0.5,'MCL':1.0,'MGC':1.0}
SPLIT='2026-04-27'
if __name__=='__main__':
    with Pool(5) as p: res=dict(p.map(per_sym,['MES','MNQ','MYM','MCL','MGC']))
    rows=[]; daily={}
    for sym,o in res.items():
        for (en,pn),d in o.items():
            if en!='1500': continue
            g_ticks=d.gross/MICRO[sym]           # gross in ticks (already net of 0 friction)
            full_net=g_ticks*FULL[sym][1]-2*FULL[sym][1]-4.5
            m5_net=5*(g_ticks*MICRO[sym]-2*MICRO[sym]-3)
            q=pd.to_datetime(d.index).to_period('Q').astype(str)
            qn=full_net.groupby(q.values).sum()
            rows.append(dict(sym=FULL[sym][0],pred=pn,n=len(d),gross_ticks=round(g_ticks.mean(),2),full_net=round(full_net.mean(),1),full_total=round(full_net.sum()),t=round(g_ticks.mean()/(g_ticks.std()/np.sqrt(len(d))),2),
                             wr=round((full_net>0).mean(),2),h1=round(full_net[full_net.index<SPLIT].sum()),h2=round(full_net[full_net.index>=SPLIT].sum()),q_pos=f"{(qn>0).sum()}/{len(qn)}",maxdd=round((full_net.cumsum()-full_net.cumsum().cummax()).min()),worst=round(full_net.min()),m5_net=round(m5_net.mean(),1),m5_total=round(m5_net.sum())))
            daily[(sym,pn)]=full_net
    r=pd.DataFrame(rows); pd.set_option('display.width',250)
    print(r.sort_values(['sym','t'],ascending=[True,False]).to_string(index=False))
    # portfolios, f30+d1500 spec, full size
    for combo in (['MYM','MCL'],['MYM','MCL','MES'],['MYM','MCL','MES','MGC'],['MYM','MCL','MES','MGC','MNQ']):
        b=None
        for s in combo:
            x=daily[(s,'f30+d1500')]; b=x if b is None else b.add(x,fill_value=0)
        q=pd.to_datetime(b.index).to_period('Q').astype(str); qn=b.groupby(q.values).sum()
        print('+'.join(FULL[s][0] for s in combo), 'days',len(b),'net/day',round(b.mean(),1),'total',round(b.sum()),'t',round(b.mean()/(b.std()/np.sqrt(len(b))),2),'maxDD',round((b.cumsum()-b.cumsum().cummax()).min()),'worst day',round(b.min()),'quarters',qn.round().to_dict())
