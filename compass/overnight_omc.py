"""Overnight Morning-Change (OMC) — forward premium scorer (2026-11-03).

Paper / measurement scope ONLY: no orders, no alerts, no live sends, no
Discord output. This module shadow-fires Josh's overnight idea exactly
as frozen in the OMC registration
(files/overnight-morning-change-registration-2026-11-03.md, sha256
23a1f84a844ac2a0a9ade52a785eee0309a3ccd7e442a8a8fccc87403aa35406):

- Signal (frozen §2): at session D's close, from the symbol's own
  daily bars — loc = (C - L) / (H - L), disp = (C - C_prev) / (H - L).
  UP (buy calls): loc >= 0.80 and disp >= +0.75. DOWN (buy puts):
  loc <= 0.20 and disp <= -0.75. Deliberately NOT raw close strength:
  M3 (strong-close overnight) failed its placebo on 2026-11-02.
- Sample (frozen §11): only signals on/after the sample start — the
  first entry session whose following session has a chain_open
  snapshot (expected 2026-11-04) — stamped at bootstrap and never
  moved. Nothing is backfilled.
- Entry (frozen §3): one outright contract at the ask of the entry
  session's EOD chain snapshot (chain_eod — the named executable
  proxy for a closing-30-minutes entry). DTE in [1, 7] from the
  snapshot date (a 0-DTE contract cannot survive overnight),
  |delta| in [0.30, 0.50] targeting 0.40 (preference: |delta -
  0.40|, then expiry closest to 3 DTE, then strike closest to spot,
  then symbol). $0.65/contract/leg/side. G2 scoreability gate on the
  pick: spread <= max(15% of ask, $0.05), else gate_excluded — a
  first-class counted outcome whose underlying outcome is still
  tracked.
- Exit (frozen §4): the held contract's bid in the NEXT session's
  morning chain snapshot (chain_open, fired 09:35 ET — Josh: "there
  are times I need to sell before 9:45 to get the move"). A morning
  quote counts only with a quote timestamp >= 09:30 ET on the exit
  date (stale overnight prints are unquoted -> data_gap). An exit
  bid of exactly 0.00 with a fresh timestamp is executable. A
  secondary, non-gated readout stamps the same contract's EOD bid.
- Earnings (frozen §6): evaluated at exit-pricing time from the
  union of stored TraderMatrix calendar states (entry date through
  resolution): an announcement dated the exit session (any time) or
  the entry session with after-close/unknown timing excludes the
  episode (earnings_excluded, underlying still recorded). No
  full-calendar coverage anywhere in range -> scored, stamped
  earnings_unverified.
- Cross-check (frozen §5): signed underlying bps, entry close ->
  morning underlying_close, counted only when the morning
  reference is a fresh same-day quote (>= 09:30 ET); otherwise the
  cross-check is missing and counted, the premium exit unaffected.
- data_gap is a first-class counted outcome throughout.

Wiring mirrors connors_ssc: episodes in the overnight_omc_episodes
table (declared in .store), registered in
active_observations.inventory(); created/opened/closed (plus open,
data_gap, gate_excluded, earnings_excluded) recomputed into the
overnight_omc:state row every pass beside the reporting limbs. The
scorer renders no verdict: the registration's frozen terms (two
consecutive countable 20-session windows, >= 100 scored each,
pooled n >= 200, pooled net > 0, PF > 1.0, halves positive,
cross-check sign agreement, placebo >= 95th pct) are read from
this accrued record.
"""
import asyncio
import logging
import time
import uuid
from datetime import date as _date, datetime, timedelta

from sqlalchemy import select, update

from .chain_eod import read_manifest as _read_manifest
from .connors_ssc import exit_quote_iv, spread_gate
from .market import NY, day, number
from .store import identity
from .store import overnight_omc_episodes as episodes
from .swings_c2 import daily_series, master_sessions, snapshot_on_or_after

