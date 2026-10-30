"""Unified push outbox drainer (code-health bundle 3, 2026-10-30).

Behind Config.push_outbox_unified (env PUSH_OUTBOX_UNIFIED, default
off). When the flag is on, the five push producers write push_outbox_v1
rows tagged with a channel, and this single drainer replaces the five
per-module run() loops: each cycle dispatches to the channel's OWN
deliver_one(db, client, url, table=push_outbox, channel=name), so
per-channel delivery semantics — Discord-receipt rules, lease and
backoff shapes, formatter quirks (ICT's exit formatter, gap/board's
header-based 429 and no-wait POST) — are preserved by construction,
not reimplemented. Gates and webhook resolution below are copied
verbatim from each module's run().

Flag off (the default and the shipped state): app.py spawns the five
legacy loops, producers write their legacy tables, and nothing reads
or writes push_outbox_v1. Flip steps and rollback are in
MORNING-REPORT.md (code-health 2026-10-29).
"""
import asyncio
import logging

import httpx

from . import (day_trading_board, flow_pulse, gap_continuation,
               ict_push, magnet_push)
from .store import push_outbox

# (channel, module, gate(cfg) or None for always-on, url(cfg), health)
CHANNELS = (
    ('magnet', magnet_push, None,
     lambda cfg: (cfg.discord_routes or {}).get('magnets', ''),
     'magnet_push'),
    ('ict', ict_push,
     lambda cfg: bool(getattr(cfg, 'ict_push_enabled', False)),
     lambda cfg: cfg.ict_push_webhook,
     'ict_push'),
    ('flow_pulse', flow_pulse,
     lambda cfg: bool(getattr(cfg, 'flow_pulse_push_enabled', False)),
     lambda cfg: getattr(cfg, 'flow_pulse_webhook', ''),
     'flow_pulse_push'),
    ('gap', gap_continuation,
     lambda cfg: bool(getattr(cfg, 'gap_push_enabled', True)),
     lambda cfg: (getattr(cfg, 'discord_routes', {}) or {}).get('intraday', ''),
     'gap_push'),
    ('day_board', day_trading_board,
     lambda cfg: bool(getattr(cfg, 'day_board_push_enabled', True)),
     lambda cfg: (getattr(cfg, 'discord_routes', {}) or {}).get('spy_morning', ''),
     'day_board_push'),
)


async def run(db, cfg):
    """One drainer for all channels: 1s cycle, per-channel dispatch."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            for name, module, gate, url_of, health in CHANNELS:
                try:
                    if gate is not None and not gate(cfg):
                        continue
                    await asyncio.to_thread(
                        module.deliver_one, db, client, url_of(cfg),
                        table=push_outbox, channel=name)
                except Exception as e:
                    try:
                        db.health(health, 'error', type(e).__name__)
                    except Exception:
                        logging.getLogger('uvicorn.error').exception(
                            '%s health write failed', health)
            await asyncio.sleep(1)
