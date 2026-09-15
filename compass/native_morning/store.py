import hashlib
import json
import time

from sqlalchemy import BigInteger, Boolean, Column, Integer, MetaData, String, Table, Text, create_engine, event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

from .models import Event

metadata = MetaData()
signals = Table("compass_native_morning_signals_v1", metadata,
    Column("signal_id", String(500), primary_key=True),
    Column("ticker", String(50), nullable=False, index=True),
    Column("signal_at_ms", BigInteger, nullable=False, index=True),
    Column("received_at_ms", BigInteger, nullable=False),
    Column("signal_json", Text, nullable=False),
    Column("signal_hash", String(64), nullable=False),
    Column("initial_received_at_ms", BigInteger, nullable=True),
    Column("is_test", Boolean, nullable=False),
)
events = Table("compass_native_morning_events_v1", metadata,
    Column("event_id", String(520), primary_key=True),
    Column("signal_id", String(500), nullable=False, index=True),
    Column("received_at_ms", BigInteger, nullable=False),
    Column("event_json", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False),
)
outbox = Table("compass_native_morning_discord_outbox_v1", metadata,
    Column("event_id", String(520), primary_key=True),
    Column("payload_json", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_ms", BigInteger, nullable=False),
    Column("lease_until_ms", BigInteger, nullable=False, default=0),
    Column("lease_token", String(40), nullable=True),
    Column("delivered_at_ms", BigInteger, nullable=True),
    Column("last_error", String(100), nullable=True),
)
option_jobs = Table("compass_native_morning_option_quotes_v1", metadata,
    Column("event_id", String(520), primary_key=True),
    Column("signal_id", String(500), nullable=False, index=True),
    Column("message_id", String(32), nullable=True),
    Column("policy_json", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("quote_json", Text, nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_until_ms", BigInteger, nullable=False, default=0),
    Column("lease_token", String(40), nullable=True),
)
stock_bars = Table("compass_native_morning_stock_bars_v2", metadata,
    Column("signal_id", String(500), primary_key=True),
    Column("open_at_ms", BigInteger, primary_key=True),
    Column("bar_json", Text, nullable=False),
    Column("bar_hash", String(64), nullable=False),
    Column("received_at_ms", BigInteger, nullable=False),
)
option_tracks = Table("compass_native_morning_option_tracks_v2", metadata,
    Column("signal_id", String(500), primary_key=True),
    Column("contract_json", Text, nullable=False),
    Column("reference_json", Text, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
)
option_samples = Table("compass_native_morning_option_samples_v2", metadata,
    Column("signal_id", String(500), primary_key=True),
    Column("minute", Integer, primary_key=True),
    Column("due_at_ms", BigInteger, nullable=False, index=True),
    Column("status", String(20), nullable=False, index=True),
    Column("quote_json", Text, nullable=True),
    Column("lease_until_ms", BigInteger, nullable=False, default=0),
    Column("lease_token", String(40), nullable=True),
)


def insert_many_once(conn, table, values):
    if values:
        insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        conn.execute(insert(table).values(values).on_conflict_do_nothing())


def now_ms():
    return int(time.time() * 1000)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def make_engine(url):
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 2}
        if ":memory:" in url:
            options["poolclass"] = StaticPool
    elif url.startswith("postgresql+psycopg://"):
        options["connect_args"] = {"connect_timeout": 2, "options": "-c statement_timeout=1500 -c lock_timeout=1000"}
        options["pool_timeout"] = 1
    else:
        raise ValueError("Use PostgreSQL or SQLite")
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_setup(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
    return engine


def insert_once(conn, table, values):
    insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    # DBAPI rowcount is not portable for INSERT (psycopg may report -1).
    # RETURNING distinguishes a real insertion from ON CONFLICT DO NOTHING.
    key = next(iter(table.primary_key.columns))
    result = conn.execute(insert(table).values(**values).on_conflict_do_nothing().returning(key))
    return result.first() is not None


class PayloadConflict(Exception):
    pass


def accept_event(engine, payload: Event, discord_payload, discord_enabled, received=None):
    received = received or now_ms()
    data = payload.model_dump()
    # Preserve v1 canonical hashes, including previously accepted retries.
    if data["stock_bars"] is None:
        data.pop("stock_bars")
    signal = data["signal"]
    sid = signal["signal_id"]
    with engine.begin() as conn:
        inserted = insert_once(conn, signals, {
            "signal_id": sid, "ticker": signal["ticker"], "signal_at_ms": signal["signal_at_ms"],
            "received_at_ms": received, "signal_json": canonical(signal), "signal_hash": digest(signal),
            "initial_received_at_ms": received if payload.event_type == "signal" else None,
            "is_test": signal["is_test"],
        })
        saved = conn.execute(select(signals).where(signals.c.signal_id == sid)).mappings().one()
        if saved["signal_hash"] != digest(signal):
            raise PayloadConflict("Signal identity already exists with different contents")
        fresh = insert_once(conn, events, {
            "event_id": payload.event_id, "signal_id": sid, "received_at_ms": received,
            "event_json": canonical(data), "payload_hash": digest(data),
        })
        if not fresh:
            existing_hash = conn.execute(select(events.c.payload_hash).where(events.c.event_id == payload.event_id)).scalar_one()
            if existing_hash != digest(data):
                raise PayloadConflict("Event identity already exists with different contents")
            return {"status": "duplicate", "event_id": payload.event_id}
        bars = data.get("stock_bars", [])
        if bars:
            insert_many_once(conn, stock_bars, [{"signal_id": sid, "open_at_ms": b["open_at_ms"],
                "bar_json": canonical(b), "bar_hash": digest(b), "received_at_ms": received} for b in bars])
            saved_bars = {r.open_at_ms: r.bar_hash for r in conn.execute(select(stock_bars.c.open_at_ms, stock_bars.c.bar_hash)
                .where(stock_bars.c.signal_id == sid))}
            if any(saved_bars[b["open_at_ms"]] != digest(b) for b in bars):
                raise PayloadConflict("Previously recorded candle has different contents")
        if payload.event_type == "signal":
            conn.execute(signals.update().where(signals.c.signal_id == sid).values(initial_received_at_ms=received))
            insert_once(conn, outbox, {
                "event_id": payload.event_id,
                "payload_json": canonical(discord_payload),
                "status": "pending" if discord_enabled and not signal["is_test"] else "disabled",
                "attempts": 0, "next_attempt_ms": received, "lease_until_ms": 0,
            })
    return {"status": "accepted", "event_id": payload.event_id, "recovered_from_checkpoint": inserted and payload.event_type == "checkpoint"}

