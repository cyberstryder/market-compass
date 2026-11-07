"""MES VWAP-continuation — forward shadow scorer (2026-11-07, Josh).

Paper / measurement scope ONLY: no orders, no alerts, no Discord
output, no live sends. This module forward-scores the single full
pass of the momentum/trend sweep (program item 1, 2026-11-06):

    MES `vwapc_z1.5_h60_long` — detector n=65, net +$1,252, PF 1.68,
    placebo 100.0th percentile; guard-kept x p90 n=65, net +$1,331,
    PF 1.74, max DD -$353. ALL limbs green.

Frozen sources:
- files/momentum-trend-sweep-registration-2026-11-06.md
  (sha256 8bdc46537ce6c12b15482142e8c621ddac8812a2adab6d98c8a409804a15ae9f)
- files/momentum-trend-sweep-2026-11-06.md (report, §4 the pass, §5
  the shadow proposal this build implements)
- Forward registration: files/vwap-cont-mes-forward-registration-2026-11-07.md

The cell, byte-identical to the sweep's detector arm
(variants_mom.gen_variants family (b) + sim.Book.horizon):

- Features (feat.add_features conventions): ATR = mean true range
  over the 14 sessions before the current one (daily aggregates:
  session high/low/last close), in price units; atr_t = ATR / tick.
  RTH VWAP accumulates close x volume over bars with tod in
  [08:30, 16:00) CT within the session. z = (close - RTH VWAP)/tick
  / max(0.2 * atr_t, 1) — the survivors' z convention.
- Signal state machine over bars with tod in [09:00, 15:00] CT only,
  reset per session: a bar whose z crosses up through +1.5 (previous
  win-bar z below +1.5, or none) ARMS the cell. While armed, the
  first bar that touches VWAP + band (low <= VWAP + 0.1*ATR) without
  closing through VWAP (close > VWAP) is the signal; a close back
  through VWAP (close < VWAP) disarms. At most one signal per arming.
  A bar with undefined z/VWAP disarms and resets the cross memory.
- Trade (sim.Book.horizon, hold 60): fill at the NEXT bar's open
  (no adverse tick on horizon fills — the -2 tick round-trip
  friction below is the whole friction), exit at the close of the
  bar 60 bars after the fill bar, capped positionally at the
  session's flatten bar (first bar with tod in [15:45, 17:00) CT,
  else the session's last bar). P&L = (exit - entry)/tick ticks
  - 2 ticks, x $1.25/tick, - $3.00 round-trip fees. One position at
  a time: a signal whose fill bar does not come after the previous
  trade's exit bar is dropped (the sweep's dedupe).
- Guard-kept arm (the verdict arm, sweep registration §2 Stage B —
  a horizon repricing): entry = signal-bar close + 1 tick adverse,
  exit bar unchanged, the -2 tick friction dropped, fees kept:
  P&L_gk = (exit - entry_gk)/tick x $1.25 - $3.00. Both arms are
  recorded per episode; the state summary reports both.

Sample rule (forward registration): only signals from the first
full session after this build deploys count. Implemented as a boot
stamp — when the scorer's state is first created, forward_from is
set to the first session date strictly after the boot session;
signals in sessions <= forward_from's predecessor never score, and
the ATR warm-up seeding never produces episodes. The
FORWARD_START floor (the build date) applies on top: no session on
or before it can score regardless of boot time.

Wiring: episodes live in the vwap_cont_mes_episodes table (declared
in .store); the feed state persists beside them so a restart resumes
mid-session and mid-trade; lifecycle transitions are appended as
vwap_cont_mes events; counts and both arms' aggregates are
recomputed from the table on every pass into the vwap_cont_mes:state
summary; health goes through Store.health(). The scorer renders no
verdict: the forward registration's frozen terms (>= 50 scored
trades spanning two consecutive 4-week windows, PF > 1.0 with both
windows net-positive on the guard-kept arm) are read from this
accrued record. Failure isolation: every step runs in its own
transaction and the worker loop catches per-pass, so this scorer can
never throw into the engine tick (it is not in the tick at all — it
is a standalone worker on the connors_ssc pattern).
"""
import asyncio
import logging
import time
import uuid

