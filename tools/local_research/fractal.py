"""Delivery-review signal adapter only. Source: fractal-model-desk-strategy.pine.
No FVG/IC mode, manual bias/POI or native target performance claimed.
"""
from types import SimpleNamespace as NS
import numpy as np
import pandas as pd

def fractal_signals(d,raw,n,prior_atr,tick=.25):
    tf=15 if n==1 else 60
    ht=raw[['open','high','low','close']].resample(f'{tf}min').agg({'open':'first','high':'max','low':'min','close':'last'})
    count=raw.close.resample(f'{tf}min').count();ht=ht[count==tf].dropna()
    result=np.zeros(len(d),int);book=[];frames=[];frame_old=None;frame_no=0
    run_dir=0;run_open=np.nan;bull_open=bear_open=np.nan;seed_bull=seed_bear=False
    audits=[]
    def finish(s,why):s.done=True;s.status=why
    def cisd(s,c,closed,frame):
        if s.cisd_time is None and np.isfinite(s.anchor) and (c-s.anchor)*s.dir>0 and (s.anchor-s.ext)*s.dir>0:
            s.cisd=s.anchor;s.cisd_time=closed;s.cisd_frame=frame
    def overlap(a,b):
        den=min(a.high-a.low,b.high-b.low)
        return max(0,min(a.high,b.high)-max(a.low,b.low))/den if den>0 else 1
    for i,(stamp,b) in enumerate(d.iterrows()):
        frame=stamp.floor(f'{tf}min');closed=stamp+pd.Timedelta(minutes=n)
        new=frame_old is None or frame!=frame_old
        ix=ht.index.searchsorted(frame)-1
        prior=ht.iloc[ix] if ix>=0 else None;pt=ht.index[ix] if ix>=0 else None
        if new:
            ready=frame_old is not None or stamp==frame
            frame_no+=1;frame_old=frame;seed_bull=seed_bear=False
            if prior is not None:frames.append((pt,prior));frames=frames[-3:]
        bodydir=int(np.sign(b.close-b.open))
        if bodydir:
            if bodydir!=run_dir:run_open=b.open
            if bodydir==1:bull_open=run_open
            else:bear_open=run_open
        run_dir=bodydir
        for s in book:
            if s.done:continue
            age=frame_no-s.frame_no+2
            if new:
                expected=s.c2time if age==3 else s.c3frame if age==4 else s.c4frame if age==5 else pt
                if pt!=expected or prior is None:finish(s,'HTF data gap')
                elif age==3:
                    s.c2open=prior.open;s.c2high=prior.high;s.c2low=prior.low;s.c2close=prior.close;s.c3frame=frame
                    if s.c1low<prior.close<s.c1high:s.closure=2;s.invalid=prior.low if s.dir==1 else prior.high
                elif age==4:
                    if not s.closure:
                        bodybreak=prior.close>max(s.c2open,s.c2close) if s.dir==1 else prior.close<min(s.c2open,s.c2close)
                        held=s.c3held and (prior.low>=s.c2low if s.dir==1 else prior.high<=s.c2high)
                        if bodybreak and held:s.closure=3;s.invalid=s.c2low if s.dir==1 else s.c2high
                        else:finish(s,'No C2/C3 closure')
                    s.c4frame=frame
                elif age>=5:finish(s,'C4 complete')
            if s.done:continue
            if age==3 and not s.closure:s.c3held &= b.low>=s.c2low if s.dir==1 else b.high<=s.c2high
            extends=age==2 and (b.low<s.ext if s.dir==1 else b.high>s.ext)
            if s.cisd_time is None and extends:
                s.ext=b.low if s.dir==1 else b.high;s.ext_time=stamp;s.anchor=bear_open if s.dir==1 else bull_open
            if s.cisd_time is not None and closed>s.cisd_time and (b.low<s.ext if s.dir==1 else b.high>s.ext):s.failed=True
            if age<=3:cisd(s,b.close,closed,frame)
            if s.closure and (b.low<s.invalid if s.dir==1 else b.high>s.invalid):finish(s,'Invalidated')
            if not s.done and s.closure and (b.high>=s.c1high if s.dir==1 else b.low<=s.c1low):s.liq=True
        if ready and prior is not None:
            bs=b.low<prior.low;ss=b.high>prior.high
            for side in (-1,1):
                detected=bs and not seed_bull if side==1 else ss and not seed_bear
                if not detected:continue
                s=NS(dir=side,frame_no=frame_no,c2time=frame,c1high=prior.high,c1low=prior.low,
                    ext=b.low if side==1 else b.high,ext_time=stamp,anchor=bear_open if side==1 else bull_open,
                    cisd_time=None,cisd_frame=None,cisd=np.nan,closure=0,c3frame=None,c4frame=None,c3held=True,
                    c2open=np.nan,c2close=np.nan,c2high=np.nan,c2low=np.nan,invalid=np.nan,done=False,status='',failed=False,
                    liq=False,dual=bs and ss,tracked=None,legbars=0,path=0.,legclose=np.nan,measured=False,quality=False,review=None)
                book.append(s)
                if seed_bull or seed_bear:
                    for other in book:
                        if other.c2time==frame:other.dual=True
                if side==1:seed_bull=True
                else:seed_bear=True
                cisd(s,b.close,closed,frame)
        while len(book)>24:
            idx=next((j for j,s in enumerate(book) if s.done),None)
            if idx is None:break
            book.pop(idx)
        chop=False
        if len(frames)==3:
            older,middle,latest=frames
            sweepcount=sum(older[0]<=s.c2time<=latest[0] for s in book)
            chop=sweepcount>=3 and overlap(older[1],middle[1])>=.7 and overlap(middle[1],latest[1])>=.7
        for s in book:
            if not s.measured and not s.done:
                if s.tracked!=s.ext_time:s.tracked=s.ext_time;s.legbars=1;s.path=abs(b.close-s.ext)
                else:s.legbars+=1;s.path+=abs(b.close-s.legclose)
                s.legclose=b.close
                if s.cisd_time is not None:
                    s.measured=True;av=prior_atr[i];warm=np.isfinite(av) and av>0
                    efficiency=min(1,max(0,(b.close-s.ext)*s.dir)/s.path) if s.path>0 else 0
                    decisive=warm and (b.close-s.cisd)*s.dir/av>=.05
                    v=decisive and s.legbars<=3 and efficiency>=.7
                    displacement=decisive and (b.close-b.open)*s.dir>0 and abs(b.close-b.open)/av>=.8 and b.high>b.low and abs(b.close-b.open)/(b.high-b.low)>=.6
                    s.quality=v or displacement
            paired=s.cisd_frame==(s.c2time if s.closure==2 else s.c3frame) if s.closure else False
            clear=not(s.done or s.failed or s.dual or s.liq or chop) and s.measured and s.quality and paired
            if s.review is None and clear:
                s.review=closed
                stop=s.invalid-s.dir*tick;target=s.c1high if s.dir==1 else s.c1low
                geometry=(b.close-stop)*s.dir>tick and (target-b.close)*s.dir>tick
                audits.append(dict(observed_at=str(closed),side=s.dir,c2_frame=str(s.c2time),cisd_at=str(s.cisd_time),closure=s.closure,geometry_ok=bool(geometry)))
                if result[i]==0 and geometry:result[i]=s.dir
    return result,audits
