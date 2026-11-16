"""Futures Sweep (ORB HUD) — forward shadow (2026-11-16, Josh).

Paper / measurement scope ONLY: no orders, no live sends, nothing to
TradersPost or any broker. The TradersPost subscription stays
disabled and the survivor live gate is untouched by this module.
The one outward surface is display-only shadow alerts to the futures
Discord channel (own outbox, own drainer, the stock_sweeps /
connors_ssc pattern).

This module shadow-fires Josh's Futures_ORB.txt ("Opening Range
Sweep & Reclaim [Manual Trading HUD v3.4]") on the 8 micro roots of
the 2026-11-12 futures sweep gauntlet, exactly as frozen in the
forward shadow registration
(files/futures-sweep-forward-shadow-registration-2026-11-16.md):

- Book (frozen): MES, MNQ, MYM, MGC, MCL, M2K, M6E, M6A — 1 contract
  each, scored per root, tick size / tick value / commissions as the
  Nov 12 gauntlet panel (panel columns govern).
- Semantics: the HUD as written, on 5-minute bars — OR 09:30-09:45
  ET; sweep & reclaim entries 09:45-11:30 ET under the shared core's
  frozen porting semantics (extension filter probe <= 1.0 x OR
  range; stop = signal-bar extreme -/+ 2 ticks; target = 1.5R); ONE
  trade per root per day (the first reclaim signal is the day's only
  trade); exits evaluated only on bars after the entry bar, TARGET
  checked before STOP (the HUD tracking blocks' order); flatten at
  the 11:30 bar's close. The state machine below (SweepMachine via
  new_state/on_bar) is an incremental port of the cutoff-extension
  harness (hidden_files/futures-orb-cutoff-extension-2026-11-16/
  engine_hud.py) at the 690 (11:30 ET) cell.
- Amendment (2026-11-16 16:00 CT, Josh): a SECOND configuration of
  the same detector is tracked in parallel — the identical HUD with
  the one-trade-per-day limit REMOVED (re-entry on later reclaim
  signals after any exit, unlimited per day, no loss lockout;
  everything else unchanged). It is a configuration flag
  (one_per_day) on this same machine, not a second detector: with
  the limit removed, sweep tracking runs on every trade-window bar
  (the HUD tracking block has no position gate, so the peak/trough
  keep updating while a position is open) and an entry consumes the
  taken side's swept flag only (the HUD's literal reset; the
  peak/trough persist). The as-written book remains the only
  alerting book; the unlimited configuration is scored silently in
  the same episode ledger (payload config = 'unlimited'), reported
  alongside per root, and the verdict clock/limbs apply to it
  independently per root.
- Scoring: the gauntlet's accounting — entry fill = signal close
  +/- 1 tick adverse; stop fill = level -/+ 2 ticks adverse; target
  and flatten fills at the level / close exactly; $0.62 per contract
  per side. Alongside each trade's net, the -/+4-tick p90 stress
  repricing (an extra 2 ticks against every stop exit) is carried,
  so the forward book is judged on the same stress basis as the
  backtest.
- Parity gate (test_futures_sweep.py): the machine reproduces the
  extension harness's 11:30 HUD cells exactly for all 8 roots over
  the gauntlet panel (2025-10-27 -> 2026-10-26); the unlimited
  configuration is verified against its own baseline cell when
  available, and otherwise programmatically (every unlimited entry
  is a gauntlet signal; the unlimited trade set is a superset of
  the as-written book's).
- Sample rule: episodes form only from bars that complete after the
  deployed shadow's bootstrap watermark (the stock_sweeps / SH-SPY
  bootstrap pattern); the first eligible session is the first
  session whose 09:30 ET bar the shadow itself feeds, recorded in
  the state summary. No backfill.

Wiring: the driver is an engine pre-ICT unit (its own transaction
via Engine._pre_ict_unit, the 2026-11-15 isolation pattern); the
alert drainer is a standalone loop spawned in app.py. Neither can
throw into the engine tick. Bars come from the production futures
bar stores via futures_shadow_common.poll_bars (bar_window first,
bar-events backfill), aggregated to 5-minute ET buckets here.
"""
import asyncio
import logging
import time
import uuid
from datetime import date as _date, datetime, time as _time

import httpx
from sqlalchemy import select, update

from .futures_shadow_common import book_stats, poll_bars
from .market import NY
from .store import identity
from .store import futures_sweep_episodes as episodes
from .store import futures_sweep_push_outbox

