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


def _prints_target_date(top_prints):
    """Most common expiry among the top prints, as a date. None if unparseable."""
    from collections import Counter
    dates = []
    for t in top_prints or []:
        try:
            dates.append(datetime.strptime(t['expiry'], '%m/%d/%y').date())
        except (ValueError, TypeError, KeyError):
            continue
    if not dates:
        return None
    return Counter(dates).most_common(1)[0][0]


def suggest_contract(db, c, cfg, pulse, now):
    """Pick a liquid directional contract expressing the pulse.

    Bearish -> put, bullish -> call, expiry matched to the flow prints'
    expiry. Returns an eligible_contracts dict or None when there is no
    fresh chain, no spot quote, or no eligible contract. Never raises:
    a missing suggestion leaves the alert unchanged.
    """
    try:
        from .option_ideas import eligible_contracts
    except Exception:
        return None
    try:
        symbol = pulse['symbol']
        side = 'short' if pulse['direction'] == 'bearish' else 'long'
        target_date = _prints_target_date(pulse.get('top_prints'))
        if target_date is None:
            return None
        target_dte = (target_date - datetime.fromtimestamp(now, timezone.utc).date()).days
        if target_dte <= 0:
            return None
        q = db.get(c, 'quote:' + symbol) or {}
        bid, ask = number(q.get('bid')), number(q.get('ask'))
        qts = number(q.get('ts'))
        if bid is None or ask is None:
            return None
        if qts is None or qts > now or now - qts > 300:
            # Never suggest a strike off a stale spot quote.
            return None
        spot = (bid + ask) / 2
        chain = db.get(c, 'chain:' + symbol) or {}
        from types import SimpleNamespace
        pick_cfg = SimpleNamespace(ideas_min_dte=0, ideas_max_dte=90,
                                   ideas_target_dte=target_dte)
        candidates = eligible_contracts(chain, symbol, side, spot, now, pick_cfg)
        if not candidates:
            return None
        iso = target_date.isoformat()
        dated = [o for o in candidates if o.get('expiry') == iso]
        return (dated or candidates)[0]
    except Exception:
        return None


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
    # Suggest a concrete contract per pulse so the alert names what to trade.
    # Missing chain/spot/eligibility -> None; the alert is unchanged.
    for pulse in pulses:
        pulse['suggested_contract'] = suggest_contract(db, c, cfg, pulse, now)
    # Dark pool confirmation: same-direction institutional block activity.
    # Evidence only — context on the pulse, never a standalone trigger.
    if getattr(cfg, 'darkpool', True):
        try:
            from .darkpool import confirmation_for
            for pulse in pulses:
                pulse['darkpool'] = confirmation_for(
                    pulse['symbol'], pulse['direction'], db, c, now)
        except Exception:
            pass
    today = day(now)
    fired, pushed = [], []
    for pulse in pulses:
        key = 'flowpulse:%s:%s:%s' % (today, pulse['symbol'], pulse['direction'])
        is_new = db.append(c, 'flow_pulse', 'flow_pulse', pulse['symbol'], now,
                           pulse, key=key)
        if is_new:
            fired.append(pulse)
            if getattr(cfg, 'flow_pulse_track_enabled', True):
                try:
                    open_tracking(db, c, pulse, key, now)
                except Exception:
                    pass
            if _maybe_queue_push(db, c, cfg, pulse, today, now):
                pushed.append(pulse['symbol'])
    db.put(c, 'flow_pulse:scanned_at', now)
    db.put(c, 'flow_pulse:latest', {'at': now, 'pulses': pulses,
                                    'fired_today': len(fired)})
    try:
        update_tracking_peaks(db, c, now)
        evaluate_tracking(db, c, cfg, now)
    except Exception:
        pass
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
               'suggested_contract': pulse.get('suggested_contract'),
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
    dp = payload.get('darkpool') or {}
    if dp.get('confirmed'):
        lines.append('Dark pool: %s$%.1fM %s block%s' % (
            'SAME-DIRECTION ' if dp.get('strong') else '',
            (dp.get('notional') or 0) / 1e6,
            dp.get('print_count') or 0,
            's' if (dp.get('print_count') or 0) != 1 else ''))
    sug = payload.get('suggested_contract') or {}
    if sug.get('symbol'):
        try:
            exp = datetime.strptime(sug['expiry'], '%Y-%m-%d').strftime('%-m/%-d')
        except (ValueError, TypeError):
            exp = sug.get('expiry') or '?'
        delta = sug.get('delta')
        dstr = ' Δ %.2f' % abs(delta) if isinstance(delta, (int, float)) else ''
        lines.append('Suggested: %s %s %g %s%s — research only, pick your fill' % (
            sug.get('underlying'), exp, sug.get('strike'),
            str(sug.get('type') or '').upper(), dstr))
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
            'track_record': track_record(db, c),
            'note': 'Evidence only. No auto-admission, no alerts, no trades.'}


