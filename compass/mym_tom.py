"""MYM Turn-of-Month — forward shadow (2026-11-18, Josh).

Paper / measurement scope ONLY: no orders, no live sends, nothing to
TradersPost or any broker. The TradersPost subscription stays
disabled and the survivor live gate is untouched by this module.
The one outward surface is display-only shadow alerts to the futures
Discord channel (own outbox, own drainer, the futures_sweep
pattern).

This module shadow-fires the MYM Turn-of-Month (TOM) cell of the
2026-11-10 Compass Session Rules gauntlet — the one cell of the 15
(rule x symbol) cells that passed all 7 gates — exactly as frozen in
the forward shadow registration
(files/mym-tom-forward-shadow-registration-2026-11-18.md), under the
original gauntlet registration
(files/mym-session-rules-registration-2026-11-10.md, sha c5c27d5f):

- Rule (Pine v1.2.1 semantics): a session is one sdate (17:00 CT
  open -> next-day 15:45 CT). tdCount = running count of sessions
  within the calendar month of sdate; the first session ever
  observed initialises count 0 / known=false, a month change sets
  count 1 / known=true, otherwise the count increments.
  isLast(S) = the next weekday after S (skipping Sat/Sun only;
  holidays NOT skipped) falls in a different month. isTOM(S) =
  (known and tdCount <= 3) or isLast(S) — the last trading day of
  the month and the first three trading days of the month.
- Trade: LONG 1 micro MYM on every TOM session, at most one trade
  per session. Entry at the session open: fill = open of the
  session's second 1-minute bar (the bar after the 17:00 trigger
  bar) + 1 tick adverse. Exit at 15:00 CT: fill = open of the first
  bar with tod in [900, 1019] searched positionally after the entry
  bar, - 1 tick adverse; if no such bar exists in the session, exit
  at the session's last bar open - 1 tick.
- Accounting (the gauntlet panel's): MYM tick 1.0 pt, $0.50/pt,
  $3.00 round-trip fees. P&L = (exit - entry) x $0.50 - $3.00.
- Stress: the gauntlet registration's p90 arm — the same trade
  repriced at 3 ticks adverse per side instead of 1, i.e. stress
  net = net - 4 ticks (-$2.00 per trade). TOM has no stops, so the
  per-side slippage arm (not the futures_sweep stop stress) is the
  consistent basis, and it is the basis the cell passed on
  (panel stress net +$2,561.00).
- Parity gate (tests/test_mym_tom.py): the machine below, fed the
  owned year-1 panel through the gauntlet's own sim.load path,
  reproduces the gauntlet's 47 TOM trades exactly (session dates,
  bar indices, entry/exit fills, per-trade P&L and stress P&L).
- Sample rule: episodes form only from bars that complete after
  the deployed shadow's bootstrap watermark (the futures_sweep /
  stock_sweeps bootstrap pattern). A session is sample-eligible
  only if it began after the bootstrap watermark (no bar of it
  existed in the store at bootstrap, so the shadow observes it
  from its first bar); the session in progress at bootstrap is
  calendar context only. The first eligible session is the first
  TOM session after deploy. No backfill.

Wiring: the driver is an engine pre-ICT unit (its own transaction
via Engine._pre_ict_unit, the 2026-11-15 isolation pattern); the
alert drainer is a standalone loop spawned in app.py. Neither can
throw into the engine tick. Bars come from the production futures
bar stores via futures_shadow_common.poll_bars (bar_window first,
bar-events backfill); (tod, sdate) are derived from each bar's ts
by futures_shadow_common.ct_parts — verified identical to the
gauntlet panel's own columns on all 339,543 panel bars.
"""
import asyncio
import logging
import time
import uuid
from datetime import date as _date, timedelta

import httpx
from sqlalchemy import select, update

from .futures_shadow_common import book_stats, ct_parts, poll_bars
from .store import identity
from .store import mym_tom_episodes as episodes
from .store import mym_tom_push_outbox

