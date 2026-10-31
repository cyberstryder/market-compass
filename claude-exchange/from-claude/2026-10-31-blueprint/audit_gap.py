import numpy as np, pandas as pd, sim, feat, families, sweep
pd.set_option('display.width',200)
orig=pd.read_csv('/home/claude/cyberstryder/market-compass/claude-exchange/from-claude/2026-10-27-futures-method-search/sweep_stop2.csv')
rows=[]
for sym in ['MES','MNQ','MYM','MCL','MGC']:
    res,trades,book=sweep.run_sym(sym)
    g=res[res.fam=='gap'][['sym','name','n','net','t','h1_net','h2_net']]
    o=orig[(orig.sym==sym)&(orig.fam=='gap')][['sym','name','n','net','t','h1_net','h2_net']]
    m=g.merge(o,on=['sym','name'],suffixes=('_re','_orig'))
    m['match']=(m.n_re==m.n_orig)&(np.isclose(m.net_re,m.net_orig))
    rows.append(m)
m=pd.concat(rows); print(m.to_string()); print('all match:',m.match.all())
