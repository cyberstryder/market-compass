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


def input_readiness(db,c,cfg,now):
    from .futures import futures_session,lead_contract,risk_day
    from .scanner import bars_from_window,features
    from .universe import data_symbols
    from .market import is_open,calendar,day,session
    from .secondary import daily_context
    import pandas as pd
    quotes=db.prefix(c,'quote:')
    windows=db.prefix(c,'bar_window:')
    def history(symbol):
        stamps=sorted({r[0] for r in windows.get('bar_window:'+symbol,[]) if r[0]+60<=now})
        last=stamps[-1] if stamps else None
        missing=[] if last is None else [t for t in range(int(last)-29*60,int(last)+1,60) if t not in stamps]
        return dict(bars=len(stamps),last_completed_at=last+60 if last is not None else None,
            last_30_complete=len(stamps)>=30 and not missing,missing_recent_minutes=len(missing),
            minute_current=last is not None and 0<=now-last-60<=90)
    stocks=[]
    for symbol in data_symbols(db,c,cfg,now):
        q=quotes.get('quote:'+symbol)
        stocks.append(dict(symbol=symbol,quote_current=fresh(q,now),quote_ts=(q or {}).get('ts'),**history(symbol)))
    stock_open=is_open(now)
    coverage=db.get(c,'secondary:data_status',{})
    daily=[{k:r.get(k) for k in ('symbol','daily_ready','daily_through','daily_reason','collection_enabled')}
           for r in coverage.get('rows',[]) if 'smoothers' in r.get('projects',[])]
    cal=calendar('XNYS')
    next_label=cal.date_to_session(pd.Timestamp(day(now))+pd.Timedelta(days=1),direction='next')
    next_open=cal.session_open(next_label).timestamp()
    for item in daily:
        future=daily_context(db,c,item['symbol'],next_open)
        received=future.get('received')
        hours=session(future['through']) if future.get('through') else None
        item.update(next_session_history_ready=bool(future.get('status')=='ready' and hours and
            received is not None and hours[1]<=received<=now),next_session_through=future.get('through'),
            latest_daily_received=received)
    dow=[]
    for root in ('YM','MYM'):
        raw=lead_contract(root,risk_day(now))['raw_symbol']
        matching=[k[6:] for k in quotes if k.startswith('quote:'+raw+'@')]
        symbol=max(matching,key=lambda s:quotes['quote:'+s].get('ts',0)) if matching else raw
        q=quotes.get('quote:'+symbol)
        rows=bars_from_window(windows.get('bar_window:'+symbol,[]))
        h=history(symbol)
        # Structural history is evaluated at its own last completed bar. It is
        # never presented as fresh input for a current decision.
        f=features(symbol,rows,h['last_completed_at']) if h['last_completed_at'] else {}
        recent=[]
        if q:
            recent=c.execute(select(events.c.ts,events.c.received,events.c.payload).where(
                events.c.kind=='quote',events.c.symbol==symbol,events.c.ts>=q['ts']-120,
                events.c.ts<=q['ts'],events.c.received<=now).order_by(events.c.ts.desc()).limit(1201)).all()
        usable=sorted(r.ts for r in recent[:1200] if quote_reason(r)=='usable')
        dow.append(dict(symbol=symbol,expected_contract=raw,market_open=futures_session(now,symbol)['is_open'],
            quote_ts=(q or {}).get('ts'),quote_current=fresh(q,now),**h,
            history_status=f.get('status','missing'),history_reason=f.get('reason'),
            htf15_completed_bars=f.get('htf15_completed_bars'),htf15_bias=f.get('htf15_bias'),
            usable_recent_quotes=len(usable),quote_window_seconds=120,quote_window_truncated=len(recent)>1200,
            quote_window_first=usable[0] if usable else None,quote_window_last=usable[-1] if usable else None,
            max_recorded_gap=max((b-a for a,b in zip(usable,usable[1:])),default=None)))
    report=db.get(c,'setup_study:report',{})
    groups=[{k:g.get(k) for k in ('symbol','strategy','side','selected','closed','open','unresolved','excluded')}
            for g in report.get('entry_variants',{}).get('groups',[]) if g.get('variant')=='repeated']
    return dict(at=now,stocks_market_open=stock_open,stock_count=len(stocks),
        stock_complete_recent_history=sum(r['last_30_complete'] for r in stocks),
        stock_current_quotes=sum(r['quote_current'] for r in stocks),
        stock_current_minutes=sum(r['minute_current'] for r in stocks),
        stock_history_gaps=[r for r in stocks if not r['last_30_complete']],
        stock_focus=[r for r in stocks if r['symbol'] in ('TGT','SBUX')],
        smoother_coverage_at=coverage.get('at'),smoother_daily_count=len(daily),
        next_stock_session=str(next_label.date()),
        smoother_next_session_ready=sum(r['next_session_history_ready'] for r in daily),
        smoother_daily_ready=sum(bool(r['daily_ready']) for r in daily),smoother_daily=daily,
        dow=dow,futures_report_at=report.get('at'),futures_report_truncated=report.get('truncated'),
        futures_groups=groups,futures_variant_activation=db.get(c,'futures-entry-variants-v1:activation'),
        note='Closed-session quote/minute age is expected; structural history does not establish live readiness. Overnight acceptance requires new session observations.')
