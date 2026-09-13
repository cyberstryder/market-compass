"""Bounded previous-RTH recovery for the explicit contracts used by the live feed."""
from datetime import date,timedelta,datetime,timezone
from .futures import selection,prior_rth
from .store import identity
from .market import number
from .diagnostics import redacted_detail


def recover(collector,client,now):
    db,cfg=collector.db,collector.cfg
    targets=selection(cfg.futures,now)
    if not targets: return
    prior,start,end=prior_rth(targets[0]["trading_day"])
    raw_symbols=[t["raw_symbol"] for t in targets]
    key="recovery:futures:"+identity(prior,raw_symbols)
    with db.tx() as c:
        existing=db.get(c,key)
    def final(job):
        return job and not job.get("retryable") and not (job.get("status")=="planning" and now-job.get("at",now)>180)
    if final(existing):
        db.health("futures_history",existing["status"],existing["detail"],existing.get("source_ts"))
        return
    plan={"day":prior,"symbols":raw_symbols,"start":start,"end":end,"status":"planning",
        "detail":"Preparing one previous regular session for explicit contracts","at":now}
    with db.tx() as c:
        if not db.lease(c,"futures_history_recovery",collector.history_owner,180): return
        if final(db.get(c,key)): return
        db.put(c,key,plan)
    reserved=False
    try:
        resolved=client.symbology.resolve(dataset="GLBX.MDP3",symbols=raw_symbols,
            stype_in="raw_symbol",stype_out="instrument_id",start_date=prior,
            end_date=(date.fromisoformat(prior)+timedelta(days=1)).isoformat())
        mappings={}
        for raw in raw_symbols:
            matches=resolved.get("result",{}).get(raw,[])
            ids={int(m["s"]) for m in matches if m.get("s") is not None}
            if len(ids)!=1: raise ValueError("Historical contract mapping is absent or ambiguous")
            mappings[ids.pop()]=raw
        expected=round((end-start)/60)
        counts={raw:set() for raw in raw_symbols}
        with db.tx() as c:
            for iid,raw in mappings.items():
                for row in db.recent(c,"bar",f"{raw}@{iid}",limit=expected+5,since=start):
                    if start<=row["ts"]<end: counts[raw].add(row["ts"])
        params={"dataset":"GLBX.MDP3","schema":"ohlcv-1m","symbols":raw_symbols,
            "stype_in":"raw_symbol","start":datetime.fromtimestamp(start,timezone.utc).isoformat(),
            "end":datetime.fromtimestamp(end,timezone.utc).isoformat(),"limit":1200}
        estimate=0.0
        if any(len(v)<expected for v in counts.values()):
            estimate=number(client.metadata.get_cost(**params))
            if estimate is None or estimate<0: raise ValueError("Historical cost estimate invalid")
            with db.tx() as c:
                budget=db.get(c,"budget:futures_history",{"reserved_usd":0.0})
                if budget["reserved_usd"]+estimate>cfg.history_budget:
                    plan.update(status="blocked",estimated_usd=estimate,detail="Historical estimate exceeds cumulative $"+str(cfg.history_budget)+" recovery budget; no download made")
                    db.put(c,key,plan)
                    blocked=True
                else:
                    blocked=False
                    budget["reserved_usd"]+=estimate
                    db.put(c,"budget:futures_history",budget)
                    plan.update(status="reserved",estimated_usd=estimate,detail="One bounded download reserved; an uncertain attempt will not be charged again automatically")
                    db.put(c,key,plan)
            if blocked:
                db.health("futures_history","blocked",plan["detail"])
                return
            reserved=True
            data=client.timeseries.get_range(**params)
            items=[]
            def capture(row):
                iid=getattr(row,"instrument_id",None)
                if iid not in mappings or not hasattr(row,"open"): return
                stamp=row.ts_event/1e9
                if not start<=stamp<end: raise ValueError("History returned an out-of-window bar")
                raw=mappings[iid]
                items.append((f"{raw}@{iid}",stamp,{"o":row.open/1e9,"h":row.high/1e9,
                    "l":row.low/1e9,"c":row.close/1e9,"v":row.volume,"instrument_id":iid,"alias":raw}))
                counts[raw].add(stamp)
                if len(items)>1200: raise ValueError("History record limit exceeded")
            data.replay(capture)
            collector.bars("databento",items)
        complete=all(len(v)==expected for v in counts.values())
        plan.update(status="available" if complete else "partial",estimated_usd=estimate,
            counts={raw:len(v) for raw,v in counts.items()},expected_per_symbol=expected,
            source_ts=max((max(v) for v in counts.values() if v),default=None),
            detail=f"{prior} same-contract RTH recovery: "+", ".join(f"{raw} {len(v)}/{expected} minutes" for raw,v in counts.items())+
                ("" if complete else "; absent trade minutes are not filled with invented bars"))
        with db.tx() as c:
            for iid,raw in mappings.items():
                target=next(t for t in targets if t["raw_symbol"]==raw)
                db.put(c,"contract:"+target["root"],{**target,"instrument_id":iid,"resolved_symbol":f"{raw}@{iid}","mapping_source":"databento historical symbology","at":now})
                db.append(c,"mapping","databento",raw,now,{"instrument_id":iid,"input":target["configured_symbol"],"output":raw})
            db.put(c,key,plan)
        db.health("futures_history",plan["status"],plan["detail"],plan["source_ts"])
    except Exception as error:
        # Provider exceptions may contain credential-bearing URLs; redact before reporting.
        detail=type(error).__name__+": "+redacted_detail(error,(cfg.databento,),180)+"; "+("download reservation retained; no automatic paid retry" if reserved else "no historical download made")
        plan.update(status="error",detail=detail,retryable=not reserved)
        with db.tx() as c: db.put(c,key,plan)
        db.health("futures_history","error",detail)