VERSION = 'overnight-omc-v1'
KIND = 'overnight_omc'
STATE_KEY = 'overnight_omc:state'
FEE = 0.65            # per contract, per leg, per side (frozen convention)
QTY = 1               # one contract per episode
SAMPLE_FLOOR = '2026-11-04'   # earliest possible sample start (ET)
MIN_DTE, MAX_DTE, TARGET_DTE = 1, 7, 3
DELTA_LO, DELTA_HI, DELTA_TARGET = 0.30, 0.50, 0.40
LOC_HI, DISP_MIN = 0.80, 0.75
OPEN_MINUTES = 9 * 60 + 30    # 09:30 ET — morning freshness boundary

# The episodes table is declared in .store (overnight_omc_episodes) so
# Store init's create_all creates it before this module is lazily
# imported (the bb1adfa lesson).


# ---------------------------------------------------------------------------
# Pure helpers (frozen math; unit-tested without a database)

def direction_signal(bar, prev_close):
    """The frozen §2 rule. Returns (side, loc, disp) with side in
    {'up', 'down', None}. No range (H == L) or no prior close -> no
    signal."""
    h, l, c = bar['h'], bar['l'], bar['c']
    if prev_close is None or h <= l:
        return None, None, None
    loc = (c - l) / (h - l)
    disp = (c - prev_close) / (h - l)
    if loc >= LOC_HI and disp >= DISP_MIN:
        return 'up', loc, disp
    if loc <= 1 - LOC_HI and disp <= -DISP_MIN:
        return 'down', loc, disp
    return None, loc, disp


def select_contract(snapshot, side):
    """The registration's contract rule applied to one EOD snapshot:
    outright call (up) or put (down), DTE in [1, 7] from the snapshot
    date, |delta| in [0.30, 0.50] (delta must be present), executable
    quote (bid > 0, ask >= bid). Deterministic preference:
    |delta - 0.40|, then expiry closest to 3 DTE, then strike
    closest to spot, then symbol. Returns the archive row or None."""
    spot = number(snapshot.get('underlying_close'))
    snap_date = snapshot.get('date')
    if not spot or spot <= 0 or not snap_date:
        return None
    want = 'call' if side == 'up' else 'put'
    start = _date.fromisoformat(snap_date)
    best = None
    for o in snapshot.get('contracts', []):
        if o.get('type') != want:
            continue
        strike = number(o.get('strike'))
        delta = number(o.get('delta'))
        expiry = o.get('expiry') or ''
        if strike is None or delta is None or not expiry:
            continue
        try:
            dte = (_date.fromisoformat(expiry) - start).days
        except ValueError:
            continue
        if not (MIN_DTE <= dte <= MAX_DTE):
            continue
        if not (DELTA_LO <= abs(delta) <= DELTA_HI):
            continue
        q = o.get('quote') or {}
        bid, ask = number(q.get('bid')), number(q.get('ask'))
        if bid is None or ask is None or bid <= 0 or ask < bid or ask <= 0:
            continue
        key = (abs(abs(delta) - DELTA_TARGET), abs(dte - TARGET_DTE),
               abs(strike - spot), o.get('symbol') or '')
        if best is None or key < best[0]:
            best = (key, o)
    return best[1] if best else None


def _et_minutes(ts):
    """(ET date str, minutes since ET midnight) for an epoch ts."""
    dt = datetime.fromtimestamp(ts, NY)
    return dt.date().isoformat(), dt.hour * 60 + dt.minute


def fresh_morning_ts(ts, date_str):
    """Frozen §4/§5 freshness: the timestamp falls on date_str at or
    after 09:30 ET."""
    if ts is None:
        return False
    try:
        d, mins = _et_minutes(float(ts))
    except (TypeError, ValueError):
        return False
    return d == date_str and mins >= OPEN_MINUTES