from sqlalchemy import select, update

from .futures_shadow_common import (SessionAgg, book_stats, ct_parts,
                                    poll_bars, raised_timeouts,
                                    seed_sessions)
from .store import identity
from .store import vwap_cont_mes_episodes as episodes

VERSION = 'vwap-cont-mes-v1'
KIND = 'vwap_cont_mes'
STATE_KEY = 'vwap_cont_mes:state'
FEED_KEY = 'vwap_cont_mes:feed'
SYMBOL = 'MES.c.0'
ROOT = 'MES'
TICK = 0.25
TICKVAL = 1.25          # $ per tick (MES: 0.25 x $5 multiplier)
FEES_RT = 3.00          # $1.50/side
FRICTION_TICKS = 2      # round-trip, detector arm
Z_ARM = 1.5
BAND_ATR = 0.1
HOLD_BARS = 60
WIN_LO, WIN_HI = 540, 900        # 09:00-15:00 CT signal window
RTH_LO, RTH_HI = 510, 960        # RTH VWAP accumulation window
FORWARD_START = '2026-11-07'     # build date floor (see docstring)
SEED_DAYS = 26                   # ATR warm-up (needs 15 sessions)
SEED_MIN_BARS = 800

# The episodes table is declared in .store (vwap_cont_mes_episodes)
# so Store init's create_all creates it before this module is lazily
# imported (the bb1adfa lesson).


# ---------------------------------------------------------------------------
# Pure feed (frozen math; unit-tested without a database)

