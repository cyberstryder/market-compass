import hashlib
import json
import time
import threading
import logging
from contextlib import contextmanager
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Float, JSON, Index, select, text, update, cast, event
from sqlalchemy.engine import make_url
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sq_insert

meta=MetaData()
events=Table("events",meta,
    Column("id",Integer,primary_key=True,autoincrement=True),
    Column("key",String(240),unique=True,nullable=False),
    Column("kind",String(40),nullable=False),
    Column("source",String(40),nullable=False),
    Column("symbol",String(100),nullable=False),
    Column("ts",Float,nullable=False),
    Column("received",Float,nullable=False),
    Column("payload",JSON,nullable=False))
Index("events_kind_symbol_time",events.c.kind,events.c.symbol,events.c.ts)
discord_jobs=Table('discord_jobs_v1',meta,
    Column('event_id',Integer,primary_key=True),Column('route',String(40),nullable=False),
    Column('status',String(20),nullable=False),Column('queued_at',Float,nullable=False),
    Column('confirmation',JSON,nullable=False))
Index('discord_jobs_route_status_id',discord_jobs.c.route,discord_jobs.c.status,discord_jobs.c.event_id)
state=Table("state",meta,Column("key",String(240),primary_key=True),
    Column("value",JSON,nullable=False),Column("updated",Float,nullable=False))
leases=Table("leases",meta,Column("key",String(100),primary_key=True),
    Column("owner",String(100),nullable=False),Column("until",Float,nullable=False))
gap_followups=Table('gap_followups_v1',meta,
    Column('id',String(64),primary_key=True),Column('status',String(24),nullable=False),
    Column('created',Float,nullable=False),Column('deadline',Float,nullable=False),
    Column('payload',JSON,nullable=False))
Index('gap_followups_active',gap_followups.c.status,gap_followups.c.deadline)
smoothers_daily=Table('smoothers_daily_comparison_v1',meta,
    Column('id',String(64),primary_key=True),Column('day',String(10),nullable=False),
    Column('family',String(32),nullable=False),Column('symbol',String(16),nullable=False),
    Column('side',String(10),nullable=False),Column('created',Float,nullable=False),
    Column('status',String(24),nullable=False),Column('next_due',Float),Column('payload',JSON,nullable=False))
Index('smoothers_daily_session',smoothers_daily.c.day,smoothers_daily.c.family)
Index('smoothers_daily_pending',smoothers_daily.c.status,smoothers_daily.c.next_due)
flow_records=Table("flow_records",meta,
    Column("day",String(10),primary_key=True),Column("vendor_id",String(200),primary_key=True),
    Column("source_ts",Float,nullable=False),Column("payload",JSON,nullable=False),
    Column("first_seen",Float,nullable=False),Column("last_seen",Float,nullable=False))
Index("flow_records_day_time",flow_records.c.day,flow_records.c.source_ts)

# Independent discovery cohorts never enter the frozen swing study.
discovery_trials=Table('discovery_trials_v1',meta,
    Column('id',String(64),primary_key=True),Column('symbol',String(100),nullable=False),
    Column('created',Float,nullable=False),Column('status',String(24),nullable=False),
    Column('payload',JSON,nullable=False))
Index('discovery_created',discovery_trials.c.created,discovery_trials.c.id)
Index('discovery_pending',discovery_trials.c.status,discovery_trials.c.symbol)
discovery_checks=Table('discovery_checks_v1',meta,
    Column('trial_id',String(64),primary_key=True),Column('horizon',String(24),primary_key=True),
    Column('target_at',Float,nullable=False),Column('status',String(24),nullable=False),
    Column('payload',JSON,nullable=False))
Index('discovery_due',discovery_checks.c.status,discovery_checks.c.target_at)

