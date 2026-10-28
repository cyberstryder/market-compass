"""Shared simulator for the futures method search.
Conventions (match desk Task 002 / production-current):
  - signal at bar close -> entry at NEXT bar open, +1 tick adverse
  - stop filled at stop -2 ticks adverse (sensitivity: -4); target filled at level
  - both touched in one bar -> stop first (conservative)
  - hard flatten at 15:45 CT (no carry); exit at that bar's open
  - fees $1.50/side/contract -> $3 round trip; all $ per 1 contract
  - prices back-adjusted across rolls (Panama) so levels are continuous; P&L in ticks unaffected
"""
import numpy as np, pandas as pd
from pathlib import Path
PARQ = Path('/home/claude/fsearch/parq')
FLATTEN = 15*60+45
FEES = 3.0
SESS = [('asia',17*60,24*60+2*60),('london',2*60,8*60+30),('ny_am',8*60+30,11*60),('ny_pm',11*60,FLATTEN)]

def session_of(tod):
    tod = np.asarray(tod)
    s = np.full(len(tod),'closed',dtype=object)
    s[(tod>=17*60)|(tod<2*60)]='asia'
    s[(tod>=2*60)&(tod<8*60+30)]='london'
    s[(tod>=8*60+30)&(tod<11*60)]='ny_am'
    s[(tod>=11*60)&(tod<FLATTEN)]='ny_pm'
    return s

def load(sym, adjust=True, drop_degraded=True):
    df = pd.read_parquet(PARQ/f'{sym}.parquet')
    if adjust:
        roll = (df['instrument_id'] != df['instrument_id'].shift()).values.copy(); roll[0]=False
        jump = np.where(roll, df['o'].values - df['c'].shift().values, 0.0)
        # cumulative adjustment applied to bars BEFORE each roll: back-adjust
        adj = np.cumsum(jump[::-1])[::-1] - jump  # sum of jumps strictly after this bar
        for col in ['o','h','l','c']: df[col] = df[col].values + adj
    if drop_degraded:
        df = df[~df['degraded']].reset_index(drop=True)
    df['sess'] = session_of(df['tod'].values)
    return df

