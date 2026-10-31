"""Blueprint strategy — the pasted transcript's "state-machine" made binary, one position at a time.

Layer 1  TIME     : entries 08:30-15:00 CT only; hard flatten at 15:45 CT (desk no-carry rule)
Layer 2  REGIME   : daily ATR(5) > ATR(20) (prior sessions)  -> TREND mode (ORB)
                    else                                      -> MR mode (gap fade)
         CHOP     : skip the day if prior-session ATR14 < rolling-60-session 25th pct
Layer 3  EXECUTION: limit orders only. A limit fills only if the bar trades THROUGH it by >= 1 tick
                    (desk non-fill rule); fill price = limit (no adverse tick). Stops fill stop-2 ticks.
TREND mode (ORB) : OR = 08:30-09:00 high/low. First 1-min close above ORH (below ORL) before 11:00
                   arms a limit at ORH (ORL), valid 60 min. Stop = OR midpoint. Target = 2R.
MR mode (gap fade): gap = 08:30 open vs prior RTH close (last 1-min close before 15:00 CT).
                   Qualify: |gap| >= GAP_PCT of price AND open beyond prior day's high/low.
                   Wait for 08:30-08:45 bar. Short (gap up): limit at bar15.high - RETRACE*range,
                   stop = bar15.high + 1 tick, target = prior RTH close. Require target/stop >= MIN_RR.
                   Limit valid until 10:30.
Risk             : TIME_STOP minutes -> exit at close; DAILY_MAX_LOSS per contract -> no new entries;
                   MAX_TRADES per day. Fees $3 RT; optional extra slippage ticks per side (friction test).
Every rule is a flag in CFG so components can be ablated.
"""
import numpy as np, pandas as pd, sim, feat

CFG = dict(
    use_time=True, entry_start=8*60+30, entry_end=15*60,
    use_regime=True, atr_fast=5, atr_slow=20,
    use_chop=True, chop_lookback=60, chop_pct=0.25,
    use_orb=True, or_minutes=30, orb_deadline=11*60, orb_limit_valid=60, orb_rr=2.0,
    use_gap=True, gap_pct=0.005, gap_beyond_hl=True, gap_wait=15, gap_retrace=0.25, gap_min_rr=2.0, gap_limit_valid=10*60+30,
    time_stop=120, daily_max_loss=150.0, max_trades=2,
    limit_through_ticks=1, stop_adverse_ticks=2, extra_slip_ticks=0, fees=3.0,
)

def session_table(df):
    """One row per session with prior-session fields and regime flags (no look-ahead)."""
    g=df.groupby('sdate',sort=False)
    d=g.agg(hi=('h','max'),lo=('l','min'),close=('c','last'))
    rth=df[(df.tod>=8*60+30)&(df.tod<15*60)]
    d['rth_close']=rth.groupby('sdate')['c'].last()
    tr=np.maximum(d.hi-d.lo,np.maximum((d.hi-d.close.shift()).abs(),(d.lo-d.close.shift()).abs()))
    d['tr']=tr
    return d

