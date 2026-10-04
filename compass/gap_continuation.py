"""Gap Continuation: gaps that break the opening range and hold, with pillars.

Pipeline per symbol per session:
  1. identify  (09:31 CT): gap% vs prior close, $2B+ market-cap filter
  2. or_complete (09:45 CT): 09:30-09:45 opening range
  3. break: first 1-minute close beyond the range after it completes
  4. hold: 10 minutes with no 1-minute close back inside the range
  5. pillars: price (the hold, mandatory) + dealer + flow + sector
  6. qualified = hold + >=2 of 3 supportive, dealer opposition vetoes

Qualified gaps push to #intraday on confirmation. Forward marks (15/30/60
minutes and session close from hold-confirm time) feed the weekly outcome
table. No auto-admission, no trades.
"""
from .market import number, session, day, CT
from .apex_magnet import _sector_for

MIN_GAP_DEFAULT = 0.015
HOLD_MINUTES = 10
FLOW_WINDOW_MINUTES = 30
FLOW_MIN_PREMIUM = 250000
SCAN_THROTTLE = 60
OR_MINUTES = 15

DEFAULT_LARGE_CAPS = (
    "AAPL,MSFT,NVDA,AMZN,META,GOOGL,GOOG,AVGO,TSLA,BRK.B,JPM,XOM,UNH,MA,V,"
    "WMT,ORCL,NFLX,COST,PG,JNJ,HD,BAC,CRM,AMD,LLY,ABBV,KO,DIS,QCOM,TXN,NKE,"
    "MRK,ADBE,PEP,TMO,CSCO,ACN,LIN"
)


def large_caps(cfg):
    raw = getattr(cfg, 'gap_large_caps', None) or DEFAULT_LARGE_CAPS
    return {s.strip().upper() for s in str(raw).split(',') if s.strip()}


def _bars(db, c, symbol, since, limit=2000):
    out = []
    for r in db.recent(c, 'bar', symbol, limit=limit, since=since):
        p = r.get('payload') or {}
        if number(p.get('c')) is None:
            continue
        out.append({'ts': r['ts'], 'o': number(p.get('o')), 'h': number(p.get('h')),
                    'l': number(p.get('l')), 'c': number(p.get('c'))})
    return sorted(out, key=lambda b: b['ts'])


def identify(symbol, daily_rows, open_bar, min_gap, caps):
    """Pure gap identification. Returns (record-dict) or (None, reason)."""
    closes = [(r['ts'], (r.get('payload') or {}).get('c')) for r in daily_rows]
    closes = [(ts, number(c)) for ts, c in closes if number(c)]
    if not closes:
        return None, 'no_prior_close'
    prior_close = sorted(closes)[-1][1]
    open_px = number((open_bar or {}).get('o'))
    if not open_px or not prior_close:
        return None, 'no_open'
    gap_pct = (open_px - prior_close) / prior_close
    if abs(gap_pct) < min_gap:
        return None, 'gap_below_minimum'
    if symbol.upper() not in caps:
        return None, 'no_market_cap_record'
    return {'gap_pct': gap_pct,
            'direction': 'up' if gap_pct > 0 else 'down',
            'prior_close': prior_close, 'open': open_px}, None


def or_range(bars, sess_open):
    """09:30-09:45 CT opening range from minute bars. Returns (high, low) or None."""
    window = [b for b in bars if sess_open <= b['ts'] < sess_open + OR_MINUTES * 60]
    if len(window) < OR_MINUTES:
        return None
    return max(b['h'] for b in window), min(b['l'] for b in window)


def check_break(bars, or_high, or_low, or_end):
    """First 1-minute close beyond the completed range. Returns (direction, ts, level)."""
    for b in bars:
        if b['ts'] < or_end:
            continue
        if b['c'] > or_high:
            return 'up', b['ts'], b['c']
        if b['c'] < or_low:
            return 'down', b['ts'], b['c']
    return None, None, None


