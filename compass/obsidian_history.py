"""Bounded retrospective OHLC audit, isolated from live quotes and reviews."""
import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .obsidian import ideas, feed_events

KEY='obsidian:history:20260914:v1'
CUTOFF=datetime(2026,9,15,tzinfo=timezone.utc).timestamp()
LOG=logging.getLogger('uvicorn.error')


def health_status(cfg, state, cursor):
    total=len(state.get('queue',[]));checked=len(state.get('results',{}))
    if state.get('complete'):
        if state.get('blocked'):
            return 'error',f'Audit finished {checked}/{total}; provider access failed: {state["blocked"]}'
        return 'complete',f'Audit finished: {checked}/{total} ideas checked; per-idea coverage remains in Obsidian Watchlist'
    if not cfg.obsidian_history:
        return 'disabled','Retrospective audit disabled; live Obsidian collection is separate'
    missing=[name for name in ('obsidian_url','massive') if not getattr(cfg,name,'')]
    if missing:
        return 'not_configured','Retrospective audit requires configuration: '+', '.join(missing)
    if state.get('blocked'):
        return 'error','Retrospective provider access failed: '+state['blocked']
    if cursor<116:
        return 'waiting','Waiting for the retained Obsidian feed to reach the audit boundary'
    return 'running',f'Retrospective audit: {checked}/{total} ideas checked'


async def collect(collector):
    """Report every lifecycle state, including an already completed audit."""
    def read():
        feed=hashlib.sha256(collector.cfg.obsidian_url.encode()).hexdigest()
        with collector.db.tx() as c:
            state=collector.db.get(c,KEY,{})
            cursor=collector.db.get(c,'obsidian:cursor:'+feed,0)
        return health_status(collector.cfg,state,cursor)
    status,detail=await asyncio.to_thread(read)
    if status=='running':
        await step(collector)
        status,detail=await asyncio.to_thread(read)
    await asyncio.to_thread(collector.db.health,'obsidian_history',status,detail)


def bounds(row):
    start=math.ceil(row['source_ts']/60)*60
    expiry=datetime.fromisoformat(row['expiry']).replace(tzinfo=ZoneInfo('America/New_York'))+timedelta(days=1)
    return start,min(expiry.timestamp(),CUTOFF)


def analyze(row, body):
    start,end=bounds(row)
    result=dict(status='unavailable',bars=0,basis='Unadjusted trade OHLC; first full minute open after idea; not bid/ask fills',
        window_start=start,window_end=end,unexpired=row['expiry']>='2026-09-15')
    if body.get('ticker')!=row['contract'] or body.get('status') not in ('OK','DELAYED'):
        return {**result,'status':'invalid_response'}
    raw=body.get('results',[])
    if not isinstance(raw,list):return {**result,'status':'invalid_response'}
    bars=[]
    for b in raw:
        if not isinstance(b,dict) or any(type(b.get(k)) not in (int,float) or not math.isfinite(b[k]) for k in ('t','o','h','l','c','v')):
            return {**result,'status':'invalid_bars'}
        if not 0<b['l']<=min(b['o'],b['c'])<=max(b['o'],b['c'])<=b['h'] or b['v']<=0:
            return {**result,'status':'invalid_bars'}
        if start<=b['t']/1000 and b['t']/1000+60<=end:bars.append(b)
    bars.sort(key=lambda b:b['t'])
    if len({b['t'] for b in bars})!=len(bars):return {**result,'status':'duplicate_bars'}
    result['truncated']=bool(body.get('next_url')) or body.get('queryCount',0)>=50000
    if not bars:return {**result,'status':'no_bars'}
    first=bars[0];anchor=first['o'];anchor_ts=first['t']/1000
    pct=lambda p:round((p/anchor-1)*100,4)
    result.update(status='partial' if result['truncated'] else 'observed',bars=len(bars),anchor=anchor,
        anchor_ts=anchor_ts,anchor_delay_seconds=anchor_ts-row['source_ts'],
        highest_pct=pct(max(b['h'] for b in bars)),lowest_pct=pct(min(b['l'] for b in bars)),
        last_close_pct=pct(bars[-1]['c']),last_bar_ts=bars[-1]['t']/1000,
        missing_minutes=sum(max(0,int((b['t']-a['t'])/60000)-1) for a,b in zip(bars,bars[1:])),
        checkpoints={},scenario_25='unresolved')
    # Actual original-time windows: no carry-forward through missing minutes.
    for minute in (15,30,60):
        target=math.floor((row['source_ts']+minute*60)/60)*60-60
        b=next((b for b in bars if b['t']/1000==target),None)
        result['checkpoints'][str(minute)]=pct(b['c']) if b else None
    # Descriptive sensitivity only. Both thresholds in one bar have unknown order.
    for b in bars:
        up=b['h']>=anchor*1.25;down=b['l']<=anchor*.75
        if up or down:
            result['scenario_25']='same_bar_ambiguous' if up and down else 'target_first' if up else 'stop_first'
            result['scenario_bar_ts']=b['t']/1000
            break
    return result