VERSION = 'futures-sweep-v1'
KIND = 'futures_sweep'
STATE_KEY = 'futures_sweep:state'
MACHINE_PREFIX = 'futures_sweep:machine:'

FEE_SIDE = 0.62       # per contract per side (the file's commission)
RR = 1.5              # target = 1.5 x risk from the signal close
EXT_MULT = 1.0        # trend-extension filter: probe <= 1.0 x OR range
OR_LO, OR_HI = 570, 585        # OR bars start 09:30 <= start < 09:45 ET
TW_LO = 585                    # trade-window bars start >= 09:45 ET
CUTOFF = 690                   # 11:30 ET: last entry bar starts < 690;
                               # the 690 bar is bracket-then-flatten
FEED_HI = 720                  # buckets fed up to 12:00 ET (the first
                               # bucket past the cutoff triggers the
                               # fallback flatten when 11:30 is missing)

# The frozen book: root -> (tick, tick value) as encoded in the
# gauntlet panel parquets (panel columns govern). Alias = root + '.c.0'.
PANEL = {
    'MES': (0.25, 1.25), 'MNQ': (0.25, 0.50), 'MYM': (1.0, 0.50),
    'MGC': (0.10, 1.00), 'MCL': (0.01, 1.00), 'M2K': (0.10, 0.50),
    'M6E': (0.0001, 1.25), 'M6A': (0.0001, 1.00),
}
ROOTS = ['MES', 'MNQ', 'MYM', 'MGC', 'MCL', 'M2K', 'M6E', 'M6A']

# The amendment's two configurations of the same detector:
# (config name, one_per_day). 'as_written' is the alerting book;
# 'unlimited' is scored silently in the same ledger.
CONFIGS = (('as_written', True), ('unlimited', False))


def alias_for(root):
    return root + '.c.0'


def stress_of(net, reason, tick, mult):
    """The gauntlet's p90 stress repricing: stop fills repriced a
    further 2 ticks adverse (level -/+ 4 ticks in total)."""
    return net - (2 * tick * mult if reason == 'stop' else 0.0)


# ---------------------------------------------------------------------------
# The strategy machine: an incremental port of the extension harness's
# first_signal + walk_trade (engine_hud.py) at cutoff 690. State is a
# plain JSON dict so it persists across ticks and restarts; bars must
# be fed strictly in (date, minute) order, each exactly once. Events
# returned per bar: entry / close dicts carrying the harness's own
# accounting (entry_fill / exit_fill / reason / net / stress_net).
# The state's one_per_day flag selects the configuration: True = the
# as-written book; False = the amendment's unlimited configuration
# (the same detector with the daily limit removed).

def new_state(root, one_per_day=True):
    tick, tickval = PANEL[root]
    return {
        'root': root, 'tick': tick, 'tickval': tickval,
        'one_per_day': one_per_day,
        'fed_any': False, 'cur_date': None, 'bar_index': -1,
        'or_high': None, 'or_low': None,
        'swept_high': False, 'swept_low': False,
        'peak': None, 'trough': None,
        'prev_high': None, 'prev_low': None,
        'trade_taken': False, 'pos': None,
        'last_le_close': None, 'last_le_idx': None,
    }


def _close(st, pos, exit_date, exit_idx, exit_fill, reason, events):
    mult = st['tickval'] / st['tick']
    net = pos['side'] * (exit_fill - pos['entry_fill']) * mult - 2 * FEE_SIDE
    ev = {'type': 'close', 'reason': reason,
          'entry_date': pos['entry_date'], 'exit_date': exit_date,
          'side': pos['side'], 'net': net,
          'stress_net': stress_of(net, reason, st['tick'], mult),
          'entry_idx': pos['entry_idx'], 'exit_idx': exit_idx,
          'entry_fill': pos['entry_fill'], 'exit_fill': exit_fill,
          'entry_close': pos['entry_close'], 'stop': pos['stop'],
          'target': pos['target'], 'entry_minute': pos['entry_minute']}
    events.append(ev)
    st['pos'] = None
    return ev


def _fallback_close(st, events):
    """The harness walk's fallback: no bracket hit and no usable
    cutoff bar — exit at the close of the last bar starting <= the
    cutoff (which may be the entry bar itself)."""
    pos = st['pos']
    if pos is None:
        return None
    return _close(st, pos, st['cur_date'], st['last_le_idx'],
                  st['last_le_close'], 'flatten_fallback', events)


