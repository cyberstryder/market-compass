"""MNQ post-news fade (t60) — forward shadow scorer (2026-11-07, Josh).

Paper / measurement scope ONLY: no orders, no alerts, no Discord
output, no live sends. This module forward-scores the PROVISIONAL
pass of the post-news continuation study (program item 4,
2026-11-06):

    MNQ fade-all t60 at the 08:30 ET anchor — n=47, net +$1,629,
    stress +$1,629, PF 1.83, halves +$308/+$1,322, placebo 97.0th
    percentile, max DD -$877. PROVISIONAL pass (§6 of the
    registration: no per-root cell on a one-year census can reach
    the n >= 50 full-pass floor). The word PROVISIONAL is part of
    the verdict and is kept everywhere this scorer reports.

Frozen sources:
- files/post-news-continuation-registration-2026-11-06.md
  (sha256 27cb1ebc4fa568523983a8b54c549442ee998bba2012e9222c2adcf6b3b8ec6d)
- files/post-news-continuation-2026-11-06.md (report)
- Forward registration: files/postnews-fade-forward-registration-2026-11-07.md

The cell, byte-identical to the gauntlet (score.py make_trade with
direction_sign=-1, exit t60, q=all, root MNQ, anchor 08:30):

- Event: a scheduled 08:30 ET release of a frozen class — Employment
  Situation, CPI, PPI, Retail Sales, GDP (advance estimate) — on its
  actual release date. Multiple classes at one datetime count ONCE.
- Reaction: i0 = first MNQ bar starting at/after 08:30 ET; the event
  is untradeable for the root if that bar starts > 30 min after the
  release or falls on a different CT date. P0 = open of i0. i1 =
  last bar starting at/before release + 10 min; P10 = close of i1.
  R0 = P10 - P0; R0 = 0 produces no trade (counted signless).
- Entry: AGAINST the reaction at P10 (fade), fill worsened 1 tick:
  entry = P10 + d * tick with d = -sign(R0). Exit t60: the open of
  the first bar starting at/after release + 70 min, capped by the
  15:45 CT flatten rule (exit at the open of the first bar at/after
  15:45 CT if the scheduled exit falls later; if none, the last bar
  at/before it — its open if it starts exactly at 15:45, else its
  close). Exit fill worsened 1 tick. P&L = d * (exit_fill -
  entry_fill) * $2.00/pt - $3.00 round-trip fees. Timed exits carry
  no stress repricing (pnl_stress == pnl), per the registration.

Runtime event source and the classification mapping (documented in
the forward registration, per the build spec): the TraderMatrix
economic calendar already integrated in Compass — the stored
intel:economic:{date} rows whose `high_impact` lists the news pause
consumes (compass/tradermatrix_intel.py). The scorer unions those
rows and classifies each stored event into a frozen class by title
patterns (classify_event below). The vendor row supplies the DATE
and the class only; the release instant is the registration's frozen
08:30 ET on that date, never the vendor clock string. Rows the
vendor explicitly rates below high impact never classify (the
event_opens_pause rule), and a row with a non-US country never
classifies. Known mapping limits, stated plainly: GDP counts only
when the title marks the advance/first estimate and does not mark a
later estimate (prelim/second/third/final/revised) — if the vendor
never marks the advance distinctly, GDP dates simply do not fire
(4 of 47 annual 08:30 event datetimes in the gauntlet census); ISM
(10:00 ET) and FOMC (14:00 ET) are NOT in this cell and never
classify here.

Sample rule (forward registration): only events dated on/after the
first ET date after this build deploys count (boot-stamped into the
scorer state, floored at the build date FORWARD_START). Nothing is
backfilled: an event whose bars have aged out of reach before it
could be evaluated is recorded data_gap, a first-class counted
outcome, never silently dropped.

Wiring: episodes in the postnews_fade_episodes table (declared in
.store); every evaluation is appended as a postnews_fade event;
counts and aggregates recomputed from the table on every pass into
the postnews_fade:state summary; health via Store.health(). The
scorer renders no verdict: the PROVISIONAL limbs of the gauntlet
registration §6, restated for the forward record in the forward
registration, are read from this accrued record. Standalone worker
(connors_ssc pattern); it cannot throw into the engine tick.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from .futures_shadow_common import (CT, NY, book_stats, ct_parts,
                                    poll_bars)
from .store import identity
from .store import postnews_fade_episodes as episodes
from .tradermatrix_intel import event_opens_pause, parse_event_time

VERSION = 'postnews-fade-v1'
KIND = 'postnews_fade'
STATE_KEY = 'postnews_fade:state'
DETAIL_KEY = 'postnews_fade:detail'
SYMBOL = 'MNQ.c.0'
ROOT = 'MNQ'
TICK = 0.25
PER_PT = 2.00           # $ per point (MNQ multiplier)
FEES_RT = 3.00          # $1.50/side
FORWARD_START = '2026-11-07'     # build date floor (see docstring)
RELEASE_HHMM_ET = (8, 30)
REACTION_MIN = 10
EXIT_MIN = 70           # release + 10m reaction + t60 hold
EVAL_LAG_MIN = 71       # evaluate once the exit bar can exist
RETRY_DAYS = 2          # bars unreachable after this -> data_gap

# The episodes table is declared in .store (postnews_fade_episodes)
# so Store init's create_all creates it before this module is lazily
# imported (the bb1adfa lesson).


# ---------------------------------------------------------------------------
# Event classification (the documented calendar mapping)

_CLASS_PATTERNS = (
    # (class, required any-of, forbidden any-of) — title, lowercased.
    ('NFP', ('nonfarm', 'non-farm', 'payroll', 'employment situation',
             'unemployment rate', 'hourly earnings'),
     ('claims', 'adp')),
    ('CPI', ('cpi', 'consumer price'), ()),
    ('PPI', ('ppi', 'producer price'), ()),
    ('RETAIL', ('retail sales',), ()),
    ('GDP', ('gdp', 'gross domestic product'),
     ('prelim', 'second', 'third', 'final', 'revised')),
)

_US_COUNTRIES = {'', 'us', 'usa', 'united states', 'united states of america'}


def classify_event(ev):
    """Map one stored intel:economic high-impact event to a frozen
    08:30 ET class ('NFP' | 'CPI' | 'PPI' | 'RETAIL' | 'GDP') or
    None. See the module docstring for the mapping and its limits.
    The event's own date is NOT checked here (the caller groups by
    date); its clock string must read as the 08:30 ET release —
    either as 08:30 (ET-rendered) or 07:30 (the same instant rendered
    in CT) — so other 08:30-ish releases and the 10:00 / 14:00
    anchors never classify."""
    if not isinstance(ev, dict):
        return None
    if not event_opens_pause(ev):
        return None
    country = str(ev.get('country') or '').strip().lower()
    if country not in _US_COUNTRIES:
        return None
    parsed = parse_event_time(ev.get('time'))
    if parsed not in ((8, 30), (7, 30)):
        return None
    title = str(ev.get('title') or '').lower()
    if not title:
        return None
    for cls, required, forbidden in _CLASS_PATTERNS:
        if any(f in title for f in forbidden):
            continue
        if any(r in title for r in required):
            if cls == 'GDP' and not ('advance' in title or 'first' in title):
                continue    # later GDP estimates are not the frozen class
            return cls
    return None


def release_ts(date_str):
    """Epoch seconds of 08:30 ET on an ET date string."""
    dt = datetime.fromisoformat(date_str).replace(
        hour=RELEASE_HHMM_ET[0], minute=RELEASE_HHMM_ET[1], tzinfo=NY)
    return int(dt.timestamp())


def _flat_ts(date_str):
    """Epoch seconds of 15:45 CT on a CT date string."""
    dt = datetime.fromisoformat(date_str).replace(
        hour=15, minute=45, tzinfo=CT)
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Pure trade construction (the gauntlet's score.py, MNQ fade-all t60)

def reaction(bars, rel_ts, date_str):
    """bars: ascending {ts,o,h,l,c} dicts. Returns
    {P0, P10, R0, R0pct, RANGE0, i1} or None when the event is
    untradeable for the root (gauntlet §2 missing-bar rule: first bar
    more than 30 min after the release, or on a different CT date)."""
    i0 = None
    for i, b in enumerate(bars):
        if b['ts'] >= rel_ts:
            i0 = i
            break
    if i0 is None:
        return None
    if bars[i0]['ts'] - rel_ts > 1800:
        return None
    if ct_parts(bars[i0]['ts'])[1] != date_str:
        return None
    i1 = i0
    for j in range(i0, len(bars)):
        if bars[j]['ts'] <= rel_ts + REACTION_MIN * 60:
            i1 = j
        else:
            break
    P0 = bars[i0]['o']
    P10 = bars[i1]['c']
    rng = (max(b['h'] for b in bars[i0:i1 + 1])
           - min(b['l'] for b in bars[i0:i1 + 1]))
    return {'P0': P0, 'P10': P10, 'R0': P10 - P0,
            'R0pct': (P10 - P0) / P0 if P0 else 0.0, 'RANGE0': rng,
            'i1': i1}


def _flat_fallback(bars, i1, flat):
    """The gauntlet's flatten fallback: the last bar at/before the
    flatten — its open if it starts exactly at the flatten, else its
    close; the reaction bar's close when no such bar exists."""
    k = None
    for i, b in enumerate(bars):
        if b['ts'] <= flat:
            k = i
        else:
            break
    if k is not None and k >= i1:
        return (bars[k]['o'] if bars[k]['ts'] == flat else bars[k]['c']), 'flat'
    return bars[i1]['c'], 'flat'