# Immutable receipt assessments; forward checkpoints have their own lifecycle.
tm_studies=Table('tm_flow_study_v1',meta,
    Column('id',String(64),primary_key=True),Column('day',String(10),nullable=False),
    Column('vendor_id',String(200),nullable=False),Column('version',String(50),nullable=False),
    Column('symbol',String(100),nullable=False),Column('first_seen',Float,nullable=False),
    Column('assessed_at',Float,nullable=False),Column('origin',String(30),nullable=False),
    Column('status',String(20),nullable=False),Column('score_status',String(30),nullable=False),
    Column('direction',String(10),nullable=False),Column('age_bucket',String(30),nullable=False),
    Column('dte_bucket',String(20),nullable=False),Column('tm_score',Float),Column('compass_score',Float),
    Column('payload',JSON,nullable=False))
Index('tm_study_source',tm_studies.c.day,tm_studies.c.vendor_id,tm_studies.c.version,unique=True)
Index('tm_study_receipt_id',tm_studies.c.first_seen,tm_studies.c.id)
Index('tm_study_active_symbols',tm_studies.c.status,tm_studies.c.symbol)
tm_checkpoints=Table('tm_flow_checkpoints_v1',meta,
    Column('study_id',String(64),primary_key=True),Column('horizon',String(20),primary_key=True),
    Column('target_at',Float),Column('due_at',Float),Column('status',String(20),nullable=False),
    Column('reason',String(100)),Column('price',Float),Column('source_ts',Float),Column('received_at',Float),
    Column('raw_return_pct',Float),Column('directional_return_pct',Float),Column('checked_at',Float))
Index('tm_checkpoints_due',tm_checkpoints.c.status,tm_checkpoints.c.due_at)

swing_trials=Table('swing_flow_trials_v1',meta,
    Column('id',String(64),primary_key=True),Column('version',String(50),nullable=False),
    Column('source_id',Integer,nullable=False),Column('symbol',String(100),nullable=False),
    Column('side',String(10),nullable=False),Column('rule',String(50),nullable=False),
    Column('first_seen',Float,nullable=False),Column('assessed_at',Float,nullable=False),
    Column('origin',String(30),nullable=False),Column('flow_group',String(30),nullable=False),
    Column('status',String(20),nullable=False),Column('payload',JSON,nullable=False))
Index('swing_trial_source',swing_trials.c.version,swing_trials.c.source_id,unique=True)
Index('swing_trial_receipt',swing_trials.c.first_seen,swing_trials.c.id)
Index('swing_trial_active',swing_trials.c.status,swing_trials.c.symbol)
swing_checkpoints=Table('swing_flow_checkpoints_v1',meta,
    Column('study_id',String(64),primary_key=True),Column('horizon',String(20),primary_key=True),
    Column('target_at',Float),Column('due_at',Float),Column('status',String(20),nullable=False),
    Column('reason',String(100)),Column('price',Float),Column('source_ts',Float),Column('received_at',Float),
    Column('raw_return_pct',Float),Column('directional_return_pct',Float),Column('checked_at',Float))
Index('swing_trial_checkpoints_due',swing_checkpoints.c.status,swing_checkpoints.c.due_at)

spy_checks=Table('spy_plan_checks_v1',meta,
    Column('id',String(64),primary_key=True),Column('version',String(50),nullable=False),
    Column('source_key',String(150),nullable=False),Column('day',String(10),nullable=False),
    Column('phase',String(30),nullable=False),Column('kind',String(24),nullable=False),
    Column('origin',String(30),nullable=False),Column('decision',String(30),nullable=False),
    Column('created',Float,nullable=False),Column('payload',JSON,nullable=False))
Index('spy_plan_check_source',spy_checks.c.version,spy_checks.c.source_key,unique=True)
Index('spy_plan_check_created',spy_checks.c.created,spy_checks.c.id)
spy_options=Table('spy_plan_options_v1',meta,
    Column('id',String(64),primary_key=True),Column('version',String(50),nullable=False),
    Column('day',String(10),nullable=False),Column('status',String(24),nullable=False),
    Column('created',Float,nullable=False),Column('updated',Float,nullable=False),Column('payload',JSON,nullable=False))
Index('spy_plan_one_observation',spy_options.c.version,spy_options.c.day,unique=True)
Index('spy_plan_options_active',spy_options.c.status,spy_options.c.created)
spy_marks=Table('spy_plan_marks_v1',meta,
    Column('id',String(64),primary_key=True),Column('study_id',String(64),nullable=False),
    Column('at',Float,nullable=False),Column('processed_at',Float,nullable=False),
    Column('payload',JSON,nullable=False))