def _reset_day(st, date):
    st['cur_date'] = date
    st['fed_any'] = True
    st['bar_index'] = -1
    st['or_high'] = st['or_low'] = None
    st['swept_high'] = st['swept_low'] = False
    st['peak'] = st['trough'] = None
    st['prev_high'] = st['prev_low'] = None
    st['trade_taken'] = False
    st['last_le_close'] = st['last_le_idx'] = None


def on_bar(st, date, minute, o, h, l, c):
    """Feed one completed 5-minute bar (ET session date + start
    minute); returns the bar's events. Mirrors engine_hud.py: OR
    capture, sweep tracking before signals on the same bar (the peak
    includes the signal bar), long block first on a both-side bar,
    one trade per day, bracket walk on bars strictly after the entry
    bar with TARGET checked before STOP, bracket-then-flatten on the
    cutoff bar, fallback at the last <= cutoff close."""
    events = []
    if st['cur_date'] != date:
        # A new session's first bar resolves any position the prior
        # session left open (its fallback flatten), then resets.
        if st['pos'] is not None:
            _fallback_close(st, events)
        _reset_day(st, date)
    st['bar_index'] += 1
    i = st['bar_index']

    if minute > CUTOFF:
        # The walk never evaluates bars past the cutoff; the first
        # such bar settles an open position at the fallback close.
        if st['pos'] is not None:
            _fallback_close(st, events)
        return events

    st['last_le_close'] = c
    st['last_le_idx'] = i

    if OR_LO <= minute < OR_HI:
        if st['or_high'] is None:
            st['or_high'], st['or_low'] = h, l
        else:
            st['or_high'] = max(st['or_high'], h)
            st['or_low'] = min(st['or_low'], l)
    elif minute >= TW_LO:
        # Sweep tracking, before the signal check on the same bar
        # (the peak/trough include the signal bar). The as-written
        # book stops tracking once its trade is taken (the HUD's
        # tradeTakenToday gate); the unlimited configuration tracks
        # on every trade-window bar — the HUD tracking block has no
        # position gate, so the peak/trough keep updating while a
        # position is open.
        if st['or_high'] is not None \
                and (not st['one_per_day'] or not st['trade_taken']):
            if h > st['or_high']:
                st['swept_high'] = True
                st['peak'] = h if st['peak'] is None \
                    else max(st['peak'], h)
            if l < st['or_low']:
                st['swept_low'] = True
                st['trough'] = l if st['trough'] is None \
                    else min(st['trough'], l)
        pos = st['pos']
        if pos is None and minute < CUTOFF \
                and st['or_high'] is not None \
                and (not st['one_per_day'] or not st['trade_taken']):
            rng_range = st['or_high'] - st['or_low']
            bull = (st['swept_low'] and st['trough'] is not None
                    and st['trough'] >= st['or_low'] - EXT_MULT * rng_range
                    and c > st['or_low']
                    and (o <= st['or_low']
                         or (st['prev_low'] is not None
                             and st['prev_low'] < st['or_low'])))
            bear = (st['swept_high'] and st['peak'] is not None
                    and st['peak'] <= st['or_high'] + EXT_MULT * rng_range
                    and c < st['or_high']
                    and (o >= st['or_high']
                         or (st['prev_high'] is not None
                             and st['prev_high'] > st['or_high'])))
            if bull or bear:
                side = 1 if bull else -1
                tick = st['tick']
                if side > 0:
                    stop = l - 2 * tick
                    target = c + RR * (c - stop)
                    entry_fill = c + tick
                else:
                    stop = h + 2 * tick
                    target = c - RR * (stop - c)
                    entry_fill = c - tick
                st['pos'] = {
                    'side': side, 'entry_idx': i, 'entry_date': date,
                    'entry_minute': minute, 'entry_close': c,
                    'entry_fill': entry_fill, 'stop': stop,
                    'target': target}
                st['trade_taken'] = True
                # The HUD consumes the taken side's swept flag on
                # entry; the peak/trough persist. (Unobservable in
                # the as-written book, whose day is over; it is the
                # unlimited configuration's re-entry state.)
                if side > 0:
                    st['swept_low'] = False
                else:
                    st['swept_high'] = False
                events.append({
                    'type': 'entry', 'side': side, 'date': date,
                    'minute': minute, 'entry_idx': i,
                    'entry_close': c, 'entry_fill': entry_fill,
                    'stop': stop, 'target': target,
                    'or_high': st['or_high'], 'or_low': st['or_low']})
        elif pos is not None and i > pos['entry_idx']:
            # Bracket walk (HUD order: target before stop); on the
            # cutoff bar the bracket is evaluated first, then the
            # position flattens at that bar's close.
            side = pos['side']
            if side > 0:
                if h >= pos['target']:
                    _close(st, pos, date, i, pos['target'], 'target',
                           events)
                elif l <= pos['stop']:
                    _close(st, pos, date, i,
                           pos['stop'] - 2 * st['tick'], 'stop', events)
                elif minute == CUTOFF:
                    _close(st, pos, date, i, c, 'flatten', events)
            else:
                if l <= pos['target']:
                    _close(st, pos, date, i, pos['target'], 'target',
                           events)
                elif h >= pos['stop']:
                    _close(st, pos, date, i,
                           pos['stop'] + 2 * st['tick'], 'stop', events)
                elif minute == CUTOFF:
                    _close(st, pos, date, i, c, 'flatten', events)

    st['prev_high'], st['prev_low'] = h, l
    return events


