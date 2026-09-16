"""Read-only production delivery inventory; never queues or sends messages."""
import json, time
from collections import Counter
from compass.config import Config
from compass.store import Store
from compass.alerts import outbox_status
from compass.alert_routes import route_for
cfg=Config()
db=Store(cfg.db)
with db.tx() as c:
    now=time.time()
    status=outbox_status(db,c,now)
    rows=db.recent(c,'alert',limit=2000,since=now-86400)
    report={
      'at':now,
      'pending':status['pending'],
      'unassigned':status['unassigned'],
      'routes':[{'route':r['route'],'mode':r['destination_mode'],'health':r['health'].get('status'),
         'channel_id':r['health'].get('destination',{}).get('channel_id'),
         'pending':r['pending'],'confirmation':r['last_confirmation']} for r in status['routes']],
      'observed_alerts_24h_bounded_2000':dict(Counter(route_for(r) for r in rows)),
      'native_ownership':{p:db.get(c,'native:ownership:'+p,{}).get('owner','original') for p in ('morning','smoothers')},
      'engine_heartbeat':db.get(c,'worker:engine'),
      'latest_spy_brief':db.get(c,'spy-brief:latest',{}).get('id'),
    }
print('ALERT_ROUTE_AUDIT '+json.dumps(report,default=str),flush=True)
db.engine.dispose()