def _timed_exit(bars, i1, target_ts, flat):
    """(exit_price, kind): the gauntlet's timed_exit — open of the
    first bar starting at/after min(target, flatten); the flatten
    fallback when that bar starts after the flatten. One production
    guard the panel never needed: when the target is at or before
    the flatten and NO bar at/after it exists in the loaded bars at
    all, the exit bar may simply not have streamed yet — returns
    (None, 'missing') and the caller decides (retry, or the flatten
    fallback once the session is provably complete)."""
    eff = min(target_ts, flat)
    j = None
    for i, b in enumerate(bars):
        if b['ts'] >= eff:
            j = i
            break
    if j is not None and bars[j]['ts'] <= flat:
        return bars[j]['o'], ('flat' if target_ts > flat else 'timed')
    if j is None and target_ts <= flat:
        return None, 'missing'
    return _flat_fallback(bars, i1, flat)


def fade_trade(bars, rel_ts, date_str, complete=False):
    """The frozen cell over one event's bars. Returns a trade dict,
    or {'status': 'untradeable'} / {'status': 'signless'} counted
    outcomes (never silent). With complete=False (the session may
    still be streaming), a not-yet-existent exit bar reports
    {'status': 'no_exit_bar'} so the caller retries; with
    complete=True the gauntlet's flatten fallback applies to the
    bars that exist — the 2026-04-03 Good Friday early close in the
    gauntlet record resolves exactly this way (bars end before the
    +70m exit; the exit is the last morning bar's close, 'flat')."""
    rx = reaction(bars, rel_ts, date_str)
    if rx is None:
        return {'status': 'untradeable'}
    if rx['R0'] == 0:
        return {'status': 'signless', 'reaction': rx}
    d = -1 if rx['R0'] > 0 else 1     # fade: against the reaction
    flat = _flat_ts(date_str)
    ex_px, kind = _timed_exit(bars, rx['i1'],
                              rel_ts + EXIT_MIN * 60, flat)
    if ex_px is None:
        if not complete:
            return {'status': 'no_exit_bar', 'reaction': rx}
        ex_px, kind = _flat_fallback(bars, rx['i1'], flat)
    entry_fill = rx['P10'] + d * TICK
    exit_fill = ex_px - d * TICK
    pnl = d * (exit_fill - entry_fill) * PER_PT - FEES_RT
    return {'status': 'closed', 'd': d, 'reaction': rx,
            'entry_fill': entry_fill, 'exit_px': ex_px,
            'exit_fill': exit_fill, 'exit_kind': kind,
            'pnl': round(pnl, 2), 'pnl_stress': round(pnl, 2)}