def liquidate_window_end(st):
    """Batch/parity use only — live operation has no window end: a
    position still open after the final fed bar resolves at the
    harness walk's fallback (the last <= cutoff close). Returns the
    close event or None when flat."""
    if st['pos'] is None:
        return None
    events = []
    _fallback_close(st, events)
    return events[0] if events else None


def run_bars(bars, root, one_per_day=True):
    """Batch driver over an ascending bar sequence of dicts with
    date/minute/o/h/l/c — the parity harness shape (the panel day
    lists, bars starting 09:30 through the afternoon; the machine
    itself enforces the window). Returns (trades, events): trades
    are the close events in harness trade-tuple shape."""
    st = new_state(root, one_per_day)
    trades, events = [], []
    for b in bars:
        evs = on_bar(st, b['date'], b['minute'], b['o'], b['h'],
                     b['l'], b['c'])
        events.extend(evs)
        trades.extend(e for e in evs if e['type'] == 'close')
    ev = liquidate_window_end(st)
    if ev is not None:
        events.append(ev)
        trades.append(ev)
    return trades, events


# ---------------------------------------------------------------------------
# Live 5-minute aggregation from the production 1-minute futures bar
# stores (futures_shadow_common.poll_bars). Mirrors the gauntlet
# panel aggregation: ET-clock buckets (bucket start = bar time),
# o/h/l/c = first/max/min/last of the minutes present; a bucket with
# no minutes does not exist. Only the morning window is kept: OR
# through the first buckets past the 11:30 cutoff (the fallback
# trigger).

