"""Flow Pulse: standalone unusual institutional options flow, no setup trial required.

On 2026-09-30/10-01, heavy call buying positioned ahead of the HOOD (+6.9%)
and COIN (+5.6%) rips, but Compass surfaced nothing: tape-confirmed can only
annotate setup trials the scanner already fired, and its +/-15min join window
missed the prints by 30-60 minutes. Flow Pulse fires directly on concentrated
directional flow per symbol.

Direction uses vendor sentiment x option type (bullish = bullish calls +
bearish puts; bearish = bearish calls + bullish puts). This deliberately
differs from tape_confirmed, which is sentiment-blind.

Evidence only: dashboard panel + API + logged events. Never trades, never
alerts. The Discord push path is built but gated behind FLOW_PULSE_PUSH_ENABLED
(default false); Josh arms it himself.
"""
import asyncio
import re
import time
from datetime import datetime, timezone
from sqlalchemy import select, func

from .market import number, day
from .store import flow_records, identity, flow_pulse_outbox

outbox = flow_pulse_outbox

VERSION = 'flow-pulse-v1'
SCAN_THROTTLE = 60
WINDOW = 1800          # rolling window, seconds
LOOKBACK = 7200        # flow rows considered, seconds
MAX_ATTEMPTS = 10
WEBHOOK_RE = r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+'


def _direction(option_type, sentiment):
    """Sentiment-aware direction. Unknown sentiment -> None (ignored)."""
    t = str(option_type or '').lower()
    s = str(sentiment or '').lower()
    otype = 'call' if t in ('call', 'c') else 'put' if t in ('put', 'p') else None
    if otype is None or s not in ('bullish', 'bearish'):
        return None
    if (otype == 'call' and s == 'bullish') or (otype == 'put' and s == 'bearish'):
        return 'bullish'
    return 'bearish'


def _dte(expiry, now):
    """Days to expiry from vendor 'MM/DD/YY' or 'MM/DD/YYYY'. None if unparsable."""
    if not expiry:
        return None
    try:
        parts = str(expiry).strip().split('/')
        if len(parts) != 3:
            return None
        m, d, y = (int(p) for p in parts)
        if y < 100:
            y += 2000
        exp = datetime(y, m, d, 16, 0, tzinfo=timezone.utc).timestamp()
        return (exp - now) / 86400.0
    except (ValueError, OverflowError):
        return None


def detect_pulses(rows, now, cfg, universe):
    """Pure function: aggregate flow rows into pulses. Never touches the DB.

    rows: iterable of flow_records row dicts (with 'payload').
    Returns list of pulse dicts, one per (symbol, direction) meeting all gates.
    """
    min_premium = number(getattr(cfg, 'flow_pulse_min_premium', 1000000)) or 1000000
    min_score = number(getattr(cfg, 'flow_pulse_min_score', 90)) or 90
    min_ratio = number(getattr(cfg, 'flow_pulse_min_ratio', 2.0)) or 2.0
    max_dte = number(getattr(cfg, 'flow_pulse_max_dte', 45)) or 45
    allowed = set(universe or ())

    buckets = {}
    for row in rows or []:
        p = row.get('payload') if isinstance(row, dict) else None
        if not isinstance(p, dict):
            continue
        symbol = str(p.get('symbol') or '').upper()
        if not symbol or (allowed and symbol not in allowed):
            continue
        stamp = number(p.get('source_ts'))
        if stamp is None or stamp < now - WINDOW or stamp > now:
            continue
        dte = _dte(p.get('expiry'), now)
        if dte is None or dte <= 0 or dte > max_dte:
            continue
        direction = _direction(p.get('option_type'), p.get('sentiment'))
        if direction is None:
            continue
        premium = number(p.get('premium')) or 0
        if premium <= 0:
            continue
        b = buckets.setdefault(symbol, {'bullish': 0.0, 'bearish': 0.0,
                                        'prints': [], 'window_start': stamp,
                                        'window_end': stamp})
        b[direction] += premium
        b['prints'].append({
            'dir': direction,
            'strike': number(p.get('strike')),
            'expiry': p.get('expiry'),
            'option_type': p.get('option_type'),
            'sentiment': p.get('sentiment'),
            'premium': premium,
            'score': number(p.get('score')),
            'source_ts': stamp,
        })
        b['window_start'] = min(b['window_start'], stamp)
        b['window_end'] = max(b['window_end'], stamp)

    pulses = []
    for symbol, b in buckets.items():
        for direction in ('bullish', 'bearish'):
            directional = b[direction]
            opposing = b['bearish'] if direction == 'bullish' else b['bullish']
            if directional < min_premium:
                continue
            if opposing > 0 and directional < min_ratio * opposing:
                continue
            prints = [x for x in b['prints'] if x['dir'] == direction]
            scores = [x['score'] for x in prints if x['score'] is not None]
            max_score = max(scores) if scores else None
            if max_score is None or max_score < min_score:
                continue
            top = sorted(prints, key=lambda x: x['premium'], reverse=True)[:5]
            pulses.append({
                'symbol': symbol, 'direction': direction,
                'window_start': b['window_start'], 'window_end': b['window_end'],
                'directional_premium': directional, 'opposing_premium': opposing,
                'print_count': len(prints), 'max_score': max_score,
                'top_prints': [{k: v for k, v in t.items() if k != 'dir'}
                               for t in top],
                'first_seen': now, 'version': VERSION,
            })
    pulses.sort(key=lambda x: x['directional_premium'], reverse=True)
    return pulses


