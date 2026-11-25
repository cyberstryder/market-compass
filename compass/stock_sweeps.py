"""Stock Sweeps — forward shadow (2026-11-16, Josh).

Paper / measurement scope ONLY: no orders, no live sends, nothing to
any broker. The one outward surface is display-only shadow alerts to
the #compass Discord channel (research route; own outbox, own
drainer, the connors_ssc / SH-SPY §6 pattern). They posted to the
0DTE channel until 2026-11-25, when Josh moved every shadow book to
#compass so #0dte carries the 0DTE scanner funnel alone.

This module shadow-fires Josh's Stock_Sweeps.txt ("Opening Range
Sweep & Reclaim [Unified Options v4.3]") on the 23 symbols that
passed the frozen full-list screen, exactly as frozen in the forward
shadow registration
(files/stock-sweeps-forward-shadow-registration-2026-11-16.md):

- Book (frozen): AMD at its fitted 0.35 stop buffer; HON, SMH, QQQ,
  SHOP, ALAB, SLV, SHW, ZS, XLK, ZSL, AAPL, VRT, BKNG, SBUX, BABA,
  KO, ROST, HIMS, NFLX, BMY, MARA, BAC at the script's 0.50 fallback.
  ZSL is a daily-reset inverse product; it is in the set because it
  passed, and its results carry that note.
- Semantics: the screen's frozen port (registration
  files/stock-sweeps-screen-registration-2026-11-12.md) reproduced
  bar-for-bar by SweepMachine below — a stateful, JSON-serializable
  machine fed one completed 15-minute RTH bar at a time. The parity
  gate (test_stock_sweeps.py) pins it against the screen's verified
  trade logs: 4,431 trades, pooled +$57,619 over
  2025-10-27 → 2026-10-28.
- Scoring (primary, underlying): the strategy's own accounting —
  100-share unit, TP1 leg 50 / runner leg 50 as separate legs,
  $0.65 per filled order — from live 15-minute bars aggregated from
  the production 1-minute bar pipeline.
- Premium measurement (secondary, never blended): at each entry, the
  NEAREST-EXPIRY contract nearest 0.50 |delta| on the signal side —
  the expiry on the entry day itself where the name has one (0DTE),
  otherwise the next available expiry (Josh's amendment 2026-11-16
  to the registration, made before any forward data existed) — its
  entry ask, and its bid at the episode's exit events, from the
  chain/quote pipeline (live chain:<SYM> state + quote:<OCC> stream
  rows, the sh_spy_options pattern). Premium net per episode follows
  the script's own options model: 2 contracts, TP1 leg 1 contract at
  the TP1-event bid, runner 1 contract at the final-exit bid (both
  contracts at the final bid when no TP1 filled), $0.65 per contract
  per side. Episodes that cannot be priced are counted as data gaps,
  never estimated.
- Sample rule: episodes form only from bars that complete after the
  deployed shadow's bootstrap watermark (the SH-SPY bootstrap
  pattern); the first eligible session is the first session whose
  09:30 bar the shadow itself feeds, recorded in the state summary.
  No backfill.

Wiring: the driver is an engine pre-ICT unit (its own transaction
via Engine._pre_ict_unit, the 2026-11-15 isolation pattern); the
alert drainer is a standalone loop spawned in app.py. Neither can
throw into the engine tick.
"""
import asyncio
import logging
import time
import uuid
from datetime import date as _date, datetime, time as _time, timedelta

import httpx
from sqlalchemy import select, update

from .market import NY, day, number
from .store import identity
from .store import stock_sweeps_episodes as episodes
from .store import stock_sweeps_push_outbox

VERSION = 'stock-sweeps-v1'
KIND = 'stock_sweeps'
STATE_KEY = 'stock_sweeps:state'
MACHINE_PREFIX = 'stock_sweeps:machine:'

COMM = 0.65           # per filled order (the script's own commission)
QTY = 100             # share unit (2 contracts x 100 x 0.50 delta)
TP1_QTY = 50
EXT_MULT = 1.0        # trend-extension filter: probe <= 1.0 x OR range
FEE = 0.65            # premium leg: per contract, per side
PREMIUM_QTY = 2       # the script's options model: 2 contracts
CHAIN_STALE_S = 120   # chain asof may lag the entry bar end by this
PRICE_GRACE_S = 7200  # premium pricing retries bounded by this

# The frozen book: symbol -> stop buffer (screen registration).
BOOK = {
    'AMD': 0.35,
    'HON': 0.50, 'SMH': 0.50, 'QQQ': 0.50, 'SHOP': 0.50, 'ALAB': 0.50,
    'SLV': 0.50, 'SHW': 0.50, 'ZS': 0.50, 'XLK': 0.50, 'ZSL': 0.50,
    'AAPL': 0.50, 'VRT': 0.50, 'BKNG': 0.50, 'SBUX': 0.50, 'BABA': 0.50,
    'KO': 0.50, 'ROST': 0.50, 'HIMS': 0.50, 'NFLX': 0.50, 'BMY': 0.50,
    'MARA': 0.50, 'BAC': 0.50,
}
SYMBOLS = sorted(BOOK)


# ---------------------------------------------------------------------------
# The strategy machine: a line-by-line incremental port of the screen's
# simulate.run_symbol (hidden_files/stock-sweeps-screen-2026-11-12/code/
# simulate.py). State is a plain JSON dict so it persists across ticks
# and restarts; bars must be fed strictly in (date, minute) order, each
# exactly once. Events returned per bar: entry / tp1 / close dicts with
# the screen's own accounting.

def new_state(buffer):
    return {
        'buffer': buffer,
        'fed_any': False, 'cur_date': None, 'bar_index': -1,
        'or_high': None, 'or_low': None, 'or_mid': None, 'or_range': None,
        'swept_high': False, 'swept_low': False, 'peak': None, 'trough': None,
        'loss_count': 0, 'lockout': False, 'pending': [],
        'pos': None, 'prev_high': None, 'prev_low': None,
        'pv': 0.0, 'vv': 0.0,
    }