def bucket_start_ts(date_str, minute):
    d = _date.fromisoformat(date_str)
    return datetime.combine(d, _time(minute // 60, minute % 60),
                            tzinfo=NY).timestamp()


def aggregate_buckets(bars):
    """bars: ascending 1-minute dicts {ts,o,h,l,c,v} (poll_bars
    shape). Returns ascending 5-min bucket dicts {date, minute, o,
    h, l, c, start_ts} for ET start minutes 570..FEED_HI."""
    buckets = {}
    for b in bars:
        dt = datetime.fromtimestamp(b['ts'], NY)
        mins = dt.hour * 60 + dt.minute
        bmin = (mins // 5) * 5
        if not (OR_LO <= bmin <= FEED_HI):
            continue
        key = (dt.date().isoformat(), bmin)
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = {'date': key[0], 'minute': bmin, 'o': b['o'],
                            'h': b['h'], 'l': b['l'], 'c': b['c'],
                            'first_ts': b['ts'], 'last_ts': b['ts']}
        else:
            if b['ts'] < cur['first_ts']:
                cur['o'] = b['o']
                cur['first_ts'] = b['ts']
            if b['ts'] > cur['last_ts']:
                cur['c'] = b['c']
                cur['last_ts'] = b['ts']
            cur['h'] = max(cur['h'], b['h'])
            cur['l'] = min(cur['l'], b['l'])
    out = []
    for b in buckets.values():
        b = dict(b)
        b['start_ts'] = bucket_start_ts(b['date'], b['minute'])
        del b['first_ts'], b['last_ts']
        out.append(b)
    out.sort(key=lambda b: (b['date'], b['minute']))
    return out


# ---------------------------------------------------------------------------
# Display-only shadow alerts (own outbox + own drainer, the
# stock_sweeps / connors_ssc precedent): destination = the futures
# route's configured webhook via alert_routes.destination. These
# alerts never enter the general alert pipeline and never reach any
# order path. Scoring is byte-identical with the alert flag on or off.

PUSH_KIND = 'futures_sweep_push'
MAX_PUSH_ATTEMPTS = 10
REASON_TYPE = {'target': 'target', 'stop': 'stop',
               'flatten': 'cutoff', 'flatten_fallback': 'cutoff'}
REASON_LABEL = {'target': 'TP HIT', 'stop': 'SL HIT',
                'flatten': 'CUTOFF FLATTEN',
                'flatten_fallback': 'CUTOFF FLATTEN (fallback)'}


def _money(v):
    if v is None:
        return '—'
    return '+$%.2f' % v if v >= 0 else '-$%.2f' % abs(v)


def _fmt_num(v, digits=2):
    try:
        return '%.*f' % (digits, float(v))
    except (TypeError, ValueError):
        return '—'


def _side_label(side):
    return 'LONG' if side == 1 else 'SHORT'


def format_entry_message(payload):
    lines = [
        '🧪 **FUTURES SWEEP SHADOW** — shadow forward test · display only',
        '🎯 **ENTRY %s** · %s · %s' % (
            _side_label(payload.get('side')), payload.get('symbol', '?'),
            payload.get('date', '?')),
        'Entry fill `$%s` (signal close `$%s`) · SL `$%s` · TP `$%s`' % (
            _fmt_num(payload.get('entry_fill')),
            _fmt_num(payload.get('entry_close')),
            _fmt_num(payload.get('stop')), _fmt_num(payload.get('target'))),
        'OR range `$%s – $%s` · 1 contract · 11:30 ET cutoff (frozen)' % (
            _fmt_num(payload.get('or_low')), _fmt_num(payload.get('or_high'))),
        '_Shadow measurement only — no order was placed or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Futures Sweep Shadow',
            'allowed_mentions': {'parse': []}}


def format_close_message(payload):
    label = REASON_LABEL.get(payload.get('reason'),
                             str(payload.get('reason', '?')).upper())
    icon = '🎯' if payload.get('reason') == 'target' else \
        '🛑' if payload.get('reason') == 'stop' else '⏰'
    lines = [
        '🧪 **FUTURES SWEEP SHADOW** — shadow forward test · display only',
        '%s **%s** · %s · %s' % (
            icon, label, payload.get('symbol', '?'),
            payload.get('exit_date', '?')),
        '%s · entry fill `$%s` → exit fill `$%s`' % (
            _side_label(payload.get('side')),
            _fmt_num(payload.get('entry_fill')),
            _fmt_num(payload.get('exit_fill'))),
        'Scored P&L **%s** · stress (p90) **%s** · 1 contract, net of '
        'panel commissions' % (
            _money(payload.get('net')), _money(payload.get('stress_net'))),
        '_Shadow measurement only — no order was placed or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Futures Sweep Shadow',
            'allowed_mentions': {'parse': []}}


FORMATTERS = {'entry': format_entry_message, 'target': format_close_message,
              'stop': format_close_message, 'cutoff': format_close_message}


def _queue_alert_row(c, db, payload):
    """Insert one shadow alert into the module's own outbox on the
    caller's connection (the engine unit's transaction: the alert
    commits or rolls back with the episode write it describes)."""
    row_id = identity('futures-sweep-push-v1', payload.get('episode_id', ''),
                      payload.get('type', ''), payload.get('bar_ts', 0))
    c.execute(db.insert(futures_sweep_push_outbox).values(
        id=row_id, status='pending', created=time.time(),
        payload=payload, delivery={'attempts': 0})
        .on_conflict_do_nothing(index_elements=['id']))
    return row_id


def queue_alert(db, cfg, payload):
    """Queue one shadow alert in its own transaction (the connors
    pattern for callers outside a unit transaction). Returns True
    when queued (or when alerts are off)."""
    if not bool(getattr(cfg, 'futures_sweep_alerts', True)):
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
            'futures_sweep: alert queue failed')
        return False


def push_destination(cfg):
    """The futures route destination (url, mode) via the routing
    layer's own resolver; url None when the route is unconfigured."""
    from .alert_routes import destination
    url, mode = destination(cfg, 'futures')
    return (url if url else None), mode


def deliver_one_push(db, client, url, now=None, max_attempts=MAX_PUSH_ATTEMPTS):
    """Attempt one pending shadow alert. Mirrors the fixed ict_push /
    connors drainer semantics: any local failure fails the row with
    the cause recorded — never strands a row in 'sending', never
    raises into the loop. Returns True if a row was attempted."""
    import re
    if not url or not re.fullmatch(
            r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+', url):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        row = c.execute(select(futures_sweep_push_outbox)
                        .where(futures_sweep_push_outbox.c.status == 'pending')
                        .order_by(futures_sweep_push_outbox.c.created).limit(1)
                        ).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        if d.get('next_attempt') and now < d['next_attempt']:
            return False
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(update(futures_sweep_push_outbox)
                  .where(futures_sweep_push_outbox.c.id == row['id'])
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
                delay = max(1, min(3600, float(resp.json().get('retry_after', 30))))
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
        job['delivery']['error'] = '%s: %s' % (type(e).__name__, str(e)[:200])

    if status == 'pending' and job['delivery'].get('attempts', 0) >= max_attempts:
        status = 'failed'

    with db.tx() as c:
        c.execute(update(futures_sweep_push_outbox)
                  .where(futures_sweep_push_outbox.c.id == job['id'])
                  .values(status=status, delivery=job['delivery']))
    if status == 'delivered':
        db.health(PUSH_KIND, 'delivered',
                  'Futures Sweep shadow alert confirmed by Discord', now)
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
                if bool(getattr(cfg, 'futures_sweep', True)) and \
                        bool(getattr(cfg, 'futures_sweep_alerts', True)):
                    url, mode = push_destination(cfg)
                    if url:
                        await asyncio.to_thread(deliver_one_push, db, client, url)
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


def _machine_key(root, cfg_name):
    return MACHINE_PREFIX + root + ('' if cfg_name == 'as_written'
                                    else ':' + cfg_name)


def _open_episode(db, c, root, cfg_name, ev, bar, now, sample):
    tick, tickval = PANEL[root]
    p = {
        'id': identity('futures-sweep-episode-v1', root, cfg_name,
                       ev['date'], bar['start_ts'], ev['side']),
        'version': VERSION, 'strategy': 'futures-sweep-shadow',
        'config': cfg_name,
        'symbol': root, 'underlying': root,
        'side': 'long' if ev['side'] == 1 else 'short', 'dir': ev['side'],
        'status': 'open', 'stage': 'open', 'sample': sample,
        'created_at': now, 'updated_at': now,
        'signal': {'date': ev['date'], 'minute': ev['minute'],
                   'bar_ts': bar['start_ts'],
                   'entry_close': ev['entry_close'],
                   'entry_fill': ev['entry_fill'], 'stop': ev['stop'],
                   'target': ev['target'], 'or_high': ev['or_high'],
                   'or_low': ev['or_low'], 'tick': tick,
                   'tickval': tickval},
        'result': None, 'pnl': None, 'outcome': None,
    }
    stmt = db.insert(episodes).values(
        id=p['id'], underlying=root, status='open', created=now,
        updated=now, payload=p)
    res = c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
    if res.rowcount == 0:
        row = c.execute(select(episodes.c.payload).where(
            episodes.c.id == p['id'])).scalars().first()
        return row, False
    _event(db, c, p, 'episode_created', now,
           {'date': ev['date'], 'side': ev['side'], 'config': cfg_name,
            'entry_fill': ev['entry_fill'], 'stop': ev['stop'],
            'target': ev['target']})
    return p, True


def _alert_payloads_for_event(p, ev, bar):
    """Alert payloads for one machine event on episode p: ENTRY on
    the entry event; TP HIT / SL HIT / CUTOFF on the close event
    (the registration's four alert types)."""
    base = {'episode_id': p['id'], 'symbol': p['symbol'],
            'bar_ts': bar['start_ts']}
    if ev['type'] == 'entry':
        return [{**base, 'type': 'entry', 'side': ev['side'],
                 'entry_close': ev['entry_close'],
                 'entry_fill': ev['entry_fill'], 'stop': ev['stop'],
                 'target': ev['target'], 'or_high': ev['or_high'],
                 'or_low': ev['or_low'], 'date': ev['date']}]
    if ev['type'] == 'close':
        return [{**base, 'type': REASON_TYPE[ev['reason']],
                 'side': ev['side'], 'reason': ev['reason'],
                 'entry_fill': ev['entry_fill'],
                 'exit_fill': ev['exit_fill'], 'net': ev['net'],
                 'stress_net': ev['stress_net'],
                 'exit_date': ev['exit_date']}]
    return []


def _apply_event(db, c, root, cfg_name, ev, bar, now, sample,
                 open_eps, alerts_out):
    """Book one machine event: episode create/update + lifecycle
    event + alert payloads (queued by the caller on this same
    transaction). open_eps maps (root, config) -> its open episode
    payload. Only the as-written book produces alert payloads; the
    unlimited configuration is booked silently (amendment)."""
    key = (root, cfg_name)
    if ev['type'] == 'entry':
        p, created = _open_episode(db, c, root, cfg_name, ev, bar,
                                   now, sample)
        open_eps[key] = p
        if created and cfg_name == 'as_written':
            alerts_out.extend(_alert_payloads_for_event(p, ev, bar))
        return p
    p = open_eps.get(key)
    if p is None:
        # A close event with no open episode in this pass: reload
        # the (root, config)'s open episode (created in an earlier
        # tick). At most one open episode exists per (root, config).
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.underlying == root, episodes.c.status == 'open')
            .order_by(episodes.c.created.desc())).scalars().all()
        for row in rows:
            if row.get('config', 'as_written') == cfg_name:
                p = row
                break
        if p is None:
            return None
        open_eps[key] = p
    if ev['type'] == 'close':
        p['result'] = {
            'entry_fill': ev['entry_fill'], 'exit_fill': ev['exit_fill'],
            'exit_date': ev['exit_date'], 'exit_minute': bar['minute'],
            'reason': ev['reason'], 'net': ev['net'],
            'stress_net': ev['stress_net']}
        p['pnl'] = ev['net']
        p['outcome'] = ('win' if ev['net'] > 0 else
                        'loss' if ev['net'] < 0 else 'breakeven')
        p['stage'] = 'closed'
        p['status'] = 'closed'
        _event(db, c, p, 'closed', now,
               {'date': ev['exit_date'], 'reason': ev['reason'],
                'config': cfg_name,
                'net': ev['net'], 'stress_net': ev['stress_net']})
        _save(db, c, p, now, status='closed')
        if cfg_name == 'as_written':
            alerts_out.extend(_alert_payloads_for_event(p, ev, bar))
        if open_eps.get(key) is p:
            del open_eps[key]
        return p
    return p


