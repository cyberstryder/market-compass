"""Bounded native workflow status; excludes credentials and message payloads."""
from sqlalchemy import select,func
from .native_outbox import snapshot as outbox_snapshot
from .native_smoothers import VERSION,weekly,monday_for


def snapshot(db,c,now):
    config=db.get(c,VERSION+':config',{})
    week=monday_for(now).isoformat()
    job=db.get(c,VERSION+':week:'+week,{})
    counts={s:n for s,n in c.execute(select(weekly.c.status,func.count()).where(weekly.c.week==week).group_by(weekly.c.status))}
    return {'morning':db.get(c,'native-morning-v1:status',{}),
        'morning_intake':db.get(c,'native_morning:last_intake',{}),
        'smoothers':{'week':week,'config_received_at':config.get('received_at'),
            'config_owner':config.get('owner','source'),'config_revision':config.get('revision'),'config_count':len(config.get('configs',[])),
            'enabled_tickers':sum(r.get('enabled',False) for r in config.get('configs',[])),
            'state':job.get('state','not_started'),'processed':job.get('index',0),
            'errors':job.get('errors',[]),'sessions':job.get('sessions',[]),'signals':counts,
            'workers':{role:{**db.get(c,VERSION+':worker:'+role,{}),'error':db.get(c,'health:'+VERSION+'_'+role,{})} for role in ('schedule','target','premium')}},
        'notifications':outbox_snapshot(c),
        'basis':'Native shadow work; live delivery and full-session/weekly acceptance remain gated'}
