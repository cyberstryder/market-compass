"""Read-only explanations of stored gaps and vendor clocks; no repaired outcomes."""
from collections import Counter
from sqlalchemy import select
from .market import fresh
from .setup_study import trials
from .store import events
from .vendor_freshness import confirmation

VERSION = 'feed-forensics-v1'


def quote_reason(row):
    q = row.payload
    if q.get('ts') != row.ts: return 'timestamp_mismatch'
    if row.ts > row.received: return 'future_at_receipt'
    if not fresh(q,row.received): return 'stale_or_invalid_at_receipt'
    return 'usable'


def futures_gaps(db,c,now):
    activation=db.get(c,'futures-entry-variants-v1:activation',{}).get('at',now-86400)
    rows=c.execute(select(trials.c.payload).where(trials.c.status=='unresolved',
        trials.c.started>=max(activation,now-86400)).order_by(trials.c.finished.desc()).limit(21)).scalars().all()
    result=[]
    for p in rows[:20]:
        if p.get('asset')!='future': continue
        after=p.get('gap_detail',{}).get('last_quote_ts',p.get('last_quote_ts'))
        until=p.get('finished',now)
        qrows=c.execute(select(events.c.ts,events.c.received,events.c.payload).where(
            events.c.kind=='quote',events.c.symbol==p['symbol'],events.c.ts>after,
            events.c.ts<=until+30).order_by(events.c.ts,events.c.id).limit(201)).all()
        before=[r for r in qrows[:200] if r.received<=until]
        next_row=qrows[0] if qrows else None
        result.append(dict(id=p['id'],symbol=p['symbol'],strategy=p['strategy'],
            started=p['started'],finished=until,last_usable_source_ts=after,
            gap_detail=p.get('gap_detail'),archive_check=p.get('archive_check'),
            exit_reason=p.get('exit_reason'),replayed_samples=p.get('replayed_samples'),
            rows_near_gap=len(qrows[:200]),quote_scan_truncated=len(qrows)>200,
            available_by_finish=dict(Counter(quote_reason(r) for r in before)),
            stored_after_finish=sum(r.received>until for r in qrows[:200]),
            first_next_quote=None if next_row is None else dict(source_ts=next_row.ts,
                received=next_row.received,reason=quote_reason(next_row)),
            observation='Stored events cannot distinguish vendor silence from a disconnected collector when no quotes were recorded.'))
    return dict(rows=result,truncated=len(rows)>20)


def clock_fields(value, prefix='', depth=0):
    """Only timing/cache keys, never raw market payloads or credential fields."""
    if depth>2 or not isinstance(value,dict): return {}
    out={}
    for key,v in list(value.items())[:100]:
        path=prefix+str(key)
        if key in ('freshness','data') and isinstance(v,dict):
            out.update(clock_fields(v,path+'.',depth+1))
        elif str(key).lower() in ('snapshottime','tradetime','computedat','lastupdated','updatedat',
                'timestamp','asof','generatedat','cached','stale','refreshseconds','ageseconds',
                'delayseconds','delayed','isdelayed','mode','marketopen','isrealtime'):
            if v is None or isinstance(v,(bool,int,float)) or isinstance(v,str) and len(v)<100:
                out[path]=v
    return out


def vendor_clocks(db,c,now):
    result=[]
    for label in ('SPY','QQQ','IWM','unusual_activity','gamma_market','thermal','vix','signals','market_pulse'):
        key='matrix:'+label if label in ('SPY','QQQ','IWM','unusual_activity') else 'research:'+label
        item=db.get(c,key,{})
        raw=c.execute(select(events.c.payload,events.c.received).where(events.c.kind=='matrix_raw',
            events.c.symbol==label).order_by(events.c.ts.desc()).limit(1)).first()
        result.append(dict(feed=label,check=confirmation(item,now),
            progress=item.get('source_progress'),
            raw_received=raw.received if raw else None,
            fields=clock_fields(raw.payload.get('data',{})) if raw else {},
            item_clock_count=sum(i.get('source_ts') is not None for i in item.get('items',[])),
            item_count=len(item.get('items',[]))))
    return result
