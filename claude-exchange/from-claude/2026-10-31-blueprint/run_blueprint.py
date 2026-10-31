import pandas as pd, numpy as np, warnings, sys, json
warnings.filterwarnings('ignore'); pd.set_option('display.width',250)
import blueprint as bp, sim
SYMS=['MES','MNQ','MYM','MCL','MGC']
dfs={s:sim.load(s) for s in SYMS}
def table(label,cfg):
    rows=[]; allT=[]
    for s in SYMS:
        T,D=bp.run(s,cfg,dfs[s]); allT.append(T)
        r=bp.report(T); r.update(sym=s,days=len(D),trend_days=int((D.regime=='trend').sum()),chop_days=int(D.chop.sum()))
        if len(T): r.update(gap_n=int((T.kind=='gap').sum()),gap_net=round(T[T.kind=='gap'].pnl.sum()),orb_n=int((T.kind=='orb').sum()),orb_net=round(T[T.kind=='orb'].pnl.sum()))
        rows.append(r)
    print(f'\n=== {label} ==='); print(pd.DataFrame(rows).to_string(index=False))
    return pd.concat(allT) if any(len(t) for t in allT) else pd.DataFrame()
T=table('FULL BLUEPRINT (desk fills: limit through 1tk, stop -2tk, $3 RT)',{})
T.to_csv('blueprint_trades.csv',index=False)
table('FRICTION TEST: +1.5 tk slippage/side on top',dict(extra_slip_ticks=1.5))
table('ABLATION: regime switch OFF (both setups every day)',dict(use_regime=False))
table('ABLATION: gap fade only, no regime/chop',dict(use_regime=False,use_chop=False,use_orb=False))
table('ABLATION: ORB only, no regime/chop',dict(use_regime=False,use_chop=False,use_gap=False))
table('ABLATION: gap fade, transcript gate relaxed (no beyond-H/L, min R:R 1.0)',dict(use_regime=False,use_chop=False,use_orb=False,gap_beyond_hl=False,gap_min_rr=1.0))
table('BOUND: ORB with fill-at-level regardless (optimistic, not tradable)',dict(use_regime=False,use_chop=False,use_gap=False,limit_through_ticks=-999))
table('ABLATION: full blueprint minus chop filter',dict(use_chop=False))
