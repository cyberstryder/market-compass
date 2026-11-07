"""Shared machinery for the 2026-11-07 futures forward shadow scorers.

Used by compass/vwap_cont_mes.py, compass/postnews_fade.py and
compass/shock_fade.py — the three measurement-only forward scorers
Josh commissioned for the new-strategy program's gauntlet passes.
Nothing in this module (or its consumers) places orders, sends
alerts or touches a live path; it only reads bars and writes the
scorers' own state/episodes.

Contents:
- ct_parts: CME session clock (trade date = CT date of ts + 7h), the
  survivors/gauntlet convention verbatim.
- SessionAgg: a streaming, persistable per-session bar aggregator
  producing the daily aggregates the gauntlets used — full-session
  open/high/low/last-close plus close_1545 (the 15:45 CT mark: the
  close of the last bar starting at or before 15:44 CT; flat_fallback
  flags sessions where no bar starts exactly at 15:44).
- Bar polling over the production stores: the live bar_window first,
  with the gap behind it backfilled from the bar-events store. Event
  reads follow the survivors seed access-path contract (2026-10-30):
  equality on (kind, symbol) with a ts range only, one dated contract
  at a time, so every query is served by the (kind, symbol, ts)
  index. No prefix scans, no COLLATE idioms.
- seed_sessions: one-shot aggregation of stored bars into completed
  session aggregates (warm-up history for the scorers' feeds).
- percentile_linear: numpy's default (linear) percentile, pure
  Python — the variance registration's frozen threshold arithmetic.
"""
import logging
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql_text

from .ict_common import _bars_from_window, resolve_bar_key
from .survivors import _dated_symbols

log = logging.getLogger('uvicorn.error')
CT = ZoneInfo("America/Chicago")
NY = ZoneInfo("America/New_York")

FLAT_TOD = 15 * 60 + 45          # 15:45 CT flatten/mark time
LAST_MARK_TOD = FLAT_TOD - 1     # the bar starting 15:44 closes at 15:45


def ct_parts(ts):
    """(tod_minutes_ct, sdate) for an epoch-second bar start. Trade
    date = CT calendar date of the instant 7h after the bar (absolute
    arithmetic, DST-safe; the gauntlet/survivors convention)."""
    dt = datetime.fromtimestamp(ts, CT)
    tod = dt.hour * 60 + dt.minute
    sdate = datetime.fromtimestamp(ts + 7 * 3600, CT).date().isoformat()
    return tod, sdate


