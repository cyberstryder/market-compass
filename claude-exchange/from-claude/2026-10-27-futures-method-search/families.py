"""Candidate signal families. Each returns dict(name, sig_idx, dir, stop_ticks, target_ticks, hold) lists of variants.
dir arrays are +1 long / -1 short; the sweep scores longs and shorts separately.
All signals evaluated at bar close; fills at next bar open (sim convention).
"""
import numpy as np, pandas as pd

def _first_bar_of_tod(df, tod):
    return np.flatnonzero((df.tod.values==tod))

def one_per_day(idx, sdate):
    """keep first index per session date"""
    s=pd.Series(idx); d=pd.Series(sdate[idx]); keep=~d.duplicated(); return idx[keep.values]

def variants(df):
    out=[]
    tod=df.tod.values; c=df.c.values; o=df.o.values; h=df.h.values; l=df.l.values
    atr=df.atr_t.values; sd=df.sdate.to_numpy(dtype=object); tick=df.tick.iloc[0]
    ok=~np.isnan(atr)
    # ---------- A. time-of-day drift: enter at slot start, hold K minutes, no stop (horizon) ----------
    for slot in range(0,24*60,30):
        idx=np.flatnonzero((tod==slot)&ok)
        if len(idx)<100: continue
        for hold in (30,60,120):
            out.append(dict(fam='tod_drift',name=f'tod{slot//60:02d}{slot%60:02d}_h{hold}',idx=idx,dir=np.ones(len(idx),int),stop=None,target=None,hold=hold))
    # ---------- B. ORB (NY 08:30) ----------
    for N in (5,15,30):
        or_end=8*60+30+N
        orh=pd.Series(h).where((tod>=8*60+30)&(tod<or_end)).groupby(sd).cummax().groupby(sd).ffill().values
        orl=pd.Series(l).where((tod>=8*60+30)&(tod<or_end)).groupby(sd).cummin().groupby(sd).ffill().values
        after=(tod>=or_end)&(tod<11*60)&ok
        up=after&(c>orh)&(np.roll(c,1)<=np.roll(orh,1)); dn=after&(c<orl)&(np.roll(c,1)>=np.roll(orl,1))
        for name,mask,d in (('long',up,1),('short',dn,-1)):
            idx=one_per_day(np.flatnonzero(mask),sd)
            rng=(orh[idx]-orl[idx])/tick
            for stopk,tgtk in ((1.0,1.0),(1.0,2.0),(0.5,1.0),(1.0,None)):
                out.append(dict(fam='orb',name=f'orb{N}_{name}_s{stopk}_t{tgtk}',idx=idx,dir=np.full(len(idx),d),stop=np.maximum(rng*stopk,4),target=None if tgtk is None else np.maximum(rng*tgtk,4),hold=None))
    # ---------- C. intraday momentum (first 30m -> later) & overnight -> open ----------
    o830=df.open_0830.values; c900=pd.Series(c).where(tod==8*60+59).groupby(sd).transform('first').values
    first30=(c900-o830)/tick
    pclose=df.p_rth_close.values
    for trig_tod,label in ((9*60,'first30_rest'),(15*60,'first30_last45'),(14*60+30,'first30_last75')):
        idx=np.flatnonzero((tod==trig_tod)&ok&~np.isnan(first30))
        d=np.sign(first30[idx]).astype(int); keep=d!=0
        out.append(dict(fam='imom',name=f'{label}',idx=idx[keep],dir=d[keep],stop=atr[idx[keep]]*0.5,target=None,hold=None))
        out.append(dict(fam='imom',name=f'{label}_contra',idx=idx[keep],dir=-d[keep],stop=atr[idx[keep]]*0.5,target=None,hold=None))
    on=(o830-pclose)/tick  # overnight gap in ticks vs prior RTH close
    idx=np.flatnonzero((tod==8*60+30)&ok&~np.isnan(on))
    for thr in (0.25,0.5):
        g=on[idx]/atr[idx]; big=np.abs(g)>=thr
        d=np.sign(on[idx]).astype(int)
        out.append(dict(fam='gap',name=f'gap_follow_{thr}',idx=idx[big],dir=d[big],stop=atr[idx[big]]*0.5,target=np.abs(on[idx[big]]),hold=None))
        out.append(dict(fam='gap',name=f'gap_fade_{thr}',idx=idx[big],dir=-d[big],stop=atr[idx[big]]*0.5,target=np.abs(on[idx[big]]),hold=None))
        out.append(dict(fam='gap',name=f'gap_fade_{thr}_h120',idx=idx[big],dir=-d[big],stop=None,target=None,hold=120))
    # ---------- D. session levels: fade (rejection) and break ----------
    levels={'asia_hi':(df.asia_hi.values,+1),'asia_lo':(df.asia_lo.values,-1),'lon_hi':(df.lon_hi.values,+1),'lon_lo':(df.lon_lo.values,-1),
            'p_hi':(df.p_hi.values,+1),'p_lo':(df.p_lo.values,-1),'p_rth_close':(df.p_rth_close.values,0),'open_midnight':(df.open_midnight.values,0),'rth_vwap':(df.rth_vwap.values,0)}
    for lname,(lv,side) in levels.items():
        valid=ok&~np.isnan(lv)
        # which window the level is tradable in
        if lname.startswith('asia'): win=(tod>=2*60)&(tod<15*60)
        elif lname.startswith('lon') or lname=='open_midnight': win=(tod>=8*60+30)&(tod<15*60)
        elif lname=='rth_vwap': win=(tod>=9*60)&(tod<15*60)
        else: win=((tod>=2*60)&(tod<15*60))
        lvp=np.roll(lv,1)
        # fade: bar pierced level and closed back on the original side
        if side>=0:  # resistance-type (or neutral both ways)
            pierce_up=win&valid&(h>=lv)&(c<lv)&(np.roll(c,1)<lvp)   # touched from below, rejected -> short
            idx=np.flatnonzero(pierce_up); 
            for cool in (1,):
                for stopa,tgta in ((0.15,0.3),(0.25,0.5),(0.25,0.25)):
                    out.append(dict(fam='lvl_fade',name=f'{lname}_fadeshort_s{stopa}_t{tgta}',idx=idx,dir=np.full(len(idx),-1),stop=atr[idx]*stopa,target=atr[idx]*tgta,hold=None))
            brk=win&valid&(c>lv)&(np.roll(c,1)<=lvp)
            idx=one_per_day(np.flatnonzero(brk),sd) if False else np.flatnonzero(brk)
            for stopa,tgta in ((0.15,0.3),(0.25,0.5),(0.25,None)):
                out.append(dict(fam='lvl_break',name=f'{lname}_breaklong_s{stopa}_t{tgta}',idx=idx,dir=np.full(len(idx),1),stop=atr[idx]*stopa,target=None if tgta is None else atr[idx]*tgta,hold=None))
        if side<=0:
            pierce_dn=win&valid&(l<=lv)&(c>lv)&(np.roll(c,1)>lvp)
            idx=np.flatnonzero(pierce_dn)
            for stopa,tgta in ((0.15,0.3),(0.25,0.5),(0.25,0.25)):
                out.append(dict(fam='lvl_fade',name=f'{lname}_fadelong_s{stopa}_t{tgta}',idx=idx,dir=np.full(len(idx),1),stop=atr[idx]*stopa,target=atr[idx]*tgta,hold=None))
            brk=win&valid&(c<lv)&(np.roll(c,1)>=lvp)
            idx=np.flatnonzero(brk)
            for stopa,tgta in ((0.15,0.3),(0.25,0.5),(0.25,None)):
                out.append(dict(fam='lvl_break',name=f'{lname}_breakshort_s{stopa}_t{tgta}',idx=idx,dir=np.full(len(idx),-1),stop=atr[idx]*stopa,target=None if tgta is None else atr[idx]*tgta,hold=None))
    # ---------- F. VWAP z-score fade / extension ----------
    for base,vw in (('svwap',df.s_vwap.values),('rvwap',df.rth_vwap.values)):
        dev=(c-vw)/tick
        z=dev/np.maximum(atr*0.2,1)   # dev in units of 0.2 ATR
        win=(tod>=9*60)&(tod<15*60) if base=='rvwap' else (tod>=19*60)|(tod<15*60)
        for zt in (1.0,1.5,2.0):
            up=win&ok&(z>=zt)&(np.roll(z,1)<zt); dn=win&ok&(z<=-zt)&(np.roll(z,1)>-zt)
            for nm,mask,d in (('short',up,-1),('long',dn,1)):
                idx=np.flatnonzero(mask)
                out.append(dict(fam='vwap_fade',name=f'{base}_z{zt}_fade{nm}',idx=idx,dir=np.full(len(idx),d),stop=atr[idx]*0.2,target=np.abs(dev[idx]),hold=None))
                out.append(dict(fam='vwap_fade',name=f'{base}_z{zt}_fade{nm}_h60',idx=idx,dir=np.full(len(idx),d),stop=None,target=None,hold=60))
                out.append(dict(fam='vwap_ext',name=f'{base}_z{zt}_follow{nm}',idx=idx,dir=np.full(len(idx),-d),stop=atr[idx]*0.2,target=atr[idx]*0.4,hold=None))
    # ---------- G. midnight-open retrace at NY open ----------
    mo=df.open_midnight.values
    idx=np.flatnonzero((tod==8*60+30)&ok&~np.isnan(mo))
    ext=(o830[idx]-mo[idx])/tick
    for thr in (0.2,0.4):
        big=np.abs(ext)/atr[idx]>=thr; d=-np.sign(ext[idx] if False else ext).astype(int)
        out.append(dict(fam='midnight',name=f'midnight_retrace_{thr}',idx=idx[big],dir=d[big],stop=atr[idx[big]]*0.4,target=np.abs(ext[big]),hold=None))
        out.append(dict(fam='midnight',name=f'midnight_follow_{thr}',idx=idx[big],dir=-d[big],stop=atr[idx[big]]*0.4,target=atr[idx[big]]*0.4,hold=None))
    # ---------- H. short-term mean reversion / momentum on 30-min & 60-min returns ----------
    for lag,col in ((30,df.c30.values),(60,df.c60.values)):
        r=(c-col)/tick; zr=r/np.maximum(atr,1)
        for sess_name,win in (('asia',(tod>=19*60)|(tod<2*60)),('london',(tod>=2*60)&(tod<8*60)),('ny',(tod>=9*60)&(tod<15*60))):
            for zt in (0.15,0.25,0.4):
                up=win&ok&(zr>=zt)&(np.roll(zr,1)<zt); dn=win&ok&(zr<=-zt)&(np.roll(zr,1)>-zt)
                for nm,mask,d in (('afterup',up,-1),('afterdn',dn,1)):
                    idx=np.flatnonzero(mask)
                    out.append(dict(fam='mr',name=f'mr{lag}_{sess_name}_z{zt}_{nm}',idx=idx,dir=np.full(len(idx),d),stop=atr[idx]*0.25,target=atr[idx]*0.25,hold=None))
                    out.append(dict(fam='mr',name=f'mr{lag}_{sess_name}_z{zt}_{nm}_h30',idx=idx,dir=np.full(len(idx),d),stop=None,target=None,hold=30))
                    out.append(dict(fam='mom',name=f'mom{lag}_{sess_name}_z{zt}_{nm}',idx=idx,dir=np.full(len(idx),-d),stop=atr[idx]*0.25,target=atr[idx]*0.5,hold=None))
    # ---------- I. 60-min channel breakout (intraday trend) ----------
    r60h=df.r60_hi.values; r60l=df.r60_lo.values
    for sess_name,win in (('asia',(tod>=19*60)|(tod<2*60)),('london',(tod>=3*60)&(tod<8*60+30)),('ny',(tod>=9*60+30)&(tod<15*60))):
        up=win&ok&(c>r60h)&(np.roll(c,1)<=np.roll(r60h,1)); dn=win&ok&(c<r60l)&(np.roll(c,1)>=np.roll(r60l,1))
        for nm,mask,d in (('long',up,1),('short',dn,-1)):
            idx=np.flatnonzero(mask)
            for stopa,tgta in ((0.2,0.4),(0.2,None),(0.3,0.9)):
                out.append(dict(fam='chan',name=f'chan60_{sess_name}_{nm}_s{stopa}_t{tgta}',idx=idx,dir=np.full(len(idx),d),stop=atr[idx]*stopa,target=None if tgta is None else atr[idx]*tgta,hold=None))
    # ---------- J. narrow-range compression breakout (NR 30-min range < 0.1 ATR) ----------
    rng30=(pd.Series(h).rolling(30).max()-pd.Series(l).rolling(30).min()).values/tick
    nr=rng30/np.maximum(atr,1)
    hi30=pd.Series(h).rolling(30).max().shift().values; lo30=pd.Series(l).rolling(30).min().shift().values
    for thr in (0.08,0.12):
        win=((tod>=2*60)&(tod<15*60))&ok
        up=win&(np.roll(nr,1)<=thr)&(c>hi30); dn=win&(np.roll(nr,1)<=thr)&(c<lo30)
        for nm,mask,d in (('long',up,1),('short',dn,-1)):
            idx=np.flatnonzero(mask)
            out.append(dict(fam='nr',name=f'nr30_{thr}_{nm}',idx=idx,dir=np.full(len(idx),d),stop=np.maximum(rng30[idx],4),target=np.maximum(rng30[idx]*2,8),hold=None))
    return out
