import hashlib
import json
import time
from contextlib import contextmanager
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Float, JSON, Index, select, text, update
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
state=Table("state",meta,Column("key",String(240),primary_key=True),
    Column("value",JSON,nullable=False),Column("updated",Float,nullable=False))
leases=Table("leases",meta,Column("key",String(100),primary_key=True),
    Column("owner",String(100),nullable=False),Column("until",Float,nullable=False))

def identity(*args):
    return hashlib.sha256(json.dumps(args,sort_keys=True,default=str).encode()).hexdigest()

class Store:
    def __init__(self,url):
        url=url.replace("postgres://","postgresql+psycopg://",1).replace("postgresql://","postgresql+psycopg://",1)
        self.engine=create_engine(url,pool_pre_ping=True,
            connect_args={"check_same_thread":False,"timeout":30} if url.startswith("sqlite") else {})
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
        return c.execute(q.on_conflict_do_nothing(index_elements=["key"])).rowcount>0

    def put(self,c,key,value):
        q=self.insert(state).values(key=key,value=value,updated=time.time())
        c.execute(q.on_conflict_do_update(index_elements=["key"],
            set_={"value":value,"updated":time.time()}))

    def get(self,c,key,default=None):
        r=c.execute(select(state.c.value).where(state.c.key==key)).first()
        return r[0] if r else default

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

    def health(self,name,status,detail,source_ts=None,**extra):
        with self.tx() as c:
            value={**self.get(c,"health:"+name,{}),"name":name,"status":status,
                "detail":detail,"checked_at":time.time(),**extra}
            if source_ts is not None: value["source_ts"]=source_ts
            self.put(c,"health:"+name,value)
