"""Bounded, read-only September 17 evidence. No providers, repairs or alerts.

Run only with DATABASE_URL for the database to inspect. Each query has an
eight-second deadline; failed sections stay explicit. Futures remain partial
until the recorded 16:00 CT session boundary. This is an audit, not a scheduler.
"""
import json
import os
import time
from sqlalchemy import create_engine, text

NOW = time.time()
LOW = 1789621200.0  # September 17 midnight CT
OPEN = LOW + 8.5 * 3600
CLOSE = LOW + 15 * 3600
FUTURE_OPEN = LOW - 7 * 3600
FUTURE_CLOSE = LOW + 16 * 3600

QUERIES = [
    ('settings', "SELECT name,setting,pending_restart FROM pg_settings WHERE name IN ('max_wal_size','fsync','synchronous_commit','full_page_writes')"),
    ('receipts', "SELECT route,status,count(*) AS n,count(*) FILTER (WHERE status='sent' AND coalesce(confirmation->>'message_id','')='') AS missing_id,min(queued_at) AS first,max(queued_at) AS last FROM discord_jobs_v1 WHERE queued_at>=:lo AND queued_at<:end GROUP BY route,status ORDER BY route,status"),
    ('pending_deliveries', "SELECT route,status,count(*) AS n,min(queued_at) AS first FROM discord_jobs_v1 WHERE status='pending' GROUP BY route,status"),
    ('unassigned', "SELECT count(*) AS n FROM events WHERE kind='alert' AND id>coalesce((SELECT CAST(CAST(value AS TEXT) AS BIGINT) FROM state WHERE key='outbox:discord:ingested'),0)"),
    ('morning_native', 'SELECT count(*) AS signals,count(initial_received_at_ms) AS initial_receipts FROM compass_native_morning_signals_v1 WHERE signal_at_ms>=:ms AND signal_at_ms<:end_ms AND NOT is_test'),
    ('morning_original', "SELECT count(*) AS signals FROM project_records_v1 WHERE project='morning' AND source_ts>=:lo AND source_ts<:end"),
    ('morning_identity', "SELECT coalesce(n.signal_id,p.source_id) AS signal_id,n.ticker,n.initial_received_at_ms,p.source_id IS NOT NULL AS original_present,n.signal_id IS NOT NULL AS native_present,n.signal_json,p.payload->'original' AS original FROM (SELECT * FROM compass_native_morning_signals_v1 WHERE signal_at_ms>=:ms AND signal_at_ms<:end_ms AND NOT is_test) n FULL JOIN (SELECT * FROM project_records_v1 WHERE project='morning' AND source_ts>=:lo AND source_ts<:end) p ON p.source_id=n.signal_id ORDER BY 1"),
    ('morning_origins', "SELECT origin,payload->>'event_type' AS kind,payload->>'entry' AS entry,count(*) AS n FROM native_morning_receipts_v1 WHERE received>=:lo AND received<:end GROUP BY 1,2,3"),
    ('morning_import', "SELECT key,value,updated FROM state WHERE key LIKE 'morning_history:%' OR key='morning_native_reconciliation:status' ORDER BY key"),
    ('morning_candles', """WITH eligible AS (SELECT signal_id FROM compass_native_morning_signals_v1 WHERE signal_at_ms>=:ms AND signal_at_ms<:end_ms AND NOT is_test),
        originals AS (SELECT h.parent AS signal_id,(b->'payload'->>'open_at_ms')::bigint AS minute,(b->'payload')::jsonb AS bar FROM morning_history_v1 h JOIN eligible e ON h.parent=e.signal_id CROSS JOIN LATERAL json_array_elements(h.payload->'payload'->'bars') b WHERE h.kind='stock_inventory'),
        natives AS (SELECT b.signal_id,b.open_at_ms AS minute,b.bar_json::jsonb AS bar FROM compass_native_morning_stock_bars_v2 b JOIN eligible e USING(signal_id)),
        pairs AS (SELECT coalesce(o.signal_id,n.signal_id) AS signal_id,o.minute AS original_minute,n.minute AS native_minute,o.bar AS original_bar,n.bar AS native_bar FROM originals o FULL JOIN natives n USING(signal_id,minute))
        SELECT e.signal_id,count(p.original_minute) AS original_bars,count(p.native_minute) AS native_bars,
        count(*) FILTER (WHERE p.original_minute IS NOT NULL AND p.native_minute IS NULL) AS missing_native,
        count(*) FILTER (WHERE p.native_minute IS NOT NULL AND p.original_minute IS NULL) AS extra_native,
        count(*) FILTER (WHERE p.original_bar<>p.native_bar) AS changed,
        EXISTS(SELECT 1 FROM morning_history_v1 h WHERE h.kind='stock_inventory' AND h.parent=e.signal_id) AS original_inventory_present
        FROM eligible e LEFT JOIN pairs p USING(signal_id) GROUP BY e.signal_id ORDER BY e.signal_id"""),
    ('morning_jobs', "SELECT j.status,count(*) AS n FROM compass_native_morning_option_quotes_v1 j JOIN compass_native_morning_signals_v1 s USING(signal_id) WHERE s.signal_at_ms>=:ms AND s.signal_at_ms<:end_ms AND NOT s.is_test GROUP BY 1"),
    ('option_cohorts', "SELECT status,payload->>'collection_version' AS collection_version,count(*) AS n,count(*) FILTER (WHERE payload->>'pnl' IS NOT NULL) AS measured,sum((payload->>'pnl')::numeric) AS independent_trial_pnl FROM option_ideas_v1 WHERE created>=:lo AND created<:end GROUP BY 1,2 ORDER BY 2,1"),
    ('option_reasons', "SELECT status,coalesce(payload->>'exit_reason',payload->>'waiting_reason') AS reason,count(*) AS n FROM option_ideas_v1 WHERE created>=:lo AND created<:end GROUP BY 1,2 ORDER BY 1,3 DESC"),
    ('option_gaps', "SELECT id,underlying,created,updated,payload->'contract' AS contract,payload->'gap_detail' AS gap,payload->>'exit_reason' AS reason,payload->>'collection_version' AS collection_version FROM option_ideas_v1 WHERE created>=:lo AND created<:end AND status='unresolved' ORDER BY created"),
    ('tm_scores', "SELECT origin,CASE WHEN first_seen>=:op AND first_seen<:cl THEN 'regular' ELSE 'outside_cash' END AS receipt_session,score_status,count(*) AS n FROM tm_flow_study_v1 WHERE first_seen>=:lo AND first_seen<:end GROUP BY 1,2,3"),
    ('tm_checkpoints', "SELECT c.horizon,c.status,c.reason,count(*) AS n,count(*) FILTER (WHERE c.status='pending' AND c.due_at<:now) AS overdue FROM tm_flow_checkpoints_v1 c JOIN tm_flow_study_v1 s ON s.id=c.study_id WHERE s.origin='prospective' AND s.first_seen>=:lo AND s.first_seen<:end GROUP BY 1,2,3"),
    ('swing_groups', "SELECT origin,flow_group,payload->>'entry_reason' AS reason,count(*) AS n FROM swing_flow_trials_v1 WHERE first_seen>=:lo AND first_seen<:end GROUP BY 1,2,3"),
    ('swing_checkpoints', "SELECT c.horizon,c.status,c.reason,count(*) AS n,count(*) FILTER (WHERE c.status='pending' AND c.due_at<:now) AS overdue FROM swing_flow_checkpoints_v1 c JOIN swing_flow_trials_v1 s ON s.id=c.study_id WHERE s.origin='prospective' AND s.first_seen>=:lo AND s.first_seen<:end GROUP BY 1,2,3"),
    ('spy_checks', "SELECT phase,kind,origin,decision,count(*) AS n FROM spy_plan_checks_v1 WHERE day='2026-09-17' GROUP BY 1,2,3,4"),
    ('spy_options', "SELECT status,count(*) AS n FROM spy_plan_options_v1 WHERE day='2026-09-17' GROUP BY 1"),
    ('setup_cohorts', "SELECT payload->>'asset' AS asset,version,status,count(*) AS n,sum((payload->>'pnl')::numeric) AS independent_trial_pnl FROM setup_trials_v1 WHERE started>=CASE WHEN payload->>'asset'='future' THEN :fo ELSE :lo END AND started<CASE WHEN payload->>'asset'='future' THEN :fc ELSE :cl END GROUP BY 1,2,3"),
    ('future_trials', "SELECT symbol,status,coalesce(payload->>'exit_reason',payload->>'reason') AS reason,count(*) AS n FROM setup_trials_v1 WHERE payload->>'asset'='future' AND started>=:fo AND started<:fc GROUP BY 1,2,3 ORDER BY 1,2"),
    ('futures_mapping', "SELECT key,value,updated FROM state WHERE key LIKE 'futures_selection:%' OR key='futures:selection' OR key='health:databento_futures'"),
    ('health', "SELECT key,updated,value FROM state WHERE key IN ('health:option_stream','health:alpaca_stocks','health:option_recovery','risk:2026-09-17','health:databento','health:futures','health:storage')"),
]