def _write_state(db, c, now, extra=None):
    """Recompute the summary from the episode table: counts, per-root
    headline + stress stats (book_stats over the same forward
    trades) reported per configuration (as_written alongside
    unlimited, per the amendment), pooled totals per configuration,
    and the verdict clock (weeks since the first eligible session;
    verdict at >= 12 weeks AND >= 50 trades per root per
    configuration — factual reporting only, no verdict logic)."""
    rows = c.execute(select(episodes.c.payload)).scalars().all()
    counts = {'created': 0, 'open': 0, 'closed': 0}
    cfg_names = [name for name, _ in CONFIGS]
    per_root = {}
    for p in rows:
        counts['created'] += 1
        if p['status'] in counts:
            counts[p['status']] += 1
        r = per_root.setdefault(
            p['symbol'],
            {name: {'nets': [], 'stress': []} for name in cfg_names})
        bucket = r.get(p.get('config', 'as_written'))
        if bucket is not None and p.get('result'):
            bucket['nets'].append(p['result']['net'])
            bucket['stress'].append(p['result']['stress_net'])

    def _sub(bucket):
        s = book_stats(bucket['nets'])
        s['stress_net'] = round(sum(bucket['stress']), 2)
        return s

    roots_out = {}
    pooled_buckets = {name: {'nets': [], 'stress': []}
                      for name in cfg_names}
    for root in ROOTS:
        r = per_root.get(root, pooled_buckets)
        roots_out[root] = {name: _sub(r[name]) for name in cfg_names}
        for name in cfg_names:
            pooled_buckets[name]['nets'].extend(r[name]['nets'])
            pooled_buckets[name]['stress'].extend(r[name]['stress'])
    pooled = {name: _sub(pooled_buckets[name]) for name in cfg_names}
    old = db.get(c, STATE_KEY, {})
    first_session = old.get('first_session')
    weeks = None
    if first_session:
        weeks = round((_date.today() - _date.fromisoformat(first_session)).days
                      / 7.0, 1)
    state = {'version': VERSION, 'at': now, 'counts': counts,
             'configs': cfg_names,
             'per_root': roots_out, 'pooled': pooled,
             'book': {r: {'tick': PANEL[r][0], 'tickval': PANEL[r][1]}
                      for r in ROOTS},
             'bootstrap_at': old.get('bootstrap_at'),
             'first_session': first_session,
             'weeks_since_first_session': weeks,
             'gap_polls': old.get('gap_polls', 0),
             **(extra or {})}
    db.put(c, STATE_KEY, state)
    return state


