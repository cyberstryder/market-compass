"""Durable, quiet, signal-relative OPRA observations. Never fetch today's quote as past data."""
import json
import uuid

import httpx
from sqlalchemy import and_, or_, select

from .options import AlpacaQuotes
from .store import canonical, insert_many_once, insert_once, now_ms, option_samples, option_tracks

VALID_QUOTES = {"available", "wide_spread"}
GRACE_MS = 5000
MAX_LATENESS_MS = 30000


def schedule_history(conn, signal, reference, now):
    contract = reference.get("contract")
    if not contract or signal.get("is_test") or signal["signal_at_ms"] + 3600000 < now:
        return
    sid = signal["signal_id"]
    inserted = insert_once(conn, option_tracks, {"signal_id": sid, "contract_json": canonical(contract),
        "reference_json": canonical(reference), "created_at_ms": now})
    if inserted:
        insert_many_once(conn, option_samples, [{"signal_id": sid, "minute": minute,
            "due_at_ms": signal["signal_at_ms"] + minute * 60000,
            "status": "pending", "lease_until_ms": 0} for minute in range(1, 61)])


def sample_batch(engine, provider, now=None):
    now = provider.clock() if now is None else now
    lease = str(uuid.uuid4())
    with engine.begin() as conn:
        rows = conn.execute(select(option_samples).where(and_(option_samples.c.due_at_ms <= now - GRACE_MS,
            or_(option_samples.c.status == "pending", and_(option_samples.c.status == "fetching",
                 option_samples.c.lease_until_ms <= now))))
            .order_by(option_samples.c.due_at_ms, option_samples.c.signal_id).limit(100)
            .with_for_update(skip_locked=True)).mappings().all()
        if not rows:
            return False
        jobs = [dict(r) for r in rows]
        keys = or_(*(and_(option_samples.c.signal_id == r["signal_id"], option_samples.c.minute == r["minute"]) for r in jobs))
        conn.execute(option_samples.update().where(keys).values(status="fetching", lease_token=lease, lease_until_ms=now + 30000))
        tracks = {r["signal_id"]: dict(r) for r in conn.execute(select(option_tracks)
            .where(option_tracks.c.signal_id.in_({r["signal_id"] for r in jobs}))).mappings()}
    contracts = {}
    for job in jobs:
        if now - job["due_at_ms"] <= MAX_LATENESS_MS:
            c = json.loads(tracks[job["signal_id"]]["contract_json"])
            contracts[c["symbol"]] = c
    quotes = provider.snapshots(list(contracts.values())) if contracts else {}
    completed = provider.clock()
    with engine.begin() as conn:
        for job in jobs:
            track = tracks[job["signal_id"]]
            contract = json.loads(track["contract_json"])
            reference = json.loads(track["reference_json"])
            if completed - job["due_at_ms"] > MAX_LATENESS_MS:
                q = {"status": "missed", "reason": "observation_window_missed", "contract": contract}
            else:
                q = dict(quotes.get(contract["symbol"], {"status": "unavailable", "reason": "quote_missing"}))
            q.update(due_at_ms=job["due_at_ms"], sampled_at_ms=completed,
                     lateness_seconds=(completed - job["due_at_ms"]) / 1000)
            # Entry quote may have arrived late. Never compare with an earlier quote.
            if q["status"] in VALID_QUOTES and q["quote_at_ms"] <= reference.get("quote_at_ms", 0):
                q.update(status="unavailable", reason="quote_not_after_initial_reference")
            if q["status"] in VALID_QUOTES and reference.get("status") in VALID_QUOTES:
                q.update(ask_to_bid_return_pct=(q["bid"] / reference["ask"] - 1) * 100,
                         midpoint_return_pct=(q["midpoint"] / reference["midpoint"] - 1) * 100,
                         ask_to_bid_pnl_per_contract=(q["bid"] - reference["ask"]) * contract["multiplier"],
                         reference_quote_at_ms=reference["quote_at_ms"])
            else:
                q["return_unavailable_reason"] = "initial_reference_unavailable" if reference.get("status") not in VALID_QUOTES else q.get("reason", "quote_unavailable")
            conn.execute(option_samples.update().where(and_(option_samples.c.signal_id == job["signal_id"],
                option_samples.c.minute == job["minute"], option_samples.c.lease_token == lease)).values(
                status="complete", quote_json=canonical(q), lease_token=None, lease_until_ms=0))
    return True


def run_history_worker(engine, config, stop):
    with httpx.Client(timeout=2, follow_redirects=False) as client:
        provider = AlpacaQuotes(config, client)
        while not stop.is_set():
            try:
                worked = sample_batch(engine, provider)
            except Exception:
                worked = False  # Lease recovery is bounded by the observation window.
            stop.wait(0.2 if worked else 1)