def main():
    url = os.environ['DATABASE_URL'].replace('postgres://', 'postgresql+psycopg://', 1).replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(url, connect_args={'connect_timeout': 10}, pool_size=1, max_overflow=0)
    failed = []
    def emit(kind, value):
        print('CLOSE_AUDIT ' + json.dumps(dict(kind=kind, at=NOW, value=value), default=str, separators=(',', ':')), flush=True)
    with engine.connect() as c:
        c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout='8s'"))
        c.execute(text("SET LOCAL lock_timeout='1s'"))
        params = dict(lo=LOW, ms=int(LOW*1000), op=OPEN, cl=CLOSE, fo=FUTURE_OPEN,
                      fc=FUTURE_CLOSE, end=min(NOW, LOW+86400), end_ms=int(min(NOW, LOW+86400)*1000), now=NOW)
        emit('scope', dict(day='2026-09-17', read_only=c.execute(text('SHOW transaction_read_only')).scalar_one(),
                          cash_closed=NOW>=CLOSE, futures_closed=NOW>=FUTURE_CLOSE,
                          cash_open=OPEN, cash_close=CLOSE, futures_open=FUTURE_OPEN, futures_close=FUTURE_CLOSE))
        for kind, sql in QUERIES:
            try:
                with c.begin_nested():
                    rows = [dict(r) for r in c.execute(text(sql), params).mappings()]
                    if kind == 'morning_identity':
                        for row in rows:
                            native = json.loads(row.pop('signal_json') or '{}')
                            original = row.pop('original') or {}
                            row['different_fields'] = [k for k in ('price','setup','script_version','settings','features') if native.get(k)!=original.get(k)] if native and original else None
                    if kind == 'health':
                        for row in rows:
                            if row['key']=='health:option_stream': row['value'].pop('symbols', None)
                    # One line per row keeps remote logging from truncating large sections.
                    emit(kind, dict(count=len(rows)))
                    for row in rows: emit(kind+'_row', row)
            except Exception as error:
                failed.append(kind)
                emit('query_error', dict(section=kind, type=type(error).__name__))
        # Indexed per-contract scans avoid one broad scan of the quote archive.
        # Minute presence proves receipt coverage, not uninterrupted sub-minute continuity.
        symbols=list(c.execute(text("SELECT DISTINCT symbol FROM setup_trials_v1 WHERE payload->>'asset'='future' AND started>=:fo AND started<:fc ORDER BY symbol"),params).scalars())
        for symbol in symbols:
            try:
                with c.begin_nested():
                    q=c.execute(text("SELECT count(*) AS samples,count(DISTINCT floor(ts/60)) AS observed_minutes,min(ts) AS first,max(ts) AS last,max(received-ts) AS largest_source_to_storage_delay FROM events WHERE kind='quote' AND symbol=:symbol AND ts>=:fo AND ts<least(:fc,:now)"),dict(params,symbol=symbol)).mappings().one()
                    emit('futures_quote_coverage',dict(symbol=symbol,**q))
            except Exception as error:
                failed.append('futures_quotes:'+symbol)
                emit('query_error',dict(section='futures_quotes',symbol=symbol,type=type(error).__name__))
        emit('done', dict(failed=failed, complete=not failed))
        c.rollback()
    engine.dispose()


if __name__ == '__main__': main()
