"""Causal research segments. Missing bars are never synthesized."""
import numpy as np
import pandas as pd
import engine

def scheduled(prev,nxt):
    # Only customary daily/weekend maintenance is bridged. Holidays are unknown.
    if (prev.hour,prev.minute)!=(15,59) or (nxt.hour,nxt.minute)!=(17,0):return False
    delta=(nxt.date()-prev.date()).days
    return (delta==0 and prev.weekday()<4) or (delta==2 and prev.weekday()==4 and nxt.weekday()==6)

def segments(raw):
    starts=[0]
    for i in range(1,len(raw)):
        prev,nxt=raw.index[i-1],raw.index[i]
        if nxt-prev!=pd.Timedelta(minutes=1) and not scheduled(prev,nxt):starts.append(i)
    return [raw.iloc[a:b] for a,b in zip(starts,starts[1:]+[len(raw)])]

def calculate(raw,n):
    bars=[];atrs=[];biases=[];effs=[];kes=[];all_sigs={};readies=[]
    for part in segments(raw):
        d=engine.make_bars(part,n)
        if len(d)<2:continue
        a,b,e,s,k=engine.features(d,part,n)
        count=part.close.resample('15min',label='right',closed='left').count()
        available=count[count==15].index.searchsorted(d.index+pd.Timedelta(minutes=n),side='right')
        ready=(np.arange(len(d))>=200)&(available>=50)
        bars.append(d);atrs.append(a);biases.append(b);effs.append(e);kes.append(k);readies.append(ready)
        for name,signal in s.items():all_sigs.setdefault(name,[]).append(np.where(ready,signal,0))
    if not bars:return None
    return (pd.concat(bars),pd.concat(atrs),np.concatenate(biases),np.concatenate(effs),
            {k:np.concatenate(v) for k,v in all_sigs.items()},np.concatenate(kes),np.concatenate(readies))
