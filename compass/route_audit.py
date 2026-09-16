"""Bounded read-only audit; historical REST is diagnostic, never a live repair."""
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
import httpx
from sqlalchemy import select, text
from compass.store import Store, events
from compass.option_ideas import ideas
from compass.market import fresh, session, ts


def emit(kind, value):
    print('SOURCE_GAP_AUDIT ' + json.dumps(dict(kind=kind, **value), sort_keys=True), flush=True)


def main():
    db=Store(os.environ['DATABASE_URL'])
    try:
        with db.tx() as c:
            c.execute(text('SET TRANSACTION READ ONLY'))
            c.execute(text("SET LOCAL statement_timeout='20s'"))
            now=time.time()
            state=db.get(c,'forward-acceptance-v1:report',{})
            emit('acceptance',{k:state.get(k) for k in ('at','feed_fix_validation','reliability')})
            config=db.get(c,'native-smoothers-v1:config',{})
            # Explicit keys only; never provider/webhook configuration.
            emit('ownership',dict(morning=db.get(c,'native:ownership:morning',{}).get('owner','original'),
                smoothers=db.get(c,'native:ownership:smoothers',{}).get('owner','original'),
                morning_routing=db.get(c,'native:morning:routing',{}),
                smoothers_config={k:config.get(k) for k in ('owner','revision','owned_since')},
                smoothers_config_count=len(config.get('configs',[]))))
            rows=c.execute(select(ideas.c.payload).where(ideas.c.status=='unresolved',
                ideas.c.created>=now-2*86400,
                ideas.c.payload['collection_version'].as_string()=='option-reliability-v3'
                ).order_by(ideas.c.created.desc()).limit(501)).scalars().all()
            current=[r for r in rows[:500] if r.get('collection_version')=='option-reliability-v3']
            emit('cohort',dict(unresolved_v3=len(current),scan_truncated=len(rows)>500))
            selected=current[:8]
            checks=[]
            for p in selected:
                end=p['gap_detail'].get('checked_at',p['finished_at'])
                after=p['gap_detail'].get('option_last',end-30)
                symbol=p['contract']['symbol']
                records=c.execute(select(events.c.ts,events.c.received,events.c.payload).where(
                    events.c.kind=='quote',events.c.symbol==symbol,events.c.ts>=after-1,
                    events.c.ts<=end+10).order_by(events.c.ts).limit(2001)).all()
                detail=[]
                for r in records[:2000]:
                    q=r.payload
                    detail.append(dict(source_ts=r.ts,received=r.received,source=q.get('source'),
                        source_age_at_socket=(q['socket_read_at']-r.ts) if q.get('socket_read_at') else None,
                        persistence_after_socket=(r.received-q['socket_read_at']) if q.get('socket_read_at') else None,
                        usable_at_receipt=r.ts<=r.received and fresh(q,r.received),
                        stored_after_failure=r.received>end))
                checks.append(dict(id=p['id'],underlying=p['underlying'],contract=symbol,after=after,end=end,
                    recorded=detail,truncated=len(records)>2000,archived_check=p.get('archive_check'),
                    option_source=p.get('option_source'),opened_at=p.get('opened_at'),
                    contract_volume=p['contract'].get('volume'),contract_oi=p['contract'].get('oi')))
            start,end=session('2026-09-15')
            symbols=['SPY','QQQ','TGT','SBUX','RIOT','BSX','BMY','ORCL','NOK','MAR','MCD','VRT']
            bar_archive={}
            for symbol in symbols:
                records=c.execute(select(events.c.ts,events.c.received,events.c.payload).where(
                    events.c.kind=='bar',events.c.symbol==symbol,events.c.ts>=start,
                    events.c.ts<end).order_by(events.c.id).limit(10001)).all()
                latest={};first={}
                for r in records[:10000]:
                    latest[r.ts]=r.payload; first[r.ts]=min(first.get(r.ts,r.received),r.received)
                bar_archive[symbol]=dict(latest=latest,first=first,truncated=len(records)>10000)
        # Release DB transaction before bounded external provider comparisons.
        with httpx.Client(timeout=12) as client:
            for check in checks:
                result=client.get('https://api.massive.com/v3/quotes/'+check['contract'],params={
                    'apiKey':os.environ['MASSIVE_API_KEY'],'timestamp.gt':str(int(check['after']*1e9)),
                    'timestamp.lte':str(int(check['end']*1e9)),'order':'asc','sort':'timestamp','limit':1000})
                provider=dict(http_status=result.status_code)
                if result.status_code==200:
                    data=result.json();raw=data.get('results',[])
                    stamps=[ts(q.get('sip_timestamp')) for q in raw]
                    provider.update(rows=len(raw),truncated=bool(data.get('next_url')),
                        first_source_ts=min(stamps) if stamps else None,last_source_ts=max(stamps) if stamps else None,
                        valid_two_sided=sum(fresh(dict(ts=ts(q.get('sip_timestamp')),bid=q.get('bid_price'),ask=q.get('ask_price'),
                            bid_size=q.get('bid_size'),ask_size=q.get('ask_size')),ts(q.get('sip_timestamp'))) for q in raw))
                check['historical_provider']=provider
                check['basis']='Retrospective provider presence does not prove availability to Compass at decision time'
                emit('option_gap',check)
            headers={'APCA-API-KEY-ID':os.environ['APCA_API_KEY_ID'], 'APCA-API-SECRET-KEY':os.environ['APCA_API_SECRET_KEY']}
            for offset in range(0,len(symbols),6):
                group=symbols[offset:offset+6]
                result=client.get('https://data.alpaca.markets/v2/stocks/bars',headers=headers,params={
                    'symbols':','.join(group),'timeframe':'1Min','feed':'sip','adjustment':'split','limit':10000,
                    'start':datetime.fromtimestamp(start,timezone.utc).isoformat(),
                    'end':datetime.fromtimestamp(end-1,timezone.utc).isoformat()})
                if result.status_code!=200:
                    emit('bar_provider_unavailable',dict(symbols=group,http_status=result.status_code));continue
                data=result.json()
                for symbol in group:
                    remote={ts(b['t']):b for b in data.get('bars',{}).get(symbol,[])}
                    saved=bar_archive[symbol];local=saved['latest']
                    slots=set(range(int(start),int(end),60))
                    changed=[t for t in remote.keys()&local.keys() if any(abs(float(remote[t][k])-float(local[t][k]))>1e-7 for k in ('o','h','l','c','v'))]
                    absent_remote=sorted(slots-remote.keys());absent_local=sorted(slots-local.keys())
                    emit('minute_candles',dict(symbol=symbol,session='2026-09-15',expected_minutes=len(slots),
                        provider_minutes=len(remote),archive_minutes=len(local),provider_truncated=bool(data.get('next_page_token')),
                        archive_truncated=saved['truncated'],missing_from_provider=absent_remote,missing_from_archive=absent_local,
                        provider_present_archive_absent=sorted(remote.keys()-local.keys()),revised_or_different_minutes=changed,
                        persisted_over_90s_after_minute_start=sum(v>t+90 for t,v in saved['first'].items()),
                        late_spy_receipts=[dict(minute_start=t,first_recorded=v,seconds_after_close=v-t-60)
                            for t,v in sorted(saved['first'].items()) if v>t+90] if symbol=='SPY' else [],
                        basis='Latest stored bars versus retrospective SIP bars; never modifies frozen decisions'))
    finally:db.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('SOURCE_GAP_AUDIT_FAILED '+type(error).__name__,flush=True)
        raise SystemExit(1) from None
