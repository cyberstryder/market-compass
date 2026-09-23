"""Causal, OHLC-only futures SIGNAL screen. See protocol.md; not Pine parity."""
from pathlib import Path

import numpy as np
import pandas as pd
OUT=Path(__file__).parent
TICK=.25; POINT=2

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def rma(s,n):
    a=s.to_numpy(dtype=float); out=np.full(len(a),np.nan); valid=[]; prev=np.nan
    for i,x in enumerate(a):
        if not np.isfinite(x):continue
        if not np.isfinite(prev):
            valid.append(x)
            if len(valid)==n:prev=float(np.mean(valid))
        else:prev=(prev*(n-1)+x)/n
        out[i]=prev
    return pd.Series(out,index=s.index)
def atr(d,n=14):
    c=d.close.shift();return rma(pd.concat([d.high-d.low,(d.high-c).abs(),(d.low-c).abs()],axis=1).max(axis=1),n)
def pivots(d,k=5):
    # at observation i only the candidate i-k is evaluated; never backdate.
    hi=d.high.shift(k);lo=d.low.shift(k)
    ph=hi.where(hi==d.high.rolling(2*k+1).max());pl=lo.where(lo==d.low.rolling(2*k+1).min())
    return ph,pl

def make_bars(raw,n):
    z=raw[['open','high','low','close']].resample(f'{n}min').agg({'open':'first','high':'max','low':'min','close':'last'})
    count=raw.close.resample(f'{n}min').count()
    return z[count==n].dropna()