def _close_trade(st, pos, exit_date, reason, events):
    """The screen's _close_trade: net stamped, net queued for the
    next bar's circuit-breaker accounting."""
    net = pos['gross'] - COMM * pos['orders']
    ev = {'type': 'close', 'reason': reason,
          'entry_date': pos['entry_date'], 'exit_date': exit_date,
          'side': pos['side'], 'net': net, 'gross': pos['gross'],
          'orders': pos['orders'], 'exit_idx': st['bar_index'],
          'entry_px': pos['entry_px'], 'exit_px': pos['last_fill'],
          'tp1_done': pos['tp1_done'], 'tp1_fill': pos['tp1_fill'],
          'tp1_gross': pos['tp1_gross'], 'runner_gross': pos['runner_gross']}
    events.append(ev)
    st['pending'].append(net)
    return ev


def on_bar(st, date, minute, o, h, l, c, v):
    """Feed one completed 15-minute RTH bar; returns the bar's events.
    Mirrors simulate.run_symbol's per-bar sequence exactly: (1) new-day
    reset, (2) circuit-breaker accounting, (3) OR capture, (4) sweep
    tracking, (5) entry evaluation, (6) exit management, (7) flatten."""
    buf = st['buffer']
    events = []
    st['bar_index'] += 1
    i = st['bar_index']
    new_day = (not st['fed_any']) or (date != st['cur_date'])
    if new_day:
        st['cur_date'] = date
        st['fed_any'] = True
        st['or_high'] = st['or_low'] = st['or_mid'] = st['or_range'] = None
        st['swept_high'] = st['swept_low'] = False
        st['peak'] = st['trough'] = None
        st['loss_count'] = 0
        st['lockout'] = False
        st['pv'] = 0.0
        st['vv'] = 0.0
        if st['pos'] is not None:
            # Literal Pine newDay reset of the exit-state vars, even
            # mid-position (screen porting note 2, ported as written).
            st['pos']['entry_var'] = None
            st['pos']['stop'] = None
            st['pos']['mid_reached'] = False
    # Circuit breaker: count trades closed at the prior bar's close.
    for p in st['pending']:
        if p < 0:
            st['loss_count'] += 1
            if st['loss_count'] >= 1:
                st['lockout'] = True
    st['pending'] = []

    # Session VWAP over the day's 15-min bars including the current
    # bar (the screen's load_bars cumsum, accumulated identically).
    tp = (h + l + c) / 3.0
    st['pv'] += tp * v
    st['vv'] += v
    vwap = st['pv'] / st['vv'] if st['vv'] else float('nan')

    # OR capture: the 09:30 bucket.
    if minute == 570:
        if st['or_high'] is None:
            st['or_high'], st['or_low'] = h, l
        else:
            st['or_high'] = max(st['or_high'], h)
            st['or_low'] = min(st['or_low'], l)
        st['or_range'] = st['or_high'] - st['or_low']
        st['or_mid'] = (st['or_high'] + st['or_low']) / 2.0

    in_trade = 585 <= minute < 870
    # Sweep tracking (runs flat or in position, as written).
    if in_trade and not st['lockout'] and st['or_high'] is not None:
        if h > st['or_high']:
            st['swept_high'] = True
            st['peak'] = h if st['peak'] is None else max(st['peak'], h)
        if l < st['or_low']:
            st['swept_low'] = True
            st['trough'] = l if st['trough'] is None else min(st['trough'], l)

    # Entries (flat only; the long block is evaluated first).
    entered_now = False
    pos = st['pos']
    if pos is None and in_trade and not st['lockout'] and st['or_high'] is not None:
        high_valid = (st['peak'] is not None
                      and st['peak'] <= st['or_high'] + st['or_range'] * EXT_MULT)
        low_valid = (st['trough'] is not None
                     and st['trough'] >= st['or_low'] - st['or_range'] * EXT_MULT)
        bullish = (st['swept_low'] and low_valid and c > st['or_low']
                   and (o <= st['or_low']
                        or (st['prev_low'] is not None and st['prev_low'] < st['or_low'])))
        bearish = (st['swept_high'] and high_valid and c < st['or_high']
                   and (o >= st['or_high']
                        or (st['prev_high'] is not None and st['prev_high'] > st['or_high'])))
        if bullish or bearish:
            side = 1 if bullish else -1
            if side == 1:
                stop = l - buf
                st['swept_low'] = False
                st['trough'] = None
            else:
                stop = h + buf
                st['swept_high'] = False
                st['peak'] = None
            pos = {
                'side': side, 'entry_px': c, 'entry_var': c, 'stop': stop,
                'mid_reached': False, 'tp1_done': False,
                'tp1_exists': (st['or_mid'] > c) if side == 1 else (st['or_mid'] < c),
                'gross': 0.0, 'tp1_gross': 0.0, 'runner_gross': 0.0,
                'orders': 1, 'entry_date': date, 'entry_idx': i,
                'tp1_fill': None, 'last_fill': None,
            }
            st['pos'] = pos
            entered_now = True
            events.append({'type': 'entry', 'side': side, 'entry_px': c,
                           'stop': stop, 'or_mid': st['or_mid'],
                           'or_high': st['or_high'], 'or_low': st['or_low'],
                           'date': date, 'minute': minute, 'entry_idx': i})

    # Exit management (bars after the entry bar only).
    if pos is not None and not entered_now and i > pos['entry_idx']:
        entry = pos['entry_var']
        if entry is not None:
            side = pos['side']
            mid = st['or_mid']
            mid_beyond = (mid > entry) if side == 1 else (mid < entry)
            stop = pos['stop']
            stop_hit = False
            stop_fill = None
            if stop is not None:
                if side == 1 and l <= stop:
                    stop_hit = True
                    stop_fill = o if o < stop else stop
                elif side == -1 and h >= stop:
                    stop_hit = True
                    stop_fill = o if o > stop else stop
            if stop_hit:
                qty_rem = QTY - (TP1_QTY if pos['tp1_done'] else 0)
                add = side * (stop_fill - pos['entry_px']) * qty_rem
                pos['gross'] += add
                pos['runner_gross'] += add
                pos['orders'] += 2 if (pos['tp1_exists'] and not pos['tp1_done']) else 1
                pos['last_fill'] = stop_fill
                _close_trade(st, pos, date, 'stop', events)
                st['pos'] = None
                pos = None
            else:
                tp1_event = None
                # TP1 limit (stop-first ordering: only when not stopped).
                if not pos['tp1_done'] and mid_beyond:
                    if side == 1 and h >= mid:
                        fill = o if o > mid else mid
                        add = (fill - pos['entry_px']) * TP1_QTY
                        pos['gross'] += add
                        pos['tp1_gross'] += add
                        pos['orders'] += 1
                        pos['tp1_done'] = True
                        pos['tp1_fill'] = fill
                        tp1_event = {'type': 'tp1', 'side': side, 'fill': fill,
                                     'date': date, 'minute': minute,
                                     'leg_gross': add, 'stop': pos['stop']}
                        events.append(tp1_event)
                    elif side == -1 and l <= mid:
                        fill = o if o < mid else mid
                        add = -1 * (fill - pos['entry_px']) * TP1_QTY
                        pos['gross'] += add
                        pos['tp1_gross'] += add
                        pos['orders'] += 1
                        pos['tp1_done'] = True
                        pos['tp1_fill'] = fill
                        tp1_event = {'type': 'tp1', 'side': side, 'fill': fill,
                                     'date': date, 'minute': minute,
                                     'leg_gross': add, 'stop': pos['stop']}
                        events.append(tp1_event)
                # Midpoint touch -> breakeven (governs later bars).
                if not pos['mid_reached'] and mid_beyond:
                    if (side == 1 and h >= mid) or (side == -1 and l <= mid):
                        pos['mid_reached'] = True
                        be = entry + 0.05 if side == 1 else entry - 0.05
                        pos['stop'] = (max(pos['stop'], be) if side == 1
                                       else min(pos['stop'], be))
                        if tp1_event is not None:
                            tp1_event['stop'] = pos['stop']
                # VWAP exit at bar close.
                if pos['mid_reached']:
                    if side == 1 and c < vwap and c > entry:
                        qty_rem = QTY - (TP1_QTY if pos['tp1_done'] else 0)
                        add = (c - pos['entry_px']) * qty_rem
                        pos['gross'] += add
                        pos['runner_gross'] += add
                        pos['orders'] += 1
                        pos['last_fill'] = c
                        _close_trade(st, pos, date, 'vwap', events)
                        st['pos'] = None
                        pos = None
                    elif side == -1 and c > vwap and c < entry:
                        qty_rem = QTY - (TP1_QTY if pos['tp1_done'] else 0)
                        add = -1 * (c - pos['entry_px']) * qty_rem
                        pos['gross'] += add
                        pos['runner_gross'] += add
                        pos['orders'] += 1
                        pos['last_fill'] = c
                        _close_trade(st, pos, date, 'vwap', events)
                        st['pos'] = None
                        pos = None
        # Flatten on the 15:45 bar (close), after exit management —
        # evaluated even when the exit-state vars were reset overnight.
        if pos is not None and minute == 945:
            qty_rem = QTY - (TP1_QTY if pos['tp1_done'] else 0)
            add = pos['side'] * (c - pos['entry_px']) * qty_rem
            pos['gross'] += add
            pos['runner_gross'] += add
            pos['orders'] += 1
            pos['last_fill'] = c
            _close_trade(st, pos, date, 'flatten', events)
            st['pos'] = None
            pos = None

    st['prev_high'], st['prev_low'] = h, l
    return events