def check_hold(bars, direction, or_high, or_low, break_ts):
    """10-minute hold: no 1-minute close back inside the range.

    Returns 'confirmed', ('failed', fail_ts), or None when the window is
    still incomplete.
    """
    deadline = break_ts + HOLD_MINUTES * 60
    seen_end = False
    for b in bars:
        if b['ts'] <= break_ts:
            continue
        if b['ts'] >= deadline:
            seen_end = True
            break
        if or_low <= b['c'] <= or_high:
            return 'failed', b['ts']  # closed back inside the range
    if not seen_end:
        return None
    return 'confirmed', None


def dealer_pillar(apex, direction, open_px, break_px):
    """Positional dealer read from vendor apex levels.

    Supportive: nearest magnet beyond the break (room to run).
    Opposing (veto): a magnet sitting between the open and the break.
    """
    levels = [number(r.get('price')) for r in (apex or {}).get('levels', [])
              if r.get('kind') == 'apex' and number(r.get('price'))]
    flip = next((number(r.get('price')) for r in (apex or {}).get('levels', [])
                 if r.get('kind') == 'gamma_flip' and number(r.get('price'))), None)
    detail = {'flip': flip}
    if not levels:
        return {'status': 'unknown', 'detail': {**detail, 'reason': 'no_apex_levels'}}
    if direction == 'up':
        between = [p for p in levels if open_px < p < break_px]
        beyond = [p for p in levels if p >= break_px]
        if between:
            return {'status': 'opposing', 'veto': True,
                    'detail': {**detail, 'overhead': min(between)}}
        if beyond:
            return {'status': 'supportive', 'detail': {**detail, 'room_to': min(beyond)}}
    else:
        between = [p for p in levels if break_px < p < open_px]
        beyond = [p for p in levels if p <= break_px]
        if between:
            return {'status': 'opposing', 'veto': True,
                    'detail': {**detail, 'underlying': max(between)}}
        if beyond:
            return {'status': 'supportive', 'detail': {**detail, 'room_to': max(beyond)}}
    return {'status': 'unknown', 'detail': {**detail, 'reason': 'no_magnet_in_play'}}


def flow_pillar(flow_rows, symbol, sess_open, direction):
    """Net same-direction premium in the first 30 minutes. Tape-Confirmed convention."""
    end = sess_open + FLOW_WINDOW_MINUTES * 60
    call_premium = put_premium = 0.0
    for row in flow_rows or []:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            continue
        stamp = number(row.get('source_ts'))
        if stamp is None or not sess_open <= stamp < end:
            continue
        premium = number(row.get('premium')) or 0
        opt = str(row.get('option_type') or '').lower()
        if opt in ('call', 'c'):
            call_premium += premium
        elif opt in ('put', 'p'):
            put_premium += premium
    directional = call_premium if direction == 'up' else put_premium
    status = 'supportive' if directional >= FLOW_MIN_PREMIUM else 'opposing'
    return {'status': status,
            'detail': {'call_premium': call_premium, 'put_premium': put_premium,
                       'directional_premium': directional,
                       'minimum': FLOW_MIN_PREMIUM}}


def sector_pillar(sector_dashboard, sector_map, symbol, direction):
    """Defensive read of the symbol's sector flow direction."""
    sector = _sector_for(symbol, sector_map)
    if not sector:
        return {'status': 'unknown', 'detail': {'reason': 'sector_unknown'}}
    flow_dir = None
    dash = sector_dashboard if isinstance(sector_dashboard, dict) else {}
    for container in (dash, dash.get('data') if isinstance(dash.get('data'), dict) else None):
        if not isinstance(container, dict):
            continue
        sectors = container.get('sectors')
        if isinstance(sectors, dict) and sector in sectors:
            entry = sectors[sector]
            flow_dir = entry.get('flow_direction') or entry.get('direction') or entry.get('sentiment')
            break
        if isinstance(sectors, list):
            for row in sectors:
                if isinstance(row, dict) and row.get('sector') == sector:
                    flow_dir = (row.get('flow_direction') or row.get('direction')
                                or row.get('sentiment'))
                    break
    flow_dir = str(flow_dir or '').lower()
    want = 'bullish' if direction == 'up' else 'bearish'
    if flow_dir == want:
        return {'status': 'supportive', 'detail': {'sector': sector, 'flow': flow_dir}}
    if flow_dir in ('bullish', 'bearish'):
        return {'status': 'opposing', 'detail': {'sector': sector, 'flow': flow_dir}}
    return {'status': 'unknown', 'detail': {'sector': sector, 'reason': 'flow_unreadable'}}


