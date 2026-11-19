"""Push alerts for ICT futures detector trades.

Follows the magnet_push.py pattern: durable outbox, async Discord drainer.
Alerts fire when a detector opens a trade (via ict_paper.submit) or a
position closes (engine exits).

Paper silence (Josh, 2026-11-03 — "silence both paper surfaces"): the
paper book talked about itself on this channel ~50x/week while the
live book it shares the channel with produced ~4 messages. Paper
entries/exits now queue in 'suppressed' status — the row is still
written (the same enqueue-as-suppressed shape as the alerts
pipeline's scanner gate), but the drainer only ever selects
'pending', so a paper push can never post. The channel carries live
sends only. Booking and event recording upstream are untouched; the
live/paper distinction is the records' own flags ('live_send' at
booking, 'traderspost_sent' on the position), so a live survivor
push — fill-verdict footers included — is byte-identical to before.

Send-refusal alerts (2026-11-04): when the survivors send path
REFUSES a live entry (the no-naked-entry invariant — the payload
would carry no stop), maybe_queue_send_refusal queues a loud alert
on this channel. The trade was booked on paper but no live order
exists; that is a live-book event, not paper noise, so the payload
carries live=True and queues as 'pending' under the silence gate.

Enable with ICT_PUSH_ENABLED=true and DISCORD_ICT_WEBHOOK_URL.
"""
import asyncio
import re
import time

import httpx
from sqlalchemy import select

from .store import identity, ict_push_outbox, push_outbox

outbox = ict_push_outbox
MAX_ATTEMPTS = 10
WEBHOOK_RE = r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+'

# Fill-verdict exit footers (2026-11-03, Josh: "make exit alerts show
# confirmed vs unconfirmed"). A live SURVIVOR exit reads the trade's
# broker-verified fill verdicts (compass/fill_confirmation.py) at queue
# time and stamps the outcome as payload['fill_state']; the formatter
# swaps the generic LIVE footer for the matching rendering below.
# Verdicts are recorded from broker verification after the fact, so at
# alert time the exit leg's verdict normally does not exist yet —
# 'pending' is the expected common state and reads as "not yet
# verified", never as an alarm. The rejected / no-order states render
# only when a verdict affirmatively says so. Paper payloads and
# non-survivor payloads never carry 'fill_state' and render exactly
# as before.
FILL_FOOTERS = {
    'confirmed': '📊 **TRACKING** — closed · fill confirmed at broker',
    'pending': '📊 **TRACKING** — closed · fill not yet verified',
    'entry_rejected': '📊 **TRACKING** — entry rejected at broker · NOT FILLED',
    'entry_no_order': '📊 **TRACKING** — no broker order for entry · NOT FILLED',
    'exit_rejected': '📊 **TRACKING** — exit rejected at broker · close UNCONFIRMED',
    'exit_no_order': '📊 **TRACKING** — no broker order for exit · close UNCONFIRMED',
}

# Tracking footer (2026-11-19): Josh retired the live book — every
# book is tracking / measurement only, and TradersPost sends are
# gated OFF in the engine, so no alert may claim a trade is live or
# was sent anywhere. The send-set branch of the formatters renders
# this line instead of the old LIVE footers; the wording matches the
# shadow books' register (futures_sweep / stock_sweeps / mym_tom:
# "measurement only — no order was placed or sent anywhere").
TRACKING_FOOTER = ('📊 **TRACKING** — measurement only · '
                   'no order was placed or sent anywhere')


