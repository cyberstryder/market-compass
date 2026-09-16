"""One-shot read-only production acceptance audit; never calls activation."""
import json,os,time
from sqlalchemy import text,select,func
from .store import Store
from .native_config import KEY as CONFIG
from .native_smoothers import monday_for,VERSION
from .smoothers_handoff import review,snapshot
from .native_outbox import outbox

def main():
    db=Store(os.environ['DATABASE_URL'])
    now=time.time();week=monday_for(now).isoformat()
    with db.tx() as c:
        c.execute(text('SET TRANSACTION READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        r,blockers,checksum=review(db,c,week,now)
        config=db.get(c,CONFIG,{})
        state=snapshot(db,c)
        source=db.get(c,'health:project_smoothers',{})
        result=dict(at=now,week=week,code=os.getenv('RAILWAY_GIT_COMMIT_SHA'),
            config_owner=config.get('owner'),config_count=len(config.get('configs',[])),config_revision=config.get('revision'),
            ownership={p:db.get(c,'native:ownership:'+p,{'owner':'original'}).get('owner') for p in ('morning','smoothers')},
            handoff_plan=state['plan'],job_state=r['job'].get('state'),
            native_rows=len(r['rows']),source_only=len(r['source_only']),blockers=blockers,
            current_week_ready=not blockers,review_checksum=checksum,
            source_health={k:source.get(k) for k in ('status','checked_at')},
            native_delivery_counts=[dict(program=p,status=s,count=n) for p,s,n in c.execute(select(outbox.c.program,outbox.c.status,func.count()).group_by(outbox.c.program,outbox.c.status))],
            in_flight=state['in_flight'],unknown_delivery=state['unknown_delivery'],
            database_read_only=c.execute(text('SHOW transaction_read_only')).scalar_one())
    db.engine.dispose()
    print('SMOOTHERS_HANDOFF_AUDIT '+json.dumps(result,sort_keys=True),flush=True)

if __name__=='__main__':main()