def _timing_class(raw):
    """Vendor earnings 'time' -> 'after' | 'before' | 'unknown'."""
    s = str(raw or '').strip().lower()
    if not s:
        return 'unknown'
    if 'bmo' in s or 'before' in s or 'pre' in s:
        return 'before'
    if 'amc' in s or 'after' in s or 'close' in s:
        return 'after'
    return 'unknown'


def earnings_verdict(events, symbol, entry_date, exit_date):
    """The frozen §6 rule over a union of calendar events. Returns
    the excluding event dict or None."""
    for ev in events:
        if ev.get('symbol') != symbol:
            continue
        d = ev.get('date')
        if d == exit_date:
            return ev
        if d == entry_date and _timing_class(ev.get('time')) != 'before':
            return ev
    return None


# ---------------------------------------------------------------------------
# Scorer

class OvernightOMC:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner = uuid.uuid4().hex

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    # -- episode persistence -------------------------------------------------

    def _save(self, c, p, now, status=None):
        p['updated_at'] = now
        c.execute(update(episodes).where(episodes.c.id == p['id']).values(
            status=status or p['status'], updated=now, payload=p))

    def _event(self, c, p, kind, now, extra=None):
        body = {'episode_id': p['id'], 'symbol': p['symbol'], 'event': kind,
                'at': now, **(extra or {})}
        self.db.append(c, KIND, VERSION, p['symbol'], now, body,
                       key=identity(KIND, p['id'], kind, body.get('date')))

    def _counts(self, c):
        rows = c.execute(select(episodes.c.status, episodes.c.payload)).all()
        counts = {'created': 0, 'opened': 0, 'closed': 0, 'open': 0,
                  'data_gap': 0, 'gate_excluded': 0, 'earnings_excluded': 0}
        for status, p in rows:
            counts['created'] += 1
            if p.get('entry'):
                counts['opened'] += 1
            if status in counts and status != 'created':
                counts[status] += 1
        return counts

    def _write_state(self, c, now, extra=None):
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        ask_lines = {'le_1.00': {'n': 0, 'net': 0.0},
                     '1.00_5.00': {'n': 0, 'net': 0.0},
                     'gt_5.00': {'n': 0, 'net': 0.0}}
        cross, gate_diag, earn_diag, secondary = [], [], [], []
        planned_open = 0.0
        cross_missing = 0
        for p in rows:
            if p['status'] == 'open' and p.get('planned_loss'):
                planned_open += p['planned_loss']
            if p['status'] == 'closed' and p.get('pnl') is not None:
                ask = p['entry']['ask']
                bucket = ('le_1.00' if ask <= 1.00 else
                          '1.00_5.00' if ask <= 5.00 else 'gt_5.00')
                ask_lines[bucket]['n'] += 1
                ask_lines[bucket]['net'] += p['pnl']
                if p.get('underlying_morning'):
                    cross.append(p['underlying_morning']['bps'])
                else:
                    cross_missing += 1
                if (p.get('secondary_eod') or {}).get('status') == 'priced':
                    secondary.append(p['secondary_eod']['pnl'])
            if p['status'] == 'gate_excluded' and p.get('underlying_morning'):
                gate_diag.append(p['underlying_morning']['bps'])
            if (p['status'] == 'earnings_excluded'
                    and p.get('underlying_morning')):
                earn_diag.append(p['underlying_morning']['bps'])

        def agg(vals):
            return {'n': len(vals),
                    'mean_bps': (sum(vals) / len(vals)) if vals else None}

        def agg_pnl(vals):
            return {'n': len(vals),
                    'mean_pnl': (sum(vals) / len(vals)) if vals else None}
        state = {'version': VERSION, 'at': now, 'counts': self._counts(c),
                 'planned_loss_open': round(planned_open, 2),
                 'ask_lines': ask_lines,
                 'underlying_crosscheck': agg(cross),
                 'crosscheck_missing': cross_missing,
                 'gate_excluded_underlying': agg(gate_diag),
                 'earnings_excluded_underlying': agg(earn_diag),
                 'secondary_eod_exit': agg_pnl(secondary),
                 **(extra or {})}
        old = self.db.get(c, STATE_KEY, {})
        merged = {**old, **state}
        self.db.put(c, STATE_KEY, merged)
        return merged

    # -- archive helpers -------------------------------------------------------

    def _snapshot_on(self, c, kind, symbol, date_str):
        """The archive payload of the given kind for (symbol, exactly
        date_str), or None. Exact-date: the frozen exit rule never
        prices from a later session's snapshot."""
        after = 0
        while True:
            rows = self.db.export_events(c, kind, symbol=symbol,
                                         after_id=after, limit=200)
            if not rows:
                return None
            for r in rows:
                payload = r['payload']
                if (payload.get('date') or '') == date_str:
                    return payload
                after = r['id']
            if len(rows) < 200:
                return None

    def _morning_decision(self, c, symbol, exit_date, sessions):
        """Has the exit session's morning snapshot DECIDED for this
        symbol? Returns (decided, payload_or_None, reason):
        - manifest for exit_date finished: symbol ok -> payload
          (None payload if the event itself is missing); symbol not
          ok -> decided with no payload.
        - no manifest for exit_date but one exists for a later
          session -> decided missing.
        - otherwise not decided (wait)."""
        m = _read_manifest(self.db, exit_date, kind='chain_open')
        if m.get('finished_at'):
            entry = (m.get('symbols') or {}).get(symbol) or {}
            if entry.get('status') == 'ok':
                return True, self._snapshot_on(c, 'chain_open', symbol,
                                               exit_date), 'snapshot'
            return True, None, 'symbol_error'
        idx = sessions.index(exit_date) if exit_date in sessions else -1
        for later in sessions[idx + 1:]:
            if later > day(time.time()):
                break
            m2 = _read_manifest(self.db, later, kind='chain_open')
            if m2.get('finished_at') or m2.get('symbols'):
                return True, None, 'morning_snapshot_missing'
        return False, None, None

    def _earnings_events(self, c, entry_date, through_date):
        """Union of stored full-calendar events from intel:earnings:*
        rows dated entry_date..through_date. Returns (events,
        coverage) — coverage True when at least one row in range
        carries the full-calendar field at all."""
        events, coverage = [], False
        d = _date.fromisoformat(entry_date)
        end = _date.fromisoformat(through_date)
        while d <= end:
            row = self.db.get(c, f'intel:earnings:{d.isoformat()}')
            if isinstance(row, dict) and 'all_earnings' in row:
                coverage = True
                events.extend(row.get('all_earnings') or [])
            d += timedelta(days=1)
        return events, coverage

    def _stamp_underlying_morning(self, p, payload, exit_date):
        """The §5 diagnostic from a morning payload, freshness-guarded.
        Returns True when a cross-check was stamped."""
        if payload is None:
            return False
        price = number(payload.get('underlying_close'))
        ts = payload.get('underlying_close_ts')
        if price is None or price <= 0 or not fresh_morning_ts(ts, exit_date):
            p['underlying_morning'] = None
            p['crosscheck_missing_reason'] = 'morning_reference_not_fresh'
            return False
        entry_close = p['signal']['close']
        sign = 1.0 if p['signal']['side'] == 'up' else -1.0
        p['underlying_morning'] = {
            'date': exit_date, 'price': price, 'reference_ts': ts,
            'bps': sign * (price - entry_close) / entry_close * 1e4}
        return True

    # -- session processing ----------------------------------------------------

    def _open_symbols(self, c):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open')).scalars().all()
        return {p['symbol'] for p in rows}

    def _create_episode(self, c, sym, d, nxt, bar, prev_close, side, loc,
                        disp, now):
        p = {
            'id': identity('overnight-omc-episode-v1', sym, d),
            'version': VERSION, 'strategy': 'overnight-morning-change',
            'symbol': sym, 'underlying': sym, 'side': side,
            'status': 'open', 'stage': 'awaiting_entry',
            'premium_status': 'awaiting_entry',
            'created_at': now,
            'signal': {'date': d, 'exit_date': nxt, 'side': side,
                       'close': bar['c'], 'open': bar['o'], 'high': bar['h'],
                       'low': bar['l'], 'prev_close': prev_close,
                       'loc': loc, 'disp': disp},
            'contract': None, 'entry': None, 'exit': None,
            'underlying_morning': None, 'secondary_eod': None,
            'planned_loss': None, 'pnl': None, 'pnl_mid': None,
            'friction_gap': None, 'return_pct': None, 'outcome': None,
        }
        stmt = self.db.insert(episodes).values(
            id=p['id'], underlying=sym, status='open', created=now,
            updated=now, payload=p)
        c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        self._event(c, p, 'episode_created', now,
                    {'date': d, 'side': side, 'loc': loc, 'disp': disp,
                     'close': bar['c']})
        return p

    def _process_session(self, c, d, sessions, now):
        ci = sessions.index(d)
        nxt = sessions[ci + 1] if ci + 1 < len(sessions) else None
        window = sessions[max(0, ci - 9):ci + 1]
        occupied = self._open_symbols(c)
        census = {'date': d, 'exit_date': nxt, 'signals_up': 0,
                  'signals_down': 0, 'created': 0, 'skipped': 0,
                  'created_symbols': []}
        if nxt is None:
            return census
        for sym in self.cfg.watch_symbols:
            bars = daily_series(self.db, c, sym, window)
            if len(bars) < 2 or bars[-1]['date'] != d:
                continue  # symbol did not trade this master session
            side, loc, disp = direction_signal(bars[-1], bars[-2]['c'])
            if side is None:
                continue
            census['signals_up' if side == 'up' else 'signals_down'] += 1
            if sym in occupied:
                census['skipped'] += 1
                continue
            self._create_episode(c, sym, d, nxt, bars[-1], bars[-2]['c'],
                                 side, loc, disp, now)
            census['created'] += 1
            census['created_symbols'].append(sym)
        self.db.append(c, KIND, VERSION, 'CENSUS', now,
                       {'event': 'session_census', **census},
                       key=identity(KIND, 'census', d))
        st = self.db.get(c, STATE_KEY, {})
        st['last_census'] = census
        st['last_session_processed'] = d
        self.db.put(c, STATE_KEY, st)
        return census

    # -- sample start (frozen §11) ----------------------------------------------

    def _determine_sample_start(self, c, sessions, today):
        """The earliest session >= SAMPLE_FLOOR whose NEXT session
        carries a chain_open manifest. Stamped once at bootstrap and
        never moved. None while the substrate has not landed."""
        state = self.db.get(c, STATE_KEY, {})
        if state.get('sample_start'):
            return state['sample_start']
        start = None
        for i, d in enumerate(sessions):
            if d < SAMPLE_FLOOR or d >= today or i + 1 >= len(sessions):
                continue
            m = _read_manifest(self.db, sessions[i + 1], kind='chain_open')
            if m.get('finished_at') or m.get('symbols'):
                start = d
                break
        if start:
            state['sample_start'] = start
            self.db.put(c, STATE_KEY, state)
            self.db.append(c, KIND, VERSION, 'CENSUS', time.time(),
                           {'event': 'sample_start', 'date': start,
                            'sample_start': start},
                           key=identity(KIND, 'sample_start', start))
        return start

    def _bootstrap(self, c, now, sample_start):
        census = {'as_of': day(now), 'sample_start': sample_start,
                  'watch_symbols': len(self.cfg.watch_symbols)}
        self.db.append(c, KIND, VERSION, 'CENSUS', now,
                       {'event': 'bootstrap', **census},
                       key=identity(KIND, 'bootstrap', VERSION))
        st = self.db.get(c, STATE_KEY, {})
        st['bootstrap'] = census
        self.db.put(c, STATE_KEY, st)
        return census

    # -- premium pricing ---------------------------------------------------------

    def _price_entries(self, c, now):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)
        ).scalars().all()
        for p in rows:
            if p.get('entry') is not None or p['premium_status'] != 'awaiting_entry':
                continue
            snap = snapshot_on_or_after(self.db, c, p['symbol'],
                                        p['signal']['date'])
            if snap is None:
                continue  # archive has not reached the signal date yet
            chosen = select_contract(snap, p['signal']['side'])
            if chosen is None:
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'awaiting_underlying'
                p['entry_data_gap'] = {
                    'snapshot_date': snap.get('date'),
                    'reason': 'no_eligible_executable_contract'}
                self._event(c, p, 'entry_data_gap', now,
                            {'date': p['signal']['date'],
                             'snapshot_date': snap.get('date')})
                self._save(c, p, now, status='data_gap')
                continue
            q = chosen['quote']
            bid, ask = number(q.get('bid')), number(q.get('ask'))
            p['contract'] = {'symbol': chosen['symbol'],
                             'expiry': chosen['expiry'],
                             'strike': chosen['strike'],
                             'type': chosen['type']}
            p['planned_loss'] = ask * 100 * QTY + 2 * FEE
            spread, gate, passes = spread_gate(bid, ask)
            if not passes:
                p['status'] = 'gate_excluded'
                p['premium_status'] = 'gate_excluded'
                p['stage'] = 'awaiting_underlying'
                p['gate_exclusion'] = {
                    'snapshot_date': snap['date'],
                    'snapshot_asof': snap.get('asof'),
                    'snapshot_source': snap.get('source'),
                    'contract_symbol': chosen['symbol'],
                    'bid': bid, 'ask': ask, 'quote_ts': q.get('ts'),
                    'iv': number(chosen.get('iv')),
                    'spread': spread, 'gate': gate}
                self._event(c, p, 'gate_excluded', now,
                            {'date': p['signal']['date'],
                             'snapshot_date': snap['date'],
                             'contract': chosen['symbol'],
                             'spread': spread, 'gate': gate})
                self._save(c, p, now, status='gate_excluded')
                continue
            p['entry'] = {
                'snapshot_date': snap['date'], 'snapshot_asof': snap.get('asof'),
                'snapshot_source': snap.get('source'),
                'underlying_close': snap.get('underlying_close'),
                'contract_symbol': chosen['symbol'],
                'expiry': chosen['expiry'], 'strike': chosen['strike'],
                'delta': chosen.get('delta'), 'iv': number(chosen.get('iv')),
                'bid': bid, 'ask': ask, 'quote_ts': q.get('ts'),
                'qty': QTY, 'commission': FEE, 'priced_at': now}
            p['premium_status'] = 'open'
            p['stage'] = 'open'
            self._event(c, p, 'entry_priced', now,
                        {'date': p['signal']['date'],
                         'snapshot_date': snap['date'],
                         'contract': chosen['symbol'], 'ask': ask})
            self._save(c, p, now)

    def _resolve_underlying_only(self, c, p, sessions, now):
        """gate_excluded / entry-data_gap episodes: stamp the §5
        diagnostic once the exit morning has decided, then done."""
        exit_date = p['signal']['exit_date']
        decided, payload, _reason = self._morning_decision(
            c, p['symbol'], exit_date, sessions)
        if not decided:
            return
        self._stamp_underlying_morning(p, payload, exit_date)
        p['stage'] = 'done'
        self._event(c, p, 'underlying_stamped', now,
                    {'date': exit_date,
                     'status': p['status'],
                     'has_crosscheck': bool(p.get('underlying_morning'))})
        self._save(c, p, now)

    def _resolve_exits(self, c, sessions, now):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status.in_(('open', 'data_gap', 'gate_excluded')))
            .order_by(episodes.c.created)).scalars().all()
        for p in rows:
            if p['premium_status'] in ('gate_excluded', 'data_gap') \
                    and p.get('stage') == 'awaiting_underlying':
                self._resolve_underlying_only(c, p, sessions, now)
                continue
            if p['premium_status'] != 'open' or not p.get('entry'):
                continue
            exit_date = p['signal']['exit_date']
            decided, payload, reason = self._morning_decision(
                c, p['symbol'], exit_date, sessions)
            if not decided:
                continue  # morning snapshot has not decided yet
            # Earnings exclusion — evaluated at exit-pricing time from
            # the union of stored calendars (frozen §6).
            events, coverage = self._earnings_events(
                c, p['signal']['date'], day(now))
            ev = earnings_verdict(events, p['symbol'], p['signal']['date'],
                                  exit_date)
            if not coverage:
                p['earnings_unverified'] = True
            if ev is not None:
                self._stamp_underlying_morning(p, payload, exit_date)
                p['status'] = 'earnings_excluded'
                p['premium_status'] = 'earnings_excluded'
                p['stage'] = 'done'
                p['earnings_exclusion'] = {
                    'event_date': ev.get('date'), 'event_time': ev.get('time'),
                    'evaluated_through': day(now)}
                self._event(c, p, 'earnings_excluded', now,
                            {'date': exit_date, 'event_date': ev.get('date'),
                             'event_time': ev.get('time')})
                self._save(c, p, now, status='earnings_excluded')
                continue
            if payload is None:
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'done'
                p['exit_data_gap'] = {'exit_date': exit_date,
                                      'reason': reason}
                self._event(c, p, 'exit_data_gap', now,
                            {'date': exit_date, 'reason': reason})
                self._save(c, p, now, status='data_gap')
                continue
            found = exit_quote_iv(payload, p['entry']['contract_symbol'])
            if found is None:
                self._stamp_underlying_morning(p, payload, exit_date)
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'done'
                p['exit_data_gap'] = {
                    'snapshot_date': payload.get('date'),
                    'reason': 'held_contract_absent_or_unquoted'}
                self._event(c, p, 'exit_data_gap', now,
                            {'date': exit_date,
                             'snapshot_date': payload.get('date')})
                self._save(c, p, now, status='data_gap')
                continue
            bid, ask, qts, iv = found
            if not fresh_morning_ts(qts, exit_date):
                self._stamp_underlying_morning(p, payload, exit_date)
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'done'
                p['exit_data_gap'] = {
                    'snapshot_date': payload.get('date'),
                    'reason': 'stale_morning_quote', 'quote_ts': qts}
                self._event(c, p, 'exit_data_gap', now,
                            {'date': exit_date,
                             'snapshot_date': payload.get('date'),
                             'reason': 'stale_morning_quote'})
                self._save(c, p, now, status='data_gap')
                continue
            self._stamp_underlying_morning(p, payload, exit_date)
            entry_ask = p['entry']['ask']
            pnl = (bid - entry_ask) * 100 * QTY - 2 * FEE
            entry_mid = (p['entry']['bid'] + entry_ask) / 2.0
            pnl_mid = ((bid + ask) / 2.0 - entry_mid) * 100 * QTY - 2 * FEE
            p['exit'] = {
                'snapshot_date': payload['date'],
                'snapshot_asof': payload.get('asof'),
                'snapshot_source': payload.get('source'),
                'contract_symbol': p['entry']['contract_symbol'],
                'bid': bid, 'ask': ask, 'quote_ts': qts, 'iv': iv,
                'qty': QTY, 'commission': FEE, 'priced_at': now,
                'reason': 'morning_exit'}
            p['pnl'] = pnl
            p['pnl_mid'] = pnl_mid
            p['friction_gap'] = pnl_mid - pnl
            p['return_pct'] = pnl / (entry_ask * 100 * QTY) if entry_ask else None
            p['outcome'] = 'win' if pnl > 0 else 'loss' if pnl < 0 else 'breakeven'
            p['premium_status'] = 'closed'
            p['stage'] = 'done'
            p['status'] = 'closed'
            self._event(c, p, 'exit_priced', now,
                        {'date': exit_date, 'snapshot_date': payload['date'],
                         'reason': 'morning_exit', 'pnl': pnl})
            self._save(c, p, now, status='closed')

    def _fill_secondary(self, c, now):
        """The §4 secondary readout: the held contract's bid in the
        exit session's EOD snapshot. Filled on a later pass once that
        snapshot exists; never gates anything."""
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'closed')).scalars().all()
        for p in rows:
            if p.get('secondary_eod') is not None or not p.get('entry'):
                continue
            exit_date = p['signal']['exit_date']
            snap = self._snapshot_on(c, 'chain_eod', p['symbol'], exit_date)
            if snap is None:
                continue  # exit session's EOD snapshot not in yet
            found = exit_quote_iv(snap, p['entry']['contract_symbol'])
            if found is None:
                p['secondary_eod'] = {'status': 'absent'}
            else:
                bid = found[0]
                p['secondary_eod'] = {
                    'status': 'priced', 'bid': bid,
                    'pnl': (bid - p['entry']['ask']) * 100 * QTY - 2 * FEE}
            self._save(c, p, now)

    # -- driver ---------------------------------------------------------------

    def process(self, now):
        sessions = master_sessions(now)
        if not sessions:
            return
        today = day(now)
        completed = [d for d in sessions if d < today]
        if not completed:
            return
        with self.db.tx() as c:
            state = self.db.get(c, STATE_KEY, {})
            sample_start = self._determine_sample_start(c, sessions, today)
            if not state.get('bootstrap'):
                self._bootstrap(c, now, sample_start)
        if sample_start is None:
            with self.db.tx() as c:
                state = self._write_state(c, now)
            self.db.health('overnight_omc', 'waiting',
                           'OMC forward scorer (paper/measurement): '
                           'waiting for the first chain_open morning '
                           'snapshot to stamp the sample start',
                           sample_start=None, **state['counts'])
            return
        with self.db.tx() as c:
            state = self.db.get(c, STATE_KEY, {})
        watermark = state.get('last_session_processed')
        todo = [d for d in completed
                if d >= sample_start
                and (watermark is None or d > watermark)]
        for d in todo:
            try:
                with self.db.tx() as c:
                    self._process_session(c, d, sessions, now)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'overnight_omc session %s failed; will retry next pass', d)
                break
        for step in (self._price_entries,):
            try:
                with self.db.tx() as c:
                    step(c, now)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'overnight_omc pricing step %s failed', step.__name__)
        try:
            with self.db.tx() as c:
                self._resolve_exits(c, sessions, now)
        except Exception:
            logging.getLogger('uvicorn.error').exception(
                'overnight_omc exit resolution failed')
        try:
            with self.db.tx() as c:
                self._fill_secondary(c, now)
        except Exception:
            logging.getLogger('uvicorn.error').exception(
                'overnight_omc secondary fill failed')
        with self.db.tx() as c:
            state = self._write_state(c, now)
        counts = state['counts']
        self.db.health('overnight_omc', 'running',
                       'Overnight Morning-Change forward scorer '
                       '(paper/measurement): '
                       f"created {counts['created']}, opened {counts['opened']}, "
                       f"closed {counts['closed']}, open {counts['open']}, "
                       f"data_gap {counts['data_gap']}, "
                       f"gate_excluded {counts['gate_excluded']}, "
                       f"earnings_excluded {counts['earnings_excluded']}",
                       last_session=state.get('last_session_processed'),
                       sample_start=sample_start,
                       **counts)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'overnight-omc', self.owner, 300):
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
                    'overnight_omc worker failed; retrying')
            await asyncio.sleep(60)
