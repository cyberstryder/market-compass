"""Private watchlist intake and independent observations; no trade execution."""
import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime,date
from decimal import Decimal,InvalidOperation
from urllib.parse import urlsplit
from sqlalchemy import Table,Column,String,Integer,Float,JSON,select,func,update
from .store import meta,identity
from .market import fresh,day
from .quote_path import recorded_path

feed_events=Table('watchlist_events_v1',meta,Column('id',String(64),primary_key=True),
    Column('feed',String(64),index=True),Column('vendor_id',Integer),Column('received',Float),
    Column('payload',JSON),Column('association',String(40)),Column('idea_id',String(64)))
ideas=Table('watchlist_ideas_v1',meta,Column('id',String(64),primary_key=True),
    Column('feed',String(64)),Column('vendor_id',Integer),Column('symbol',String(20)),
    Column('contract',String(40)),Column('expiry',String(10),index=True),
    Column('source_ts',Float),Column('received',Float),Column('data',JSON),Column('measurements',JSON))
LOG=logging.getLogger('uvicorn.error')


def validate_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!='www.accessobsidian.com' or not re.fullmatch(r'/api/feed/[A-Za-z0-9_-]{16,200}',p.path) or p.query or p.fragment:
        raise ValueError('Obsidian requires the private HTTPS feed link')
    return url


def normalize(row,now):
    if not isinstance(row,dict) or type(row.get('id')) is not int or row['id']<=0:
        raise ValueError('Invalid event identity')
    if row.get('type') not in ('new','update'): raise ValueError('Unknown watchlist event type')
    symbol=row.get('ticker','')
    if not isinstance(symbol,str) or not re.fullmatch('[A-Z]{1,6}',symbol): raise ValueError('Unsupported option root')
    if row.get('pc') not in ('Call','Put'): raise ValueError('Unknown option type')
    expiry=date.fromisoformat(row['expiration']).isoformat()
    stamp=datetime.fromisoformat(row['time'].replace('Z','+00:00'))
    if stamp.tzinfo is None or not 0<stamp.timestamp()<=now: raise ValueError('Invalid source clock')
    strike=Decimal(str(row['strike']))*1000
    if not strike.is_finite() or strike!=int(strike) or not 0<strike<100000000: raise ValueError('Invalid strike')
    pct=row.get('percentage')
    if pct is not None and (isinstance(pct,bool) or not isinstance(pct,(int,float)) or not __import__('math').isfinite(pct)):
        raise ValueError('Invalid reported percentage')
    occ=symbol+datetime.strptime(expiry,'%Y-%m-%d').strftime('%y%m%d')+row['pc'][0]+f'{int(strike):08d}'
    return dict(id=row['id'],type=row['type'],symbol=symbol,contract='O:'+occ,
        expiry=expiry,strike=float(strike/1000),pc=row['pc'],source_ts=stamp.timestamp(),vendor_percentage=pct)


def ingest_page(db,feed,body,now):
    if not isinstance(body,dict) or not isinstance(body.get('events'),list) or len(body['events'])>1000 or type(body.get('cursor')) is not int:
        raise ValueError('Invalid feed page')
    rows=body['events']
    if any(not isinstance(r,dict) or type(r.get('id')) is not int or r['id']<=0 for r in rows):
        raise ValueError('Invalid event identity; cursor retained')
    with db.tx() as c:
        previous=db.get(c,'obsidian:cursor:'+feed,0)
        if body['cursor']<previous or body['cursor']<max((r['id'] for r in rows),default=previous):
            raise ValueError('Invalid replay cursor')
        counts={'new':0,'updates':0,'ambiguous':0,'quarantined':0}
        for raw in sorted(rows,key=lambda r:r['id']):
            key=identity(feed,raw['id'])
            if c.execute(select(feed_events.c.id).where(feed_events.c.id==key)).first(): continue
            try: item=normalize(raw,now)
            except (ValueError,KeyError,TypeError,InvalidOperation,OverflowError):
                item={'id':raw['id'],'type':'invalid','reason':'Unsupported or malformed event; retained as quarantine',
                    'reported_fields':{k:str(raw.get(k))[:200] for k in ('type','ticker','strike','pc','expiration','percentage','time')}}
            association='new' if item['type']=='new' else 'quarantined' if item['type']=='invalid' else 'unmatched'
            linked=None
            if item['type']=='new':
                linked=key
                c.execute(db.insert(ideas).values(id=key,feed=feed,vendor_id=item['id'],symbol=item['symbol'],
                    contract=item['contract'],expiry=item['expiry'],source_ts=item['source_ts'],received=now,
                    data=item,measurements={'anchor_status':'waiting' if now-item['source_ts']<=60 else 'late_import',
                    'basis':'First fresh ask after live receipt to subsequent bid; may wait for market open, not a provider fill',
                    'path_complete':True,'samples':0}))
                counts['new']+=1
            elif item['type']=='update':
                matches=c.execute(select(ideas.c.id).where(ideas.c.feed==feed,ideas.c.contract==item['contract'],
                    ideas.c.source_ts<=item['source_ts'],ideas.c.vendor_id<item['id']).limit(2)).scalars().all()
                if len(matches)==1: linked=matches[0];association='matched'
                elif len(matches)>1: association='ambiguous';counts['ambiguous']+=1
                counts['updates']+=1
            else: counts['quarantined']+=1
            c.execute(db.insert(feed_events).values(id=key,feed=feed,vendor_id=raw['id'],received=now,
                payload=item,association=association,idea_id=linked))
        db.put(c,'obsidian:cursor:'+feed,body['cursor'])
        db.put(c,'obsidian:status',dict(at=now,cursor=body['cursor'],page_events=len(rows),**counts,
            status='receiving',basis='Cursor commits with retained events; upstream replay completeness is not guaranteed'))
    LOG.info('Obsidian feed: cursor=%s events=%s new=%s updates=%s ambiguous=%s quarantined=%s',body['cursor'],len(rows),counts['new'],counts['updates'],counts['ambiguous'],counts['quarantined'])
    return counts


