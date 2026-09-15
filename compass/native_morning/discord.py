import json
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, case, or_, select

from .store import now_ms, outbox
from .options import PENDING_TEXT, enqueue_option


def signal_message(signal, received, option_policy=None):
    s = signal.model_dump() if hasattr(signal, "model_dump") else signal
    stamp = datetime.fromtimestamp(s["signal_at_ms"] / 1000, timezone.utc).astimezone(ZoneInfo("America/Chicago"))
    age = max(0, (received - s["signal_at_ms"]) / 1000)
    rvol = s["features"]["rvol"]
    late = f"\nDelayed receipt: {age:.0f} seconds after signal." if age > 30 else ""
    rvol_text = f"{rvol:.2f}x" if rvol is not None else "unavailable"
    event = "TEST — NO TRADE" if s.get("is_test") else "SIGNAL"
    setup = {"STANDARD_ORB": "Opening-range breakout", "FAST_OPEN": "Fast-open breakout"}.get(s['setup'], s['setup'].replace('_', ' '))
    content = (
        f"**MORNING ALGO | {event} | {s['ticker']} · CALL BIAS**\n"
        f"Signal only · Morning intraday\n"
        f"Setup: {setup} · {s['timeframe_min']}m confirmation\n"
        f"Stock reference: ${s['price']:.4f} | Local RVOL: {rvol_text}\n"
        f"Signal time: {stamp:%Y-%m-%d %H:%M:%S} CT\n"
        f"Source: TradingView · {s['logic_mode'].replace('_', ' ')} · v{s['script_version']}\n"
        f"Ref: {s['signal_id']}"
    )
    content += "\nConfirmed stock signal; price is a reference, not a fill." + late
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if option_policy and not s.get("is_test"):
        payload["content"] += "\n" + PENDING_TEXT
        payload["_option_policy"] = option_policy
    return payload