Index('spy_plan_mark_path',spy_marks.c.study_id,spy_marks.c.at,spy_marks.c.id)

def identity(*args):
    return hashlib.sha256(json.dumps(args,sort_keys=True,default=str).encode()).hexdigest()

class Store:
    def __init__(self,url):
        url=url.replace("postgres://","postgresql+psycopg://",1).replace("postgresql://","postgresql+psycopg://",1)
        self.engine=create_engine(url,pool_pre_ping=True,
            connect_args={"check_same_thread":False,"timeout":30} if url.startswith("sqlite") else {"connect_timeout":10,
                "options":str(make_url(url).query.get("options", ""))+" -c statement_timeout=30000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=30000",
                "keepalives":1,"keepalives_idle":10,"keepalives_interval":5,
                "keepalives_count":3,"tcp_user_timeout":30000})
        if not url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def bound_transaction(connection, record):
                # PostgreSQL 17+ can also bound a transaction doing client-side work.
                if connection.info.server_version >= 170000:
                    with connection.cursor() as cursor:
                        cursor.execute("SET SESSION transaction_timeout = '60s'")
                    connection.commit()
        self.insert=sq_insert if url.startswith("sqlite") else pg_insert
        self._closing = False
        self._connections = {}
        self._condition = threading.Condition()
        @event.listens_for(self.engine, "checkout")
        def track(connection, record, proxy):
            with self._condition:
                self._connections[record] = connection
        @event.listens_for(self.engine, "checkin")
        def returned(connection, record):
            with self._condition:
                self._connections.pop(record, None)
                self._condition.notify_all()

    def shutdown(self, timeout=10):
        """Cancel this process's checked-out PostgreSQL queries, then drain them."""
        with self._condition:
            self._closing = True
            connections = list(self._connections.values())
        for connection in connections:
            if self.engine.dialect.name == 'postgresql':
                try:
                    # psycopg binary ships libpq 17+; no unbounded legacy cancel.
                    from psycopg import capabilities
                    if capabilities.has_cancel_safe():
                        connection.cancel_safe(timeout=1)
                except Exception:
                    logging.getLogger('uvicorn.error').warning('Database shutdown cancellation failed')
        with self._condition:
            drained = self._condition.wait_for(lambda: not self._connections, timeout)
        if not drained:
            logging.getLogger('uvicorn.error').warning('Database shutdown drain timed out; server timeouts remain active')
        self.engine.dispose()
        return drained

    def initialize(self):
        with self.engine.begin() as c:
            if self.engine.dialect.name=="postgresql":
                c.execute(text("SELECT pg_advisory_xact_lock(8675309001)"))
            meta.create_all(c)

    @contextmanager
    def tx(self):
        if self._closing:
            raise RuntimeError("Database is shutting down")
        with self.engine.begin() as c:
            if self._closing:
                raise RuntimeError("Database is shutting down")
            yield c

    def append(self,c,kind,source,symbol,ts,payload,key=None):
        q=self.insert(events).values(key=key or identity(kind,source,symbol,ts,payload),
            kind=kind,source=source,symbol=symbol,ts=ts,received=time.time(),payload=payload)
        # INSERT rowcount is not portable (some drivers return -1). The row
        # returned by PostgreSQL/SQLite distinguishes a new insert from dedup.
        return c.execute(q.on_conflict_do_nothing(index_elements=["key"])
            .returning(events.c.id)).first() is not None

    def append_quotes(self,c,source,items):
        """Persist every accepted sample with bounded inserts, not one SQL call each."""
        for start in range(0,len(items),500):
            received=time.time()
            rows=[dict(key=identity('quote',source,symbol,q['ts']),kind='quote',
                source=source,symbol=symbol,ts=q['ts'],received=received,payload=q)
                for symbol,q in items[start:start+500]]
            c.execute(self.insert(events).values(rows)
                .on_conflict_do_nothing(index_elements=['key']))

    def append_bars(self,c,kind,source,items):
        """Bounded multi-row inserts keep history recovery off the live write path."""
        received=time.time()
        for start in range(0,len(items),500):
            rows=[dict(key=identity(kind,source,symbol,stamp,payload),kind=kind,
                source=source,symbol=symbol,ts=stamp,received=received,payload=payload)
                for symbol,stamp,payload in items[start:start+500]]
            c.execute(self.insert(events).values(rows)
                .on_conflict_do_nothing(index_elements=['key']))

    def put(self,c,key,value):
        q=self.insert(state).values(key=key,value=value,updated=time.time())
        c.execute(q.on_conflict_do_update(index_elements=["key"],
            set_={"value":value,"updated":time.time()}))

    def get(self,c,key,default=None):
        r=c.execute(select(state.c.value).where(state.c.key==key)).first()
        return r[0] if r else default

    def locked_get(self,c,key,default):
        """Serialize read/modify/write windows across live and backfill writers."""
        c.execute(self.insert(state).values(key=key,value=default,updated=time.time())
            .on_conflict_do_nothing(index_elements=['key']))
        return c.execute(select(state.c.value).where(state.c.key==key).with_for_update()).scalar_one()

    def put_quote(self,c,key,value):
        """Atomic source-time comparison: a late snapshot cannot rewind a stream."""
        q=self.insert(state).values(key=key,value=value,updated=time.time())
        result=c.execute(q.on_conflict_do_update(index_elements=['key'],
            set_={'value':value,'updated':time.time()},
            where=state.c.value['ts'].as_float()<=value['ts']).returning(state.c.key))
        return result.first() is not None

    def put_quotes(self,c,items):
        """One ordered upsert for a bounded batch; never rewind source time."""
        latest={}
        for symbol,value in items:
            if symbol not in latest or value['ts']>=latest[symbol]['ts']:
                latest[symbol]=value
        if not latest:return
        now=time.time()
        q=self.insert(state).values([dict(key='quote:'+symbol,value=latest[symbol],updated=now)
            for symbol in sorted(latest)])
        c.execute(q.on_conflict_do_update(index_elements=['key'],
            set_={'value':q.excluded.value,'updated':q.excluded.updated},
            where=state.c.value['ts'].as_float()<=q.excluded.value['ts'].as_float()))

    def put_max(self,c,key,value):
        q=self.insert(state).values(key=key,value=value,updated=time.time())
        c.execute(q.on_conflict_do_update(index_elements=['key'],
            set_={'value':value,'updated':time.time()},
            where=cast(cast(state.c.value,String),Float)<value))

    def prefix(self,c,prefix):
        return {r.key:r.value for r in c.execute(select(state).where(state.c.key.startswith(prefix)))}

    def recent(self,c,kind,symbol=None,limit=100,since=None):
        q=select(events).where(events.c.kind==kind)
        if symbol is not None: q=q.where(events.c.symbol==symbol)
        if since is not None: q=q.where(events.c.ts>=since)
        return [dict(r) for r in c.execute(q.order_by(events.c.ts.desc(),events.c.id.desc()).limit(limit)).mappings()]

    def lease(self,c,name,owner,seconds=30):
        now=time.time()
        c.execute(self.insert(leases).values(key=name,owner=owner,until=now+seconds)
            .on_conflict_do_nothing(index_elements=["key"]))
        r=c.execute(update(leases).where(leases.c.key==name,
            (leases.c.until<now)|(leases.c.owner==owner)).values(owner=owner,until=now+seconds))
        return r.rowcount==1

    def health(self,name,status,detail,source_ts=None,monotonic_source=False,**extra):
        with self.tx() as c:
            previous=self.locked_get(c,"health:"+name,{}) if monotonic_source else self.get(c,"health:"+name,{})
            value={**previous,"name":name,"status":status,
                "detail":detail,"checked_at":time.time(),**extra}
            if source_ts is not None:
                value["source_ts"]=max(source_ts,previous.get("source_ts") or source_ts) if monotonic_source else source_ts
            self.put(c,"health:"+name,value)