async def step(collector):
    db=collector.db
    feed=hashlib.sha256(collector.cfg.obsidian_url.encode()).hexdigest()
    with db.tx() as c:
        if not db.lease(c,KEY,collector.history_owner,60):return
        state=db.get(c,KEY,{})
        if state.get('complete'):return
        if db.get(c,'obsidian:cursor:'+feed,0)<116:return
        if not state:
            rows=c.execute(select(ideas).where(ideas.c.feed==feed,ideas.c.vendor_id<=116,
                ideas.c.source_ts<CUTOFF).order_by(ideas.c.vendor_id)).mappings().all()
            linked=set(c.execute(select(feed_events.c.idea_id).where(feed_events.c.feed==feed,
                feed_events.c.association=='matched')).scalars())
            rows=sorted(rows,key=lambda r:r['id'] in linked)
            state=dict(started_at=time.time(),cutoff=CUTOFF,queue=[dict(id=r['id'],vendor_id=r['vendor_id'],
                contract=r['contract'],source_ts=r['source_ts'],expiry=r['expiry'],has_update=r['id'] in linked) for r in rows],
                results={},attempts={},complete=False)
            db.put(c,KEY,state)
        row=next((r for r in state['queue'] if r['id'] not in state['results']),None)
        if row is None:
            state['complete']=True;state['finished_at']=time.time();db.put(c,KEY,state)
            statuses={}
            for r in state['results'].values():statuses[r['status']]=statuses.get(r['status'],0)+1
            LOG.info('Obsidian history complete: total=%s statuses=%s',len(state['results']),json.dumps(statuses))
            return
        attempts=state['attempts'].get(row['id'],0)+1
        state['attempts'][row['id']]=attempts
        db.put(c,KEY,state)
    start,end=bounds(row)
    body=None
    if attempts>3:result={'status':'retry_exhausted'}
    elif state.get('blocked'):result={'status':state['blocked']}
    else:
        try:
            async with collector.client.stream('GET',
                f"https://api.massive.com/v2/aggs/ticker/{row['contract']}/range/1/minute/{int(start*1000)}/{int(end*1000)-1}",
                params={'adjusted':'false','sort':'asc','limit':50000},
                headers={'Authorization':'Bearer '+collector.cfg.massive},timeout=20,follow_redirects=False) as response:
                code=response.status_code
                if code in (401,403):
                    result={'status':'access_denied','http_status':code};state['blocked']='access_denied'
                elif code in (429,500,502,503,504) and attempts<3:return
                elif code!=200:result={'status':'http_error','http_status':code}
                else:
                    data=bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data)>12000000:raise ValueError('Response exceeds audit size bound')
                    body=json.loads(data)
                    result=analyze(row,body)
        except asyncio.CancelledError:raise
        except Exception as error:
            if attempts<3:return
            result={'status':'request_failed','error_type':type(error).__name__}
    with db.tx() as c:
        if body is not None:
            # Exact OHLC evidence remains separate from quote archives and live state.
            db.put(c,KEY+':bars:'+row['id'],{k:body.get(k) for k in ('ticker','status','queryCount','resultsCount','results')})
        result.update(contract=row['contract'],vendor_id=row['vendor_id'],has_update=row['has_update'],checked_at=time.time())
        state['results'][row['id']]=result
        db.put(c,KEY,state)
    LOG.info('Obsidian history result: %s',json.dumps(result,sort_keys=True))


def snapshot(db,c):
    state=db.get(c,KEY,{})
    return {k:state.get(k) for k in ('started_at','cutoff','complete','finished_at')}|{
        'total':len(state.get('queue',[])),'results':list(state.get('results',{}).values())}
