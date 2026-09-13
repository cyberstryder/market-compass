"""Durable rotating reconciliation of a paginated, moving vendor feed."""
import math
from datetime import date,timedelta
from sqlalchemy import select,func
from .market import day
from .store import flow_records,identity
from .vendor import flow_page


def checkpoint(db,c,label):
    return db.get(c,"recovery:flow:"+label,{"day":label,"next_page":2,"total":0,
        "page_size":100,"passes":0,"rejected_observations":0})


def ingest(db,label,page,part,now):
    """Rows and resume point commit together; repeated IDs update corrections."""
    with db.tx() as c:
        cp=checkpoint(db,c,label)
        if page!=part["page"]: raise ValueError("TraderMatrix returned an unexpected page")
        for row in part["rows"]:
            q=db.insert(flow_records).values(day=label,vendor_id=row["vendor_id"],
                source_ts=row["source_ts"],payload=row,first_seen=now,last_seen=now)
            c.execute(q.on_conflict_do_update(index_elements=["day","vendor_id"],
                set_={"source_ts":row["source_ts"],"payload":row,"last_seen":now}))
            db.append(c,"vendor_flow","tradermatrix",row["symbol"],row["source_ts"],row,
                key=identity("tradermatrix-flow",label,row["vendor_id"],{k:v for k,v in row.items() if k!="received"}))
        ceiling=max(1,math.ceil(part["total"]/part["page_size"]))
        cp.update(total=part["total"],page_size=part["page_size"],page_ceiling=ceiling,
            at=now,last_page=page,rejected_observations=cp["rejected_observations"]+part["rejected"])
        if page>=ceiling and (part["raw_count"]>0 or part["total"]==0):
            cp.update(next_page=2,passes=cp["passes"]+1,last_full_pass_at=now)
        elif page>1:
            cp["next_page"]=page+1
        count=c.execute(select(func.count()).select_from(flow_records).where(flow_records.c.day==label)).scalar_one()
        cp.update(unique_rows=count,estimated_gap=max(0,part["total"]-count))
        cp["status"]="catching_up" if cp["estimated_gap"] or not cp.get("last_full_pass_at") else "reconciled"
        db.put(c,"recovery:flow:"+label,cp)
    return cp


async def collect(collector,now,page_cap=5):
    db=collector.db
    today=day(now)
    yesterday=(date.fromisoformat(today)-timedelta(days=1)).isoformat()
    requests=0
    current_rows=0
    rejected=0

    async def fetch(label,page):
        nonlocal requests,current_rows,rejected
        frame="today" if label==today else "yesterday"
        data,received=await collector.matrix_request(
            f"/unusual-activity?timeFrame={frame}&minPremium=50000&page={page}&pageSize=100","unusual_activity")
        part=flow_page(data,received)
        cp=ingest(db,label,page,part,received)
        requests+=1
        current_rows+=part["raw_count"]
        rejected+=part["rejected"]
        return part,cp

    first,cp=await fetch(today,1)  # Newest page gets every cycle, even during recovery.
    with db.tx() as c:
        old=db.get(c,"recovery:flow:"+yesterday)
    if requests<page_cap and old and old.get("status")!="reconciled":
        # Yesterday is still available from the documented endpoint after a restart.
        await fetch(yesterday,max(1,min(old["next_page"],old.get("page_ceiling",1))))
    # Resume with one overlapping page. Offset pagination can move; never claim an
    # atomic snapshot. Repeated full passes repair shifted pages and corrections.
    overlap=1 if page_cap-requests>=2 else 0
    page=max(2,min(cp["next_page"]-overlap,cp["page_ceiling"]))
    while requests<page_cap and page<=cp["page_ceiling"] and first["total"]:
        part,cp=await fetch(today,page)
        if page>=cp["page_ceiling"] or not part["raw_count"]: break
        page+=1
    with db.tx() as c:
        cp=checkpoint(db,c,today)
        rows=[r[0] for r in c.execute(select(flow_records.c.payload).where(flow_records.c.day==today)
            .order_by(flow_records.c.source_ts.desc()).limit(100))]
        recovery={k[14:]:v for k,v in db.prefix(c,"recovery:flow:").items()}
        summary={"source":"tradermatrix","received":cp["at"],"day":today,
            "source_ts":max((r["source_ts"] for r in rows),default=None),"rows":rows,
            "total":cp["total"],"pages":requests,"page_cap":page_cap,"fetched_rows":current_rows,
            "unique_rows":cp["unique_rows"],"rejected":rejected,
            "limited":cp["status"]!="reconciled","status":"partial" if cp["status"]!="reconciled" or rejected else "available",
            "filters":first["filters"],"aggregates":first["aggregates"],"recovery":cp,
            "prior_gaps":[v for k,v in recovery.items() if k!=today and v.get("status")!="reconciled"][-7:],
            "coverage_note":"$50k vendor-filtered feed. Page 1 refreshes each cycle; deeper pages resume durably with overlap. Daily unique IDs and corrections are retained. Estimated gaps compare vendor count with stored IDs; moving pages, deletions or vendor omissions can still leave gaps after a full pass.",
            "schema_validation":"Live rows accepted against documented fields; classifications remain vendor supplied" if rows else "No live row received for this day yet"}
        db.put(c,"matrix:unusual_activity",summary)
    db.health("tradermatrix_flow",summary["status"],
        f"{today}: {cp['unique_rows']} unique rows / {cp['total']} vendor count; estimated gap {cp['estimated_gap']}; resume page {cp['next_page']}",summary["source_ts"])
    return summary