def features(d,raw,n):
    c=d.close;o=d.open;h=d.high;l=d.low;a=atr(d)
    htf=raw.close.resample('15min',label='right',closed='left').last()
    counts=raw.close.resample('15min',label='right',closed='left').count()
    htf=htf[counts==15].dropna();he=ema(htf,50)
    ht=he.reindex(d.index+pd.Timedelta(minutes=n),method='ffill').to_numpy()
    e21=ema(c,21);e50=ema(c,50)
    bias=np.where((c>e50)&(e21>e50)&(c>ht),1,np.where((c<e50)&(e21<e50)&(c<ht),-1,0))
    efficiency=(c-c.shift(20)).abs()/c.diff().abs().rolling(20).sum()
    high20=h.rolling(20).max().shift();low20=l.rolling(20).min().shift()
    mid=c.rolling(20).mean();sd=c.rolling(20).std(ddof=0)
    sig={
      'Trend breakout':np.where((bias==1)&(c>high20),1,np.where((bias==-1)&(c<low20),-1,0)),
      'Trend pullback':np.where((bias==1)&(l<=e21)&(c>e21)&(c>o),1,np.where((bias==-1)&(h>=e21)&(c<e21)&(c<o),-1,0)),
      'Band reentry':np.where((c.shift()< (mid-2*sd).shift())&(c>=mid-2*sd),1,np.where((c.shift()>(mid+2*sd).shift())&(c<=mid+2*sd),-1,0)),
      'Failed breakout':np.where((l<low20-TICK)&(c>low20+TICK),1,np.where((h>high20+TICK)&(c<high20-TICK),-1,0))
    }
    # Stochastic Pop: no volume agreement gate at shipped defaults.
    k=100*(c-l.rolling(14).min())/(h.rolling(14).max()-l.rolling(14).min())
    ke=ema(k.rolling(5).mean(),4);e200=ema(c,200);a10=atr(d,10)
    crossup=(ke>55)&(ke.shift()<=55);crossdn=(ke<45)&(ke.shift()>=45)
    sig['Stochastic Pop']=np.where(crossup&(c>e200)&(e200>e200.shift(10))&(c-e200<=3*a10),1,np.where(crossdn&(c<e200)&(e200<e200.shift(10))&(e200-c<=3*a10),-1,0))
    # ORB range requires all 15 constituent minute observations.
    orb=np.zeros(len(d),int);mins=d.index.hour*60+d.index.minute
    for day,inds in pd.Series(np.arange(len(d)),index=d.index).groupby(d.index.date):
        r=raw[(raw.index.date==day)&(raw.index.hour*60+raw.index.minute>=510)&(raw.index.hour*60+raw.index.minute<525)]
        if len(r)!=15:continue
        top=r.high.max();bot=r.low.min();up=down=0
        for i in inds.to_numpy():
            if 525<=mins[i] and mins[i]+n<=895:
                up=up+1 if c.iloc[i]>top else 0;down=down+1 if c.iloc[i]<bot else 0
                orb[i]=1 if up>=2 else -1 if down>=2 else 0
    sig['Opening Range Desk']=orb
    # Location state machine, frozen range and confirmation-time swings.
    ph,pl=pivots(d);sh=sl=np.nan;hb=lb=False;dr=0;br=0;origin=ext=np.nan
    phase=0;top=bot=0.;lastin=initbar=0;dire=0
    loc=np.zeros(len(d),int);rh=h.rolling(6).max().to_numpy();rl=l.rolling(6).min().to_numpy()
    layers=[(h.rolling(round(tf*100/n)).max().to_numpy(),l.rolling(round(tf*100/n)).min().to_numpy()) for tf in (5,15)]
    oo,hh,ll,cc,aa,pph,ppl=[x.to_numpy() for x in (o,h,l,c,a,ph,pl)]
    def zone(px,high,low):
        if not np.isfinite(high+low) or high<=low:return 9
        p=(px-low)/(high-low)
        return 2 if p>=.75 else -2 if p<=.25 else 0 if .45<=p<=.55 else 1 if p>.5 else -1
    for i in range(len(d)):
        if np.isfinite(pph[i]):sh=pph[i];hb=False
        if np.isfinite(ppl[i]):sl=ppl[i];lb=False
        if not hb and cc[i]>sh:hb=True;dr=1;br=i;origin=sl if np.isfinite(sl) else ll[i];ext=hh[i]
        if not lb and cc[i]<sl:lb=True;dr=-1;br=i;origin=sh if np.isfinite(sh) else hh[i];ext=ll[i]
        if dr==1:ext=max(ext,hh[i])
        elif dr==-1:ext=min(ext,ll[i])
        state=0 if dr and i-br>60 and sl<cc[i]<sh else dr
        if not np.isfinite(aa[i]):continue
        if phase==0:
            if rh[i]-rl[i]<=1.2*aa[i]:phase=1;top=rh[i];bot=rl[i];lastin=i
        elif phase==1:
            width=top-bot
            if bot<=cc[i]<=top:
                nt=max(top,hh[i]);nb=min(bot,ll[i])
                if nt-nb<=1.2*aa[i]:top=nt;bot=nb
                lastin=i
            elif cc[i]>top+width or cc[i]<bot-width:phase=2;dire=1 if cc[i]>top else -1;initbar=i
            elif i-lastin>10:phase=0
        else:
            through=cc[i]<bot if dire==1 else cc[i]>top
            touch=ll[i]<=top if dire==1 else hh[i]>=bot
            if through:phase=0
            elif touch:
                px=(top+bot)/2;zs=[zone(px,x[i],y[i]) for x,y in layers];cont=state==dire
                agree=sum(z==-2*dire or (cont and z==0) for z in zs)
                if cont and np.isfinite(origin+ext) and abs(ext-origin)>0 and abs(px-(origin+ext)/2)<=abs(ext-origin)*.05:agree+=1
                if state==dire and agree>=1 and zs[0]!=2*dire:loc[i]=dire
                phase=0
            elif i-initbar>30:phase=0
    sig['Location Desk']=loc
    # Signal Desk default Setup Count gate does NOT depend on learned weights/VWAP.
    e20=ema(c,20);e50=ema(c,50);delta=c.diff();up=rma(delta.clip(lower=0),14);dn=rma((-delta).clip(lower=0),14)
    rsi=100-100/(1+up/dn);rsiavg=rsi.rolling(14).mean();a10=atr(d,10)
    upper=(h+l)/2+3*a10;lower=(h+l)/2-3*a10
    st=np.zeros(len(d),int);fu=fl=np.nan;trend=-1
    for i in range(len(d)):
        if not np.isfinite(a10.iloc[i]):continue
        u=upper.iloc[i];b=lower.iloc[i]
        if np.isfinite(fu):u=u if u<fu or cc[i-1]>fu else fu;b=b if b>fl or cc[i-1]<fl else fl
        if trend==-1 and cc[i]>u:trend=1
        elif trend==1 and cc[i]<b:trend=-1
        fu,fl=u,b;st[i]=trend
    struct=np.zeros(len(d),int);sh=sl=np.nan;bias_s=0
    for i in range(len(d)):
        if np.isfinite(pph[i]):sh=pph[i]
        if np.isfinite(ppl[i]):sl=ppl[i]
        if cc[i]>sh:bias_s=1;sh=np.nan
        if cc[i]<sl:bias_s=-1;sl=np.nan
        struct[i]=bias_s
    ftrend=np.where((st==1)&(e20>e50),1,np.where((st==-1)&(e20<e50),-1,0))
    mom=np.where((rsi>rsiavg)&(rsi>50),1,np.where((rsi<rsiavg)&(rsi<50),-1,0))
    rng=h-l;body=(c-o).abs();dist=(c-e20)/a;run=c.rolling(3).max()-c.rolling(3).min()
    bull=(c>o)&((h-c)<=.35*rng)&(body>=.4*rng)&(rng>0)
    bear=(c<o)&((c-l)<=.35*rng)&(body>=.4*rng)&(rng>0)
    lastL=lastS=-10000;ss=np.zeros(len(d),int)
    for i in range(len(d)):
        if ll[i]<=e20.iloc[i]+.3*aa[i]:lastL=i
        if hh[i]>=e20.iloc[i]-.3*aa[i]:lastS=i
        if i<200:continue
        for direction,candle,lasttouch in [(1,bool(bull.iloc[i]),lastL),(-1,bool(bear.iloc[i]),lastS)]:
            locok=(-.5<=dist.iloc[i]<=1) if direction==1 else (-1<=dist.iloc[i]<=.5)
            grade=int(ftrend[i]==direction)+int(mom[i]==direction)+int(struct[i]==direction)+int(locok)+int(candle and rng.iloc[i]<=2.5*aa[i])
            turn=((rsi.iloc[i]>rsiavg.iloc[i]) and (rsi.iloc[i-1]<=rsiavg.iloc[i-1] or rsi.iloc[i]>rsi.iloc[i-1])) if direction==1 else ((rsi.iloc[i]<rsiavg.iloc[i]) and (rsi.iloc[i-1]>=rsiavg.iloc[i-1] or rsi.iloc[i]<rsi.iloc[i-1]))
            veto=rng.iloc[i]>2.5*aa[i] or dist.iloc[i]*direction>2 or (run.iloc[i]>3*aa[i] and (cc[i]-cc[i-3])*direction>0)
            if ftrend[i]==direction and i-lasttouch<=3 and candle and (cc[i]-e20.iloc[i])*direction>0 and turn and grade>=4 and not veto:ss[i]=direction;break
    sig['Signal Desk']=ss
    from fractal import fractal_signals
    sig['Fractal Model'],_=fractal_signals(d,raw,n,a.shift().to_numpy(),tick=TICK)
    return a,bias,efficiency.to_numpy(),sig,ke.to_numpy()

