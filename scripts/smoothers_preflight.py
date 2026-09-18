"""Read-only upcoming-week readiness; never creates a weekly signal or delivery.

Run with existing provider credentials and DATABASE_URL. Prints a JSON report.
"""
import json
import time
from datetime import datetime,timedelta
import httpx
from compass.config import Config
from compass.store import Store
from compass.native_smoothers_data import Data,ET
from compass.native_config import validate
from compass.smoothers_math import compute_smoother


def inspect(data, config, now):
    today=datetime.fromtimestamp(now,ET).date()
    monday=today+timedelta(days=(7-today.weekday())%7 or 7)
    rows=validate(config['configs'])
    sessions=data.calendar(monday)
    result=dict(at=now,week=monday.isoformat(),revision=config.get('revision'),sessions=sessions,
        enabled=sum(r['enabled'] for r in rows),rows=[],mode='read_only_preflight',
        live_gate='Monday first completed hour, native/source reconciliation, then full weekly cycle',
        capture_after=sessions[0]['open']+3900 if sessions else None,
        capture_deadline=sessions[0]['open']+7200 if sessions else None)
    for row in rows:
        if not row['enabled']:continue
        item=dict(ticker=row['ticker'])
        try:
            data.deadline=time.monotonic()+45
            hourly=data.hourly(row['ticker'],now);daily=data.daily(row['ticker'],now)
            required=4*max(row[k] for k in ('s1','s2','s3'))+10
            warm=not hourly.empty and len(hourly)>=required and all(
                compute_smoother(hourly,row[k]).notna().iloc[-1] for k in ('s1','s2','s3'))
            item.update(hourly_bars=len(hourly),required_hourly=required,daily_bars=len(daily),
                history_ready=bool(warm and len(daily)>=15),last_hourly=str(hourly.index[-1]) if len(hourly) else None)
            if len(hourly):
                price=float(hourly['close'].iloc[-1])
                item['contracts']={direction:data.contract(row['ticker'],monday+timedelta(days=4),direction,price)
                                   for direction in ('CALL','PUT')}
                item['chain_ready']=all(item['contracts'].values())
            else:item['chain_ready']=False
            item['status']='ready_for_live_capture' if item['history_ready'] and item['chain_ready'] and sessions else 'review'
        except Exception as error:
            item.update(status='error',error=type(error).__name__,reason=str(error)[:120])
        result['rows'].append(item)
        print('PREFLIGHT_PROGRESS '+row['ticker']+' '+item['status'],flush=True)
    result['ready']=sum(r['status']=='ready_for_live_capture' for r in result['rows'])
    return result


def main():
    cfg=Config();db=Store(cfg.db)
    try:
        with db.tx() as c:config=db.get(c,'native-smoothers-v1:config',{})
        with httpx.Client(timeout=10,follow_redirects=False) as client:
            report=inspect(Data(cfg,client),config,time.time())
        print(json.dumps(report,sort_keys=True))
    finally:db.engine.dispose()


if __name__=='__main__':main()
