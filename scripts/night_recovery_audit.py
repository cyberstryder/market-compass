"""Read-only September 17 gap investigation and current overnight health."""
import json
import base64
import gzip
import hashlib
import os
import time
from sqlalchemy import create_engine, text

LOW=1789621200.0
END=1789707600.0
NOW=time.time()
QUERIES=[
('storage',"SELECT pg_database_size(current_database()) AS database_bytes,(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()) AS connections"),
('tables',"SELECT schemaname,relname,n_live_tup,pg_total_relation_size(relid) AS total_bytes FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC"),
('ownership',"SELECT key,value,updated FROM state WHERE key IN ('native:ownership:morning','native:ownership:smoothers','native:morning:routing','native-morning-v1:status')"),
('imports',"SELECT key,value,updated FROM state WHERE key LIKE 'morning_history:%' OR key='morning_native_reconciliation:status'"),
('health',"SELECT key,value,updated FROM state WHERE key IN ('health:option_stream','health:alpaca_stocks','health:databento','health:databento_futures','health:strategy_tracking','health:futures','health:option_recovery')"),
('pending_deliveries',"SELECT route,status,count(*) AS n FROM discord_jobs_v1 WHERE status<>'sent' GROUP BY 1,2"),
('native_receipts',"""SELECT s.signal_id,s.ticker,s.signal_at_ms,s.received_at_ms,s.initial_received_at_ms,
 p.source_id IS NOT NULL AS original_present,(p.payload->'initial_received_at_ms')::jsonb AS original_initial_received_at_ms,
 min(r.received) AS first_direct_receipt,count(r.id) AS direct_receipts,
 count(*) FILTER (WHERE (r.payload->>'entry')::boolean) AS direct_entry_receipts,
 min(f.observed_at_ms) AS first_frame_observed,min(f.received_at_ms) AS first_frame_received,
 count(*) FILTER (WHERE f.event_id IS NOT NULL) AS frame_receipts
 FROM compass_native_morning_signals_v1 s
 LEFT JOIN project_records_v1 p ON p.project='morning' AND p.source_id=s.signal_id
 LEFT JOIN native_morning_receipts_v1 r ON r.signal_id=s.signal_id AND r.origin='direct'
 LEFT JOIN compass_native_morning_frames_v4 f ON f.event_id=r.event_id
 WHERE s.signal_at_ms>=:ms AND s.signal_at_ms<:end_ms AND NOT s.is_test
 GROUP BY s.signal_id,p.source_id,(p.payload->'initial_received_at_ms')::jsonb ORDER BY s.signal_at_ms,s.ticker"""),
('frame_minutes',"SELECT floor(observed_at_ms/60000)*60000 AS observed,count(*) AS frames,min(received_at_ms) AS first_received,max(received_at_ms) AS last_received FROM compass_native_morning_frames_v4 WHERE observed_at_ms>=:ms AND observed_at_ms<:end_ms GROUP BY 1 ORDER BY 1"),
('field_differences',"""SELECT s.signal_id,s.ticker,s.signal_json::jsonb AS native,p.payload->'original' AS original
 FROM compass_native_morning_signals_v1 s JOIN project_records_v1 p ON p.project='morning' AND p.source_id=s.signal_id
 WHERE s.signal_at_ms>=:ms AND s.signal_at_ms<:end_ms AND NOT s.is_test
 AND s.signal_json::jsonb<> (p.payload->'original')::jsonb ORDER BY s.signal_id"""),
('gap_cohort',"SELECT id,underlying,created,updated,payload FROM option_ideas_v1 WHERE created>=:lo AND created<:end AND status='unresolved' ORDER BY updated"),
('overnight_trials',"SELECT status,count(*) AS n FROM setup_trials_v1 WHERE payload->>'asset'='future' AND started>=1789682400 GROUP BY 1"),
('futures_quotes',"SELECT symbol,count(*) AS n,min(ts) AS first,max(ts) AS last,max(received-ts) AS largest_source_to_write FROM events WHERE kind='quote' AND source='databento' AND ts>=:recent GROUP BY symbol ORDER BY symbol"),
]
BAR_CTE="""WITH eligible AS (SELECT signal_id FROM compass_native_morning_signals_v1 WHERE signal_at_ms>=:ms AND signal_at_ms<:end_ms AND NOT is_test),
 originals AS (SELECT h.parent AS signal_id,(b->'payload'->>'open_at_ms')::bigint AS minute,(b->'payload')::jsonb AS bar
 FROM morning_history_v1 h JOIN eligible e ON h.parent=e.signal_id CROSS JOIN LATERAL json_array_elements(h.payload->'payload'->'bars') b WHERE h.kind='stock_inventory'),
 natives AS (SELECT b.signal_id,b.open_at_ms AS minute,b.bar_json::jsonb AS bar FROM compass_native_morning_stock_bars_v2 b JOIN eligible e USING(signal_id)),
 pairs AS (SELECT coalesce(o.signal_id,n.signal_id) AS signal_id,o.minute AS original_minute,n.minute AS native_minute,o.bar AS original_bar,n.bar AS native_bar FROM originals o FULL JOIN natives n USING(signal_id,minute)) """