def liquidate_window_end(st, date, close):
    """The screen's window-end liquidation (batch/parity use only —
    live operation has no window end). Returns the close event or
    None when flat. The net is NOT queued for circuit-breaker
    accounting, exactly as the screen's tail block passes a throwaway
    pending list."""
    pos = st['pos']
    if pos is None:
        return None
    qty_rem = QTY - (TP1_QTY if pos['tp1_done'] else 0)
    add = pos['side'] * (close - pos['entry_px']) * qty_rem
    pos['gross'] += add
    pos['runner_gross'] += add
    pos['orders'] += 1
    pos['last_fill'] = close
    net = pos['gross'] - COMM * pos['orders']
    ev = {'type': 'close', 'reason': 'window_end',
          'entry_date': pos['entry_date'], 'exit_date': date,
          'side': pos['side'], 'net': net, 'gross': pos['gross'],
          'orders': pos['orders'], 'exit_idx': st['bar_index'],
          'entry_px': pos['entry_px'], 'exit_px': close,
          'tp1_done': pos['tp1_done'], 'tp1_fill': pos['tp1_fill'],
          'tp1_gross': pos['tp1_gross'], 'runner_gross': pos['runner_gross']}
    st['pos'] = None
    return ev


def run_bars(bars, buffer):
    """Batch driver over an ascending bar sequence of dicts with
    date/minute/o/h/l/c/v — the parity harness shape. Returns
    (trades, events): trades are the screen's trade-log rows (close
    events), events every machine event in order."""
    st = new_state(buffer)
    trades, events = [], []
    for b in bars:
        evs = on_bar(st, b['date'], b['minute'], b['o'], b['h'],
                     b['l'], b['c'], b['v'])
        events.extend(evs)
        trades.extend(e for e in evs if e['type'] == 'close')
    if bars:
        ev = liquidate_window_end(st, bars[-1]['date'], bars[-1]['c'])
        if ev is not None:
            events.append(ev)
            trades.append(ev)
    return trades, events


# ---------------------------------------------------------------------------
# Live 15-minute aggregation from the production 1-minute bar pipeline.
# Mirrors the screen's aggregate_15m.py: RTH minutes 570..959, bucket =
# (minute // 15) * 15, a bucket exists only if it holds >= 1 traded
# minute; OHLC = first/max/min/last in ts order, volume = sum.