def percentile_linear(values, q):
    """The q-th percentile of `values` with numpy's default linear
    interpolation. `values` must be non-empty."""
    w = sorted(values)
    if len(w) == 1:
        return w[0]
    rank = (q / 100.0) * (len(w) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return w[lo]
    frac = rank - lo
    return w[lo] + (w[hi] - w[lo]) * frac


def book_stats(pnls):
    """(n, net, profit factor) over a P&L list; PF None when there
    are no losses (or no trades)."""
    n = len(pnls)
    if not n:
        return {'n': 0, 'net': 0.0, 'pf': None}
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {'n': n, 'net': round(sum(pnls), 2),
            'pf': (round(gross_win / gross_loss, 3) if gross_loss > 0
                   else None)}


# ---------------------------------------------------------------------------
# Streaming session aggregator

class SessionAgg:
    """Aggregates 1-minute bars ({ts,o,h,l,c,v}) into CME sessions.

    feed() returns the finalized session dict when a bar belonging to
    a NEW session arrives (the finalized session is the previous one),
    else None. The finalized dict: {sdate, o, h, l, close, c1545,
    flat_fallback, bars, partial, first_ts, last_ts}. `close` is the
    last bar's close (the gauntlet feat.py d_close); `c1545` is the
    close of the last bar starting at or before 15:44 CT (the H-Y3 /
    variance panel's close_1545 mark), with flat_fallback True when
    no bar starts exactly at 15:44. `partial` marks a session whose
    bar stream has an internal hole > 6 hours (collector/feed gap) —
    the production analogue of the panels' dropped degraded sessions.
    """

    HOLE_SECONDS = 6 * 3600

    def __init__(self):
        self.cur = None        # in-progress session dict
        self._prev_ts = None   # last bar ts inside the current session

    def _start(self, bar, tod, sdate):
        self.cur = {'sdate': sdate, 'o': bar['o'], 'h': bar['h'],
                    'l': bar['l'], 'close': bar['c'],
                    'c1545': bar['c'] if tod <= LAST_MARK_TOD else None,
                    'flat_fallback': tod != LAST_MARK_TOD,
                    'bars': 0, 'partial': False,
                    'first_ts': bar['ts'], 'last_ts': bar['ts']}
        self._prev_ts = bar['ts']

    def feed(self, bar):
        tod, sdate = ct_parts(bar['ts'])
        finalized = None
        if self.cur is None:
            self._start(bar, tod, sdate)
        elif sdate != self.cur['sdate']:
            finalized = self.cur
            self._start(bar, tod, sdate)
        cur = self.cur
        if self._prev_ts is not None and bar['ts'] - self._prev_ts > self.HOLE_SECONDS \
                and sdate == cur['sdate']:
            cur['partial'] = True
        self._prev_ts = bar['ts']
        cur['h'] = max(cur['h'], bar['h'])
        cur['l'] = min(cur['l'], bar['l'])
        cur['close'] = bar['c']
        cur['bars'] += 1
        cur['last_ts'] = bar['ts']
        if tod <= LAST_MARK_TOD:
            cur['c1545'] = bar['c']
            if tod == LAST_MARK_TOD:
                cur['flat_fallback'] = False
        return finalized

    def to_state(self):
        return {'cur': self.cur, 'prev_ts': self._prev_ts}

    @classmethod
    def from_state(cls, state):
        agg = cls()
        if state:
            agg.cur = state.get('cur')
            agg._prev_ts = state.get('prev_ts')
        return agg


# ---------------------------------------------------------------------------
# Bar access (bar_window first, events backfill for the gap behind it)

_EVENT_BARS_SQL = (
    "SELECT ts, payload FROM events WHERE kind='bar' "
    "AND symbol = :symbol AND ts > :tlo AND ts <= :thi ORDER BY ts")


def _payload(row):
    p = row
    if isinstance(p, str):
        import json
        try:
            p = json.loads(p)
        except Exception:
            return {}
    return p or {}


def events_bars(db, c, root, lo_ts, hi_ts):
    """All stored 1-min bars for a root in (lo_ts, hi_ts], merged
    across its dated contracts and ts-ordered. One equality+range
    query per contract (the access-path contract). Bars are dicts
    {ts,o,h,l,c,v}; rows missing o/h/l/c are skipped."""
    bars = []
    for sym in _dated_symbols(c, root, lo_ts):
        rows = c.execute(sql_text(_EVENT_BARS_SQL),
                         {'symbol': sym, 'tlo': lo_ts, 'thi': hi_ts})
        for row in rows:
            m = dict(row._mapping) if hasattr(row, '_mapping') else dict(row)
            p = _payload(m.get('payload'))
            ts = m.get('ts')
            if ts is None or p.get('o') is None or p.get('h') is None \
                    or p.get('l') is None or p.get('c') is None:
                continue
            bars.append({'ts': ts, 'o': p['o'], 'h': p['h'], 'l': p['l'],
                         'c': p['c'], 'v': p.get('v')})
    bars.sort(key=lambda b: b['ts'])
    return bars


def poll_bars(db, c, alias, root, after_ts):
    """Bars strictly after `after_ts` for a futures alias, ascending.

    Primary source: the resolved bar_window (the live ~1800-bar
    window). When the window no longer reaches back to after_ts (the
    worker was down longer than the window, or the window rolled to a
    new contract), the missing stretch is backfilled from the
    bar-events store (365-day retention) so the feed never silently
    skips bars. Returns (bars, unfilled_gap) — unfilled_gap is True
    when even the events store could not cover the stretch (callers
    treat the affected session as partial)."""
    key = resolve_bar_key(db, c, alias)
    window = []
    if key:
        window = _bars_from_window(db.get(c, 'bar_window:' + key, []))
    window = [b for b in window if after_ts is None or b['ts'] > after_ts]
    if after_ts is None or not window:
        return window, False
    first_ts = window[0]['ts']
    if first_ts <= after_ts + 60:
        return window, False
    backfill = events_bars(db, c, root, after_ts, first_ts - 1)
    merged = {b['ts']: b for b in backfill}
    for b in window:
        merged[b['ts']] = b
    bars = [merged[k] for k in sorted(merged)]
    gap = not backfill or backfill[0]['ts'] > after_ts + 60
    return bars, gap


# ---------------------------------------------------------------------------
# One-shot session seeding from the events store

def seed_sessions(db, c, root, now, days, min_bars=800):
    """Aggregate stored bars over the last `days` days into completed
    session aggregates (SessionAgg shape, ascending by sdate).

    Streaming per contract: each contract's bars are aggregated into
    per-session dicts on the fly (never a full bar list in memory),
    then contracts are merged per session — open from the earliest
    bar, close / c1545 from the latest qualifying bar, extremes across
    contracts (the survivors seed merge semantics, extended with the
    open and the 15:45 mark). Sessions with fewer than min_bars bars
    are dropped: in a seed, bar count is the only continuity proxy —
    a short session is indistinguishable from a half-collected one,
    and the gauntlet panels likewise dropped degraded sessions.
    Sessions still in progress at `now` are excluded."""
    lo_ts = now - days * 86400
    _, now_sdate = ct_parts(now)
    per_contract = []
    for sym in _dated_symbols(c, root, lo_ts):
        rows = c.execute(sql_text(
            "SELECT ts, payload FROM events WHERE kind='bar' "
            "AND symbol = :symbol AND ts >= :tlo ORDER BY ts"),
            {'symbol': sym, 'tlo': lo_ts})
        sessions = {}
        for row in rows:
            m = dict(row._mapping) if hasattr(row, '_mapping') else dict(row)
            p = _payload(m.get('payload'))
            ts = m.get('ts')
            if ts is None or p.get('o') is None or p.get('h') is None \
                    or p.get('l') is None or p.get('c') is None:
                continue
            tod, sdate = ct_parts(ts)
            s = sessions.get(sdate)
            if s is None:
                s = sessions[sdate] = {
                    'sdate': sdate, 'o': p['o'], 'h': p['h'], 'l': p['l'],
                    'close': p['c'],
                    'c1545': p['c'] if tod <= LAST_MARK_TOD else None,
                    'flat_fallback': tod != LAST_MARK_TOD,
                    'bars': 0, 'partial': False,
                    'first_ts': ts, 'last_ts': ts,
                    '_c_ts': ts,
                    '_m_ts': ts if tod <= LAST_MARK_TOD else None}
            s['h'] = max(s['h'], p['h'])
            s['l'] = min(s['l'], p['l'])
            if ts >= s['_c_ts']:
                s['close'] = p['c']
                s['_c_ts'] = ts
            if tod <= LAST_MARK_TOD and (s['_m_ts'] is None or ts >= s['_m_ts']):
                s['c1545'] = p['c']
                s['_m_ts'] = ts
                if tod == LAST_MARK_TOD:
                    s['flat_fallback'] = False
            s['bars'] += 1
            s['last_ts'] = ts
        per_contract.append(sessions)
    merged = {}
    for sessions in per_contract:
        for sdate, s in sessions.items():
            cur = merged.get(sdate)
            if cur is None:
                merged[sdate] = dict(s)
                continue
            if s['first_ts'] < cur['first_ts']:
                cur['o'] = s['o']
                cur['first_ts'] = s['first_ts']
            cur['h'] = max(cur['h'], s['h'])
            cur['l'] = min(cur['l'], s['l'])
            if s['_c_ts'] >= cur['_c_ts']:
                cur['close'] = s['close']
                cur['_c_ts'] = s['_c_ts']
            if s['_m_ts'] is not None and (cur['_m_ts'] is None
                                           or s['_m_ts'] >= cur['_m_ts']):
                cur['c1545'] = s['c1545']
                cur['_m_ts'] = s['_m_ts']
                cur['flat_fallback'] = s['flat_fallback']
            cur['bars'] += s['bars']
            cur['last_ts'] = max(cur['last_ts'], s['last_ts'])
    out = []
    for sdate in sorted(merged):
        s = merged[sdate]
        if sdate >= now_sdate or s['bars'] < min_bars or s['c1545'] is None:
            continue
        s.pop('_c_ts', None)
        s.pop('_m_ts', None)
        out.append(s)
    return out


def raised_timeouts(c):
    """Raise statement/transaction timeouts inside the caller's
    current transaction (Postgres only), mirroring the survivors
    seed isolation. No-op on other dialects."""
    if c.dialect.name == 'postgresql':
        c.execute(sql_text('SET LOCAL statement_timeout = 120000'))
        version = c.execute(sql_text(
            "SELECT current_setting('server_version_num')::int")).scalar()
        if version and version >= 170000:
            c.execute(sql_text("SET LOCAL transaction_timeout = '240s'"))
