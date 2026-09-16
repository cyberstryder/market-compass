"""Durable signal-relative OPRA observations, shared batches for baseline and 0DTE."""
import json
import uuid

import httpx
from sqlalchemy import and_, or_, select

from .options import AlpacaQuotes
from .store import (canonical, insert_many_once, insert_once, option_samples, option_tracks,
                    expiration_samples, expiration_tracks)

VALID_QUOTES = {"available", "wide_spread"}
GRACE_MS = 5000
MAX_LATENESS_MS = 30000
TABLES = {"baseline": (option_tracks, option_samples), "zero_dte": (expiration_tracks, expiration_samples)}


def schedule_history(conn, signal, reference, now):
    references = {"baseline": reference}
    zero = reference.get("expiration_comparison", {}).get("variants", {}).get("zero_dte")
    if zero:
        references["zero_dte"] = zero
    for key, ref in references.items():
        contract = ref.get("contract")
        if not contract or signal.get("is_test") or signal["signal_at_ms"] + 3600000 < now:
            continue
        tracks, samples = TABLES[key]
        sid = signal["signal_id"]
        inserted = insert_once(conn, tracks, {"signal_id": sid, "contract_json": canonical(contract),
            "reference_json": canonical(ref), "created_at_ms": now})
        if inserted:
            insert_many_once(conn, samples, [{"signal_id": sid, "minute": minute,
                "due_at_ms": signal["signal_at_ms"] + minute * 60000,
                "status": "pending", "lease_until_ms": 0} for minute in range(1, 61)])


def sample_batch(engine, provider, now=None):
    now = provider.clock() if now is None else now
    lease = str(uuid.uuid4())
    tracks = {}
    with engine.begin() as conn:
        candidates = []
        for key, (_, samples) in TABLES.items():
            rows = conn.execute(select(samples).where(and_(samples.c.due_at_ms <= now - GRACE_MS,
                or_(samples.c.status == "pending", and_(samples.c.status == "fetching", samples.c.lease_until_ms <= now))))
                .order_by(samples.c.due_at_ms, samples.c.signal_id).limit(100)
                .with_for_update(skip_locked=True)).mappings().all()
            candidates.extend(dict(r, variant=key) for r in rows)
        # At most 100 distinct contracts; neither tape can starve the other.
        jobs = sorted(candidates, key=lambda r: (r["due_at_ms"], r["signal_id"], r["variant"]))[:100]
        if not jobs:
            return False
        for key, (track_table, samples) in TABLES.items():
            selected = [r for r in jobs if r["variant"] == key]
            if not selected:
                continue
            keys = or_(*(and_(samples.c.signal_id == r["signal_id"], samples.c.minute == r["minute"]) for r in selected))
            conn.execute(samples.update().where(keys).values(status="fetching", lease_token=lease, lease_until_ms=now + 30000))
            for r in conn.execute(select(track_table).where(track_table.c.signal_id.in_({r["signal_id"] for r in selected}))).mappings():
                tracks[(key, r["signal_id"])] = dict(r)
    contracts = {}
    for job in jobs:
        if now - job["due_at_ms"] <= MAX_LATENESS_MS:
            c = json.loads(tracks[(job["variant"], job["signal_id"])]["contract_json"])
            contracts[c["symbol"]] = c
    quotes = provider.snapshots(list(contracts.values())) if contracts else {}
    completed = provider.clock()
    with engine.begin() as conn:
        for job in jobs:
            track = tracks[(job["variant"], job["signal_id"])]
            contract = json.loads(track["contract_json"])
            reference = json.loads(track["reference_json"])
            if completed - job["due_at_ms"] > MAX_LATENESS_MS:
                q = {"status": "missed", "reason": "observation_window_missed", "contract": contract}
            else:
                q = dict(quotes.get(contract["symbol"], {"status": "unavailable", "reason": "quote_missing"}))
            q.update(due_at_ms=job["due_at_ms"], sampled_at_ms=completed,
                     lateness_seconds=(completed - job["due_at_ms"]) / 1000)
            if q["status"] in VALID_QUOTES and q["quote_at_ms"] <= reference.get("quote_at_ms", 0):
                q.update(status="unavailable", reason="quote_not_after_initial_reference")
            if q["status"] in VALID_QUOTES and reference.get("cost_model") and not job["due_at_ms"] <= q["quote_at_ms"] <= job["due_at_ms"] + MAX_LATENESS_MS:
                q.update(status="unavailable", reason="quote_outside_observation_window")
            if q["status"] in VALID_QUOTES and reference.get("status") in VALID_QUOTES:
                q.update(ask_to_bid_return_pct=(q["bid"] / reference["ask"] - 1) * 100,
                         midpoint_return_pct=(q["midpoint"] / reference["midpoint"] - 1) * 100,
                         ask_to_bid_pnl_per_contract=(q["bid"] - reference["ask"]) * contract["multiplier"],
                         reference_quote_at_ms=reference["quote_at_ms"])
            else:
                q["return_unavailable_reason"] = "initial_reference_unavailable" if reference.get("status") not in VALID_QUOTES else q.get("reason", "quote_unavailable")
            samples = TABLES[job["variant"]][1]
            conn.execute(samples.update().where(and_(samples.c.signal_id == job["signal_id"],
                samples.c.minute == job["minute"], samples.c.lease_token == lease)).values(
                status="complete", quote_json=canonical(q), lease_token=None, lease_until_ms=0))
    return True


def run_history_worker(engine, config, stop):
    with httpx.Client(timeout=2, follow_redirects=False) as client:
        provider = AlpacaQuotes(config, client)
        while not stop.is_set():
            try:
                worked = sample_batch(engine, provider)
            except Exception:
                worked = False
            stop.wait(0.2 if worked else 1)