VERSION = 'mym-tom-v1'
KIND = 'mym_tom'
STATE_KEY = 'mym_tom:state'
MACHINE_KEY = 'mym_tom:machine'

ROOT = 'MYM'
ALIAS = 'MYM.c.0'
TICK = 1.0            # MYM panel tick (points)
TICKVAL = 0.50        # $ per tick (panel columns govern)
MULT = TICKVAL / TICK  # $ per point
FEES = 3.0            # house futures cost model, round trip per micro
STRESS_EXTRA_TICKS = 4  # p90 arm: 3 ticks/side vs 1 base = 2 extra
                        # ticks per side, 4 in total
EXIT_LO, EXIT_HI = 900, 1019   # the 15:00 exit bar window (tod, CT)


# ---------------------------------------------------------------------------
# The strategy machine: an incremental port of the gauntlet's TOM
# implementation (hidden_files/mym-session-rules-2026-11-10/
# gauntlet.py, rules R4 with the §3 fill convention). State is a
# plain JSON dict so it persists across ticks and restarts; bars
# must be fed strictly in ts order, each exactly once. Bar dicts:
# {sdate, tod, o, h, l, c, ts}. Events returned per bar:
# 'session' (a new session started; carries the calendar verdict),
# 'entry' and 'close' (carrying the gauntlet's own accounting:
# entry_fill / exit_fill / net / stress_net / bar indices).

def new_state():
    return {
        'bar_index': -1,        # global fed-bar counter (panel frame)
        'cur_sdate': None,
        'tdcount': 0, 'known': False, 'prev_month': None,
        'sess_idx': -1,         # bar index within the current session
        'istom': False, 'islast': False,
        'sessions_seen': 0,
        'pos': None,            # {sdate, i, entry_fill, entry_ts}
        'last_open': None, 'last_idx': None, 'last_ts': None,
    }


def _close_event(st, pos, exit_idx, exit_ts, exit_fill, reason):
    net = (exit_fill - pos['entry_fill']) * MULT - FEES
    return {'type': 'close', 'reason': reason, 'sdate': pos['sdate'],
            'entry_idx': pos['i'], 'exit_idx': exit_idx,
            'entry_ts': pos['entry_ts'], 'exit_ts': exit_ts,
            'entry_fill': pos['entry_fill'], 'exit_fill': exit_fill,
            'net': round(net, 2),
            'stress_net': round(net - STRESS_EXTRA_TICKS * TICKVAL, 2)}


def _fallback_close(st, events):
    """The gauntlet's fallback: the session ended with no bar in the
    15:00 window — exit at the session's last bar open - 1 tick."""
    pos = st['pos']
    if pos is None:
        return None
    ev = _close_event(st, pos, st['last_idx'], st['last_ts'],
                      st['last_open'] - TICK, 'fallback')
    events.append(ev)
    st['pos'] = None
    return ev


