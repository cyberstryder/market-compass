import hashlib
import json
import time
from contextlib import contextmanager
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Float, JSON, Index, select, text, update, cast
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
flow_records=Table("flow_records",meta,
    Column("day",String(10),primary_key=True),Column("vendor_id",String(200),primary_key=True),
    Column("source_ts",Float,nullable=False),Column("payload",JSON,nullable=False),
    Column("first_seen",Float,nullable=False),Column("last_seen",Float,nullable=False))
Index("flow_records_day_time",flow_records.c.day,flow_records.c.source_ts)

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

def identity(*args):
    return hashlib.sha256(json.dumps(args,sort_keys=True,default=str).encode()).hexdigest()

class Store:
    def __init__(self,url):
        url=url.replace("postgres://","postgresql+psycopg://",1).replace("postgresql://","postgresql+psycopg://",1)
        self.engine=create_engine(url,pool_pre_ping=True,
            connect_args={"check_same_thread":False,"timeout":30} if url.startswith("sqlite") else {"connect_timeout":10})
        self.insert=sq_insert if url.startswith("sqlite") else pg_insert

    def initialize(self):
        with self.engine.begin() as c:
            if self.engine.dialect.name=="postgresql":
                c.execute(text("SELECT pg_advisory_xact_lock(8675309001)"))
            meta.create_all(c)

    @contextmanager
    def tx(self):
        with self.engine.begin() as c:
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