def aggregate_buckets(rows):
    """rows: bar event dicts ({ts, id, payload{o,h,l,c,v}}). Returns
    ascending 15-min bucket dicts {date, minute, o, h, l, c, v,
    start_ts}. Duplicate minute rows keep the highest event id."""
    by_ts = {}
    for r in rows:
        p = r.get('payload') or {}
        vals = [number(p.get(k)) for k in ('o', 'h', 'l', 'c', 'v')]
        if any(v is None for v in vals):
            continue
        ts = r['ts']
        if ts not in by_ts or (r.get('id') or 0) > by_ts[ts][0]:
            by_ts[ts] = (r.get('id') or 0, vals)
    buckets = {}
    for ts, (_id, (o, h, l, c, v)) in by_ts.items():
        dt = datetime.fromtimestamp(ts, NY)
        mins = dt.hour * 60 + dt.minute
        if not (570 <= mins < 960):
            continue
        bmin = (mins // 15) * 15
        key = (dt.date().isoformat(), bmin)
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = {'date': key[0], 'minute': bmin, 'o': o, 'h': h,
                            'l': l, 'c': c, 'v': v,
                            'first_ts': ts, 'last_ts': ts}
        else:
            if ts < cur['first_ts']:
                cur['o'] = o
                cur['first_ts'] = ts
            if ts > cur['last_ts']:
                cur['c'] = c
                cur['last_ts'] = ts
            cur['h'] = max(cur['h'], h)
            cur['l'] = min(cur['l'], l)
            cur['v'] += v
    out = []
    for b in buckets.values():
        b = dict(b)
        b['start_ts'] = bucket_start_ts(b['date'], b['minute'])
        del b['first_ts'], b['last_ts']
        out.append(b)
    out.sort(key=lambda b: (b['date'], b['minute']))
    return out


def bucket_start_ts(date_str, minute):
    d = _date.fromisoformat(date_str)
    return datetime.combine(d, _time(minute // 60, minute % 60),
                            tzinfo=NY).timestamp()


# ---------------------------------------------------------------------------
# Premium measurement: nearest-expiry contract nearest 0.50 |delta|
# (amended registration text), priced from the chain/quote pipeline.

def expiry_instant(expiry_date):
    """An expiry's life ends at 16:00 ET on its expiry date."""
    return datetime.fromisoformat(expiry_date).replace(
        hour=16, minute=0, second=0, tzinfo=NY).timestamp()


def select_contract(contracts, side, entry_instant, spot):
    """Nearest-expiry selection (Josh's 2026-11-16 amendment): type
    by signal side (long -> call, short -> put); the FIRST expiry
    whose life extends past the entry instant — the entry day's own
    expiry where the name has one (0DTE), otherwise the next
    available expiry; within it, the strike whose |delta| is nearest
    0.50 over the strikes carrying a quoted delta, tie-breaks strike
    closest to spot, then lower strike. Returns the chain contract
    dict or None."""
    want = 'call' if side == 1 else 'put'
    typed = [o for o in contracts or []
             if str(o.get('type') or '').lower() == want and o.get('expiry')]
    expiries = sorted({str(o['expiry']) for o in typed})
    chosen_expiry = None
    for e in expiries:
        if expiry_instant(e) > entry_instant:
            chosen_expiry = e
            break
    if chosen_expiry is None:
        return None
    cands = []
    for o in typed:
        if str(o['expiry']) != chosen_expiry:
            continue
        delta = number(o.get('delta'))
        strike = number(o.get('strike'))
        if delta is None or strike is None or not o.get('symbol'):
            continue
        cands.append(o)
    if not cands:
        return None

    def key(o):
        return (abs(abs(number(o['delta'])) - 0.50),
                abs(number(o['strike']) - spot) if spot else 0.0,
                number(o['strike']))
    return min(cands, key=key)


def _quote_candidate(q, source):
    """Normalize a quote dict to (ts, bid, ask, source) or None
    (the sh_spy_options pattern)."""
    if not isinstance(q, dict):
        return None
    ts, bid, ask = number(q.get('ts')), number(q.get('bid')), number(q.get('ask'))
    if ts is None or bid is None or ask is None or ask < bid or ask <= 0:
        return None
    return ts, bid, ask, source


def first_quote_at_or_after(db, c, chain, contract_symbol, since_ts):
    """The first OPRA quote for the contract at/after since_ts that
    the scorer can observe: the stream quote row (quote:<OCC>) and
    the live chain state's contract quote are both candidates; the
    smallest quote timestamp at/after since_ts wins, the stream row
    breaking ties. Returns (ts, bid, ask, source) or None.
    (sh_spy_options pattern, verbatim in spirit.)"""
    cands = []
    q = db.get(c, 'quote:' + contract_symbol)
    cand = _quote_candidate(q, 'stream')
    if cand and cand[0] >= since_ts:
        cands.append(cand)
    for o in (chain or {}).get('contracts', []):
        if o.get('symbol') == contract_symbol:
            cand = _quote_candidate(o.get('quote') or {}, 'chain')
            if cand and cand[0] >= since_ts:
                cands.append(cand)
            break
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], 0 if x[3] == 'stream' else 1))
    return cands[0]


def premium_net(entry_ask, tp1_bid, exit_bid, tp1_done):
    """The script's options model, per episode: 2 contracts in at
    the ask; the TP1 leg (1 contract) out at the TP1-event bid and
    the runner (1 contract) at the final-exit bid — or both contracts
    at the final bid when no TP1 filled; $0.65/contract/side."""
    if tp1_done:
        proceeds = (tp1_bid + exit_bid) * 100
    else:
        proceeds = 2 * exit_bid * 100
    return proceeds - 2 * entry_ask * 100 - FEE * 2 * PREMIUM_QTY


# ---------------------------------------------------------------------------
# Display-only shadow alerts (own outbox + own drainer, the
# connors_ssc / SH-SPY §6 precedent): destination = the research
# route's configured webhook via alert_routes.destination. These
# alerts never enter the general alert pipeline. Scoring is
# byte-identical with the alert flag on or off.

PUSH_KIND = 'stock_sweeps_push'
MAX_PUSH_ATTEMPTS = 10
REASON_LABEL = {'stop': 'STOP', 'vwap': 'VWAP MOMENTUM EXIT',
                'flatten': 'EOD FLATTEN', 'window_end': 'WINDOW END'}


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
    return 'LONG (CALL side)' if side == 1 else 'SHORT (PUT side)'


def format_entry_message(payload):
    lines = [
        '🧪 **STOCK SWEEPS SHADOW** — shadow forward test · display only',
        '🎯 **ENTRY %s** · %s · %s' % (
            _side_label(payload.get('side')), payload.get('symbol', '?'),
            payload.get('date', '?')),
        'Underlying entry `$%s` · stop `$%s` · OR midpoint TP1 `$%s`' % (
            _fmt_num(payload.get('entry_px')), _fmt_num(payload.get('stop')),
            _fmt_num(payload.get('or_mid'))),
        'OR range `$%s – $%s` · stop buffer `$%s` (frozen book)' % (
            _fmt_num(payload.get('or_low')), _fmt_num(payload.get('or_high')),
            _fmt_num(payload.get('buffer'))),
        '_Shadow measurement only — not a bookable trade; nothing was booked or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Stock Sweeps Shadow',
            'allowed_mentions': {'parse': []}}


