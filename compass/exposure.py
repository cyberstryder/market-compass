import math
from collections import defaultdict
from datetime import datetime
from .market import number,NY

def vanna(spot,strike,iv,years,rate=.04):
    if min(spot,strike,iv,years)<=0: return None
    d1=(math.log(spot/strike)+(rate+iv*iv/2)*years)/(iv*math.sqrt(years))
    d2=d1-iv*math.sqrt(years)
    return -math.exp(-d1*d1/2)/math.sqrt(2*math.pi)*d2/iv

def calculate(contracts,spot,asof,source,complete=True):
    out={"source":source,"asof":asof,"spot":spot,"contracts":len(contracts),
        "chain_complete":complete,"usable_gex":0,"usable_vex":0,"strikes":[],
        "sign_model":"Calls positive, puts negative: assumed inventory proxy",
        "gex_units":"USD delta change per 1% spot move",
        "vex_units":"USD delta change per +1 volatility point",
        "oi_timing":"Daily OI; not live dealer positions","oi_dates":[]}
    if not spot or spot<=0: return {**out,"status":"blocked","reason":"Missing fresh underlying price"}
    groups=defaultdict(lambda:{"gex":0.,"vex":0.,"contracts":0})
    dates=set()
    for o in contracts:
        oi,gamma,strike=number(o.get("oi")),number(o.get("gamma")),number(o.get("strike"))
        if oi is None or oi<0 or gamma is None or gamma<0 or not strike or o.get("type") not in {"call","put"}: continue
        if number(o.get("multiplier"))!=100: continue
        sign=1 if o["type"]=="call" else -1
        g=groups[strike]
        g["gex"]+=sign*gamma*oi*100*spot**2*.01
        g["contracts"]+=1
        out["usable_gex"]+=1
        if o.get("oi_date"): dates.add(o["oi_date"])
        iv=number(o.get("iv"))
        if iv and o.get("expiry") and not o.get("underlying","").startswith("SPX"):
            expiry=datetime.fromisoformat(o["expiry"]).replace(hour=16,tzinfo=NY).timestamp()
            vv=vanna(spot,strike,iv,(expiry-asof)/(365*86400))
            if vv is not None:
                g["vex"]+=sign*vv*oi*100*spot*.01
                out["usable_vex"]+=1
    out["strikes"]=[{"strike":k,**v} for k,v in sorted(groups.items())]
    out["coverage"]=out["usable_gex"]/len(contracts) if contracts else 0
    out["gex"]=sum(v["gex"] for v in groups.values()) if out["usable_gex"] else None
    out["vex"]=sum(v["vex"] for v in groups.values()) if out["usable_vex"] else None
    out["oi_dates"]=sorted(dates)
    out["status"]="available" if complete and out["coverage"]>=.8 else "partial"
    if not out["usable_gex"]: out["status"]="blocked"
    out["reason"]="Research proxy; OI date may be unavailable. No exposure-driven orders enabled."
    return out
