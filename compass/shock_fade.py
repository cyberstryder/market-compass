"""Post-shock next-session fade — forward shadow scorer (2026-11-07, Josh).

Paper / measurement scope ONLY: no orders, no alerts, no Discord
output, no live sends. This module forward-scores the variance
screen's pass (program item 8, 2026-11-06) — one phenomenon at the
three nested scopes at which it passed, of which the report's
shadow proposal registers exactly two lines:

    (i)  POOLED p97.5 fade1, all 8 micro roots: n=476, net
         +$24,634.50, PF 1.472, placebo 100.0th percentile, stress
         +$22,968.50 / PF 1.433, max DD -$6,934.75.
    (ii) MNQ p95 fade1 standalone: n=113, net +$18,816.50, PF
         2.011, placebo 100.0th percentile, stress +$18,590.50 /
         PF 1.993, max DD -$2,301.50.

Frozen sources:
- files/variance-screen-registration-2026-11-06.md
  (sha256 a5949fb5755ebdb84c8d8f01cf1e1915d107a68ac303ddf8ba431d712de8a9c8)
- files/variance-screen-2026-11-06.md (report, shadow proposal)
- Forward registration: files/shock-fade-forward-registration-2026-11-07.md

The cell (registration §5b, fade1), byte-identical:

- Daily sessions per root (CME trade dates). Session close is the
  15:45 CT mark (close_1545: the close of the last bar starting at
  or before 15:44 CT); open/high/low are full-session values.
- TR_t = max(H - L, |H - C1545_{t-1}|, |L - C1545_{t-1}|). All
  trailing windows require the full 252 values: the percentile
  window at t is {TR_{t-251..t}} (including t), threshold = the
  q-th percentile value of that window (linear interpolation).
- Shock event at t iff TR_t >= threshold(q), q = 97.5 for the
  pooled line, q = 95 for the MNQ line. Shock direction
  s = sign(C1545_t - C1545_{t-1}); a zero-move shock day fires no
  event (counted).
- Entry: session t+1's open, AGAINST the shock direction (fade).
  Exit: session t+1's close_1545 (fade1 — flatten-consistent, no
  exception to Josh's 15:45 rule needed).
- P&L (the registration's daily cost stack) = dir * (exit - entry)
  * multiplier - (2 ticks * tick-$) - $3.00 fees; stress arm =
  dir * (exit - entry) * multiplier - (6 ticks * tick-$) - $3.00.
  Multipliers / tick-$ are the registration §1 panel table:
  MES 5/$1.25, MNQ 2/$0.50, MYM 0.5/$0.50, M2K 5/$0.50, MCL
  100/$1.00, MGC 10/$1.00, M6E 12500/$1.25, M6A 10000/$1.00.
  (instruments.FUTURES disagrees for the FX roots' tick size; the
  frozen registration's table governs scoring and is used here.)
- One position at a time per cell: fade1 entries and exits are the
  same session, so the dedupe can never bind; it is implemented
  anyway (an event completing while its line is open is skipped).

Production session ledger: each root's feed aggregates the live
1-minute bar stream into sessions (futures_shadow_common.SessionAgg)
and persists the last 300 finalized sessions with their TRs. At
boot, each root's ledger is seeded from the bar-events store (365
-day bar retention; 400-day lookback, sessions with >= 800 bars —
in a seed, bar count is the only continuity proxy, and the panels
likewise dropped degraded sessions; the seed runs one root per
cycle in its own raised-timeout transaction, the survivors seed
isolation). A live session whose bar stream has an internal hole
> 6h (or no 15:45 mark) is partial: it is excluded from the ledger
— no TR, no shock — exactly as the panel dropped degraded
sessions, and a trade entered on it resolves data_gap. Until a
root holds the full 252 TRs it produces no events (its depth is
reported in the state summary and health row); if the events store
cannot seed the full window for a root, that root simply accrues
one session per day until it can — named here so the silence is
never mistaken for a signal drought.

Sample rule (forward registration): only shocks on sessions after
the first full session following deployment count (boot-stamped,
floored at the FORWARD_START build date).

Wiring: episodes in the shock_fade_episodes table (declared in
.store), one per (line, root, shock session); lifecycle events
appended as shock_fade events; counts and per-line aggregates
recomputed from the table on every pass into the shock_fade:state
summary; health via Store.health(). The scorer renders no verdict:
the forward registration's frozen terms are read from this accrued
record. Standalone worker (connors_ssc pattern); it cannot throw
into the engine tick.
"""
import asyncio
import logging
import time
import uuid

