import json
from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, select
from .store import PayloadConflict, canonical, digest, insert_many_once, insert_once, metadata, now_ms

research_sessions = Table("compass_native_morning_research_sessions_v3", metadata,
    Column("session_id", String(500), primary_key=True),
    Column("ticker", String(50), nullable=False, index=True),
    Column("session_open_ms", BigInteger, nullable=False, index=True),
    Column("session_json", Text, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("is_test", Boolean, nullable=False), Column("received_at_ms", BigInteger, nullable=False))
research_batches = Table("compass_native_morning_research_batches_v3", metadata,
    Column("event_id", String(550), primary_key=True), Column("session_id", String(500), nullable=False, index=True),
    Column("event_json", Text, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("received_at_ms", BigInteger, nullable=False))
research_bars = Table("compass_native_morning_research_bars_v3", metadata,
    Column("session_id", String(500), primary_key=True), Column("open_at_ms", BigInteger, primary_key=True),
    Column("bar_json", Text, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("received_at_ms", BigInteger, nullable=False))
research_records = Table("compass_native_morning_research_records_v3", metadata,
    Column("record_id", String(550), primary_key=True), Column("session_id", String(500), nullable=False, index=True),
    Column("kind", String(30), nullable=False, index=True), Column("at_ms", BigInteger, nullable=False, index=True),
    Column("source_signal_id", String(500), nullable=True, unique=True),
    Column("record_json", Text, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("received_at_ms", BigInteger, nullable=False))


def checked_insert(conn, table, values, key, identity):
    inserted = insert_once(conn, table, values)
    stored = conn.execute(select(table.c.payload_hash).where(key == identity)).scalar_one_or_none()
    if stored != values["payload_hash"]:
        raise PayloadConflict("Research identity already exists with different contents")
    return inserted


def accept_research(engine, payload, received=None):
    received = received or now_ms()
    data = payload.model_dump()
    session = data["session"]
    sid = session["session_id"]
    with engine.begin() as conn:
        checked_insert(conn, research_sessions, {"session_id": sid, "ticker": session["ticker"],
            "session_open_ms": session["session_open_ms"], "session_json": canonical(session),
            "payload_hash": digest(session), "is_test": session["is_test"], "received_at_ms": received}, research_sessions.c.session_id, sid)
        fresh = checked_insert(conn, research_batches, {"event_id": data["event_id"], "session_id": sid,
            "event_json": canonical(data), "payload_hash": digest(data), "received_at_ms": received}, research_batches.c.event_id, data["event_id"])
        if fresh:
            insert_many_once(conn, research_bars, [{"session_id": sid, "open_at_ms": b["open_at_ms"],
                "bar_json": canonical(b), "payload_hash": digest(b), "received_at_ms": received} for b in data["bars"]])
            saved = {r.open_at_ms: r.payload_hash for r in conn.execute(select(research_bars.c.open_at_ms, research_bars.c.payload_hash)
                .where(research_bars.c.session_id == sid, research_bars.c.open_at_ms.in_([b["open_at_ms"] for b in data["bars"]])))}
            if any(saved[b["open_at_ms"]] != digest(b) for b in data["bars"]):
                raise PayloadConflict("Previously recorded research candle has different contents")
            for r in data["records"]:
                checked_insert(conn, research_records, {"record_id": r["record_id"], "session_id": sid, "kind": r["kind"],
                    "at_ms": r["at_ms"], "source_signal_id": r["source_signal_id"], "record_json": canonical(r),
                    "payload_hash": digest(r), "received_at_ms": received}, research_records.c.record_id, r["record_id"])
    return {"status": "accepted" if fresh else "duplicate", "event_id": data["event_id"], "research_only": True}


def read_tapes(conn, ids):
    result = {sid: [] for sid in ids}
    if ids:
        for r in conn.execute(select(research_bars).where(research_bars.c.session_id.in_(ids)).order_by(research_bars.c.open_at_ms)).mappings():
            result[r["session_id"]].append(dict(json.loads(r["bar_json"]), received_at_ms=r["received_at_ms"]))
    return result