# ---------------------------------------------------------------------------
# Scorer worker

class PostnewsFade:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner = uuid.uuid4().hex

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    # -- calendar --------------------------------------------------------------

    def _calendar_events(self, c, now):
        """Union of stored intel:economic high-impact events from the
        last ~16 days of refresh rows, grouped {et_date: {classes}}.
        Only frozen-class 08:30 ET events (classify_event) appear."""
        today = datetime.fromtimestamp(now, NY).date()
        cutoff = (today - timedelta(days=16)).isoformat()
        grouped = {}
        for key, row in self.db.prefix(c, 'intel:economic:').items():
            if key.rsplit(':', 1)[-1] < cutoff:
                continue
            for ev in (row or {}).get('high_impact', []):
                cls = classify_event(ev)
                if cls is None:
                    continue
                d = ev.get('date')
                if not d:
                    continue
                grouped.setdefault(d, set()).add(cls)
        return grouped

    # -- episodes ---------------------------------------------------------------

    def _record(self, c, date_str, classes, result, now):
        p = {
            'id': identity('postnews-fade-episode-v1', date_str),
            'version': VERSION, 'strategy': 'mnq-postnews-fade-all-t60',
            'symbol': SYMBOL, 'underlying': SYMBOL,
            'side': None, 'status': result['status'],
            'created_at': now, 'date': date_str,
            'classes': sorted(classes),
            'rel_ts': release_ts(date_str),
            'reaction': None, 'entry': None, 'exit': None,
            'pnl': None, 'pnl_stress': None, 'outcome': None,
        }
        rx = result.get('reaction')
        if rx:
            p['reaction'] = {'P0': rx['P0'], 'P10': rx['P10'],
                             'R0': rx['R0'], 'R0pct': rx['R0pct'],
                             'RANGE0': rx['RANGE0']}
        if result['status'] == 'closed':
            p['side'] = 'long' if result['d'] > 0 else 'short'
            p['entry'] = {'px': result['entry_fill'],
                          'at': 'release+10m_close'}
            p['exit'] = {'px': result['exit_fill'],
                         'raw_px': result['exit_px'],
                         'kind': result['exit_kind'],
                         'at': 'release+70m_open_or_flatten'}
            p['pnl'] = result['pnl']
            p['pnl_stress'] = result['pnl_stress']
            p['outcome'] = ('win' if result['pnl'] > 0 else
                            'loss' if result['pnl'] < 0 else 'breakeven')
        stmt = self.db.insert(episodes).values(
            id=p['id'], underlying=SYMBOL, status=p['status'],
            created=now, updated=now, payload=p)
        c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        self.db.append(c, KIND, VERSION, SYMBOL, now,
                       {'episode_id': p['id'], 'event': p['status'],
                        'date': date_str, 'classes': p['classes'],
                        'pnl': p['pnl'], 'at': now},
                       key=identity(KIND, p['id'], p['status']))
        return p

    def _write_state(self, c, now, detail):
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        closed = [p for p in rows if p['status'] == 'closed']
        agg = book_stats([p['pnl'] for p in closed])
        counts = {'created': len(rows), 'closed': len(closed),
                  'untradeable': sum(1 for p in rows
                                     if p['status'] == 'untradeable'),
                  'signless': sum(1 for p in rows
                                  if p['status'] == 'signless'),
                  'data_gap': sum(1 for p in rows
                                  if p['status'] == 'data_gap')}
        state = {'version': VERSION, 'at': now, 'counts': counts,
                 'book': agg, 'provisional': True,
                 'forward_from': detail.get('forward_from')}
        self.db.put(c, STATE_KEY, state)
        return state

    # -- driver ---------------------------------------------------------------

    def process(self, now):
        with self.db.tx() as c:
            detail = self.db.get(c, DETAIL_KEY, {})
        if not detail:
            boot_date = datetime.fromtimestamp(now, NY).date()
            forward = max((boot_date + timedelta(days=1)).isoformat(),
                          (datetime.fromisoformat(FORWARD_START)
                           + timedelta(days=1)).isoformat())
            detail = {'forward_from': forward, 'evaluated': {}}
            with self.db.tx() as c:
                self.db.put(c, DETAIL_KEY, detail)
        forward_from = detail['forward_from']
        evaluated = detail.setdefault('evaluated', {})
        with self.db.tx() as c:
            grouped = self._calendar_events(c, now)
        todo = []
        for date_str in sorted(grouped):
            if date_str < forward_from or date_str in evaluated:
                continue
            rel = release_ts(date_str)
            if now < rel + EVAL_LAG_MIN * 60:
                continue    # the exit bar cannot exist yet
            todo.append((date_str, grouped[date_str], rel))
        for date_str, classes, rel in todo:
            try:
                with self.db.tx() as c:
                    bars, _gap = poll_bars(self.db, c, SYMBOL, ROOT,
                                           rel - 3600)
                bars = [b for b in bars if b['ts'] <= rel + 30 * 3600]
                # The session is provably complete once any bar past
                # the 15:45 CT flatten exists in the store.
                complete = any(b['ts'] > _flat_ts(date_str) for b in bars)
                result = fade_trade(bars, rel, date_str,
                                    complete=complete) if bars \
                    else {'status': 'untradeable'}
                if result['status'] in ('untradeable', 'no_exit_bar'):
                    if now < rel + RETRY_DAYS * 86400:
                        continue    # bars may still arrive; retry
                    if not bars:
                        result = {'status': 'data_gap'}
                    elif result['status'] == 'no_exit_bar':
                        # Best available bars are final: the frozen
                        # flatten fallback prices the exit.
                        result = fade_trade(bars, rel, date_str,
                                            complete=True)
                    # An untradeable event WITH session bars on
                    # record is the frozen outcome (e.g. a holiday
                    # schedule), not a data gap: it stands.
                with self.db.tx() as c:
                    self._record(c, date_str, classes, result, now)
                evaluated[date_str] = result['status']
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'postnews_fade event %s failed; will retry', date_str)
        # Prune the evaluated ledger to the last 90 days.
        cutoff = (datetime.fromtimestamp(now, NY).date()
                  - timedelta(days=90)).isoformat()
        detail['evaluated'] = {d: s for d, s in evaluated.items()
                               if d >= cutoff}
        with self.db.tx() as c:
            self.db.put(c, DETAIL_KEY, detail)
            state = self._write_state(c, now, detail)
        counts = state['counts']
        self.db.health(
            'postnews_fade', 'running',
            'MNQ post-news fade t60 forward shadow scorer '
            '(PROVISIONAL pass; paper/measurement): '
            f"closed {counts['closed']}, net {state['book']['net']}, "
            f"untradeable {counts['untradeable']}, "
            f"signless {counts['signless']}, "
            f"data_gap {counts['data_gap']}",
            forward_from=forward_from, provisional=True, **counts)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'postnews-fade', self.owner, 300):
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
                    'postnews_fade worker failed; retrying')
            await asyncio.sleep(60)