def _flow_rows(c, now):
    rows = c.execute(select(flow_records).where(
        flow_records.c.day == day(now),
        flow_records.c.source_ts >= now - LOOKBACK,
        flow_records.c.source_ts <= now,
        flow_records.c.first_seen <= now,
        flow_records.c.last_seen <= now,
    ).order_by(flow_records.c.source_ts.desc()).limit(5001)).mappings().all()
    return [dict(r) for r in rows]


def scan(db, c, cfg, now):
    """Throttled pass: detect pulses, persist new ones, optionally queue push."""
    if not getattr(cfg, 'flow_pulse', True):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'flow_pulse:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    rows = _flow_rows(c, now)
    pulses = detect_pulses(rows, now, cfg, getattr(cfg, 'watch_symbols', ()))
    today = day(now)
    fired, pushed = [], []
    for pulse in pulses:
        key = 'flowpulse:%s:%s:%s' % (today, pulse['symbol'], pulse['direction'])
        is_new = db.append(c, 'flow_pulse', 'flow_pulse', pulse['symbol'], now,
                           pulse, key=key)
        if is_new:
            fired.append(pulse)
            if _maybe_queue_push(db, c, cfg, pulse, today, now):
                pushed.append(pulse['symbol'])
    db.put(c, 'flow_pulse:scanned_at', now)
    db.put(c, 'flow_pulse:latest', {'at': now, 'pulses': pulses,
                                    'fired_today': len(fired)})
    return {'ran': True, 'pulses': len(pulses), 'fired': len(fired),
            'pushed': pushed}