def on_bar(st, bar):
    """Feed one completed 1-minute bar; returns the bar's events.
    Mirrors gauntlet.py exactly: the TOM calendar (first session
    count 0 / known false; month change count 1 / known true;
    isLast via next weekday skipping Sat/Sun only), entry at the
    session's second bar open + 1 tick on TOM sessions, exit at the
    open of the first bar with tod in [900, 1019] positionally after
    the entry bar - 1 tick, fallback at the session's last bar."""
    events = []
    st['bar_index'] += 1
    sdate, tod = bar['sdate'], bar['tod']
    if sdate != st['cur_sdate']:
        # A new session resolves any position the prior session left
        # open (its fallback exit), then the calendar advances.
        if st['pos'] is not None:
            _fallback_close(st, events)
        d = _date.fromisoformat(sdate)
        if st['prev_month'] is None:
            st['tdcount'], st['known'] = 0, False
        elif d.month != st['prev_month']:
            st['tdcount'], st['known'] = 1, True
        else:
            st['tdcount'] += 1
        st['prev_month'] = d.month
        nd = d + timedelta(days=3 if d.weekday() == 4 else
                           2 if d.weekday() == 5 else 1)
        st['islast'] = nd.month != d.month
        st['istom'] = bool((st['known'] and st['tdcount'] <= 3)
                           or st['islast'])
        st['cur_sdate'] = sdate
        st['sess_idx'] = -1
        st['sessions_seen'] += 1
        st['last_open'] = None
        st['last_idx'] = None
        st['last_ts'] = None
        events.append({'type': 'session', 'sdate': sdate,
                       'istom': st['istom'], 'islast': st['islast'],
                       'tdcount': st['tdcount'], 'known': st['known'],
                       'first_tod': tod,
                       'first_session_seen': st['sessions_seen'] == 1})
    st['sess_idx'] += 1
    st['last_open'] = bar['o']
    st['last_idx'] = st['bar_index']
    st['last_ts'] = bar.get('ts')
    if st['sess_idx'] == 1 and st['istom'] and st['pos'] is None:
        # The session's second bar: entry fill at its open + 1 tick.
        st['pos'] = {'sdate': sdate, 'i': st['bar_index'],
                     'entry_fill': bar['o'] + TICK,
                     'entry_ts': bar.get('ts')}
        events.append({'type': 'entry', 'sdate': sdate,
                       'entry_idx': st['bar_index'],
                       'entry_ts': bar.get('ts'),
                       'entry_fill': bar['o'] + TICK,
                       'tdcount': st['tdcount'],
                       'islast': st['islast']})
    elif st['pos'] is not None and st['sess_idx'] > 1 \
            and EXIT_LO <= tod <= EXIT_HI:
        pos = st['pos']
        events.append(_close_event(st, pos, st['bar_index'],
                                   bar.get('ts'), bar['o'] - TICK,
                                   'exit_1500'))
        st['pos'] = None
    return events


def liquidate_window_end(st):
    """Batch/parity use only — live operation has no window end: a
    position still open after the final fed bar resolves at the
    gauntlet's fallback (the session's last bar open - 1 tick).
    Returns the close event or None when flat."""
    if st['pos'] is None:
        return None
    events = []
    _fallback_close(st, events)
    return events[0] if events else None


def run_bars(bars):
    """Batch driver over an ascending sequence of 1-minute bar dicts
    {ts, o, h, l, c} (the live poll_bars shape); (tod, sdate) are
    derived via ct_parts, the production convention. Returns
    (trades, events): trades are the close events."""
    st = new_state()
    trades, events = [], []
    for b in bars:
        tod, sdate = ct_parts(b['ts'])
        bar = {'sdate': sdate, 'tod': tod, 'o': b['o'], 'h': b['h'],
               'l': b['l'], 'c': b['c'], 'ts': b['ts']}
        evs = on_bar(st, bar)
        events.extend(evs)
        trades.extend(e for e in evs if e['type'] == 'close')
    ev = liquidate_window_end(st)
    if ev is not None:
        events.append(ev)
        trades.append(ev)
    return trades, events


# ---------------------------------------------------------------------------
# Display-only shadow alerts (own outbox + own drainer, the
# futures_sweep precedent): destination = the futures route's
# configured webhook via alert_routes.destination. These alerts
# never enter the general alert pipeline and never reach any order
# path. Scoring is byte-identical with the alert flag on or off.

PUSH_KIND = 'mym_tom_push'
MAX_PUSH_ATTEMPTS = 10
REASON_LABEL = {'exit_1500': 'SESSION EXIT (15:00)',
                'fallback': 'FALLBACK EXIT (no 15:00 bar)'}


def _money(v):
    if v is None:
        return '—'
    return '+$%.2f' % v if v >= 0 else '-$%.2f' % abs(v)


def _fmt_num(v, digits=2):
    try:
        return '%.*f' % (digits, float(v))
    except (TypeError, ValueError):
        return '—'


def format_entry_message(payload):
    lines = [
        '🧪 **MYM TOM SHADOW** — shadow forward test · display only',
        '🎯 **ENTRY LONG** · MYM · %s (turn of month)' % (
            payload.get('sdate', '?'),),
        'Entry fill `$%s` (session open + 1 tick) · exit scheduled '
        '15:00 CT' % (_fmt_num(payload.get('entry_fill')),),
        '1 contract · house fill model · $3.00 RT fees',
        '_Shadow measurement only — no order was placed or sent '
        'anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'MYM TOM Shadow',
            'allowed_mentions': {'parse': []}}