def _fill_state(db, c, trade):
    """Queue-time fill-verdict state for a live survivor exit.

    Returns one of the FILL_FOOTERS keys, or None when the trade is
    not a live survivor exit (the caller then leaves the payload
    untouched and the alert renders exactly as before). Consumes
    verdicts only — never records or alters them; any read error
    also returns None so an alert is never lost over a label."""
    if not trade.get('traderspost_sent'):
        return None
    if trade.get('source') != 'survivors':
        return None
    try:
        from . import fill_confirmation
        verdicts = fill_confirmation.get_verdicts(db, c, trade.get('id'))
        proj = fill_confirmation.derive_live(trade, verdicts)
    except Exception:
        return None
    status = proj.get('live_status')
    if status == 'confirmed_closed':
        return 'confirmed'
    if status == 'not_filled':
        return ('entry_no_order'
                if proj.get('fill_entry_status') == 'no_order'
                else 'entry_rejected')
    if status == 'exit_unconfirmed':
        return ('exit_no_order'
                if proj.get('fill_exit_status') == 'no_order'
                else 'exit_rejected')
    # 'pending' / 'pending_exit' (and any defensive remainder): sent,
    # but no complete verdict recorded yet.
    return 'pending'


def maybe_queue(db, c, cfg, trade):
    """Queue a Discord alert for a newly opened ICT trade.

    The payload's 'live' flag comes from trade['live_send'] (set at
    booking when the trade will be queued to TradersPost); the
    formatter labels live sends instead of calling them paper.
    
    Returns the outbox row id, or None if push is disabled.
    """
    if not bool(getattr(cfg, 'ict_push_enabled', False)):
        return None
    
    detector = trade.get('detector', 'unknown')
    symbol = trade.get('symbol', '?')
    side = trade.get('side', '?').upper()
    entry = trade.get('entry', 0)
    stop = trade.get('stop', 0)
    target = trade.get('target', 0)
    strategy = trade.get('strategy', detector)
    
    # Risk/reward
    risk = abs(entry - stop) if entry and stop else 0
    reward = abs(target - entry) if target and entry else 0
    rr = reward / risk if risk > 0 else 0
    
    # Time in CT for readability
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ct = ZoneInfo('America/Chicago')
    entry_time = datetime.fromtimestamp(trade.get('entered_at', time.time()), ct)
    time_str = entry_time.strftime('%a %H:%M CT')
    
    row_id = identity('ict-push-v1', trade.get('id', ''), int(time.time()))
    payload = {
        'detector': detector,
        'strategy': strategy,
        'symbol': symbol,
        'side': side,
        'entry': entry,
        'stop': stop,
        'target': target,
        'rr': round(rr, 2),
        'qty': trade.get('qty', 1),
        'time_str': time_str,
        'at': time.time(),
        # Live labeling (2026-10-31): set by the booking path when this
        # trade will be queued to TradersPost (survivors set live_send
        # at submit). Absent/False = paper, footer unchanged.
        'live': bool(trade.get('live_send')),
    }
    # Paper silence (2026-11-03): paper entries queue as 'suppressed'
    # and never drain; live entries queue as 'pending' exactly as
    # before. See the module docstring.
    status = 'pending' if payload['live'] else 'suppressed'
    if getattr(cfg, 'push_outbox_unified', False):
        c.execute(db.insert(push_outbox).values(
            id=row_id, channel='ict', status=status, created=time.time(),
            payload=payload, delivery={'attempts': 0})
            .on_conflict_do_nothing(index_elements=['id']))
    else:
        c.execute(db.insert(outbox).values(
            id=row_id, status=status, created=time.time(), payload=payload,
            delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return row_id


def _fmt_price(v):
    """Render a payload price for an alert body.

    Payloads carry the record's own values, and horizon trades
    (survivors S1/S3) have stop/target None BY CONSTRUCTION — the
    record legitimately has no levels. '%.2f' % None raised TypeError
    in the drainer: the alert never posted, the row stranded in
    'sending', and the ict_push health row latched at error
    (2026-11-04). None (or any non-numeric) renders as '—'; numeric
    values render exactly as '%.2f' always did."""
    if v is None:
        return '—'
    try:
        return '%.2f' % float(v)
    except (TypeError, ValueError):
        return '—'


def format_message(payload):
    """Discord-safe alert for an ICT entry (paper or tracking)."""
    side_emoji = '🟢' if payload.get('side') == 'LONG' else '🔴'
    # The 'live' payload flag marks the send-set books (survivors /
    # ICT live set). Since 2026-11-19 those books are tracking only
    # and sends are gated off — the footer says so, in the shadow
    # books' register. Nothing is queued to TradersPost anymore.
    footer = (TRACKING_FOOTER
              if payload.get('live') else '_Paper trade — watch only_')
    if payload.get('stop') is None and payload.get('target') is None:
        # Horizon trade: no bracket exists on the record, so there
        # are no levels to print — say what the trade actually is.
        levels = 'Entry `%s` · no stop/target — timed exit' % (
            _fmt_price(payload.get('entry')),)
    else:
        levels = 'Entry `%s` → Target `%s` | Stop `%s`' % (
            _fmt_price(payload.get('entry')),
            _fmt_price(payload.get('target')),
            _fmt_price(payload.get('stop')),
        )
    lines = [
        '%s **ICT %s** %s %s' % (
            side_emoji,
            payload.get('detector', 'unknown').replace('_', ' ').title(),
            payload.get('side', '?'),
            payload.get('symbol', '?')
        ),
        levels,
        'R:R `%.2f` · Qty `%d` · %s' % (
            payload.get('rr', 0),
            payload.get('qty', 1),
            payload.get('time_str', ''),
        ),
        footer,
    ]
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'ICT Signals',
            'allowed_mentions': {'parse': []}}


def _is_session_flatten(flatten_at):
    """True when a 'session_flatten' exit is the 15:45 CT session
    deadline rather than the position's own timed (horizon) exit.

    engine.exits records 'session_flatten' for whichever deadline
    fires; a position with no flatten_at of its own used the session
    deadline by construction (the exits() default), so None reads
    True. A flatten_at at any other CT time is the position's timed
    exit. Mirrors the label logic in alert_format.futures_message.
    """
    if not flatten_at:
        return True
    from datetime import datetime
    from zoneinfo import ZoneInfo
    t = datetime.fromtimestamp(flatten_at, ZoneInfo('America/Chicago'))
    return (t.hour, t.minute) == (15, 45)


def maybe_queue_exit(db, c, cfg, trade):
    """Queue a Discord alert for a closed ICT trade.

    The payload's 'live' flag comes from the position's recorded
    traderspost_sent, so a live position's exit is labeled live.
    Live survivor exits additionally carry 'fill_state' — the
    fill-verdict rendering state read at queue time (see _fill_state).

    Returns the outbox row id, or None if push is disabled.
    """
    if not bool(getattr(cfg, 'ict_push_enabled', False)):
        return None
    
    reason = trade.get('exit_reason', 'unknown')
    pnl = trade.get('pnl', 0)

    # Map exit reasons to display. 'session_flatten' is recorded for
    # ANY flatten_at deadline — the 15:45 CT session end AND a
    # position's own timed (horizon) exit — so the label splits on
    # the position's own deadline: a timed exit must not read as the
    # session flatten (2026-11-17: an S3 exit ~16 min after entry was
    # labeled 'Session flatten (15:45 CT)').
    if 'target' in reason:
        emoji, result = '🎯', 'TARGET HIT'
    elif 'stop' in reason:
        emoji, result = '🛑', 'STOPPED OUT'
    elif reason == 'session_flatten' and not _is_session_flatten(
            trade.get('flatten_at')):
        emoji, result = '⏰', 'TIMED EXIT'
    elif 'flatten' in reason:
        emoji, result = '⏰', 'FLATTENED'
    else:
        emoji, result = '📊', reason.upper()
    
    pnl_str = '+$%.2f' % pnl if pnl >= 0 else '-$%.2f' % abs(pnl)
    
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ct = ZoneInfo('America/Chicago')
    exit_time = datetime.fromtimestamp(trade.get('exited_at', time.time()), ct)
    time_str = exit_time.strftime('%a %H:%M CT')
    
    row_id = identity('ict-push-v1', 'exit', trade.get('id', ''), int(time.time()))
    payload = {
        'type': 'exit',
        'detector': trade.get('detector', 'unknown'),
        'strategy': trade.get('strategy', ''),
        'symbol': trade.get('symbol', '?'),
        'side': trade.get('side', '?').upper(),
        'entry': trade.get('entry', 0),
        'exit': trade.get('exit', 0),
        'pnl': pnl,
        'pnl_str': pnl_str,
        'result': result,
        'emoji': emoji,
        'time_str': time_str,
        'at': time.time(),
        # Live labeling (2026-10-31): keyed off the position's own
        # recorded traderspost_sent — the same field the engine's
        # TradersPost close-send gates read. Absent/False = paper.
        'live': bool(trade.get('traderspost_sent')),
    }
    # Fill-verdict label (2026-11-03): live survivor exits only;
    # stamped from the verdicts recorded at queue time. Absent for
    # paper and non-survivor exits, which render exactly as before.
    fill_state = _fill_state(db, c, trade)
    if fill_state:
        payload['fill_state'] = fill_state
    # Paper silence (2026-11-03): same gate as entries, keyed on the
    # payload's 'live' flag (the position's recorded traderspost_sent).
    # Paper exits queue as 'suppressed' and never drain; live exits —
    # fill-verdict footers included — queue exactly as before.
    status = 'pending' if payload['live'] else 'suppressed'
    if getattr(cfg, 'push_outbox_unified', False):
        c.execute(db.insert(push_outbox).values(
            id=row_id, channel='ict', status=status, created=time.time(),
            payload=payload, delivery={'attempts': 0})
            .on_conflict_do_nothing(index_elements=['id']))
    else:
        c.execute(db.insert(outbox).values(
            id=row_id, status=status, created=time.time(), payload=payload,
            delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return row_id


def maybe_queue_send_refusal(db, c, cfg, trade, reason):
    """Queue a LOUD alert for a REFUSED live send (2026-11-04).

    The survivors send path refused this trade's live entry (the
    no-naked-entry invariant: its payload carried no stop). The
    paper booking stands; no live order was placed. The payload is
    stamped live=True on purpose — it reports on the LIVE book — so
    under the paper-silence gate it queues as 'pending' and drains
    like any live alert. Returns the outbox row id, or None if push
    is disabled."""
    if not bool(getattr(cfg, 'ict_push_enabled', False)):
        return None

    from datetime import datetime
    from zoneinfo import ZoneInfo
    ct = ZoneInfo('America/Chicago')
    at = time.time()
    entry_time = datetime.fromtimestamp(
        trade.get('entered_at', at), ct)
    time_str = entry_time.strftime('%a %H:%M CT')

    row_id = identity('ict-push-v1', 'refusal', trade.get('id', ''),
                      int(at))
    payload = {
        'type': 'send_refusal',
        'detector': trade.get('detector', 'unknown'),
        'strategy': trade.get('strategy', ''),
        'symbol': trade.get('symbol', '?'),
        'side': trade.get('side', '?').upper(),
        'entry': trade.get('signal_price') or trade.get('entry', 0),
        'reason': reason,
        'time_str': time_str,
        'at': at,
        'live': True,
    }
    status = 'pending' if payload['live'] else 'suppressed'
    if getattr(cfg, 'push_outbox_unified', False):
        c.execute(db.insert(push_outbox).values(
            id=row_id, channel='ict', status=status, created=time.time(),
            payload=payload, delivery={'attempts': 0})
            .on_conflict_do_nothing(index_elements=['id']))
    else:
        c.execute(db.insert(outbox).values(
            id=row_id, status=status, created=time.time(), payload=payload,
            delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return row_id


def format_refusal_message(payload):
    """Discord-safe alert for a refused send (loud by design)."""
    lines = [
        '🚨 **SEND REFUSED** — ICT %s %s %s' % (
            payload.get('detector', 'unknown').replace('_', ' ').title(),
            payload.get('side', '?'),
            payload.get('symbol', '?')
        ),
        'Entry `%.2f` was NOT sent to TradersPost — %s' % (
            payload.get('entry', 0),
            payload.get('reason', 'unknown'),
        ),
        'Tracking position booked; no order placed. · %s' % (
            payload.get('time_str', ''),
        ),
        '📊 **TRACKING** — refused by the send-path safety invariant',
    ]
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'ICT Signals',
            'allowed_mentions': {'parse': []}}


def format_exit_message(payload):
    """Discord-safe alert for an ICT exit (paper or tracking)."""
    footer = (TRACKING_FOOTER
              if payload.get('live') else '_Paper trade — watch only_')
    # Fill-verdict label (2026-11-03): a send-set survivor exit queued
    # with a fill_state renders its verdict footer instead of the
    # generic tracking one. Rows queued before this change carry no
    # fill_state and keep the generic footer.
    if payload.get('live') and payload.get('fill_state') in FILL_FOOTERS:
        footer = FILL_FOOTERS[payload['fill_state']]
    lines = [
        '%s **ICT %s** %s %s — %s' % (
            payload.get('emoji', '📊'),
            payload.get('detector', 'unknown').replace('_', ' ').title(),
            payload.get('side', '?'),
            payload.get('symbol', '?'),
            payload.get('result', '?'),
        ),
        'Entry `%s` → Exit `%s`' % (
            _fmt_price(payload.get('entry')),
            _fmt_price(payload.get('exit')),
        ),
        'P&L **%s** · %s' % (
            payload.get('pnl_str', '$0.00'),
            payload.get('time_str', ''),
        ),
        footer,
    ]
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'ICT Signals',
            'allowed_mentions': {'parse': []}}


def deliver_one(db, client, url, now=None, max_attempts=MAX_ATTEMPTS,
                table=None, channel=None):
    """Attempt one pending row. Returns True if a row was attempted.

    table/channel (bundle 3): unified-drainer mode reads/updates
    push_outbox rows for `channel`; delivery logic unchanged. Note this
    module's selection intentionally stays pending-only with no lease
    or next_attempt check, exactly as the legacy path behaves.
    """
    box = table if table is not None else outbox
    if not re.fullmatch(WEBHOOK_RE, url or ''):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        q = (select(box).where(box.c.status == 'pending')
             .order_by(box.c.created).limit(1))
        if channel is not None:
            q = q.where(box.c.channel == channel)
        row = c.execute(q).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(box.update().where(box.c.id == row['id']).values(
            status='sending', delivery=d))
        job = dict(row, delivery=d)
    
    status = 'failed'
    try:
        _ptype = job['payload'].get('type')
        formatter = (format_exit_message if _ptype == 'exit'
                     else format_refusal_message
                     if _ptype == 'send_refusal'
                     else format_message)
        resp = client.post(url, params={'wait': 'true'}, json=formatter(job['payload']))
        if 200 <= resp.status_code < 300:
            status = 'delivered'
        elif resp.status_code == 429:
            status = 'pending'
            try:
                delay = max(1, min(3600, float(resp.json().get('retry_after', 30))))
            except:
                delay = 30
            job['delivery']['next_attempt'] = now + delay
        elif resp.status_code >= 500:
            status = 'pending'
            job['delivery']['next_attempt'] = now + 30
    except httpx.HTTPError:
        status = 'pending'
        job['delivery']['next_attempt'] = now + 30
    except Exception as e:
        # Any other local failure (a payload the formatter cannot
        # render is the known case — the horizon-entry TypeError of
        # 2026-11-04) must not strand the row in 'sending' with the
        # alert silently lost while the drainer latches its health
        # row at error. Fail the row with the cause in its delivery
        # record; the final status update below still runs and the
        # loop moves on to the next row. `status` stays 'failed'.
        job['delivery']['error'] = '%s: %s' % (
            type(e).__name__, str(e)[:200])

    if status == 'pending' and job['delivery'].get('attempts', 0) >= max_attempts:
        status = 'failed'
    
    with db.tx() as c:
        c.execute(box.update().where(box.c.id == job['id']).values(
            status=status, delivery=job['delivery']))
    return True


async def run(db, cfg):
    """Drainer loop: POST pending ICT alerts to Discord."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if bool(getattr(cfg, 'ict_push_enabled', False)):
                    await asyncio.to_thread(deliver_one, db, client, cfg.ict_push_webhook)
            except Exception as e:
                db.health('ict_push', 'error', type(e).__name__)
            await asyncio.sleep(1)
