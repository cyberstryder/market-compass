"""Bounded previous-RTH recovery for the explicit contracts used by the live feed."""
from datetime import date,timedelta,datetime,timezone
from .futures import selection,prior_rth
from .store import identity
from .market import number
from .diagnostics import redacted_detail


def verify_counts(db,client,plan,now):
    """Free count checks distinguish sparse OHLCV from an incomplete download.

    RTH endpoints are 10-minute aligned, as required for exact metadata counts.
    Databento publishes no OHLCV record for an interval without trades.
    """
    counts={}
    for raw in plan["symbols"]:
        count=client.metadata.get_record_count(dataset="GLBX.MDP3",schema="ohlcv-1m",
            symbols=[raw],stype_in="raw_symbol",
            start=datetime.fromtimestamp(plan["start"],timezone.utc).isoformat(),
            end=datetime.fromtimestamp(plan["end"],timezone.utc).isoformat())
        if not isinstance(count,int) or count<0: raise ValueError("Invalid historical record count")
        counts[raw]=count
    plan.update(provider_record_counts=counts,count_verified_at=now)
    complete=all(0<counts[s]==plan["counts"][s] for s in plan["symbols"])
    plan.update(status="available" if complete else "partial",
        detail=plan["day"]+" same-contract RTH: "+", ".join(f"{s} {plan['counts'][s]}/{counts[s]} provider bars" for s in plan["symbols"])+
        ("; provider count verified. No-trade minutes have no bar; no prices synthesized" if complete else "; stored/provider count mismatch; references remain incomplete"))
    with db.tx() as c:
        contracts=db.prefix(c,"contract:")
        for contract in contracts.values():
            raw=contract["raw_symbol"]
            if raw not in counts: continue
            symbol=contract["resolved_symbol"]
            coverage={"day":plan["day"],"record_count":counts[raw],"stored_count":plan["counts"][raw],
                "verified":0<counts[raw]==plan["counts"][raw],"verified_at":now}
            db.put(c,"historycoverage:"+symbol+":"+plan["day"],coverage)
            level=db.get(c,"levels:"+symbol)
            if level and level.get("prior_day")==plan["day"] and level.get("prior_bars")==counts[raw]:
                level.update(prior_complete=coverage["verified"],prior_history_basis="Provider record count verified; zero-trade intervals omitted")
                db.put(c,"levels:"+symbol,level)


def recover(collector,client,now):
    db,cfg=collector.db,collector.cfg
    targets=selection(cfg.futures,now)
    if not targets: return
    prior,start,end=prior_rth(targets[0]["trading_day"])
    raw_symbols=[t["raw_symbol"] for t in targets]
    key="recovery:futures:"+identity(prior,raw_symbols)
    with db.tx() as c:
        existing=db.get(c,key)
    if existing and existing.get("counts") and not existing.get("count_verified_at"):
        # An existing partial download needs only free metadata verification.
        try: verify_counts(db,client,existing,now)
        except Exception as error:
            existing["verification_error"]=redacted_detail(error,(cfg.databento,),180)
        with db.tx() as c: db.put(c,key,existing)
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
        try: verify_counts(db,client,plan,now)
        except Exception as error:
            plan["verification_error"]=redacted_detail(error,(cfg.databento,),180)
        with db.tx() as c: db.put(c,key,plan)
        db.health("futures_history",plan["status"],plan["detail"],plan["source_ts"])
    except Exception as error:
        # Provider exceptions may contain credential-bearing URLs; redact before reporting.
        detail=type(error).__name__+": "+redacted_detail(error,(cfg.databento,),180)+"; "+("download reservation retained; no automatic paid retry" if reserved else "no historical download made")
        plan.update(status="error",detail=detail,retryable=not reserved)
        with db.tx() as c: db.put(c,key,plan)
        db.health("futures_history","error",detail)