def format_close_message(payload):
    label = REASON_LABEL.get(payload.get('reason'),
                             str(payload.get('reason', '?')).upper())
    lines = [
        '🧪 **MYM TOM SHADOW** — shadow forward test · display only',
        '🏁 **%s** · MYM · %s' % (label, payload.get('sdate', '?')),
        'LONG · entry fill `$%s` → exit fill `$%s`' % (
            _fmt_num(payload.get('entry_fill')),
            _fmt_num(payload.get('exit_fill'))),
        'Scored P&L **%s** · stress (p90) **%s** · 1 contract, net '
        'of $3.00 RT fees' % (
            _money(payload.get('net')),
            _money(payload.get('stress_net'))),
        '_Shadow measurement only — no order was placed or sent '
        'anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'MYM TOM Shadow',
            'allowed_mentions': {'parse': []}}


FORMATTERS = {'entry': format_entry_message, 'exit': format_close_message}


def _queue_alert_row(c, db, payload):
    """Insert one shadow alert into the module's own outbox on the
    caller's connection (the engine unit's transaction: the alert
    commits or rolls back with the episode write it describes)."""
    row_id = identity('mym-tom-push-v1', payload.get('episode_id', ''),
                      payload.get('type', ''), payload.get('bar_ts', 0))
    c.execute(db.insert(mym_tom_push_outbox).values(
        id=row_id, status='pending', created=time.time(),
        payload=payload, delivery={'attempts': 0})
        .on_conflict_do_nothing(index_elements=['id']))
    return row_id


def queue_alert(db, cfg, payload):
    """Queue one shadow alert in its own transaction (the connors
    pattern for callers outside a unit transaction). Returns True
    when queued (or when alerts are off)."""
    if not bool(getattr(cfg, 'mym_tom_alerts', True)):
        return True
    try:
        with db.tx() as c:
            _queue_alert_row(c, db, payload)
        return True
    except Exception:
        try:
            db.health(PUSH_KIND, 'error', 'shadow alert queue failed')
        except Exception:
            pass
        logging.getLogger('uvicorn.error').exception(
            'mym_tom: alert queue failed')
        return False


def push_destination(cfg):
    """The futures route destination (url, mode) via the routing
    layer's own resolver; url None when the route is unconfigured."""
    from .alert_routes import destination
    url, mode = destination(cfg, 'futures')
    return (url if url else None), mode


def deliver_one_push(db, client, url, now=None,
                     max_attempts=MAX_PUSH_ATTEMPTS):
    """Attempt one pending shadow alert. Mirrors the fixed ict_push /
    connors drainer semantics: any local failure fails the row with
    the cause recorded — never strands a row in 'sending', never
    raises into the loop. Returns True if a row was attempted."""
    import re
    if not url or not re.fullmatch(
            r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+',
            url):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        row = c.execute(select(mym_tom_push_outbox)
                        .where(mym_tom_push_outbox.c.status == 'pending')
                        .order_by(mym_tom_push_outbox.c.created).limit(1)
                        ).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        if d.get('next_attempt') and now < d['next_attempt']:
            return False
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(update(mym_tom_push_outbox)
                  .where(mym_tom_push_outbox.c.id == row['id'])
                  .values(status='sending', delivery=d))
        job = dict(row, delivery=d)

    status = 'failed'
    try:
        formatter = FORMATTERS[job['payload'].get('type')]
        resp = client.post(url, params={'wait': 'true'},
                           json=formatter(job['payload']))
        if 200 <= resp.status_code < 300:
            status = 'delivered'
        elif resp.status_code == 429:
            status = 'pending'
            try:
                delay = max(1, min(3600, float(
                    resp.json().get('retry_after', 30))))
            except Exception:
                delay = 30
            job['delivery']['next_attempt'] = now + delay
        elif resp.status_code >= 500:
            status = 'pending'
            job['delivery']['next_attempt'] = now + 30
    except httpx.HTTPError:
        status = 'pending'
        job['delivery']['next_attempt'] = now + 30
    except Exception as e:
        job['delivery']['error'] = '%s: %s' % (type(e).__name__,
                                               str(e)[:200])

    if status == 'pending' and job['delivery'].get('attempts', 0) \
            >= max_attempts:
        status = 'failed'

    with db.tx() as c:
        c.execute(update(mym_tom_push_outbox)
                  .where(mym_tom_push_outbox.c.id == job['id'])
                  .values(status=status, delivery=job['delivery']))
    if status == 'delivered':
        db.health(PUSH_KIND, 'delivered',
                  'MYM TOM shadow alert confirmed by Discord', now)
    elif status == 'failed':
        db.health(PUSH_KIND, 'error',
                  'shadow alert delivery failed: %s'
                  % job['delivery'].get('error', 'attempts exhausted'))
    return True