# ---------------------------------------------------------------------------
# Paper tracking: every pulse that fires with a suggested contract is
# paper-tracked for two weeks so the signal can prove (or disprove) itself.
# One contract, entry at the suggestion's ask + $0.01, exit at the bid -
# $0.01, $0.65/side fees — the house paper convention. The exit is purely
# time-based (no stop/target): the scorecard measures the signal, not an
# exit strategy. The suggested contract is always a long option (call for
# bullish pulses, put for bearish), so P&L is long-option P&L.
# ---------------------------------------------------------------------------
TRACK_HORIZON = 14 * 86400      # two weeks, calendar days
TRACK_SLIPPAGE = 0.01
TRACK_FEE_SIDE = 0.65
TRACK_EVAL_THROTTLE = 3600


def _chain_quote(chain, occ_symbol):
    for o in chain.get('contracts', []):
        if str(o.get('symbol')) == occ_symbol:
            q = o.get('quote') or {}
            return number(q.get('bid')), number(q.get('ask'))
    return None, None


def open_tracking(db, c, pulse, key, now):
    """Paper-track one fired pulse. Returns the track key or None."""
    sug = pulse.get('suggested_contract') or {}
    occ = sug.get('symbol')
    if not occ:
        return None
    chain = db.get(c, 'chain:' + pulse['symbol']) or {}
    _, ask = _chain_quote(chain, occ)
    if ask is None or ask <= 0:
        return None
    q = db.get(c, 'quote:' + pulse['symbol']) or {}
    spot = None
    if q.get('bid') and q.get('ask'):
        spot = round((q['bid'] + q['ask']) / 2, 2)
    rec = {
        'key': key, 'symbol': pulse['symbol'], 'direction': pulse['direction'],
        'fired_at': now, 'spot_at_fire': spot,
        'contract': occ, 'strike': sug.get('strike'), 'expiry': sug.get('expiry'),
        'type': sug.get('type'), 'delta': sug.get('delta'),
        'entry': round(ask + TRACK_SLIPPAGE, 2), 'qty': 1, 'multiplier': 100,
        'horizon_at': now + TRACK_HORIZON, 'status': 'open',
        # High-water mark: the best exit seen during the hold. Updated on
        # every scan from the latest chain snapshot, so the scorecard can
        # show "it was up X% at some point" even when the 2-week close is
        # flat or red — flow-driven spikes often fade before the horizon.
        'peak_bid': None, 'peak_at': None,
    }
    db.put(c, 'flow_pulse_track:' + key, rec)
    return key


def update_tracking_peaks(db, c, now):
    """Refresh the high-water mark on open tracks. Cheap: runs every scan."""
    for tkey, rec in db.prefix(c, 'flow_pulse_track:').items():
        if not isinstance(rec, dict) or rec.get('status') != 'open':
            continue
        try:
            chain = db.get(c, 'chain:' + rec['symbol']) or {}
            bid, _ = _chain_quote(chain, rec['contract'])
            if bid is None or bid <= 0:
                continue
            if rec.get('peak_bid') is None or bid > rec['peak_bid']:
                rec['peak_bid'] = bid
                rec['peak_at'] = now
                db.put(c, tkey, rec)
        except Exception:
            continue