def forward_marks(bars, hold_ts, direction, sess_close):
    """15/30/60-minute and session-close forward returns from hold-confirm time."""
    base = next((b['c'] for b in bars if b['ts'] >= hold_ts), None)
    if base is None:
        return {}
    sign = 1 if direction == 'up' else -1
    marks = {}
    for name, offset in (('m15', 900), ('m30', 1800), ('m60', 3600)):
        px = next((b['c'] for b in bars if b['ts'] >= hold_ts + offset), None)
        marks[name] = sign * (px - base) / base if px else None
    closes = [b['c'] for b in bars if b['ts'] < sess_close]
    marks['close'] = sign * (closes[-1] - base) / base if closes else None
    return marks


def _advance(db, c, symbol, now, cfg, ctx):
    """Advance one symbol's state machine. ctx carries shared feed snapshots."""
    today = day(now)
    hours = session(today)
    if not hours:
        return None
    sess_open, sess_close = hours
    key = 'gap_cont:%s:%s' % (symbol, today)
    state = db.get(c, key) or {'symbol': symbol, 'day': today, 'stage': 'pending'}
    bars = _bars(db, c, symbol, since=sess_open - 3600)
    if state['stage'] == 'pending' and now >= sess_open + 60:
        daily_rows = db.recent(c, 'daily', symbol, limit=5)
        open_bar = next((b for b in bars if b['ts'] >= sess_open), None)
        rec, reason = identify(symbol, daily_rows, open_bar,
                               number(getattr(cfg, 'gap_min_gap', MIN_GAP_DEFAULT)) or MIN_GAP_DEFAULT,
                               large_caps(cfg))
        if rec is None:
            state.update(stage='excluded', exclusion=reason, evaluated_at=now)
        else:
            state.update(stage='identified', evaluated_at=now, **rec)
    if state['stage'] == 'identified' and now >= sess_open + OR_MINUTES * 60:
        rng = or_range(bars, sess_open)
        if rng:
            state.update(stage='or_complete', or_high=rng[0], or_low=rng[1], evaluated_at=now)
    if state['stage'] == 'or_complete':
        direction, bts, level = check_break(bars, state['or_high'], state['or_low'],
                                            sess_open + OR_MINUTES * 60)
        if direction:
            state.update(stage='broken', break_direction=direction, break_ts=bts,
                         break_level=level, evaluated_at=now)
    if state['stage'] == 'broken':
        held = check_hold(bars, state['break_direction'], state['or_high'],
                          state['or_low'], state['break_ts'])
        if held:
            outcome, fail_ts = held
            state.update(hold=outcome, hold_ts=(state['break_ts'] + HOLD_MINUTES * 60
                                                if outcome == 'confirmed' else fail_ts),
                         evaluated_at=now)
            if outcome == 'confirmed':
                pillars = {
                    'price': {'status': 'supportive',
                              'detail': {'hold_minutes': HOLD_MINUTES}},
                    'dealer': dealer_pillar(ctx['apex'].get(symbol), state['break_direction'],
                                            state['open'], state['break_level']),
                    'flow': flow_pillar(ctx['flow_rows'], symbol, sess_open,
                                        state['break_direction']),
                    'sector': sector_pillar(ctx['sector_dashboard'], ctx['sector_map'],
                                            symbol, state['break_direction']),
                }
                supportive = sum(1 for k, p in pillars.items()
                                 if k != 'price' and p['status'] == 'supportive')
                veto = any(p.get('veto') for p in pillars.values())
                state.update(stage='done', pillars=pillars,
                             qualified=(supportive >= 2 and not veto), veto=veto,
                             evaluated_at=now)
                if state['qualified']:
                    db.append(c, 'gap_continuation', 'gap_continuation', symbol, now,
                              {'symbol': symbol, 'day': today,
                               'direction': state['break_direction'],
                               'gap_pct': state['gap_pct'],
                               'pillars': {k: v['status'] for k, v in pillars.items()}},
                              key='gapq:%s:%s' % (symbol, today))
            else:
                state.update(stage='done', qualified=False, evaluated_at=now)
    if state.get('stage') == 'done' and state.get('hold') == 'confirmed':
        marks = forward_marks(bars, state['hold_ts'], state['break_direction'], sess_close)
        if marks and marks != state.get('marks'):
            state['marks'] = marks
            state['evaluated_at'] = now
    db.put(c, key, state)
    return state


