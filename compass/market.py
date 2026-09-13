import math
import time
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo
import exchange_calendars as xc
import pandas as pd

NY=ZoneInfo("America/New_York")
CT=ZoneInfo("America/Chicago")

def ts(v):
    if v is None: return None
    if isinstance(v,(float,int)):
        return float(v)/(1e9 if v>1e17 else 1e6 if v>1e14 else 1e3 if v>1e11 else 1)
    return datetime.fromisoformat(str(v).replace("Z","+00:00")).timestamp()

def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError): return None

def day(t): return datetime.fromtimestamp(t,NY).date().isoformat()

@lru_cache(maxsize=2)
def calendar(name): return xc.get_calendar(name)

@lru_cache(maxsize=100)
def session(d):
    cal=calendar("XNYS")
    label=pd.Timestamp(d)
    if not cal.is_session(label): return None
    return cal.session_open(label).timestamp(),cal.session_close(label).timestamp()

def is_open(now=None,future=False):
    try:
        return bool(calendar("CMES" if future else "XNYS").is_open_on_minute(
            pd.Timestamp(now or time.time(),unit="s",tz="UTC").floor("min")))
    except (ValueError,KeyError): return False

def fresh(q,now,age=5):
    if not q or q.get("ts") is None or not -1<=now-q["ts"]<=age: return False
    b,a=number(q.get("bid")),number(q.get("ask"))
    return b is not None and a is not None and 0<b<a and (number(q.get("bid_size")) or 0)>0 and (number(q.get("ask_size")) or 0)>0

def dedup(rows,asof):
    latest={}
    for r in sorted(rows,key=lambda r:r.get("id",0)):
        if r["ts"]+60<=asof: latest[r["ts"]]=r
    return sorted(latest.values(),key=lambda r:r["ts"])

def levels(rows,asof):
    rows=dedup(rows,asof)
    hours=session(day(asof))
    out={"asof":asof,"day":day(asof),"or_complete":False}
    if not hours: return {**out,"reason":"No equity regular session today"}
    start,end=hours
    current=[r for r in rows if start<=r["ts"]<min(end,asof)]
    opening=[r for r in current if r["ts"]<start+900]
    prior=[]
    for d in sorted({day(r["ts"]) for r in rows if day(r["ts"])<day(asof)},reverse=True):
        h=session(d)
        if h:
            prior=[r for r in rows if h[0]<=r["ts"]<h[1]]
            if prior: break
    overnight=[r for r in rows if start-15.5*3600<=r["ts"]<start]
    out.update(session_open=start,session_close=end,bars=len(current),
        or_complete=len(opening)==15 and asof>=start+900,
        prior_complete=bool(prior) and len(prior)==round((session(day(prior[0]["ts"]))[1]-session(day(prior[0]["ts"]))[0])/60),
        prior_bars=len(prior))
    for label,group in [("prior",prior),("overnight",overnight),("or",opening)]:
        if group:
            out[label+"_high"]=max(r["payload"]["h"] for r in group)
            out[label+"_low"]=min(r["payload"]["l"] for r in group)
    if prior: out["prior_close"]=prior[-1]["payload"]["c"]
    if current:
        out["price"]=current[-1]["payload"]["c"]
        out["price_asof"]=current[-1]["ts"]+60
        out["vwap_method"]="Volume-weighted source bar VWAP" if all("vw" in r["payload"] for r in current) else "Volume-weighted minute-close approximation"
        volume=sum(r["payload"]["v"] for r in current)
        out["vwap"]=sum(r["payload"].get("vw",r["payload"]["c"])*r["payload"]["v"] for r in current)/volume if volume else None
    if len(rows)>=15:
        tr=[max(b["payload"]["h"]-b["payload"]["l"],abs(b["payload"]["h"]-a["payload"]["c"]),
            abs(b["payload"]["l"]-a["payload"]["c"])) for a,b in zip(rows[-15:],rows[-14:])]
        out["atr14"]=sum(tr)/14
    return out
