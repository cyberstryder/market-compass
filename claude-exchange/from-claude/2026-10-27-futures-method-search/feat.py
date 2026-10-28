"""Per-bar context features for a symbol (all computed with no look-ahead)."""
import numpy as np, pandas as pd
import sim

def add_features(df):
    g = df.groupby('sdate', sort=False)
    tod=df.tod.values
    # session-cumulative high/low/vwap from 17:00
    df['s_hi']=g['h'].cummax(); df['s_lo']=g['l'].cummin()
    pv=(df.c*df.v); df['s_vwap']=(pv.groupby(df.sdate).cumsum()/df.v.groupby(df.sdate).cumsum().replace(0,np.nan))
    # Asia window H/L frozen at 02:00; London H/L frozen at 08:30
    asia=(tod>=17*60)|(tod<2*60); lon=(tod>=2*60)&(tod<8*60+30)
    df['asia_hi']=df.h.where(asia).groupby(df.sdate).cummax().groupby(df.sdate).ffill()
    df['asia_lo']=df.l.where(asia).groupby(df.sdate).cummin().groupby(df.sdate).ffill()
    df['lon_hi']=df.h.where(lon).groupby(df.sdate).cummax().groupby(df.sdate).ffill()
    df['lon_lo']=df.l.where(lon).groupby(df.sdate).cummin().groupby(df.sdate).ffill()
    # RTH-ish: from 08:30
    rth=(tod>=8*60+30)&(tod<16*60)
    df['rth_hi']=df.h.where(rth).groupby(df.sdate).cummax(); df['rth_lo']=df.l.where(rth).groupby(df.sdate).cummin()
    rpv=(df.c*df.v).where(rth); rv=df.v.where(rth)
    df['rth_vwap']=rpv.groupby(df.sdate).cumsum()/rv.groupby(df.sdate).cumsum().replace(0,np.nan)
    # opens
    def first_at(mask):
        s=df.o.where(mask).groupby(df.sdate).transform('first')
        return s
    df['open_0830']=first_at(tod==8*60+30).where(tod>=8*60+30)
    df['open_midnight']=first_at(tod==0).where((tod>=0)&(tod<17*60))
    df['open_1700']=g['o'].transform('first')
    # prior session stats
    daily=g.agg(d_hi=('h','max'),d_lo=('l','min'),d_close=('c','last'),d_open=('o','first'))
    # RTH close ~ last bar before 15:00 CT (equity settle), use last bar with tod<15*60
    rth_close=df[df.tod<15*60][df.tod>=8*60+30].groupby('sdate')['c'].last().rename('rth_close')
    daily=daily.join(rth_close)
    tr=np.maximum(daily.d_hi-daily.d_lo, np.maximum((daily.d_hi-daily.d_close.shift()).abs(),(daily.d_lo-daily.d_close.shift()).abs()))
    daily['atr14']=tr.rolling(14).mean().shift()   # ATR of prior 14 sessions, known at session start
    prev=daily.shift()
    for c in ['d_hi','d_lo','d_close','rth_close']:
        df['p_'+c[2:] if c.startswith('d_') else 'p_'+c]=df.sdate.map(prev[c])
    df['atr']=df.sdate.map(daily['atr14'])
    df['atr_t']=df['atr']/df.tick   # ATR in ticks
    # rolling 1-min stats
    df['ret1']=df.c.diff()
    df['r60_hi']=df.h.rolling(60).max().shift(); df['r60_lo']=df.l.rolling(60).min().shift()
    df['sd30']=df.c.diff().rolling(30).std()
    df['ema20']=df.c.ewm(span=20).mean(); df['ema60']=df.c.ewm(span=60).mean()
    # 30-min ago close (for momentum)
    df['c30']=df.c.shift(30); df['c60']=df.c.shift(60)
    # minute index within session
    df['sbar']=g.cumcount()
    return df