def evaluate_tracking(db, c, cfg, now):
    """Close out tracks past their horizon. Throttled to hourly."""
    if not getattr(cfg, 'flow_pulse_track_enabled', True):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'flow_pulse:track_eval_at', 0) < TRACK_EVAL_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    db.put(c, 'flow_pulse:track_eval_at', now)
    opened = closed = 0
    for tkey, rec in db.prefix(c, 'flow_pulse_track:').items():
        if not isinstance(rec, dict) or rec.get('status') != 'open':
            continue
        opened += 1
        if now < rec.get('horizon_at', 0):
            continue
        chain = db.get(c, 'chain:' + rec['symbol']) or {}
        bid, _ = _chain_quote(chain, rec['contract'])
        if bid is None or bid <= 0:
            # No exit quote yet; retry on the next pass. If the contract
            # has expired, mark unresolved instead of hanging forever.
            try:
                exp = datetime.fromisoformat(rec['expiry']).date()
                if datetime.fromtimestamp(now, timezone.utc).date() > exp:
                    rec['status'] = 'unresolved'
                    rec['note'] = 'no exit quote after expiry'
                    db.put(c, tkey, rec)
                    closed += 1
            except (ValueError, TypeError):
                pass
            continue
        exit_px = round(bid - TRACK_SLIPPAGE, 2)
        qty = rec.get('qty', 1)
        gross = (exit_px - rec['entry']) * 100 * qty
        net = round(gross - 2 * TRACK_FEE_SIDE, 2)
        cost = rec['entry'] * 100 * qty
        # What the trade looked like at its best: peak bid marked the same
        # way as an exit (bid - slippage, minus round-trip fees).
        peak_bid = rec.get('peak_bid')
        peak_pnl = (round((peak_bid - TRACK_SLIPPAGE - rec['entry']) * 100 * qty
                          - 2 * TRACK_FEE_SIDE, 2)
                    if peak_bid else None)
        rec.update(status='closed', exit=exit_px, exited_at=now, pnl=net,
                   return_pct=round(100 * net / cost, 2) if cost else None,
                   outcome='win' if net > 0 else 'loss',
                   peak_pnl=peak_pnl,
                   peak_return_pct=(round(100 * peak_pnl / cost, 2)
                                    if peak_pnl is not None and cost else None))
        db.put(c, tkey, rec)
        closed += 1
    return {'ran': True, 'open': opened, 'closed': closed}


def track_record(db, c):
    """Aggregate scorecard over closed tracks."""
    recs = [r for r in db.prefix(c, 'flow_pulse_track:').values()
            if isinstance(r, dict)]
    closed = [r for r in recs if r.get('status') == 'closed']
    open_n = sum(1 for r in recs if r.get('status') == 'open')
    wins = [r for r in closed if r.get('outcome') == 'win']
    rets = [r['return_pct'] for r in closed if r.get('return_pct') is not None]
    peak_rets = [r['peak_return_pct'] for r in closed
                 if r.get('peak_return_pct') is not None]
    hit_50 = sum(1 for r in closed
                 if (r.get('peak_return_pct') or 0) >= 50)
    return {
        'tracked': len(closed), 'open': open_n,
        'wins': len(wins),
        'win_rate': round(len(wins) / len(closed), 3) if closed else None,
        'avg_return_pct': round(sum(rets) / len(rets), 2) if rets else None,
        'total_pnl': round(sum(r.get('pnl', 0) for r in closed), 2),
        # Path stats: how the trade looked at its best, and how many were
        # ever up 50%+ even if the 2-week close didn't hold it.
        'avg_peak_return_pct': (round(sum(peak_rets) / len(peak_rets), 2)
                                if peak_rets else None),
        'hit_plus_50': hit_50,
        'note': 'Paper-tracked suggested contracts: 1 contract, 2-week hold, $0.65/side fees.',
    }
