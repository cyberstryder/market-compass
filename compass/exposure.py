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
        "gex":None,"vex":None,"coverage":0,"vex_coverage":0,
        "missing_oi":0,"missing_gamma":0,"invalid_contract":0,"missing_vanna_inputs":0,
        "sign_model":"Calls positive, puts negative: assumed inventory proxy",
        "gex_units":"USD delta change per 1% spot move",
        "vex_units":"USD delta change per +1 volatility point",
        "oi_timing":"Daily OI; not live dealer positions","oi_dates":[]}
    spot=number(spot)
    groups=defaultdict(lambda:{"gex":0.,"vex":None,"contracts":0})
    dates=set()
    for o in contracts:
        oi,gamma,strike=number(o.get("oi")),number(o.get("gamma")),number(o.get("strike"))
        invalid=not strike or strike<=0 or o.get("type") not in {"call","put"} or number(o.get("multiplier"))!=100
        out["missing_oi"]+=oi is None or oi<0
        out["missing_gamma"]+=gamma is None or gamma<0
        out["invalid_contract"]+=bool(invalid)
        if oi is None or oi<0 or gamma is None or gamma<0 or invalid: continue
        out["usable_gex"]+=1
        if o.get("oi_date"): dates.add(o["oi_date"])
        iv,years=number(o.get("iv")),None
        try:
            expiry=datetime.fromisoformat(o["expiry"]).replace(hour=16,tzinfo=NY).timestamp()
            if iv and iv>0 and expiry>asof and not o.get("underlying","").startswith("SPX"):
                years=(expiry-asof)/(365*86400)
        except (TypeError,KeyError,ValueError): pass
        if years is not None: out["usable_vex"]+=1
        else: out["missing_vanna_inputs"]+=1
        if spot is None or spot<=0: continue
        sign=1 if o["type"]=="call" else -1
        g=groups[strike]
        g["gex"]+=sign*gamma*oi*100*spot**2*.01
        g["contracts"]+=1
        if years is not None:
            vv=vanna(spot,strike,iv,years)
            g["vex"]=(g["vex"] or 0)+sign*vv*oi*100*spot*.01
    out["strikes"]=[{"strike":k,**v} for k,v in sorted(groups.items())]
    out["coverage"]=out["usable_gex"]/len(contracts) if contracts else 0
    out["vex_coverage"]=out["usable_vex"]/len(contracts) if contracts else 0
    out["oi_dates"]=sorted(dates)
    if spot is None or spot<=0: return {**out,"status":"blocked","reason":"Missing fresh underlying price; input coverage is measured independently"}
    out["gex"]=sum(v["gex"] for v in groups.values()) if groups else None
    vex_values=[v["vex"] for v in groups.values() if v["vex"] is not None]
    out["vex"]=sum(vex_values) if vex_values else None
    out["status"]="available" if complete and out["coverage"]>=.8 else "partial"
    if not out["usable_gex"]: out["status"]="blocked"
    out["reason"]="Research proxy; OI date may be unavailable. No exposure-driven orders enabled."
    return out