class Book:
    """Run brackets over one symbol's bar arrays."""
    def __init__(self, df, stop_adverse_ticks=2):
        self.df = df
        self.o=df.o.values; self.h=df.h.values; self.l=df.l.values; self.c=df.c.values
        self.ts=df.ts.values; self.tod=df.tod.values; self.sdate=df.sdate.to_numpy(dtype=object)
        self.tick=float(df.tick.iloc[0]); self.tv=float(df.tickval.iloc[0])
        self.sa=stop_adverse_ticks
        # index of last bar before flatten in each session: precompute per bar the flatten index
        n=len(df); self.flat_idx=np.empty(n,dtype=np.int64)
        sd=self.sdate; tod=self.tod
        # for each session, flatten index = first bar with tod>=15:45 (same sdate) else last bar of session
        starts=np.r_[0,np.flatnonzero(sd[1:]!=sd[:-1])+1].astype(np.int64); ends=np.r_[starts[1:],n]
        for a,b in zip(starts,ends):
            k=np.flatnonzero((tod[a:b]>=FLATTEN)&(tod[a:b]<17*60))
            fi = a+k[0] if len(k) else b-1
            self.flat_idx[a:b]=fi
    def run(self, sig_idx, direction, stop_ticks, target_ticks=None, max_bars=None, trail_ticks=None, be_at_ticks=None):
        """sig_idx: bar index of signal (fill at sig_idx+1 open). direction +1/-1.
        stop_ticks/target_ticks: distances from fill in ticks (arrays or scalars). Returns DataFrame of trades."""
        sig_idx=np.asarray(sig_idx); direction=np.asarray(direction)
        stop_ticks=np.broadcast_to(np.asarray(stop_ticks,dtype=float),sig_idx.shape)
        tgt=None if target_ticks is None else np.broadcast_to(np.asarray(target_ticks,dtype=float),sig_idx.shape)
        rows=[]
        for k in range(len(sig_idx)):
            i=int(sig_idx[k])+1
            if i>=len(self.o): continue
            fi=self.flat_idx[i]
            if i>fi or self.sdate[i]!=self.sdate[i-1]: continue   # signal was last bar of session
            d=int(direction[k]); tk=self.tick
            fill=self.o[i]+d*tk
            stop=fill-d*stop_ticks[k]*tk
            target=None if tgt is None else fill+d*tgt[k]*tk
            last = fi if max_bars is None else min(fi, i+int(max_bars))
            exit_px=None; reason=None; j=i; best=0.0; cur_stop=stop
            for j in range(i,last+1):
                hi,lo=self.h[j],self.l[j]
                if j>i and trail_ticks is not None:
                    fav = (hi-fill)/tk if d>0 else (fill-lo)/tk
                    best=max(best,fav)
                    cur_stop = max(cur_stop,fill+(best-trail_ticks)*tk) if d>0 else min(cur_stop,fill-(best-trail_ticks)*tk)
                if j>i and be_at_ticks is not None:
                    fav = (hi-fill)/tk if d>0 else (fill-lo)/tk
                    if fav>=be_at_ticks: cur_stop = max(cur_stop,fill) if d>0 else min(cur_stop,fill)
                if j==i:
                    # entry bar: only check after fill; use bar range conservatively
                    pass
                hit_stop = lo<=cur_stop if d>0 else hi>=cur_stop
                hit_tgt = (target is not None) and (hi>=target if d>0 else lo<=target)
                if hit_stop:
                    exit_px=cur_stop-d*self.sa*tk; reason='stop'; break
                if hit_tgt:
                    exit_px=target; reason='target'; break
                if j==last:
                    exit_px=self.o[j] if j==fi else self.c[j]; reason='flatten' if j==fi else 'time'; break
            pnl_ticks=d*(exit_px-fill)/tk
            rows.append((k,i,j,d,fill,exit_px,reason,pnl_ticks,pnl_ticks*self.tv-FEES,self.ts[i],self.tod[i],self.sdate[i],float(stop_ticks[k])))
        return pd.DataFrame(rows,columns=['k','i','j','dir','fill','exit','reason','ticks','pnl','ts','tod','sdate','stop_ticks'])
    def horizon(self, sig_idx, direction, bars):
        """signed move from next-bar-open fill to close `bars` later, in ticks, minus 2 ticks friction, capped at flatten."""
        sig_idx=np.asarray(sig_idx); direction=np.asarray(direction)
        i=sig_idx+1; ok=(i<len(self.o)-1)
        i=np.minimum(i,len(self.o)-1)
        j=np.minimum(i+bars, self.flat_idx[i]); ok&= (j>i)
        mv=direction*(self.c[j]-self.o[i])/self.tick - 2
        return np.where(ok,mv,np.nan)

def stats(tr, pnl='pnl'):
    if len(tr)==0: return dict(n=0)
    p=tr[pnl].values
    g=p[p>0].sum(); l=-p[p<0].sum()
    eq=np.cumsum(p); dd=(eq-np.maximum.accumulate(eq)).min() if len(eq) else 0
    return dict(n=len(p), net=round(p.sum(),0), avg=round(p.mean(),1), wr=round((p>0).mean(),3),
                pf=round(g/l,2) if l>0 else float('inf'), dd=round(dd,0),
                t=round(p.mean()/(p.std(ddof=1)/np.sqrt(len(p))),2) if len(p)>2 and p.std()>0 else 0.0)

def placebo(book, tr, n_draws=200, seed=0, target_ticks=None, same_hour=True):
    """Session-matched random entries: for each real trade draw a random bar on the same sdate within the same hour,
    same direction, same stop/target geometry. Returns percentile of real net within placebo nets, placebo mean net."""
    rng=np.random.default_rng(seed)
    df=book.df
    by_day={}
    sd=book.sdate; tod=book.tod
    for d in np.unique(tr.sdate.values):
        by_day[d]=np.flatnonzero(sd==d)
    real=tr.pnl.sum(); nets=[]
    for _ in range(n_draws):
        idx=[]; 
        for _,r in tr.iterrows():
            cand=by_day[r.sdate]
            if same_hour:
                c2=cand[(tod[cand]//60)==(r.tod//60)]
                cand=c2 if len(c2) else cand
            idx.append(rng.choice(cand))
        idx=np.array(idx)-1
        t2=book.run(idx, tr.dir.values, tr.stop_ticks.values, None if target_ticks is None else target_ticks)
        nets.append(t2.pnl.sum())
    nets=np.array(nets)
    return dict(pct=round((nets<real).mean()*100,1), plc_mean=round(nets.mean(),0), plc_sd=round(nets.std(),0))