class VwapContFeed:
    """Streaming replica of the sweep's vwapc z1.5 h60 long cell.

    Feed bars ({ts,o,h,l,c,v}) in ascending ts order. Completed
    trades accumulate in .trades (detector + guard-kept P&L); signals
    dropped by the one-position dedupe count in .dropped. State
    round-trips through to_state/from_state so the worker can
    persist it across restarts."""

    def __init__(self):
        self.agg = SessionAgg()
        self.history = []          # completed session aggregates
        self.atr = None            # price units, current session
        self.rth_pv = 0.0
        self.rth_vol = 0.0
        self.bar_count = -1        # positional index of last bar fed
        self.m_sdate = None        # sdate of last win bar processed
        self.armed = False
        self.zprev = None
        self.pending = None        # signal awaiting its fill bar
        self.open_trade = None
        self.last_exit_count = -1
        self.trades = []
        self.dropped = 0

    # -- session / ATR ----------------------------------------------------

    def _recompute_atr(self):
        self.atr = None
        hist = self.history
        if len(hist) >= 15:
            trs = []
            for m in range(len(hist) - 14, len(hist)):
                hh = hist[m]
                pc = hist[m - 1]['close']
                trs.append(max(hh['h'] - hh['l'],
                               abs(hh['h'] - pc), abs(hh['l'] - pc)))
            self.atr = sum(trs) / len(trs)

    def seed_history(self, sessions):
        """Install completed-session aggregates (cold start). Only
        sessions strictly before the in-progress one are used."""
        cur = self.agg.cur['sdate'] if self.agg.cur else None
        merged = {s['sdate']: s for s in self.history}
        for s in sessions:
            if cur is None or s['sdate'] < cur:
                merged[s['sdate']] = s
        self.history = [merged[k] for k in sorted(merged)][-40:]
        self._recompute_atr()

    # -- trade lifecycle ----------------------------------------------------

    def _close_trade(self, exit_px, exit_count, exit_ts, reason):
        t = self.open_trade
        move_ticks = (exit_px - t['entry_px']) / TICK
        pnl = (move_ticks - FRICTION_TICKS) * TICKVAL - FEES_RT
        gk_entry = t['signal_close'] + TICK
        pnl_gk = ((exit_px - gk_entry) / TICK) * TICKVAL - FEES_RT
        rec = {**t, 'exit_px': exit_px, 'exit_ts': exit_ts,
               'exit_count': exit_count, 'exit_reason': reason,
               'pnl': round(pnl, 2), 'pnl_gk': round(pnl_gk, 2)}
        self.trades.append(rec)
        self.last_exit_count = exit_count
        self.open_trade = None
        return rec

    # -- main entry ---------------------------------------------------------

    def feed(self, bar):
        """Returns the list of trade records closed by this bar."""
        closed = []
        tod, sdate = ct_parts(bar['ts'])
        self.bar_count += 1
        count = self.bar_count
        finalized = self.agg.feed(bar)
        if finalized is not None:
            # At boot the seeded history can already hold this
            # session (seed + window catch-up overlap): replace,
            # never duplicate, or the ATR window shifts by one.
            if self.history and self.history[-1]['sdate'] == finalized['sdate']:
                self.history[-1] = finalized
            else:
                self.history.append(finalized)
            self.history = self.history[-40:]
            self._recompute_atr()
            self.rth_pv = 0.0
            self.rth_vol = 0.0
            if self.open_trade is not None:
                # No flatten bar and the 60-bar hold never completed:
                # the flatten index was the old session's last bar.
                closed.append(self._close_trade(
                    finalized['close'], count - 1, finalized['last_ts'],
                    'flatten'))
        # RTH VWAP accumulation (the bar that rolls the session has
        # the new session's tod, so this lands in the right session).
        v = bar.get('v')
        if v is None or (isinstance(v, float) and v != v):
            v = 0
        if RTH_LO <= tod < RTH_HI:
            self.rth_pv += bar['c'] * v
            self.rth_vol += v
        # Pending signal fills at this bar's open.
        if self.pending is not None:
            sig = self.pending
            self.pending = None
            self.open_trade = {
                'signal_ts': sig['ts'], 'signal_sdate': sig['sdate'],
                'signal_close': sig['close'], 'z': sig['z'],
                'vwap': sig['vwap'], 'atr': sig['atr'],
                'fill_count': count, 'entry_px': bar['o'],
                'entry_ts': bar['ts'], 'sdate': sdate,
                'flat_seen': False}
        # Open-trade exit: the flatten bar or the 60th bar after the
        # fill, positionally, whichever comes first (sim horizon cap).
        t = self.open_trade
        if t is not None and count > t['fill_count']:
            is_flat = (sdate == t['sdate'] and 945 <= tod < 1020
                       and not t['flat_seen'])
            if is_flat:
                t['flat_seen'] = True
                closed.append(self._close_trade(
                    bar['c'], count, bar['ts'], 'flatten'))
            elif count == t['fill_count'] + HOLD_BARS:
                closed.append(self._close_trade(
                    bar['c'], count, bar['ts'], 'time'))
        # Signal state machine over win bars only.
        if WIN_LO <= tod <= WIN_HI:
            if sdate != self.m_sdate:
                self.armed = False
                self.zprev = None
                self.m_sdate = sdate
            vwap = (self.rth_pv / self.rth_vol) if self.rth_vol > 0 else None
            z = None
            if vwap is not None and self.atr:
                atr_t = self.atr / TICK
                z = ((bar['c'] - vwap) / TICK) / max(0.2 * atr_t, 1.0)
            if z is None or vwap is None:
                self.armed = False
                self.zprev = None
            elif not self.armed:
                if z >= Z_ARM and (self.zprev is None or self.zprev < Z_ARM):
                    self.armed = True
                self.zprev = z
            else:
                if bar['c'] < vwap:
                    self.armed = False
                elif bar['l'] <= vwap + BAND_ATR * self.atr \
                        and bar['c'] > vwap:
                    fill_count = count + 1
                    if self.open_trade is None and self.pending is None \
                            and fill_count > self.last_exit_count:
                        self.pending = {'ts': bar['ts'], 'sdate': sdate,
                                        'close': bar['c'], 'z': z,
                                        'vwap': vwap, 'atr': self.atr}
                    else:
                        self.dropped += 1
                    self.armed = False
                self.zprev = z
        return closed

    # -- persistence ----------------------------------------------------------

    def to_state(self):
        return {'version': VERSION, 'agg': self.agg.to_state(),
                'history': self.history, 'atr': self.atr,
                'rth_pv': self.rth_pv, 'rth_vol': self.rth_vol,
                'bar_count': self.bar_count, 'm_sdate': self.m_sdate,
                'armed': self.armed, 'zprev': self.zprev,
                'pending': self.pending, 'open_trade': self.open_trade,
                'last_exit_count': self.last_exit_count,
                'dropped': self.dropped}

    @classmethod
    def from_state(cls, state):
        f = cls()
        if not state:
            return f
        f.agg = SessionAgg.from_state(state.get('agg'))
        f.history = state.get('history') or []
        f.atr = state.get('atr')
        f.rth_pv = state.get('rth_pv') or 0.0
        f.rth_vol = state.get('rth_vol') or 0.0
        f.bar_count = state.get('bar_count', -1)
        f.m_sdate = state.get('m_sdate')
        f.armed = bool(state.get('armed'))
        f.zprev = state.get('zprev')
        f.pending = state.get('pending')
        f.open_trade = state.get('open_trade')
        f.last_exit_count = state.get('last_exit_count', -1)
        f.dropped = state.get('dropped') or 0
        return f