from sqlalchemy import select, update

from .futures_shadow_common import (SessionAgg, book_stats, ct_parts,
                                    percentile_linear, poll_bars,
                                    raised_timeouts, seed_sessions)
from .store import identity
from .store import shock_fade_episodes as episodes

VERSION = 'shock-fade-v1'
KIND = 'shock_fade'
STATE_KEY = 'shock_fade:state'
FEED_KEY = 'shock_fade:feeds'
SYMBOL = 'shock_fade'   # event-log symbol column for this scorer
FORWARD_START = '2026-11-07'     # build date floor (see docstring)
WINDOW = 252
LEDGER_CAP = 300
SEED_DAYS = 400
SEED_MIN_BARS = 800
FEES_RT = 3.00

# root -> (alias, multiplier, tick-$) — the variance registration
# §1 panel table, verbatim (see the module docstring).
ROOTS = {
    'MES': ('MES.c.0', 5.0, 1.25), 'MNQ': ('MNQ.c.0', 2.0, 0.50),
    'MYM': ('MYM.c.0', 0.5, 0.50), 'M2K': ('M2K.c.0', 5.0, 0.50),
    'MCL': ('MCL.v.0', 100.0, 1.00), 'MGC': ('MGC.v.0', 10.0, 1.00),
    'M6E': ('M6E.c.0', 12500.0, 1.25), 'M6A': ('M6A.c.0', 10000.0, 1.00),
}
LINES = ('pooled_p975', 'mnq_p950')
LINE_Q = {'pooled_p975': 97.5, 'mnq_p950': 95.0}

# The episodes table is declared in .store (shock_fade_episodes) so
# Store init's create_all creates it before this module is lazily
# imported (the bb1adfa lesson).


def true_range(sess, prev_c1545):
    """Registration §4 TR vs the prior session's close_1545."""
    if prev_c1545 is None:
        return None
    return max(sess['h'] - sess['l'], abs(sess['h'] - prev_c1545),
               abs(sess['l'] - prev_c1545))


# ---------------------------------------------------------------------------
# Pure ledger (frozen math; unit-tested without a database)