def scan(db, c, cfg, now):
    """Throttled pass over the stock watchlist."""
    if not getattr(cfg, 'gap_continuation', True):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'gap_continuation:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    ctx = {'flow_rows': (db.get(c, 'matrix:unusual_activity', {}) or {}).get('rows') or [],
           'sector_dashboard': db.get(c, 'research:sector_dashboard', {}),
           'sector_map': db.get(c, 'research:extra_sector_map', {}),
           'apex': {k[5:]: v for k, v in db.prefix(c, 'apex:').items()}}
    states = [s for s in (_advance(db, c, symbol, now, cfg, ctx) for symbol in cfg.stocks)
              if s is not None]
    pushed = queue_push_for_qualified(db, c, cfg, now)
    db.put(c, 'gap_continuation:scanned_at', now)
    return {'ran': True, 'symbols': len(states),
            'qualified': sum(1 for s in states if s.get('qualified')),
            'pushed': pushed}


def display(db, c, now, days=7):
    """Morning board plus the weekly outcome table."""
    today = day(now)
    board, outcomes = [], []
    for key, state in db.prefix(c, 'gap_cont:').items():
        # Skips gap_continuation:scanned_at, which shares the prefix but is a float.
        if not isinstance(state, dict):
            continue
        if state.get('day') == today:
            board.append(state)
        elif state.get('qualified') and state.get('marks', {}).get('close') is not None:
            outcomes.append(state)
    board.sort(key=lambda s: -abs(s.get('gap_pct') or 0))

    def bucket(gap_pct):
        a = abs(gap_pct or 0)
        return '1.5-3%' if a < 0.03 else '3-5%' if a < 0.05 else '5%+'

    table = {}
    for s in outcomes[-200:]:
        pillars = s.get('pillars', {})
        support = sum(1 for k, p in pillars.items()
                      if k != 'price' and (p or {}).get('status') == 'supportive')
        cell = (s.get('break_direction'), bucket(s.get('gap_pct')), support,
                'veto' if s.get('veto') else 'no_veto')
        cell_stats = table.setdefault(cell, {'n': 0, 'wins': 0, 'sum_ret': 0.0})
        ret = s['marks']['close']
        cell_stats['n'] += 1
        cell_stats['wins'] += 1 if ret > 0 else 0
        cell_stats['sum_ret'] += ret
    rows = [{'direction': k[0], 'gap_bucket': k[1], 'confirmations': k[2], 'dealer': k[3],
             'n': v['n'], 'win_rate': v['wins'] / v['n'],
             'mean_return': v['sum_ret'] / v['n']} for k, v in sorted(table.items())]
    return {'asof': now, 'board': board, 'weekly_outcomes': rows,
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}


# --- Push on qualified gap (alerts live by default; kill fast if noisy) ---
import asyncio
import re
import time as _time
from sqlalchemy import select as _select
from .store import gap_push_outbox as _outbox, identity as _identity

_GAP_WEBHOOK_RE = r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+'
_GAP_MAX_ATTEMPTS = 10


