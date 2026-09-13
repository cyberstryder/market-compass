"""Explain provider input gaps without synthesizing missing Greeks."""
from collections import defaultdict
import pandas as pd
from .market import number,calendar,day


def diagnose(contracts,reference,reference_ts,now,complete):
    cal=calendar("XNYS")
    label=cal.date_to_session(pd.Timestamp(day(now)),direction="next")
    if now>=cal.session_close(label).timestamp(): label=cal.next_session(label)
    target=label.date().isoformat()
    groups=defaultdict(lambda:{"contracts":0,"gamma_usable":0,"gamma_absent":0,"gamma_null":0,
        "gamma_invalid":0,"oi_missing":0,"iv_missing":0})
    samples=[]
    for o in contracts:
        strike=number(o.get("strike"))
        m=(reference-strike)/reference if reference and strike else None
        itm=m if o.get("type")=="call" else -m if m is not None else None
        bucket="unknown" if m is None else "near_ATM_2pct" if abs(m)<=.02 else "deep_ITM_10pct" if itm>=.10 else "other"
        gamma=number(o.get("gamma"))
        state=o.get("gamma_field","null" if o.get("gamma") is None else "value")
        usable=gamma is not None and gamma>=0
        labels=[("all","all","all"),(o.get("expiry"),o.get("type"),bucket)]
        if o.get("expiry")==target and bucket=="near_ATM_2pct": labels.append((target,"both","target_near_ATM"))
        for key in labels:
            g=groups[key]
            g["contracts"]+=1
            g["gamma_usable"]+=usable
            g["gamma_absent"]+=not usable and state=="absent"
            g["gamma_null"]+=not usable and state=="null"
            g["gamma_invalid"]+=not usable and state not in {"absent","null"}
            g["oi_missing"]+=number(o.get("oi")) is None or (number(o.get("oi")) or 0)<0
            g["iv_missing"]+=not (number(o.get("iv")) or 0)>0
        if not usable:
            samples.append({k:o.get(k) for k in ("symbol","expiry","type","strike","oi","iv","gamma_field","greek_fields")}|{
                "moneyness":bucket,"quote_ts":(o.get("quote") or {}).get("ts")})
    samples.sort(key=lambda o:(o["expiry"]!=target,o["moneyness"]!="near_ATM_2pct",abs((number(o["strike"]) or 0)-(reference or 0))))
    total=groups[("all","all","all")]
    near=groups.get((target,"both","target_near_ATM"),{"contracts":0,"gamma_usable":0})
    return {"asof":now,"complete_pagination":complete,"target_expiry":target,
        "reference":reference,"reference_ts":reference_ts,"totals":total,"target_near_atm":near,
        "groups":[{"expiry":k[0],"type":k[1],"moneyness":k[2],**v} for k,v in sorted(groups.items()) if k[0]!="all" and k[1]!="both"],
        "missing_samples":samples[:12],
        "finding":f"Provider omitted gamma on {total['gamma_absent']} contracts and returned null on {total['gamma_null']}; {total['gamma_invalid']} invalid values. Raw field presence is retained before normalization.",
        "note":"Historical reference is used only to classify moneyness and select subscriptions. Missing Greeks remain missing. Deep ITM can explain some provider omissions; this breakdown does not establish the cause of every missing value or make stale exposure current."}