def _maybe_queue_push(db, c, cfg, pulse, today, now):
    """Write one outbox row per new pulse. Gated; default off."""
    if not getattr(cfg, 'flow_pulse_push_enabled', False):
        return False
    webhook = getattr(cfg, 'flow_pulse_webhook', '')
    if not re.fullmatch(WEBHOOK_RE, webhook or ''):
        return False
    row_id = identity('flow-pulse-push-v1', today, pulse['symbol'],
                      pulse['direction'], pulse['window_end'])
    payload = {'kind': 'flow_pulse', 'symbol': pulse['symbol'],
               'direction': pulse['direction'], 'session': today,
               'directional_premium': pulse['directional_premium'],
               'opposing_premium': pulse['opposing_premium'],
               'print_count': pulse['print_count'],
               'max_score': pulse['max_score'],
               'top_prints': pulse['top_prints'],
               'window_start': pulse['window_start'],
               'window_end': pulse['window_end'], 'at': now}
    c.execute(db.insert(outbox).values(
        id=row_id, status='pending', created=now, payload=payload,
        delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return True


def format_message(payload):
    """Discord-safe message for one pulse. No mentions, <2000 chars."""
    arrow = 'BULLISH' if payload.get('direction') == 'bullish' else 'BEARISH'
    prem = payload.get('directional_premium') or 0
    lines = [
        '**%s** flow pulse **%s**' % (payload.get('symbol'), arrow),
        '$%.1fM directional premium | %d prints | max score %.0f' % (
            prem / 1e6, payload.get('print_count') or 0,
            payload.get('max_score') or 0),
    ]
    for t in (payload.get('top_prints') or [])[:3]:
        lines.append('%s %s %s $%.0fk (score %.0f)' % (
            t.get('option_type') or '?', t.get('strike'), t.get('expiry'),
            (t.get('premium') or 0) / 1000, t.get('score') or 0))
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'Market Compass',
            'allowed_mentions': {'parse': []}}


def deliver_one(db, client, url, now=None, max_attempts=MAX_ATTEMPTS):
    """Attempt one pending row. Returns True if a row was attempted."""
    if not re.fullmatch(WEBHOOK_RE, url or ''):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        candidates = c.execute(select(outbox)
            .where(outbox.c.status.in_(['pending', 'sending']))
            .order_by(outbox.c.created).limit(20)).mappings().all()
        job = None
        for row in candidates:
            d = dict(row['delivery'])
            if row['status'] == 'sending' and d.get('lease_until', 0) >= now:
                continue
            if d.get('next_attempt', 0) > now:
                continue
            d.update(attempts=d.get('attempts', 0) + 1, lease_until=now + 30)
            c.execute(outbox.update().where(outbox.c.id == row['id'])
                      .values(status='sending', delivery=d))
            job = dict(row, delivery=d)
            break
    if job is None:
        return False
    status, error, message_id = 'ambiguous', None, None
    try:
        response = client.post(url, params={'wait': 'true'},
                               json=format_message(job['payload']))
        if 200 <= response.status_code < 300:
            try:
                message_id = response.json().get('id')
            except (ValueError, AttributeError):
                pass
            if isinstance(message_id, str) and re.fullmatch(r'[0-9]{1,32}', message_id):
                status = 'delivered'
            else:
                error = 'missing_discord_receipt'
        elif response.status_code == 429:
            status, error = 'pending', 'rate_limited'
            try:
                delay = max(1, min(3600, float(response.json().get('retry_after', 30))))
            except (ValueError, TypeError, AttributeError):
                delay = 30
            job['delivery']['next_attempt'] = now + delay
        elif response.status_code >= 500:
            status, error = 'pending', 'server_error'
            job['delivery']['next_attempt'] = now + 30
        else:
            status, error = 'failed', 'http_%d' % response.status_code
    except Exception:
        error = 'network_outcome_unknown'
    if job['delivery']['attempts'] >= max_attempts and status == 'pending':
        status, error = 'failed', 'retry_budget_exhausted'
    with db.tx() as c:
        d = dict(job['delivery'])
        d.update(error=error, message_id=message_id, finished_at=now, lease_until=0)
        c.execute(outbox.update().where(outbox.c.id == job['id'])
                  .values(status=status, delivery=d))
    return True


async def run(db, cfg):
    """Drainer: 1s poll, 429 backoff, retry budget. Rows accumulate if down."""
    import httpx
    url = getattr(cfg, 'flow_pulse_webhook', '')
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if getattr(cfg, 'flow_pulse_push_enabled', False):
                    deliver_one(db, client, url)
            except Exception:
                db.health('flow_pulse_push', 'error', 'drainer exception')
            await asyncio.sleep(1)


def display(db, c, now, days=7):
    """Recent pulses for the dashboard panel."""
    since = now - days * 86400
    events = db.recent(c, 'flow_pulse', limit=200)
    pulses = [dict(e.get('payload') or {}, ts=e.get('ts'))
              for e in events if (e.get('ts') or 0) >= since]
    pulses.sort(key=lambda x: x.get('ts') or 0, reverse=True)
    latest = db.get(c, 'flow_pulse:latest', {})
    return {'asof': now, 'version': VERSION, 'pulses': pulses[:50],
            'last_scan': latest.get('at'),
            'note': 'Evidence only. No auto-admission, no alerts, no trades.'}