def format_tp1_message(payload):
    lines = [
        '🧪 **STOCK SWEEPS SHADOW** — shadow forward test · display only',
        '🎯 **TP1 MIDPOINT HIT** · %s · %s' % (
            payload.get('symbol', '?'), payload.get('date', '?')),
        'Scaled out 50%% at `$%s` (leg %s) · stop moved to breakeven `$%s`' % (
            _fmt_num(payload.get('fill')), _money(payload.get('leg_gross')),
            _fmt_num(payload.get('stop'))),
        '_Shadow measurement only — not a bookable trade; nothing was booked or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Stock Sweeps Shadow',
            'allowed_mentions': {'parse': []}}


def format_close_message(payload):
    reason = REASON_LABEL.get(payload.get('reason'),
                              str(payload.get('reason', '?')).upper())
    lines = [
        '🧪 **STOCK SWEEPS SHADOW** — shadow forward test · display only',
        '🏁 **CLOSE — %s** · %s · %s' % (
            reason, payload.get('symbol', '?'), payload.get('exit_date', '?')),
        '%s · entry `$%s` → exit `$%s`' % (
            _side_label(payload.get('side')),
            _fmt_num(payload.get('entry_px')), _fmt_num(payload.get('exit_px'))),
        'Scored P&L **%s** (underlying, 100 sh) · TP1 leg %s · runner leg %s' % (
            _money(payload.get('net')), _money(payload.get('tp1_gross')),
            _money(payload.get('runner_gross'))),
    ]
    # Premium leg (2026-11-25): the episode's own premium scoring,
    # attached at queue time by _attach_premium — the same result
    # that feeds the tracking premium stats. A premium still being
    # measured or lost to a data gap is labeled, never estimated;
    # payloads queued before this field existed render as before.
    premium_status = payload.get('premium_status')
    if premium_status == 'closed' and payload.get('premium_net') is not None:
        lines.append('Premium leg scored P&L **%s** (options model, '
                     '2 contracts)' % _money(payload.get('premium_net')))
    elif premium_status == 'data_gap':
        lines.append('Premium leg: data gap — unmeasured')
    elif premium_status:
        lines.append('Premium leg: measurement pending')
    lines.append('_Shadow measurement only — not a bookable trade; '
                 'nothing was booked or sent anywhere._')
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Stock Sweeps Shadow',
            'allowed_mentions': {'parse': []}}


FORMATTERS = {'entry': format_entry_message, 'tp1': format_tp1_message,
              'close': format_close_message}


def _queue_alert_row(c, db, payload):
    """Insert one shadow alert into the module's own outbox on the
    caller's connection (the engine unit's transaction: the alert
    commits or rolls back with the episode write it describes)."""
    row_id = identity('stock-sweeps-push-v1', payload.get('episode_id', ''),
                      payload.get('type', ''), payload.get('bar_ts', 0))
    c.execute(db.insert(stock_sweeps_push_outbox).values(
        id=row_id, status='pending', created=time.time(),
        payload=payload, delivery={'attempts': 0})
        .on_conflict_do_nothing(index_elements=['id']))
    return row_id


def queue_alert(db, cfg, payload):
    """Queue one shadow alert in its own transaction (the connors
    pattern for callers outside a unit transaction). Returns True
    when queued (or when alerts are off)."""
    if not bool(getattr(cfg, 'stock_sweeps_alerts', True)):
        return True
    try:
        with db.tx() as c:
            _queue_alert_row(c, db, payload)
        return True
    except Exception:
        try:
            db.health(PUSH_KIND, 'error',
                      'shadow alert queue failed')
        except Exception:
            pass
        logging.getLogger('uvicorn.error').exception(
            'stock_sweeps: alert queue failed')
        return False