def _maybe_queue_push(db, c, cfg, symbol, today, now, state):
    """Write one outbox row per newly qualified gap. Alerts live by default."""
    if not getattr(cfg, 'gap_push_enabled', True):
        return False
    webhook = (getattr(cfg, 'discord_routes', {}) or {}).get('intraday', '')
    if not re.fullmatch(_GAP_WEBHOOK_RE, webhook or ''):
        return False
    row_id = _identity('gap-push-v1', today, symbol, state.get('break_direction'))
    payload = {'kind': 'gap_qualified', 'symbol': symbol,
               'direction': state.get('break_direction'), 'session': today,
               'gap_pct': state.get('gap_pct'),
               'break_level': state.get('break_level'),
               'pillars': {k: v.get('status') for k, v in
                           (state.get('pillars') or {}).items()},
               'at': now}
    c.execute(db.insert(_outbox).values(
        id=row_id, status='pending', created=now, payload=payload,
        delivery={'attempts': 0}).on_conflict_do_nothing(index_elements=['id']))
    return True


def queue_push_for_qualified(db, c, cfg, now):
    """Called from scan(): push any newly qualified gaps. Returns symbols pushed."""
    pushed = []
    today = day(now)
    for symbol in getattr(cfg, 'stocks', ()):
        key = 'gap_cont:%s:%s' % (symbol, today)
        state = db.get(c, key) or {}
        if not state.get('qualified'):
            continue
        if db.get(c, 'gap_push_sent:%s:%s' % (symbol, today)):
            continue
        if _maybe_queue_push(db, c, cfg, symbol, today, now, state):
            db.put(c, 'gap_push_sent:%s:%s' % (symbol, today), now)
            pushed.append(symbol)
    return pushed


def format_message(payload):
    """Discord-safe message for one qualified gap. No mentions, <2000 chars."""
    direction = 'LONG' if payload.get('direction') == 'up' else 'SHORT'
    pillars = payload.get('pillars') or {}
    supportive = [k for k, v in pillars.items() if v == 'supportive']
    lines = [
        '**%s** gap continuation **%s**' % (payload.get('symbol'), direction),
        'Gap %+.2f%% | OR break held 10min | pillars: %s' % (
            (payload.get('gap_pct') or 0) * 100, ', '.join(supportive) or 'price'),
    ]
    content = '\n'.join(lines)
    return {'content': content[:1900], 'username': 'Market Compass',
            'allowed_mentions': {'parse': []}}


def deliver_one(db, client, url, now=None, max_attempts=_GAP_MAX_ATTEMPTS):
    """Attempt one pending row. Returns True if a row was attempted."""
    if not re.fullmatch(_GAP_WEBHOOK_RE, url or ''):
        return False
    now = _time.time() if now is None else now
    with db.tx() as c:
        candidates = c.execute(_select(_outbox)
            .where(_outbox.c.status.in_(['pending', 'sending']))
            .order_by(_outbox.c.created).limit(20)).mappings().all()
        job = None
        for row in candidates:
            d = dict(row['delivery'])
            if row['status'] == 'sending' and d.get('lease_until', 0) > now:
                continue
            job = dict(row)
            break
        if job is None:
            return False
        d = dict(job['delivery'])
        d.update(attempts=d.get('attempts', 0) + 1, lease_until=now + 120)
        c.execute(_outbox.update().where(_outbox.c.id == job['id'])
                  .values(status='sending', delivery=d))
    message_id, error, status = None, None, 'pending'
    try:
        response = client.post(url, json=format_message(job['payload']))
        if response.status_code in (200, 201, 204):
            status = 'delivered'
            try:
                message_id = response.json().get('id')
            except Exception:
                pass
        elif response.status_code == 429:
            retry_after = response.headers.get('retry-after')
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 5
            status, error = 'pending', 'rate_limited'
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
        c.execute(_outbox.update().where(_outbox.c.id == job['id'])
                  .values(status=status, delivery=d))
    return True


async def run(db, cfg):
    """Drainer: 1s poll, 429 backoff, retry budget. Rows accumulate if down."""
    import httpx
    url = (getattr(cfg, 'discord_routes', {}) or {}).get('intraday', '')
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if getattr(cfg, 'gap_push_enabled', True):
                    deliver_one(db, client, url)
            except Exception:
                db.health('gap_push', 'error', 'drainer exception')
            await asyncio.sleep(1)