def scan(db, c, cfg, now):
    """One live pass over the frozen book: poll each root's newly
    arrived 1-minute bars, aggregate the completed 5-minute morning
    buckets, feed them into the root's persisted machines (both
    configurations), and book the events as episodes + shadow
    alerts (alerts for the as-written book only). Runs inside the
    caller's (engine unit) transaction."""
    if not getattr(cfg, 'futures_sweep', True):
        return {'ran': False, 'reason': 'disabled'}
    alerts_on = bool(getattr(cfg, 'futures_sweep_alerts', True))
    state = db.get(c, STATE_KEY, {})
    if 'bootstrap_at' not in state:
        state['bootstrap_at'] = now
        state['gap_polls'] = 0
        db.put(c, STATE_KEY, state)
    alerts_out = []
    open_eps = {}
    fed = entries = closes = 0
    entries_u = closes_u = 0
    for root in ROOTS:
        # One poll per root feeds both configurations' machines
        # (same bars, same buckets; the machines differ only in the
        # one_per_day flag). Their cursors advance in lockstep.
        machines = []
        for cfg_name, one_pd in CONFIGS:
            mkey = _machine_key(root, cfg_name)
            m = db.get(c, mkey)
            if m is None:
                m = {'state': new_state(root, one_pd),
                     'fed_bucket_ts': None, 'poll_after': None,
                     'bootstrapped_at': now}
            machines.append((cfg_name, mkey, m))
        bars, gap = poll_bars(db, c, alias_for(root), root,
                              machines[0][2].get('poll_after'))
        if gap:
            state['gap_polls'] = state.get('gap_polls', 0) + 1
            db.put(c, STATE_KEY, state)
        buckets = aggregate_buckets(bars)
        if any(m.get('poll_after') is None for _, _, m in machines):
            # Bootstrap watermark (stock_sweeps / SH-SPY pattern):
            # episodes form only from bars that complete AFTER the
            # shadow is live — the cursor starts at the newest polled
            # bar and history is never fed. The first session whose
            # 09:30 ET bar the shadow itself feeds becomes the first
            # eligible session.
            done = [b for b in buckets if b['start_ts'] + 300 <= now]
            for cfg_name, mkey, m in machines:
                if m.get('poll_after') is None:
                    m['poll_after'] = bars[-1]['ts'] if bars else now
                    m['fed_bucket_ts'] = done[-1]['start_ts'] \
                        if done else 0
                db.put(c, mkey, m)
            continue
        # Consume every polled minute except those belonging to a
        # morning bucket that has not completed yet — those are
        # re-polled next pass (poll_after stays before them).
        incomplete = [b['start_ts'] for b in buckets
                      if b['start_ts'] + 300 > now]
        horizon = min(incomplete) if incomplete else None
        for b in buckets:
            if b['start_ts'] + 300 > now:
                continue
            if b['minute'] == OR_LO and not state.get('first_session'):
                state['first_session'] = b['date']
                db.put(c, STATE_KEY, state)
            sample = bool(state.get('first_session')) \
                and b['date'] >= state['first_session']
            for cfg_name, mkey, m in machines:
                if b['start_ts'] <= (m.get('fed_bucket_ts') or 0):
                    continue
                evs = on_bar(m['state'], b['date'], b['minute'],
                             b['o'], b['h'], b['l'], b['c'])
                m['fed_bucket_ts'] = b['start_ts']
                fed += 1
                for ev in evs:
                    if ev['type'] == 'entry':
                        if cfg_name == 'as_written':
                            entries += 1
                        else:
                            entries_u += 1
                    if ev['type'] == 'close':
                        if cfg_name == 'as_written':
                            closes += 1
                        else:
                            closes_u += 1
                    _apply_event(db, c, root, cfg_name, ev, b, now,
                                 sample, open_eps, alerts_out)
        for cfg_name, mkey, m in machines:
            if bars:
                if horizon is None:
                    m['poll_after'] = bars[-1]['ts']
                else:
                    before = [b['ts'] for b in bars
                              if b['ts'] < horizon]
                    if before:
                        m['poll_after'] = max(before)
            db.put(c, mkey, m)
    if alerts_on:
        for payload in alerts_out:
            _queue_alert_row(c, db, payload)
    summary = _write_state(db, c, now)
    counts = summary['counts']
    return {'ran': True, 'roots': len(ROOTS), 'fed': fed,
            'entries': entries, 'closes': closes,
            'entries_unlimited': entries_u, 'closes_unlimited': closes_u,
            'open_episodes': counts['open'],
            'first_session': summary.get('first_session')}


class FuturesSweep:
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
            if not self.db.lease(c, 'futures-sweep', self.owner, 300):
                return
        self.process(self.now(time.time()))