async def run_push(db, cfg):
    """Drainer loop for the shadow alerts (own outbox, futures route
    destination). Never raises out of the loop."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if bool(getattr(cfg, 'mym_tom', True)) and \
                        bool(getattr(cfg, 'mym_tom_alerts', True)):
                    url, mode = push_destination(cfg)
                    if url:
                        await asyncio.to_thread(deliver_one_push, db,
                                                client, url)
                    else:
                        db.health(PUSH_KIND, 'not_configured',
                                  'futures route has no webhook; '
                                  'shadow alerts stay queued')
            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    db.health(PUSH_KIND, 'error', type(e).__name__)
                except Exception:
                    pass
            await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Episode persistence + the live driver. scan() runs as an engine
# pre-ICT unit in the unit's own transaction: machine state, episode
# writes and outbox rows commit or roll back together.

def _save(db, c, p, now, status=None):
    p['updated_at'] = now
    c.execute(update(episodes).where(episodes.c.id == p['id']).values(
        status=status or p['status'], updated=now, payload=p))


def _event(db, c, p, kind, now, extra=None):
    body = {'episode_id': p['id'], 'symbol': p['symbol'], 'event': kind,
            'at': now, **(extra or {})}
    db.append(c, KIND, VERSION, p['symbol'], now, body,
              key=identity(KIND, p['id'], kind, body.get('date')))


def _open_episode(db, c, ev, now, sample):
    p = {
        'id': identity('mym-tom-episode-v1', ROOT, ev['sdate'],
                       ev['entry_ts']),
        'version': VERSION, 'strategy': 'mym-tom-shadow',
        'symbol': ROOT, 'underlying': ROOT,
        'side': 'long', 'dir': 1,
        'status': 'open', 'stage': 'open', 'sample': sample,
        'created_at': now, 'updated_at': now,
        'signal': {'sdate': ev['sdate'], 'tdcount': ev['tdcount'],
                   'islast': ev['islast'], 'entry_ts': ev['entry_ts'],
                   'entry_fill': ev['entry_fill'], 'tick': TICK,
                   'tickval': TICKVAL, 'exit_due': '15:00 CT'},
        'result': None, 'pnl': None, 'outcome': None,
    }
    stmt = db.insert(episodes).values(
        id=p['id'], underlying=ROOT, status='open', created=now,
        updated=now, payload=p)
    res = c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
    if res.rowcount == 0:
        row = c.execute(select(episodes.c.payload).where(
            episodes.c.id == p['id'])).scalars().first()
        return row, False
    _event(db, c, p, 'episode_created', now,
           {'date': ev['sdate'], 'entry_fill': ev['entry_fill']})
    return p, True


def _alert_payloads_for_event(p, ev):
    """Alert payloads for one machine event on episode p: ENTRY on
    the entry event; EXIT on the close event (the registration's two
    alert types)."""
    if ev['type'] == 'entry':
        return [{'episode_id': p['id'], 'symbol': p['symbol'],
                 'type': 'entry', 'sdate': ev['sdate'],
                 'entry_fill': ev['entry_fill'],
                 'bar_ts': ev['entry_ts']}]
    if ev['type'] == 'close':
        return [{'episode_id': p['id'], 'symbol': p['symbol'],
                 'type': 'exit', 'sdate': ev['sdate'],
                 'reason': ev['reason'],
                 'entry_fill': ev['entry_fill'],
                 'exit_fill': ev['exit_fill'], 'net': ev['net'],
                 'stress_net': ev['stress_net'],
                 'bar_ts': ev['exit_ts']}]
    return []


def _write_state(db, c, now, extra=None):
    """Recompute the summary from the episode table: counts, the
    running headline + stress stats (book_stats over the same
    forward trades), and the verdict clock (weeks since the first
    eligible session; first read at >= 12 trades, verdict at >= 50
    trades AND >= 12 weeks — factual reporting only, no verdict
    logic)."""
    rows = c.execute(select(episodes.c.payload)).scalars().all()
    counts = {'created': 0, 'open': 0, 'closed': 0}
    nets, stress = [], []
    for p in rows:
        counts['created'] += 1
        if p['status'] in counts:
            counts[p['status']] += 1
        if p.get('result'):
            nets.append(p['result']['net'])
            stress.append(p['result']['stress_net'])
    stats = book_stats(nets)
    stats['stress_net'] = round(sum(stress), 2)
    old = db.get(c, STATE_KEY, {})
    first_session = old.get('first_session')
    weeks = None
    if first_session:
        weeks = round((_date.today()
                       - _date.fromisoformat(first_session)).days
                      / 7.0, 1)
    state = {'version': VERSION, 'at': now, 'counts': counts,
             'stats': stats,
             'book': {'symbol': ROOT, 'tick': TICK, 'tickval': TICKVAL},
             'bootstrap_at': old.get('bootstrap_at'),
             'first_session': first_session,
             'weeks_since_first_session': weeks,
             'gap_polls': old.get('gap_polls', 0),
             'verdict': {'min_trades': 50, 'min_weeks': 12,
                         'first_read_trades': 12},
             **(extra or {})}
    db.put(c, STATE_KEY, state)
    return state


def scan(db, c, cfg, now):
    """One live pass: poll MYM's newly arrived 1-minute bars, feed
    the completed ones into the persisted machine, and book TOM
    entries/exits as episodes + shadow alerts. Runs inside the
    caller's (engine unit) transaction.

    Sample rule (registration): a session is sample-eligible only
    if it began after the bootstrap watermark — no bar of it
    existed in the store at bootstrap, so the shadow observes it
    from its first bar. The session in progress at bootstrap (the
    newest stored bar's session) is calendar context only. Every
    later session is eligible by construction (bars are fed in
    order from the watermark)."""
    if not getattr(cfg, 'mym_tom', True):
        return {'ran': False, 'reason': 'disabled'}
    alerts_on = bool(getattr(cfg, 'mym_tom_alerts', True))
    state = db.get(c, STATE_KEY, {})
    if 'bootstrap_at' not in state:
        state['bootstrap_at'] = now
        state['gap_polls'] = 0
        db.put(c, STATE_KEY, state)
    m = db.get(c, MACHINE_KEY)
    if m is None:
        m = {'state': new_state(), 'fed_ts': None, 'poll_after': None,
             'bootstrapped_at': now, 'bootstrap_last_sdate': None}
    bars, gap = poll_bars(db, c, ALIAS, ROOT, m.get('poll_after'))
    if gap:
        state['gap_polls'] = state.get('gap_polls', 0) + 1
        db.put(c, STATE_KEY, state)
    if m.get('poll_after') is None:
        # Bootstrap watermark (futures_sweep / stock_sweeps pattern):
        # episodes form only from bars that complete AFTER the
        # shadow is live — the cursor starts at the newest polled
        # bar and history is never fed. The newest bar's session is
        # the in-progress session the shadow joins mid-way; it is
        # recorded so that session stays sample-ineligible.
        m['poll_after'] = bars[-1]['ts'] if bars else now
        m['fed_ts'] = m['poll_after']
        if bars:
            m['bootstrap_last_sdate'] = ct_parts(bars[-1]['ts'])[1]
        db.put(c, MACHINE_KEY, m)
        summary = _write_state(db, c, now)
        return {'ran': True, 'fed': 0, 'entries': 0, 'closes': 0,
                'open_episodes': summary['counts']['open'],
                'first_session': summary.get('first_session')}
    alerts_out = []
    open_ep = [None]   # this pass's open episode payload (at most one)
    fed = entries = closes = 0

    def _load_open_episode():
        if open_ep[0] is None:
            rows = c.execute(select(episodes.c.payload).where(
                episodes.c.status == 'open')
                .order_by(episodes.c.created.desc())).scalars().all()
            open_ep[0] = rows[0] if rows else False
        return open_ep[0] or None

    for b in bars:
        if b['ts'] <= (m['fed_ts'] or 0):
            continue
        if b['ts'] + 60 > now:
            # The in-progress minute is not a completed bar; it is
            # re-polled next pass (poll_after stays behind it).
            break
        tod, sdate = ct_parts(b['ts'])
        bar = {'sdate': sdate, 'tod': tod, 'o': b['o'], 'h': b['h'],
               'l': b['l'], 'c': b['c'], 'ts': b['ts']}
        evs = on_bar(m['state'], bar)
        m['fed_ts'] = b['ts']
        fed += 1
        for ev in evs:
            if ev['type'] == 'session':
                eligible = True
                if ev['first_session_seen']:
                    last0 = m.get('bootstrap_last_sdate')
                    eligible = last0 is None or ev['sdate'] > last0
                if eligible and not state.get('first_session'):
                    state['first_session'] = ev['sdate']
                    db.put(c, STATE_KEY, state)
                continue
            sample = bool(state.get('first_session')) \
                and ev['sdate'] >= state['first_session']
            if ev['type'] == 'entry':
                if not sample:
                    continue
                p, created = _open_episode(db, c, ev, now, sample)
                open_ep[0] = p
                if created:
                    entries += 1
                    alerts_out.extend(_alert_payloads_for_event(p, ev))
            elif ev['type'] == 'close':
                p = _load_open_episode()
                if p is None:
                    continue
                p['result'] = {
                    'entry_fill': ev['entry_fill'],
                    'exit_fill': ev['exit_fill'],
                    'exit_ts': ev['exit_ts'], 'reason': ev['reason'],
                    'net': ev['net'], 'stress_net': ev['stress_net']}
                p['pnl'] = ev['net']
                p['outcome'] = ('win' if ev['net'] > 0 else
                                'loss' if ev['net'] < 0 else 'breakeven')
                p['stage'] = 'closed'
                p['status'] = 'closed'
                _event(db, c, p, 'closed', now,
                       {'date': ev['sdate'], 'reason': ev['reason'],
                        'net': ev['net'],
                        'stress_net': ev['stress_net']})
                _save(db, c, p, now, status='closed')
                open_ep[0] = False
                closes += 1
                alerts_out.extend(_alert_payloads_for_event(p, ev))
    m['poll_after'] = m['fed_ts']
    db.put(c, MACHINE_KEY, m)
    if alerts_on:
        for payload in alerts_out:
            _queue_alert_row(c, db, payload)
    summary = _write_state(db, c, now)
    return {'ran': True, 'fed': fed, 'entries': entries,
            'closes': closes,
            'open_episodes': summary['counts']['open'],
            'first_session': summary.get('first_session')}


class MymTom:
    """Lease-guarded standalone driver (parity with the house shadow
    pattern) — the engine unit is the production path; this class
    exists for tests and manual passes."""

    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner = uuid.uuid4().hex

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    def process(self, now):
        with self.db.tx() as c:
            return scan(self.db, c, self.cfg, now)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'mym-tom', self.owner, 300):
                return
        self.process(self.now(time.time()))