def push_destination(cfg):
    """The research route destination (url, mode) via the routing
    layer's own resolver; url None when the route is unconfigured.
    (2026-11-25, Josh: the shadow books post to #compass; the
    options_0dte route belongs to the 0DTE scanner funnel alone.
    The destination is resolved here at drain time and outbox rows
    persist no route, so rows still queued when this shipped
    follow the move automatically — none are stranded and none
    keep posting to #0dte.)"""
    from .alert_routes import destination
    url, mode = destination(cfg, 'research')
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
        row = c.execute(select(stock_sweeps_push_outbox)
                        .where(stock_sweeps_push_outbox.c.status == 'pending')
                        .order_by(stock_sweeps_push_outbox.c.created).limit(1)
                        ).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        if d.get('next_attempt') and now < d['next_attempt']:
            return False
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(update(stock_sweeps_push_outbox)
                  .where(stock_sweeps_push_outbox.c.id == row['id'])
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
        c.execute(update(stock_sweeps_push_outbox)
                  .where(stock_sweeps_push_outbox.c.id == job['id'])
                  .values(status=status, delivery=job['delivery']))
    if status == 'delivered':
        db.health(PUSH_KIND, 'delivered',
                  'Stock Sweeps shadow alert confirmed by Discord', now)
    elif status == 'failed':
        db.health(PUSH_KIND, 'error',
                  'shadow alert delivery failed: %s'
                  % job['delivery'].get('error', 'attempts exhausted'))
    return True


async def run_push(db, cfg):
    """Drainer loop for the shadow alerts (own outbox, research
    route destination). Never raises out of the loop."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if bool(getattr(cfg, 'stock_sweeps', True)) and \
                        bool(getattr(cfg, 'stock_sweeps_alerts', True)):
                    url, mode = push_destination(cfg)
                    if url:
                        await asyncio.to_thread(deliver_one_push, db, client, url)
                    else:
                        db.health(PUSH_KIND, 'not_configured',
                                  'research route has no webhook; '
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


def _open_episode(db, c, sym, ev, bar, now, sample):
    p = {
        'id': identity('stock-sweeps-episode-v1', sym, ev['date'],
                       bar['start_ts'], ev['side']),
        'version': VERSION, 'strategy': 'stock-sweeps-shadow',
        'symbol': sym, 'underlying': sym,
        'side': 'long' if ev['side'] == 1 else 'short', 'dir': ev['side'],
        'status': 'open', 'stage': 'open', 'sample': sample,
        'premium_status': 'awaiting_entry',
        'created_at': now, 'updated_at': now,
        'signal': {'date': ev['date'], 'minute': ev['minute'],
                   'bar_ts': bar['start_ts'],
                   'entry_bar_end': bar['start_ts'] + 900,
                   'price': ev['entry_px'], 'stop': ev['stop'],
                   'or_mid': ev['or_mid'], 'or_high': ev['or_high'],
                   'or_low': ev['or_low'], 'buffer': BOOK[sym]},
        'legs': {'tp1': {'qty': TP1_QTY, 'filled': False, 'fill': None,
                         'gross': 0.0, 'date': None, 'bar_ts': None},
                 'runner': {'qty': QTY - TP1_QTY, 'gross': 0.0}},
        'underlying_leg': None, 'pnl': None, 'outcome': None,
        'contract': None, 'entry': None,
        'premium': {'tp1_bid': None, 'exit_bid': None, 'net': None,
                    'data_gap': None},
    }
    stmt = db.insert(episodes).values(
        id=p['id'], underlying=sym, status='open', created=now,
        updated=now, payload=p)
    res = c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
    if res.rowcount == 0:
        row = c.execute(select(episodes.c.payload).where(
            episodes.c.id == p['id'])).scalars().first()
        return row, False
    _event(db, c, p, 'episode_created', now,
           {'date': ev['date'], 'side': ev['side'], 'price': ev['entry_px'],
            'stop': ev['stop']})
    return p, True


def _alert_payloads_for_event(p, ev, bar):
    """Alert payloads for one machine event on episode p (already
    updated with the event). ENTRY / TP1 / CLOSE (the vwap close
    carries the VWAP MOMENTUM EXIT label via REASON_LABEL)."""
    base = {'episode_id': p['id'], 'symbol': p['symbol'],
            'bar_ts': bar['start_ts']}
    if ev['type'] == 'entry':
        return [{**base, 'type': 'entry', 'side': ev['side'],
                 'entry_px': ev['entry_px'], 'stop': ev['stop'],
                 'or_mid': ev['or_mid'], 'or_high': ev['or_high'],
                 'or_low': ev['or_low'], 'buffer': BOOK[p['symbol']],
                 'date': ev['date']}]
    if ev['type'] == 'tp1':
        return [{**base, 'type': 'tp1', 'side': ev['side'],
                 'fill': ev['fill'], 'leg_gross': ev['leg_gross'],
                 'stop': ev['stop'], 'date': ev['date']}]
    if ev['type'] == 'close':
        return [{**base, 'type': 'close', 'side': ev['side'],
                 'reason': ev['reason'], 'entry_px': ev['entry_px'],
                 'exit_px': ev['exit_px'], 'net': ev['net'],
                 'tp1_gross': ev['tp1_gross'],
                 'runner_gross': ev['runner_gross'],
                 'exit_date': ev['exit_date']}]
    return []


def _apply_event(db, c, sym, ev, bar, now, sample, open_eps, alerts_out):
    """Book one machine event: episode create/update + lifecycle
    event + alert payloads (queued by the caller on this same
    transaction). open_eps maps symbol -> its open episode payload."""
    if ev['type'] == 'entry':
        p, created = _open_episode(db, c, sym, ev, bar, now, sample)
        open_eps[sym] = p
        if created:
            alerts_out.extend(_alert_payloads_for_event(p, ev, bar))
        return p
    p = open_eps.get(sym)
    if p is None:
        # An exit event with no open episode in this pass: reload the
        # symbol's open episode (created in an earlier tick).
        row = c.execute(select(episodes.c.payload).where(
            episodes.c.underlying == sym, episodes.c.status == 'open')
            .order_by(episodes.c.created.desc())).scalars().first()
        if row is None:
            return None
        p = row
        open_eps[sym] = p
    if ev['type'] == 'tp1':
        leg = p['legs']['tp1']
        leg.update(filled=True, fill=ev['fill'], gross=ev['leg_gross'],
                   date=ev['date'], bar_ts=bar['start_ts'])
        _event(db, c, p, 'tp1_filled', now,
               {'date': ev['date'], 'fill': ev['fill'],
                'leg_gross': ev['leg_gross']})
        _save(db, c, p, now)
        alerts_out.extend(_alert_payloads_for_event(p, ev, bar))
        return p
    if ev['type'] == 'close':
        p['legs']['tp1']['gross'] = ev['tp1_gross']
        p['legs']['runner']['gross'] = ev['runner_gross']
        p['underlying_leg'] = {
            'entry_px': ev['entry_px'], 'exit_px': ev['exit_px'],
            'exit_date': ev['exit_date'], 'reason': ev['reason'],
            'gross': ev['gross'], 'net': ev['net'], 'orders': ev['orders'],
            'tp1_done': ev['tp1_done']}
        p['pnl'] = ev['net']
        p['outcome'] = ('win' if ev['net'] > 0 else
                        'loss' if ev['net'] < 0 else 'breakeven')
        p['stage'] = 'closed'
        p['exit_bar_ts'] = bar['start_ts']
        p['exit_instant'] = bar['start_ts'] + 900
        _event(db, c, p, 'underlying_closed', now,
               {'date': ev['exit_date'], 'reason': ev['reason'],
                'net': ev['net']})
        _save(db, c, p, now)
        alerts_out.extend(_alert_payloads_for_event(p, ev, bar))
        if open_eps.get(sym) is p:
            del open_eps[sym]
        return p
    return p


def _finalize(db, c, p, now):
    """Terminal status once the underlying is closed AND the premium
    measurement has resolved (priced or data-gapped)."""
    if p['stage'] != 'closed' or p['status'] != 'open':
        return
    if p['premium_status'] == 'closed':
        p['status'] = 'closed'
        _save(db, c, p, now, status='closed')
    elif p['premium_status'] == 'data_gap':
        p['status'] = 'data_gap'
        _save(db, c, p, now, status='data_gap')


def _underlying_leg(p):
    """The underlying-leg result dict for an episode payload. Stored
    under 'underlying_leg' since the 2026-11-22 fix; legacy rows carry
    it in 'underlying', where it clobbered the symbol string (the
    /api/state 500 root cause). Returns None when the leg has not
    closed yet."""
    leg = p.get('underlying_leg')
    if isinstance(leg, dict):
        return leg
    leg = p.get('underlying')
    return leg if isinstance(leg, dict) else None


def _premium_gap(db, c, p, now, reason, at_stage):
    p['premium_status'] = 'data_gap'
    p['premium']['data_gap'] = {'reason': reason, 'stage': at_stage,
                                'at': now}
    _event(db, c, p, 'premium_data_gap', now,
           {'date': p['signal']['date'], 'reason': reason})
    _save(db, c, p, now)
    _finalize(db, c, p, now)


def _price_entries(db, c, now):
    """Premium entry pricing from the live chain state: nearest
    expiry (amended text), |delta| nearest 0.50, entry ask from the
    first observable quote at/after the entry bar's start. Retries
    while the chain is stale or the quote unobservable, bounded by
    PRICE_GRACE_S after the entry bar's end; then a counted data
    gap — never an estimate."""
    rows = c.execute(select(episodes.c.payload).where(
        episodes.c.status == 'open').order_by(episodes.c.created)
        ).scalars().all()
    for p in rows:
        if p['premium_status'] != 'awaiting_entry':
            continue
        entry_end = p['signal']['entry_bar_end']
        expired = now > entry_end + PRICE_GRACE_S
        chain = db.get(c, 'chain:' + p['symbol']) or {}
        chain_asof = number(chain.get('asof'))
        fresh = bool(chain) and chain_asof is not None \
            and chain_asof >= entry_end - CHAIN_STALE_S
        if not fresh:
            if expired:
                _premium_gap(db, c, p, now,
                             'chain_unavailable_or_stale', 'entry')
            continue
        if p.get('contract') is None:
            spot = number(chain.get('selection_reference')) \
                or number(chain.get('underlying_close')) \
                or p['signal']['price']
            chosen = select_contract(chain.get('contracts'), p['dir'],
                                     entry_end, spot)
            if chosen is None:
                if expired:
                    _premium_gap(db, c, p, now,
                                 'no_admissible_contract', 'entry')
                continue
            p['contract'] = {'symbol': chosen['symbol'],
                             'expiry': chosen['expiry'],
                             'strike': chosen['strike'],
                             'type': chosen['type']}
            _save(db, c, p, now)
        q = first_quote_at_or_after(db, c, chain, p['contract']['symbol'],
                                    p['signal']['bar_ts'])
        if q is None:
            if expired:
                _premium_gap(db, c, p, now, 'no_entry_quote', 'entry')
            continue
        ts, bid, ask, source = q
        p['entry'] = {
            'contract_symbol': p['contract']['symbol'],
            'expiry': p['contract']['expiry'],
            'strike': p['contract']['strike'],
            'type': p['contract']['type'],
            'bid': bid, 'ask': ask, 'quote_ts': ts, 'quote_source': source,
            'chain_asof': chain_asof, 'priced_at': now}
        p['premium_status'] = 'open'
        _event(db, c, p, 'premium_entry_priced', now,
               {'date': p['signal']['date'],
                'contract': p['contract']['symbol'], 'ask': ask})
        _save(db, c, p, now)


def _price_exits(db, c, now):
    """Premium exit pricing: the held contract's bid at the TP1
    event and at the final exit event, first observable quote
    at/after the event bar's start. Completing both prices resolves
    the episode's premium net; a price still unobservable
    PRICE_GRACE_S after its event is a counted data gap."""
    rows = c.execute(select(episodes.c.payload).where(
        episodes.c.status == 'open').order_by(episodes.c.created)
        ).scalars().all()
    for p in rows:
        if p['premium_status'] != 'open' or not p.get('entry'):
            continue
        prem = p['premium']
        chain = db.get(c, 'chain:' + p['symbol']) or {}
        sym_contract = p['entry']['contract_symbol']
        leg = p['legs']['tp1']
        if leg['filled'] and prem['tp1_bid'] is None:
            q = first_quote_at_or_after(db, c, chain, sym_contract,
                                        leg['bar_ts'])
            if q is not None:
                prem['tp1_bid'] = q[1]
                prem['tp1_bid_ts'] = q[0]
                prem['tp1_bid_source'] = q[3]
                _save(db, c, p, now)
            elif now > leg['bar_ts'] + 900 + PRICE_GRACE_S:
                _premium_gap(db, c, p, now, 'no_tp1_bid', 'tp1')
                continue
        if p['stage'] == 'closed' and prem['exit_bid'] is None:
            q = first_quote_at_or_after(db, c, chain, sym_contract,
                                        p['exit_bar_ts'])
            if q is not None:
                prem['exit_bid'] = q[1]
                prem['exit_bid_ts'] = q[0]
                prem['exit_bid_source'] = q[3]
            elif now > p['exit_instant'] + PRICE_GRACE_S:
                _premium_gap(db, c, p, now, 'no_exit_bid', 'exit')
                continue
            if prem['exit_bid'] is not None:
                if leg['filled'] and prem['tp1_bid'] is None:
                    continue  # TP1 price still pending within grace
                prem['net'] = premium_net(
                    p['entry']['ask'], prem['tp1_bid'], prem['exit_bid'],
                    bool(leg['filled']))
                p['premium_status'] = 'closed'
                _event(db, c, p, 'premium_closed', now,
                       {'date': _underlying_leg(p)['exit_date'],
                        'net': prem['net']})
                _save(db, c, p, now)
                _finalize(db, c, p, now)


def _write_state(db, c, now, extra=None):
    """Recompute the summary from the episode table: counts, pooled
    underlying + premium stats, per-symbol nets, and the verdict
    clock's 4-week windows (entry-date windows anchored at the first
    eligible session). Factual reporting only — no verdict logic."""
    rows = c.execute(select(episodes.c.payload)).scalars().all()
    counts = {'created': 0, 'open': 0, 'closed': 0, 'data_gap': 0,
              'premium_closed': 0, 'premium_data_gap': 0,
              'premium_pending': 0}
    und_nets, prem_nets = [], []
    per_symbol = {}
    for p in rows:
        counts['created'] += 1
        if p['status'] in counts:
            counts[p['status']] += 1
        if p['premium_status'] == 'closed':
            counts['premium_closed'] += 1
        elif p['premium_status'] == 'data_gap':
            counts['premium_data_gap'] += 1
        elif p['stage'] == 'closed':
            counts['premium_pending'] += 1
        leg_result = _underlying_leg(p)
        if leg_result:
            und_nets.append(leg_result['net'])
            s = per_symbol.setdefault(p['symbol'], {'n': 0, 'net': 0.0})
            s['n'] += 1
            s['net'] += leg_result['net']
        if p['premium_status'] == 'closed' and p['premium']['net'] is not None:
            prem_nets.append(p['premium']['net'])

    def agg(nets):
        gp = sum(x for x in nets if x > 0)
        gl = -sum(x for x in nets if x < 0)
        return {'n': len(nets), 'net': round(sum(nets), 2),
                'pf': (gp / gl) if gl > 0 else None,
                'win_rate': (100.0 * sum(1 for x in nets if x > 0) / len(nets))
                if nets else None}

    old = db.get(c, STATE_KEY, {})
    first_session = old.get('first_session')
    windows = []
    if first_session:
        anchor = _date.fromisoformat(first_session)
        by_window = {}
        for p in rows:
            d = _date.fromisoformat(p['signal']['date'])
            k = (d - anchor).days // 28
            if k < 0:
                continue
            w = by_window.setdefault(k, {'underlying': [], 'premium': []})
            leg_result = _underlying_leg(p)
            if leg_result:
                w['underlying'].append(leg_result['net'])
            if p['premium_status'] == 'closed' \
                    and p['premium']['net'] is not None:
                w['premium'].append(p['premium']['net'])
        for k in sorted(by_window):
            w = by_window[k]
            windows.append({
                'window': k + 1,
                'start': (anchor + timedelta(days=28 * k)).isoformat(),
                'end': (anchor + timedelta(days=28 * (k + 1))).isoformat(),
                'underlying': agg(w['underlying']),
                'premium_net': round(sum(w['premium']), 2),
                'premium_n': len(w['premium'])})
    state = {'version': VERSION, 'at': now, 'counts': counts,
             'underlying': agg(und_nets), 'premium': agg(prem_nets),
             'per_symbol': per_symbol, 'windows': windows,
             'book': BOOK,
             'bootstrap_at': old.get('bootstrap_at'),
             'first_session': first_session,
             **(extra or {})}
    db.put(c, STATE_KEY, state)
    return state


def _attach_premium(c, payload):
    """Attach the episode's premium-leg result to a CLOSE alert
    payload at queue time (2026-11-25). scan() runs the premium
    pricing steps before queueing, so a premium resolved in this
    pass is already on the episode row in this transaction and is
    copied verbatim — the same premium_net that feeds the tracking
    premium stats. A premium still pending or data-gapped is
    labeled by its status only; the number is never estimated, and
    a later resolution does not rewrite the queued post."""
    row = c.execute(select(episodes.c.payload).where(
        episodes.c.id == payload.get('episode_id'))).scalars().first()
    if row is None:
        return payload
    payload['premium_status'] = row.get('premium_status')
    if row.get('premium_status') == 'closed':
        payload['premium_net'] = (row.get('premium') or {}).get('net')
    return payload


def scan(db, c, cfg, now):
    """One live pass over the frozen book: feed each symbol's newly
    completed 15-minute buckets into its persisted machine, book the
    events as episodes + shadow alerts, then run the premium pricing
    steps. Runs inside the caller's (engine unit) transaction."""
    if not getattr(cfg, 'stock_sweeps', True):
        return {'ran': False, 'reason': 'disabled'}
    alerts_on = bool(getattr(cfg, 'stock_sweeps_alerts', True))
    state = db.get(c, STATE_KEY, {})
    if 'bootstrap_at' not in state:
        state['bootstrap_at'] = now
        db.put(c, STATE_KEY, state)
    alerts_out = []
    open_eps = {}
    fed = entries = closes = 0
    for sym in SYMBOLS:
        mkey = MACHINE_PREFIX + sym
        m = db.get(c, mkey)
        if m is None:
            m = {'state': new_state(BOOK[sym]), 'fed_through': None,
                 'bootstrapped_at': now}
        since = (m['fed_through'] - 86400) \
            if m.get('fed_through') is not None else now - 3 * 86400
        rows = db.recent(c, 'bar', sym, limit=3000, since=since)
        buckets = aggregate_buckets(rows)
        if m.get('fed_through') is None:
            # Bootstrap watermark (SH-SPY pattern): episodes form only
            # from bars that complete AFTER the shadow is live — the
            # cursor starts at the latest completed bucket and history
            # is never fed. The first session whose 09:30 bar the
            # shadow itself feeds becomes the first eligible session.
            completed = [b for b in buckets if b['start_ts'] + 900 <= now]
            m['fed_through'] = completed[-1]['start_ts'] if completed else 0
            db.put(c, mkey, m)
            continue
        for b in buckets:
            if b['start_ts'] <= m['fed_through'] or b['start_ts'] + 900 > now:
                continue
            if b['minute'] == 570 and not state.get('first_session'):
                state['first_session'] = b['date']
                db.put(c, STATE_KEY, state)
            sample = bool(state.get('first_session')) \
                and b['date'] >= state['first_session']
            evs = on_bar(m['state'], b['date'], b['minute'], b['o'],
                         b['h'], b['l'], b['c'], b['v'])
            m['fed_through'] = b['start_ts']
            fed += 1
            for ev in evs:
                if ev['type'] == 'entry':
                    entries += 1
                if ev['type'] == 'close':
                    closes += 1
                _apply_event(db, c, sym, ev, b, now, sample,
                             open_eps, alerts_out)
        db.put(c, mkey, m)
    _price_entries(db, c, now)
    _price_exits(db, c, now)
    if alerts_on:
        for payload in alerts_out:
            if payload.get('type') == 'close':
                _attach_premium(c, payload)
            _queue_alert_row(c, db, payload)
    summary = _write_state(db, c, now)
    counts = summary['counts']
    return {'ran': True, 'symbols': len(SYMBOLS), 'fed': fed,
            'entries': entries, 'closes': closes,
            'open_episodes': counts['open'],
            'first_session': summary.get('first_session')}


class StockSweeps:
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
            if not self.db.lease(c, 'stock-sweeps', self.owner, 300):
                return
        self.process(self.now(time.time()))