def barrier(o,h,l,stop,target,side):
    hit_s=l<=stop if side==1 else h>=stop; hit_t=h>=target if side==1 else l<=target
    if hit_s:return min(stop,o) if side==1 else max(stop,o),'stop',bool(hit_t)
    if hit_t:return target,'target',False
    return None,'',False

def simulate(raw,d,n,signal,a,ke,name,eligible,base_cost=2.5,stress_cost=5.5):
    times=raw.index;sec=times.asi8//10**9; oo,hh,ll,cc=[raw[k].to_numpy() for k in ('open','high','low','close')]
    minutes=np.asarray(times.hour*60+times.minute);day=np.asarray((times.tz_localize(None)+pd.Timedelta(hours=7)).strftime('%Y-%m-%d'))
    ds=d.index+pd.Timedelta(minutes=n);loc=times.get_indexer(ds);endsec=ds.asi8//10**9
    aa=a.to_numpy();out=[];censored=[];next_time=-1;last_stoch=-10000;resetL=resetS=True;daily={}
    for k,side in enumerate(signal):
        if np.isfinite(ke[k]):
            if ke[k]<45:resetL=True
            if ke[k]>55:resetS=True
        i=loc[k]
        if not side or i<1 or k<200 or not np.isfinite(aa[k]) or aa[k]<=0:continue
        if sec[i]-sec[i-1]!=60 or sec[i]!=endsec[k] or day[i] not in eligible or day[i]!=day[i-1]:continue
        if next_time>=sec[i] or 945<=minutes[i]<1020:continue
        if name=='Stochastic Pop' and (k-last_stoch<5 or not (resetL if side==1 else resetS)):continue
        cap=1 if name=='Opening Range Desk' else 3 if name=='Location Desk' else 2 if name=='Signal Desk' else 100000
        if daily.get(day[i],0)>=cap:continue
        risk=np.ceil(1.5*aa[k]/TICK-1e-9)*TICK;entry=oo[i];stop=entry-side*risk;target=entry+side*2*risk
        invalid=False;exitprice=None;reason='timeout';ambiguous=False
        deadline=sec[i]+60*60
        cutoff=895 if name=='Opening Range Desk' else 945
        for j in range(i,len(raw)):
            if (j>i and sec[j]-sec[j-1]!=60) or day[j]!=day[i]:invalid=True;break
            exitprice,why,amb=barrier(oo[j],hh[j],ll[j],stop,target,side)
            if exitprice is not None:reason=why;ambiguous=amb;break
            if sec[j]+60>=deadline or (minutes[j]+1>=cutoff and minutes[j]<1020):exitprice=cc[j];reason='cutoff' if minutes[j]+1>=cutoff and minutes[j]<1020 else 'timeout';break
        if exitprice is None:invalid=True
        next_time=int(sec[j]+60+10*60)
        daily[day[i]]=daily.get(day[i],0)+1
        if name=='Stochastic Pop':last_stoch=k;resetL=resetL if side==-1 else False;resetS=resetS if side==1 else False
        if invalid:
            censored.append(dict(day=day[i],entry_at=str(times[i]),reason='missing exit path'));continue
        gross=(exitprice-entry)*side*POINT
        out.append(dict(day=day[i],entry_at=str(times[i]),signal_at=str(ds[k]),exit_at=str(times[j]+pd.Timedelta(minutes=1)),side=int(side),entry=entry,exit=float(exitprice),risk_points=float(risk),gross=float(gross),net=float(gross-base_cost),stress_net=float(gross-stress_cost),reason=reason,ambiguous=ambiguous))
    return pd.DataFrame(out,columns=['day','entry_at','signal_at','exit_at','side','entry','exit','risk_points','gross','net','stress_net','reason','ambiguous']),censored

def metrics(t,days,cost='net'):
    p=t[cost].to_numpy();daily=t.groupby('day')[cost].sum().reindex(days,fill_value=0).to_numpy();eq=np.r_[0,p.cumsum()]
    losses=-p[p<0].sum();rng=np.random.default_rng(23923)
    ci=np.quantile(rng.choice(daily,size=(5000,len(daily)),replace=True).mean(axis=1),[.025,.975]) if len(days) else [0,0]
    return dict(trades=len(t),net=round(p.sum(),2),pf=round(p[p>0].sum()/losses,3) if losses else None,win_pct=round(100*(p>0).mean(),1) if len(p) else None,drawdown=round((np.maximum.accumulate(eq)-eq).max(),2),daily_ci_low=round(ci[0],2),daily_ci_high=round(ci[1],2),without_best_day=round(daily.sum()-max(daily,default=0),2),positive_days=int((daily>0).sum()),days=len(days),ambiguous_minutes=int(t.ambiguous.sum()))