def run(sym, cfg=CFG, df=None):
    cfg={**CFG,**cfg}
    if df is None: df=sim.load(sym)
    tick=float(df.tick.iloc[0]); tv=float(df.tickval.iloc[0])
    S=session_table(df)
    # prior-session-only features
    S['atr14']=S.tr.rolling(14).mean().shift()
    S['atr_fast']=S.tr.rolling(cfg['atr_fast']).mean().shift()
    S['atr_slow']=S.tr.rolling(cfg['atr_slow']).mean().shift()
    S['chop_floor']=S.atr14.rolling(cfg['chop_lookback']).quantile(cfg['chop_pct']).shift()
    S['p_hi']=S.hi.shift(); S['p_lo']=S.lo.shift(); S['p_rth_close']=S.rth_close.shift()
    o=df.o.values;h=df.h.values;l=df.l.values;c=df.c.values;tod=df.tod.values;ts=df.ts.values
    sd=df.sdate.values
    starts=np.r_[0,np.flatnonzero(sd[1:]!=sd[:-1])+1]; ends=np.r_[starts[1:],len(df)]
    FLAT=sim.FLATTEN; sa=cfg['stop_adverse_ticks']*tick; slip=cfg['extra_slip_ticks']*tick; thr=cfg['limit_through_ticks']*tick
    trades=[]; days=[]
    for a,b in zip(starts,ends):
        day=sd[a]; row=S.loc[day]
        if np.isnan(row.atr14) or np.isnan(row.p_rth_close): continue
        if cfg['use_regime'] and np.isnan(row.atr_slow): continue
        if cfg['use_chop'] and np.isnan(row.chop_floor): continue
        regime='trend' if (not cfg['use_regime']) or row.atr_fast>row.atr_slow else 'mr'
        chop = cfg['use_chop'] and row.atr14<row.chop_floor
        day_pnl=0.0; n_tr=0
        # --- precompute day structures
        k=np.arange(a,b); t=tod[a:b]
        i830=k[t==8*60+30]; 
        if len(i830)==0 or chop: days.append((day,regime,chop,0,0.0)); continue
        i830=i830[0]
        open830=o[i830]
        # gap setup
        gap_sig=None
        if cfg['use_gap'] and (regime=='mr' or not cfg['use_regime']):
            gap=open830-row.p_rth_close; gp=abs(gap)/row.p_rth_close
            beyond=(gap>0 and open830>row.p_hi) or (gap<0 and open830<row.p_lo)
            if gp>=cfg['gap_pct'] and (beyond or not cfg['gap_beyond_hl']):
                w=k[(t>=8*60+30)&(t<8*60+30+cfg['gap_wait'])]
                if len(w)>=cfg['gap_wait']-2:
                    bh=h[w].max(); bl=l[w].min(); rngw=bh-bl; after=w[-1]+1
                    if gap>0:
                        d=-1; lim=bh-cfg['gap_retrace']*rngw; stop=bh+tick; tgt=row.p_rth_close
                    else:
                        d=1; lim=bl+cfg['gap_retrace']*rngw; stop=bl-tick; tgt=row.p_rth_close
                    risk=abs(lim-stop); rew=abs(tgt-lim)
                    if risk>0 and rew/risk>=cfg['gap_min_rr']:
                        gap_sig=dict(kind='gap',d=d,lim=lim,stop=stop,tgt=tgt,arm=after,expire=cfg['gap_limit_valid'])
        # ORB setup
        orb_sig=None
        if cfg['use_orb'] and (regime=='trend' or not cfg['use_regime']):
            w=k[(t>=8*60+30)&(t<8*60+30+cfg['or_minutes'])]
            if len(w)>=cfg['or_minutes']-2:
                orh=h[w].max(); orl=l[w].min(); mid=(orh+orl)/2; after=w[-1]+1
                for j in range(after,b):
                    if tod[j]>=cfg['orb_deadline']: break
                    if c[j]>orh:
                        risk=orh-mid; orb_sig=dict(kind='orb',d=1,lim=orh,stop=mid,tgt=orh+cfg['orb_rr']*risk,arm=j+1,expire=tod[j]+cfg['orb_limit_valid']); break
                    if c[j]<orl:
                        risk=mid-orl; orb_sig=dict(kind='orb',d=-1,lim=orl,stop=mid,tgt=orl-cfg['orb_rr']*risk,arm=j+1,expire=tod[j]+cfg['orb_limit_valid']); break
        sigs=[s for s in (gap_sig,orb_sig) if s]
        sigs.sort(key=lambda s:s['arm'])
        pos_until=-1
        for s in sigs:
            if n_tr>=cfg['max_trades'] or day_pnl<=-cfg['daily_max_loss']: break
            d=s['d']; lim=s['lim']; stop=s['stop']; tgt=s['tgt']
            # wait for fill
            fill_i=None
            for j in range(max(s['arm'],pos_until+1),b):
                if tod[j]>=s['expire'] or tod[j]>=cfg['entry_end'] or tod[j]>=FLAT: break
                if cfg['use_time'] and not (cfg['entry_start']<=tod[j]<cfg['entry_end']): continue
                through = (l[j]<=lim-thr) if d>0 else (h[j]>=lim+thr)
                if through: fill_i=j; break
            if fill_i is None: continue
            fill=lim+d*slip
            # stop on the fill bar? if the fill bar also pierced the stop, assume stop (conservative)
            exit_px=None; reason=None; last=b-1
            for j in range(fill_i,b):
                if tod[j]>=FLAT: exit_px=o[j]; reason='flatten'; break
                if cfg['time_stop'] and j-fill_i>=cfg['time_stop']: exit_px=c[j]; reason='time'; break
                hit_stop = l[j]<=stop if d>0 else h[j]>=stop
                hit_tgt  = h[j]>=tgt if d>0 else l[j]<=tgt
                if hit_stop: exit_px=stop-d*sa-d*slip; reason='stop'; break
                if hit_tgt: exit_px=tgt-d*slip; reason='target'; break
                if j==last: exit_px=c[j]; reason='eod'; break
            ticks=d*(exit_px-fill)/tick; pnl=ticks*tv-cfg['fees']
            trades.append(dict(sym=sym,sdate=day,regime=regime,kind=s['kind'],dir=d,fill_tod=int(tod[fill_i]),exit_tod=int(tod[j]),hold=int(j-fill_i),
                               fill=fill,stop=stop,tgt=tgt,exit=exit_px,reason=reason,ticks=ticks,pnl=pnl,ts=int(ts[fill_i])))
            day_pnl+=pnl; n_tr+=1; pos_until=j
        days.append((day,regime,chop,n_tr,day_pnl))
    T=pd.DataFrame(trades); D=pd.DataFrame(days,columns=['sdate','regime','chop','n','pnl'])
    return T,D

def report(T, split='2026-04-27'):
    if len(T)==0: return dict(n=0)
    st=sim.stats(T); h1=sim.stats(T[T.sdate<split]); h2=sim.stats(T[T.sdate>=split])
    rng=np.random.default_rng(0); real=T.pnl.sum()
    # sign-shuffle null on per-trade ticks is not exact (geometry asymmetry); use day-level sign flip of pnl as a quick null
    nets=[(T.pnl.values*rng.choice([-1,1],len(T))).sum() for _ in range(2000)]
    return dict(**st,h1_net=h1.get('net'),h1_n=h1.get('n'),h2_net=h2.get('net'),h2_n=h2.get('n'),
                shuffle_pct=round((np.array(nets)<real).mean()*100,1),
                tgt=round((T.reason=='target').mean(),2),stp=round((T.reason=='stop').mean(),2),
                time=round((T.reason=='time').mean(),2),flat=round((T.reason=='flatten').mean(),2))