QUERIES += [
('missing_minutes',BAR_CTE+"SELECT original_minute AS minute,count(*) AS missing_pairs FROM pairs WHERE native_minute IS NULL GROUP BY 1 ORDER BY 1"),
('candle_conflicts',BAR_CTE+"SELECT * FROM pairs WHERE original_bar<>native_bar ORDER BY signal_id,original_minute"),
('candle_counts',BAR_CTE+"SELECT signal_id,count(original_minute) AS original_bars,count(native_minute) AS native_bars,count(*) FILTER(WHERE original_minute IS NOT NULL AND native_minute IS NULL) AS missing_native,count(*) FILTER(WHERE native_minute IS NOT NULL AND original_minute IS NULL) AS extra_native,count(*) FILTER(WHERE original_bar<>native_bar) AS changed,min(original_minute) FILTER(WHERE native_minute IS NULL) AS first_missing,max(original_minute) FILTER(WHERE native_minute IS NULL) AS last_missing FROM pairs GROUP BY 1 ORDER BY 1"),
]


def main():
    url=os.environ['DATABASE_URL'].replace('postgres://','postgresql+psycopg://',1).replace('postgresql://','postgresql+psycopg://',1)
    engine=create_engine(url,connect_args={'connect_timeout':10},pool_size=1,max_overflow=0)
    retained=[]
    def emit(kind,value):
        row=dict(kind=kind,at=NOW,value=value)
        retained.append(row)
        if kind.endswith('_summary') or kind in ('query_error','done'):
            print('NIGHT_AUDIT '+json.dumps(row,default=str,separators=(',',':')),flush=True)
    failures=[];cohort=[]
    params=dict(lo=LOW,ms=int(LOW*1000),end=END,end_ms=int(END*1000),recent=NOW-300)
    with engine.connect() as c:
        c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout='10s'"))
        c.execute(text("SET LOCAL lock_timeout='1s'"))
        for kind,query in QUERIES:
            try:
                with c.begin_nested():
                    found=[dict(r) for r in c.execute(text(query),params).mappings()]
                    if kind=='gap_cohort':cohort=found
                    for row in found:
                        if kind=='health':row['value'].pop('symbols',None)
                        if kind=='field_differences':
                            a,b=row.pop('native'),row.pop('original')
                            row['differences']={k:{'native':a.get(k),'original':b.get(k)} for k in a.keys()|b.keys() if a.get(k)!=b.get(k)}
                        if kind=='gap_cohort':
                            p=row.pop('payload')
                            row.update({k:p.get(k) for k in ('contract','gap_detail','archive_check','observation_model','collection_version','seen_option_quote','seen_underlying_quote','last_quote','last_underlying_quote')})
                        emit(kind,row)
                    emit(kind+'_summary',{'rows':len(found)})
            except Exception as exc:
                failures.append(kind);emit('query_error',{'kind':kind,'error':type(exc).__name__})
        # Inspect retained quotes, not provider history, with explicit time/count bounds.
        for row in cohort:
            gap=row.get('gap_detail') or {}
            checked=gap.get('checked_at')
            if not checked:continue
            for side,symbol in [('option',(row.get('contract') or {}).get('symbol')),('underlying',row['underlying'])]:
                last=gap.get(side+'_last')
                if last is None or not symbol:continue
                lower=max(last-0.001,checked-180)
                try:
                    with c.begin_nested():
                        values=[dict(x) for x in c.execute(text("SELECT ts,received,source,payload FROM events WHERE kind='quote' AND symbol=:symbol AND ts>=:lower AND ts<=:checked+1 ORDER BY ts,id LIMIT 2001"),dict(symbol=symbol,lower=lower,checked=checked)).mappings()]
                    truncated=len(values)>2000
                    values=values[:2000]
                    def valid(x):
                        p=x['payload'];bid=p.get('bid');ask=p.get('ask')
                        return (isinstance(bid,(int,float)) and isinstance(ask,(int,float))
                            and 0<bid<ask and (p.get('bid_size') or 0)>0
                            and (p.get('ask_size') or 0)>0 and p.get('ts')==x['ts'])
                    def usable(x):
                        return valid(x) and 0<=x['received']-x['ts']<=5
                    newer=[x for x in values if last<x['ts']<=checked]
                    by_gate=[x for x in newer if x['received']<=checked]
                    late=[x for x in newer if x['received']>checked]
                    def compact(x):
                        p=x['payload'];return dict(ts=x['ts'],write=x['received'],source=x['source'],
                            bid=p.get('bid'),ask=p.get('ask'),bid_size=p.get('bid_size'),ask_size=p.get('ask_size'),socket=p.get('socket_read_at'),
                            version=p.get('collection_version'),usable_when_written=usable(x))
                    emit('gap_quotes',dict(id=row['id'],side=side,symbol=symbol,last=last,checked=checked,
                        lower=lower,truncated=truncated,time_window_clipped=lower>last,rows=len(values),newer_rows=len(newer),
                        newer_by_gate=len(by_gate),usable_newer_by_gate=sum(usable(x) for x in by_gate),
                        invalid_newer_by_gate=sum(not valid(x) for x in by_gate),
                        stale_newer_by_gate=sum(valid(x) and not usable(x) for x in by_gate),
                        later_written_newer=len(late),
                        max_source_to_write=max((x['received']-x['ts'] for x in values),default=None),
                        max_source_to_socket=max((x['payload']['socket_read_at']-x['ts'] for x in values if x['payload'].get('socket_read_at')),default=None),
                        max_socket_to_write=max((x['received']-x['payload']['socket_read_at'] for x in values if x['payload'].get('socket_read_at')),default=None),
                        first_newer=compact(newer[0]) if newer else None,last_quote=compact(values[-1]) if values else None,
                        late_examples=[compact(x) for x in late[:3]]))
                except Exception as exc:
                    failures.append('gap:'+row['id']+':'+side)
                    emit('query_error',dict(kind='gap_quotes',id=row['id'],side=side,error=type(exc).__name__))
    emit('done',{'failed':failures,'complete':not failures})
    engine.dispose()
    # Bounded checksummed chunks avoid losing evidence to high-rate log ingestion.
    encoded=json.dumps(retained,default=str,separators=(',',':')).encode()
    packed=base64.b64encode(gzip.compress(encoded)).decode()
    chunks=[packed[i:i+4000] for i in range(0,len(packed),4000)]
    for i,chunk in enumerate(chunks):
        print('NIGHT_PACK '+json.dumps(dict(index=i,total=len(chunks),data=chunk)),flush=True)
        time.sleep(0.1)
    print('NIGHT_PACK_END '+json.dumps(dict(rows=len(retained),chunks=len(chunks),sha256=hashlib.sha256(encoded).hexdigest())),flush=True)


if __name__=='__main__':main()