class ShockLedger:
    """One root's session ledger + fade1 detection for both lines.

    Streaming use: feed() bars; entry/exit event dicts come back as
    they occur. Batch use (parity/tests): add_session() per
    finalized session dict {sdate,o,h,l,c1545[,partial]}.
    sample_gate(shock_sdate) -> bool decides whether an entry is a
    sample trade (worker: the forward boundary; batch: always)."""

    def __init__(self, root, sample_gate=None):
        self.root = root
        self.alias, self.mult, self.tickusd = ROOTS[root]
        self.agg = SessionAgg()
        self.sessions = []       # finalized, ledger-eligible sessions
        self.trs = []            # aligned TRs (None for the first)
        self.pending = {}        # line -> shock record awaiting entry
        self.opens = {}          # line -> open trade record
        self.sample_gate = sample_gate or (lambda sdate: True)
        self.zero_dir = 0
        self.skipped_open = 0

    # -- ledger core ---------------------------------------------------------

    def _append_session(self, sess):
        # At boot the seeded ledger can already hold this session
        # (seed + window catch-up overlap): replace it and recompute
        # its TR, never duplicate, or the percentile window corrupts.
        if self.sessions and self.sessions[-1]['sdate'] == sess['sdate']:
            self.sessions[-1] = sess
            prev = self.sessions[-2]['c1545'] if len(self.sessions) >= 2 \
                else None
            self.trs[-1] = true_range(sess, prev)
            return
        prev = self.sessions[-1]['c1545'] if self.sessions else None
        self.sessions.append(sess)
        self.trs.append(true_range(sess, prev))
        self.sessions = self.sessions[-LEDGER_CAP:]
        self.trs = self.trs[-LEDGER_CAP:]

    def _evaluate_shock(self, sess):
        """Shock detection on the just-appended session."""
        if len(self.trs) < WINDOW + 1:
            return
        window = self.trs[-WINDOW:]
        if any(t is None for t in window):
            return
        tr = self.trs[-1]
        prev_c = self.sessions[-2]['c1545']
        s = sess['c1545'] - prev_c
        if s == 0:
            self.zero_dir += 1
            return
        direction = -1 if s > 0 else 1     # fade the shock direction
        for line in LINES:
            if line == 'mnq_p950' and self.root != 'MNQ':
                continue
            thr = percentile_linear(window, LINE_Q[line])
            if tr >= thr:
                if line in self.opens or line in self.pending:
                    self.skipped_open += 1
                    continue
                self.pending[line] = {
                    'line': line, 'root': self.root,
                    'shock_sdate': sess['sdate'], 'dir': direction,
                    'tr': tr, 'thr': thr,
                    'sample': bool(self.sample_gate(sess['sdate']))}

    def _finalize(self, sess):
        """A session completed: close its entries at its 15:45 mark,
        then take it into the ledger and evaluate it for shocks."""
        events = []
        for line in list(self.opens):
            t = self.opens.pop(line)
            if t['entry_sdate'] != sess['sdate']:
                self.opens[line] = t     # not this session's entry
                continue
            rec = {**t, 'exit_sdate': sess['sdate'],
                   'exit_px': sess['c1545']}
            if sess.get('partial') or sess['c1545'] is None:
                rec.update(status='data_gap', pnl=None, pnl_stress=None)
            else:
                mv = t['dir'] * (sess['c1545'] - t['entry_px']) * self.mult
                rec.update(status='closed',
                           pnl=round(mv - (2 * self.tickusd + FEES_RT), 2),
                           pnl_stress=round(
                               mv - (6 * self.tickusd + FEES_RT), 2))
            rec['type'] = 'exit'
            events.append(rec)
        if sess.get('partial') or sess['c1545'] is None:
            return events     # excluded from the ledger (see docstring)
        self._append_session(sess)
        self._evaluate_shock(sess)
        return events

    def _open_pending(self, entry_sdate, entry_px):
        events = []
        for line in list(self.pending):
            p = self.pending.pop(line)
            t = {**p, 'entry_sdate': entry_sdate, 'entry_px': entry_px}
            self.opens[line] = t
            events.append({'type': 'entry', **t})
        return events

    # -- batch entry point (parity / tests) -----------------------------------

    def add_session(self, sess):
        events = self._open_pending(sess['sdate'], sess['o'])
        events.extend(self._finalize(sess))
        return events

    # -- streaming entry point --------------------------------------------------

    def feed(self, bar):
        events = []
        finalized = self.agg.feed(bar)
        if finalized is not None:
            events.extend(self._finalize(finalized))
        if self.agg.cur is not None and self.agg.cur['bars'] == 1 \
                and self.pending:
            events.extend(self._open_pending(self.agg.cur['sdate'],
                                             bar['o']))
        return events

    # -- seeding / persistence ---------------------------------------------------

    def install_seed(self, sessions):
        """Install seeded finalized sessions (all strictly before
        the in-progress one). No shock is evaluated on the last
        seeded session: its entry session is the in-progress one,
        whose first bar may already have streamed — the forward
        boundary gate would exclude it anyway in the worker (seeded
        sessions predate the boot session)."""
        cur = self.agg.cur['sdate'] if self.agg.cur else None
        have = {s['sdate'] for s in self.sessions}
        for s in sessions:
            if s['sdate'] in have or (cur is not None and s['sdate'] >= cur):
                continue
            if s.get('partial') or s['c1545'] is None:
                continue
            self._append_session(s)
            have.add(s['sdate'])

    def to_state(self):
        return {'agg': self.agg.to_state(), 'sessions': self.sessions,
                'trs': self.trs, 'pending': self.pending,
                'opens': self.opens, 'zero_dir': self.zero_dir,
                'skipped_open': self.skipped_open}

    @classmethod
    def from_state(cls, root, state, sample_gate=None):
        led = cls(root, sample_gate=sample_gate)
        if state:
            led.agg = SessionAgg.from_state(state.get('agg'))
            led.sessions = state.get('sessions') or []
            led.trs = state.get('trs') or []
            led.pending = state.get('pending') or {}
            led.opens = state.get('opens') or {}
            led.zero_dir = state.get('zero_dir') or 0
            led.skipped_open = state.get('skipped_open') or 0
        return led