async def poll(collector):
    url=validate_url(collector.cfg.obsidian_url)
    feed=hashlib.sha256(url.encode()).hexdigest()
    with collector.db.tx() as c: cursor=collector.db.get(c,'obsidian:cursor:'+feed,0)
    try:
        async with collector.client.stream('GET',url+'/recent',params={'since':cursor},timeout=15,follow_redirects=False) as response:
            if response.status_code!=200: raise RuntimeError('Feed returned HTTP '+str(response.status_code))
            data=bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data)>2000000: raise ValueError('Feed page exceeds size limit')
        counts=await asyncio.to_thread(ingest_page,collector.db,feed,json.loads(data),time.time())
        collector.db.health('obsidian','receiving','Watchlist events retained; independent prices required',poll_ts=time.time(),**counts)
    except asyncio.CancelledError: raise
    except Exception as error:
        # HTTP exception strings include the private URL; never forward them.
        raise RuntimeError('Obsidian poll failed: '+type(error).__name__) from None


def active(c,now):
    return c.execute(select(ideas).where(ideas.c.expiry>=day(now)).order_by(ideas.c.source_ts.desc()).limit(501)).mappings().all()


def contracts(c,now): return [r['contract'] for r in active(c,now)[:500]]
def symbols(c,now):
    # These index roots are option contracts, not subscribable equity tickers.
    indices={'SPX','SPXW','XSP','NDX','NDXP','RUT','RUTW','VIX','DJX','OEX'}
    return list(dict.fromkeys(r['symbol'] for r in active(c,now)[:500] if r['symbol'] not in indices))


def tick(db,cfg,now):
    from .secondary import Secondary
    with db.tx() as c:
        rows=active(c,now)
        for row in rows[:500]:
            m=dict(row['measurements']);q=db.get(c,'quote:'+row['contract'])
            if now-row['source_ts']<=65:
                item=row['data']
                candidate=dict(project='obsidian',source_key=row['id'],source_id=str(row['vendor_id']),
                    symbol=row['symbol'],side='long' if item['pc']=='Call' else 'short',source_ts=row['source_ts'],
                    strategy='Obsidian watchlist direction context',source_version='watchlist-v1',
                    option_contract=row['contract'],entry=None,stop=None,target=None)
                Secondary(db,cfg).review(c,candidate,{},now)
            if m['anchor_status']=='waiting':
                if fresh(q,now) and row['received']<=q['ts']<=now:
                    m.update(anchor_status='observed',anchor_ask=q['ask'],anchor_ts=q['ts'],last_quote_ts=q['ts'],
                        latest_bid=q['bid'],last_observed_at=now,samples=1,anchor_delay_seconds=q['ts']-row['source_ts'],
                        liquidation_pct=(q['bid']/q['ask']-1)*100,max_bid=q['bid'],min_bid=q['bid'])
            if m['anchor_status']=='observed':
                path,truncated,check=recorded_path(db,c,row['contract'],m['last_quote_ts'],now)
                for quote in path:
                    if quote['ts']-m['last_quote_ts']>15: m['path_complete']=False
                    m.update(last_quote_ts=quote['ts'],latest_bid=quote['bid'],last_observed_at=quote['recorded_at'],
                        samples=m['samples']+1,max_bid=max(m['max_bid'],quote['bid']),min_bid=min(m['min_bid'],quote['bid']),
                        liquidation_pct=(quote['bid']/m['anchor_ask']-1)*100)
                if not truncated and now-m['last_quote_ts']>15: m['path_complete']=False
                m['replay_pending']=truncated
            if m!=row['measurements']: c.execute(update(ideas).where(ideas.c.id==row['id']).values(measurements=m))
        db.put(c,'obsidian:tracking',dict(at=now,active=min(len(rows),500),truncated=len(rows)>500))


async def run(db,cfg):
    while True:
        try: await asyncio.to_thread(tick,db,cfg,time.time())
        except asyncio.CancelledError: raise
        except Exception as error: LOG.warning('Obsidian tracking failed: %s',type(error).__name__)
        await asyncio.sleep(5)


def snapshot(db,c,now):
    rows=c.execute(select(ideas).order_by(ideas.c.source_ts.desc()).limit(100)).mappings().all()
    recent=c.execute(select(feed_events.c.vendor_id,feed_events.c.payload,feed_events.c.association,feed_events.c.idea_id)
        .order_by(feed_events.c.received.desc(),feed_events.c.vendor_id.desc()).limit(100)).mappings().all()
    status=db.get(c,'obsidian:status',{})
    health=db.get(c,'health:obsidian',{})
    if health.get('status') in ('error','not_configured'): status={**status,'status':health['status']}
    return dict(status=status,tracking=db.get(c,'obsidian:tracking',{}),
        total_events=c.execute(select(func.count()).select_from(feed_events)).scalar_one(),
        total_ideas=c.execute(select(func.count()).select_from(ideas)).scalar_one(),
        ideas=[{k:r[k] for k in ('id','symbol','contract','expiry','source_ts','received','data','measurements')} for r in rows],
        events=[dict(r) for r in recent])