# ---------------------------------------------------------------------------
# Scorer worker

class VwapContMes:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner = uuid.uuid4().hex

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    # -- episode persistence -------------------------------------------------

    def _event(self, c, p, kind, now, extra=None):
        body = {'episode_id': p['id'], 'symbol': SYMBOL, 'event': kind,
                'at': now, **(extra or {})}
        self.db.append(c, KIND, VERSION, SYMBOL, now, body,
                       key=identity(KIND, p['id'], kind,
                                    body.get('signal_ts')))

    def _open_episode(self, c, trade, now):
        p = {
            'id': identity('vwap-cont-mes-episode-v1',
                           trade['signal_ts'], trade['entry_ts']),
            'version': VERSION, 'strategy': 'mes-vwapc-z1.5-h60-long',
            'symbol': SYMBOL, 'underlying': SYMBOL, 'side': 'long',
            'status': 'open',
            'created_at': now,
            'signal': {'ts': trade['signal_ts'],
                       'sdate': trade['signal_sdate'],
                       'close': trade['signal_close'], 'z': trade['z'],
                       'vwap': trade['vwap'], 'atr': trade['atr']},
            'entry': {'ts': trade['entry_ts'], 'px': trade['entry_px'],
                      'sdate': trade['sdate']},
            'exit': None, 'pnl': None, 'pnl_gk': None, 'outcome': None,
        }
        stmt = self.db.insert(episodes).values(
            id=p['id'], underlying=SYMBOL, status='open', created=now,
            updated=now, payload=p)
        c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        self._event(c, p, 'entry', now,
                    {'signal_ts': trade['signal_ts'],
                     'entry_px': trade['entry_px'], 'z': trade['z']})
        return p

    def _close_episode(self, c, trade, now):
        eid = identity('vwap-cont-mes-episode-v1',
                       trade['signal_ts'], trade['entry_ts'])
        row = c.execute(select(episodes.c.payload).where(
            episodes.c.id == eid)).scalars().first()
        if row is None:
            return
        p = row
        p['exit'] = {'ts': trade['exit_ts'], 'px': trade['exit_px'],
                     'reason': trade['exit_reason']}
        p['pnl'] = trade['pnl']
        p['pnl_gk'] = trade['pnl_gk']
        p['outcome'] = ('win' if trade['pnl'] > 0 else
                        'loss' if trade['pnl'] < 0 else 'breakeven')
        p['status'] = 'closed'
        p['updated_at'] = now
        c.execute(update(episodes).where(episodes.c.id == eid).values(
            status='closed', updated=now, payload=p))
        self._event(c, p, 'exit', now,
                    {'exit_ts': trade['exit_ts'], 'pnl': trade['pnl'],
                     'pnl_gk': trade['pnl_gk'],
                     'reason': trade['exit_reason']})

    def _write_state(self, c, now, feed_state):
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        det = book_stats([p['pnl'] for p in rows if p.get('pnl') is not None])
        gk = book_stats([p['pnl_gk'] for p in rows
                         if p.get('pnl_gk') is not None])
        counts = {'created': len(rows),
                  'closed': sum(1 for p in rows if p['status'] == 'closed'),
                  'open': sum(1 for p in rows if p['status'] == 'open')}
        state = {'version': VERSION, 'at': now, 'counts': counts,
                 'detector': det, 'guard_kept': gk,
                 'forward_from': feed_state.get('forward_from'),
                 'dropped_signals': feed_state.get('dropped_total', 0)}
        self.db.put(c, STATE_KEY, state)
        return state

    # -- driver ---------------------------------------------------------------

    def process(self, now):
        with self.db.tx() as c:
            feed_state = self.db.get(c, FEED_KEY, {})
        feed = VwapContFeed.from_state(feed_state.get('feed'))
        if not feed_state:
            # Boot: stamp the forward sample boundary (first session
            # strictly after the boot session, floored at the build
            # date) and seed the ATR warm-up in its own transaction.
            _, boot_sdate = ct_parts(now)
            feed_state = {'forward_from': max(boot_sdate, FORWARD_START),
                          'boot_sdate': boot_sdate, 'dropped_total': 0}
            try:
                with self.db.tx() as c:
                    raised_timeouts(c)
                    sessions = seed_sessions(self.db, c, ROOT, now,
                                             SEED_DAYS, SEED_MIN_BARS)
                feed.seed_history(sessions)
                feed_state['seed_sessions'] = len(sessions)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'vwap_cont_mes seed failed; ATR warms up live')
        forward_from = feed_state['forward_from']
        last_ts = feed_state.get('last_ts')
        with self.db.tx() as c:
            bars, gap = poll_bars(self.db, c, SYMBOL, ROOT, last_ts)
        opened = []
        closed_recs = []
        for bar in bars:
            was_pending = feed.pending is not None
            closed_recs.extend(feed.feed(bar))
            if was_pending and feed.open_trade is not None \
                    and feed.open_trade['entry_ts'] == bar['ts']:
                t = feed.open_trade
                if t['signal_sdate'] > forward_from:
                    opened.append(t)
            last_ts = bar['ts']
        feed_state['feed'] = feed.to_state()
        feed_state['last_ts'] = last_ts
        feed_state['dropped_total'] = feed.dropped
        if gap:
            feed_state['last_gap_at'] = now
        with self.db.tx() as c:
            for t in opened:
                self._open_episode(c, t, now)
            for rec in closed_recs:
                if rec['signal_sdate'] > forward_from:
                    self._close_episode(c, rec, now)
            self.db.put(c, FEED_KEY, feed_state)
            state = self._write_state(c, now, feed_state)
        counts = state['counts']
        self.db.health(
            'vwap_cont_mes', 'running',
            'MES VWAP-continuation z1.5 h60 forward shadow scorer '
            '(paper/measurement): '
            f"closed {counts['closed']}, open {counts['open']}, "
            f"detector net {state['detector']['net']}, "
            f"guard-kept net {state['guard_kept']['net']}",
            forward_from=forward_from,
            seed_sessions=feed_state.get('seed_sessions'), **counts)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'vwap-cont-mes', self.owner, 300):
                return
        self.process(self.now(time.time()))

    async def run(self):
        self.clock = time.time
        while True:
            try:
                await asyncio.to_thread(self.cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'vwap_cont_mes worker failed; retrying')
            await asyncio.sleep(60)
