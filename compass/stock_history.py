"""Resumable, fair stock backfill; one provider page per worker turn."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from .diagnostics import database_error
from .market import ts

PREFIX='stock_history_job:'
REFRESH_SECONDS=3600
REFRESH_GRACE_SECONDS=900


def health_summary(rows, now):
    """A routine refresh does not erase a previous completed provider scan."""
    initialized=sum(bool(r.get('completed_at')) for r in rows)
    current=sum(r.get('status')=='complete' and now-r.get('completed_at',0)<REFRESH_SECONDS for r in rows)
    overdue=sum(bool(r.get('completed_at')) and now-r['completed_at']>REFRESH_SECONDS+REFRESH_GRACE_SECONDS for r in rows)
    errors=[{'symbols':r.get('symbols',[]),'frame':r.get('frame'),'reason':r['error']}
            for r in rows if r.get('error')]
    status=('error' if errors else 'stale' if overdue else 'partial' if initialized<len(rows)
            else 'refreshing' if current<len(rows) else 'available')
    return status,dict(completed_batches=initialized,current_batches=current,total_batches=len(rows),
        refreshing_batches=sum(bool(r.get('completed_at')) and
            (r.get('status')!='complete' or now-r['completed_at']>=REFRESH_SECONDS) for r in rows),
        pending_batches=len(rows)-initialized,overdue_batches=overdue,failed_batches=errors)


def jobs(universe, saved):
    result=[]
    for offset in range(0,len(universe),8):
        batch=list(universe[offset:offset+8])
        for frame,days,kind in [('1Min',7,'bar'),('1Day',120,'daily')]:
            key=PREFIX+frame+':'+','.join(batch)
            groups=[[s] for s in batch] if saved.get(key,{}).get('split') else [batch]
            for group in groups:
                result.append((PREFIX+frame+':'+','.join(group),group,frame,days,kind))
    return result


async def collect(collector):
    from .providers import FeedError
    db=collector.db
    now=time.time()
    universe=await asyncio.to_thread(collector.stock_symbols)
    def read():
        with db.tx() as c:return db.prefix(c,PREFIX)
    saved=await asyncio.to_thread(read)
    work=jobs(universe,saved)
    due=[j for j in work if now>=saved.get(j[0],{}).get('retry_at',0)
         and (saved.get(j[0],{}).get('status')!='complete'
              or now-saved[j[0]].get('completed_at',0)>=REFRESH_SECONDS)]
    due.sort(key=lambda j:saved.get(j[0],{}).get('attempted_at',0))
    if due:
        key,batch,frame,days,kind=due[0]
        previous=saved.get(key,{})
        params=previous.get('params') if previous.get('status')!='complete' else None
        params=dict(params or dict(symbols=','.join(batch),timeframe=frame,
            start=(datetime.fromtimestamp(now,timezone.utc)-timedelta(days=days)).isoformat(),
            end=datetime.fromtimestamp(now,timezone.utc).isoformat(),limit=10000,
            feed=collector.cfg.feed,adjustment='split',sort='asc'))
        job={**previous,'attempted_at':now,'symbols':batch,'frame':frame,'params':params}
        try:
            data=await collector.get('https://data.alpaca.markets/v2/stocks/bars',collector.alpaca_headers,params)
            items=[(symbol,ts(b['t']),{k:b[k] for k in ['o','h','l','c','v','vw'] if k in b})
                   for symbol,bars in data.get('bars',{}).items() for b in bars]
            await asyncio.to_thread(collector.bars,'alpaca',items,kind)
            # Advance only after every chunk commits. Replaying a partial page
            # after failure/restart is idempotent; no received bars are skipped.
            next_page=data.get('next_page_token')
            if next_page and next_page==params.get('page_token'):
                raise FeedError('History pagination repeated its cursor')
            job.update(status='collecting' if next_page else 'complete',error=None,retry_at=0,
                       pages=(previous.get('pages',0) if previous.get('status')!='complete' else 0)+1)
            observed=[t for _,t,_ in items if t is not None]
            if observed:job['source_ts']=max(previous.get('source_ts',0),max(observed))
            if next_page:job['params']={**params,'page_token':next_page}
            else:job.update(params=None,completed_at=time.time())
        except asyncio.CancelledError:raise
        except Exception as error:
            job.update(status='error',error=str(error) if isinstance(error,FeedError) else database_error(error),retry_at=now+120)
            if isinstance(error,FeedError) and error.status_code in (400,422):
                if params.get('page_token'):
                    job['params']=None  # Expired/invalid cursor: safely replay this batch.
                elif len(batch)>1:
                    job.update(split=True,retry_at=0)
        def save():
            with db.tx() as c:db.put(c,key,job)
        await asyncio.to_thread(save)
        saved[key]=job
        work=jobs(universe,saved)
    rows=[saved.get(j[0],{}) for j in work]
    status,summary=health_summary(rows,now)
    source=max((r.get('source_ts',0) for r in rows),default=0) or None
    await asyncio.to_thread(db.health,'alpaca_history',status,
        f'History scans: {summary["completed_batches"]}/{len(work)} batches initialized; '
        f'{summary["current_batches"]} current; {summary["refreshing_batches"]} refreshing; '
        f'{summary["pending_batches"]} initial backfills; {summary["overdue_batches"]} overdue; '
        f'{len(summary["failed_batches"])} retrying. Provider scans do not establish a continuous price path.',
        source,monotonic_source=True,**summary,
        committed_pages=sum(r.get('pages',0) for r in rows),
        watch_symbols=len(universe),poll_ts=time.time())