# ---------------------------------------------------------------------------
# Scorer worker

class ShockFade:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner = uuid.uuid4().hex

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    # -- episodes ---------------------------------------------------------------

    def _event(self, c, p, kind, now, extra=None):
        body = {'episode_id': p['id'], 'symbol': p['underlying'],
                'event': kind, 'at': now, **(extra or {})}
        self.db.append(c, KIND, VERSION, p['underlying'], now, body,
                       key=identity(KIND, p['id'], kind,
                                    body.get('shock_sdate')))

    def _open_episode(self, c, ev, now):
        p = {
            'id': identity('shock-fade-episode-v1', ev['line'],
                           ev['root'], ev['shock_sdate']),
            'version': VERSION, 'strategy': 'shock-fade1',
            'line': ev['line'], 'root': ev['root'],
            'symbol': ROOTS[ev['root']][0],
            'underlying': ROOTS[ev['root']][0],
            'side': 'long' if ev['dir'] > 0 else 'short',
            'status': 'open', 'created_at': now,
            'shock': {'sdate': ev['shock_sdate'], 'tr': ev['tr'],
                      'thr': ev['thr'], 'q': LINE_Q[ev['line']]},
            'entry': {'sdate': ev['entry_sdate'], 'px': ev['entry_px']},
            'exit': None, 'pnl': None, 'pnl_stress': None,
            'outcome': None,
        }
        stmt = self.db.insert(episodes).values(
            id=p['id'], underlying=p['underlying'], status='open',
            created=now, updated=now, payload=p)
        c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        self._event(c, p, 'entry', now,
                    {'shock_sdate': ev['shock_sdate'],
                     'entry_px': ev['entry_px'], 'line': ev['line']})
        return p

    def _close_episode(self, c, ev, now):
        eid = identity('shock-fade-episode-v1', ev['line'], ev['root'],
                       ev['shock_sdate'])
        row = c.execute(select(episodes.c.payload).where(
            episodes.c.id == eid)).scalars().first()
        if row is None:
            return
        p = row
        p['exit'] = {'sdate': ev['exit_sdate'], 'px': ev['exit_px'],
                     'mark': 'close_1545'}
        p['pnl'] = ev['pnl']
        p['pnl_stress'] = ev['pnl_stress']
        p['status'] = ev['status']
        p['outcome'] = (None if ev['pnl'] is None else
                        'win' if ev['pnl'] > 0 else
                        'loss' if ev['pnl'] < 0 else 'breakeven')
        p['updated_at'] = now
        c.execute(update(episodes).where(episodes.c.id == eid).values(
            status=ev['status'], updated=now, payload=p))
        self._event(c, p, 'exit', now,
                    {'exit_sdate': ev['exit_sdate'], 'pnl': ev['pnl'],
                     'status': ev['status'], 'line': ev['line']})

    def _write_state(self, c, now, feeds_state):
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        lines = {}
        for line in LINES:
            mine = [p for p in rows if p['line'] == line]
            closed = [p for p in mine if p['status'] == 'closed']
            lines[line] = {
                'book': book_stats([p['pnl'] for p in closed]),
                'stress_net': round(sum(p['pnl_stress'] for p in closed), 2),
                'open': sum(1 for p in mine if p['status'] == 'open'),
                'data_gap': sum(1 for p in mine
                                if p['status'] == 'data_gap')}
        counts = {'created': len(rows),
                  'closed': sum(1 for p in rows if p['status'] == 'closed'),
                  'open': sum(1 for p in rows if p['status'] == 'open'),
                  'data_gap': sum(1 for p in rows
                                  if p['status'] == 'data_gap')}
        depth = {r: len(((feeds_state.get('roots') or {}).get(r, {})
                         .get('ledger') or {}).get('trs', []))
                 for r in ROOTS}
        state = {'version': VERSION, 'at': now, 'counts': counts,
                 'lines': lines, 'tr_depth': depth,
                 'forward_from': feeds_state.get('forward_from')}
        self.db.put(c, STATE_KEY, state)
        return state

    # -- driver ---------------------------------------------------------------

    def process(self, now):
        with self.db.tx() as c:
            feeds_state = self.db.get(c, FEED_KEY, {})
        if not feeds_state:
            _, boot_sdate = ct_parts(now)
            feeds_state = {'forward_from': max(boot_sdate, FORWARD_START),
                           'boot_sdate': boot_sdate, 'roots': {}}
            with self.db.tx() as c:
                self.db.put(c, FEED_KEY, feeds_state)
        forward_from = feeds_state['forward_from']

        def gate(sdate, _ff=forward_from):
            return sdate > _ff

        roots_state = feeds_state.setdefault('roots', {})
        for root in ROOTS:
            roots_state.setdefault(
                root, {'last_ts': None, 'seeded': False, 'ledger': None})
        # Seed one unseeded root per cycle, in its own transaction.
        for root in ROOTS:
            rs = roots_state[root]
            if rs['seeded']:
                continue
            try:
                with self.db.tx() as c:
                    raised_timeouts(c)
                    sessions = seed_sessions(self.db, c, root, now,
                                             SEED_DAYS, SEED_MIN_BARS)
                led = ShockLedger.from_state(root, rs['ledger'],
                                             sample_gate=gate)
                led.install_seed(sessions)
                rs['ledger'] = led.to_state()
                rs['seeded'] = True
                rs['seed_sessions'] = len(sessions)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'shock_fade seed failed for %s; will retry', root)
            break    # one seed attempt per cycle, success or failure
        entries, exits = [], []
        for root, (alias, _m, _t) in ROOTS.items():
            rs = roots_state[root]
            led = ShockLedger.from_state(root, rs['ledger'],
                                         sample_gate=gate)
            try:
                with self.db.tx() as c:
                    bars, gap = poll_bars(self.db, c, alias, root,
                                          rs['last_ts'])
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'shock_fade bar poll failed for %s', root)
                continue
            for bar in bars:
                for ev in led.feed(bar):
                    if not ev.get('sample', True):
                        continue
                    if ev['type'] == 'entry':
                        entries.append(ev)
                    else:
                        exits.append(ev)
                rs['last_ts'] = bar['ts']
            if gap:
                rs['last_gap_at'] = now
            rs['ledger'] = led.to_state()
        with self.db.tx() as c:
            for ev in entries:
                self._open_episode(c, ev, now)
            for ev in exits:
                self._close_episode(c, ev, now)
            self.db.put(c, FEED_KEY, feeds_state)
            state = self._write_state(c, now, feeds_state)
        counts = state['counts']
        shallow = [r for r, d in state['tr_depth'].items() if d <= WINDOW]
        self.db.health(
            'shock_fade', 'running',
            'Post-shock next-session fade forward shadow scorer '
            '(paper/measurement): '
            f"closed {counts['closed']}, open {counts['open']}, "
            f"data_gap {counts['data_gap']}; pooled net "
            f"{state['lines']['pooled_p975']['book']['net']}, MNQ p95 "
            f"net {state['lines']['mnq_p950']['book']['net']}"
            + (f"; warming up: {', '.join(shallow)}" if shallow else ''),
            forward_from=forward_from,
            tr_depth=state['tr_depth'], **counts)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'shock-fade', self.owner, 300):
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
                    'shock_fade worker failed; retrying')
            await asyncio.sleep(60)
