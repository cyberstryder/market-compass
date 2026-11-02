"""C2 weekly cross-sectional momentum — forward premium scorer (2026-11-02).

Paper / measurement scope ONLY: no orders, no alerts, no live sends. This
module shadow-fires the C2 candidate exactly as frozen in the 2026-11-01
swings generator gauntlet (files/swings-generator-gauntlet-2026-11-01.md)
and scores its premium translation forward, per that report's §5/§6:

- Signal: at each week's last XNYS session, rank eligible watchlist names
  on close[t-5]/close[t-65] - 1 (12 weeks, skipping the most recent week);
  long the top decile (k = max(1, round(10% of eligible)), ties by symbol).
  Eligible = closes present at t, t-5 and t-65 on the master (XNYS)
  calendar and >= 70 sessions of history up to t.
- Episode: a symbol's continuous basket membership. Entry at the
  rebalance close; catastrophe stop = entry - 3x Wilder ATR(14), fixed at
  entry, evaluated on the symbol's own daily bars (open <= stop exits at
  the open; low <= stop exits at the stop). Membership exit at the first
  rebalance close where the symbol has left the basket (walk back to the
  last available close when the symbol has no bar that session, reason
  rebal_out_stale). Stops are evaluated before membership at a rebalance,
  as in the gauntlet.
- Premium leg: one outright call per episode (C2 is long-only), selected
  from the EOD chain archive (chain_eod) under the retired swing book's
  standard: 14-60 DTE, |delta| 0.30-0.70, strike within +/-3% of the
  snapshot's underlying close. Entry priced at the ask of the first
  archive snapshot on/after the signal date; exit priced at the bid of
  the first archive snapshot on/after the underlying exit date;
  $0.65/contract/leg/side commissions. No stream quote is ever consulted:
  the chain snapshot IS the entry/exit quote standard (scorecard §4.2).
- data_gap is a first-class counted outcome: an episode whose entry
  snapshot holds no eligible executable contract, or whose held contract
  is absent/unquoted in the exit snapshot, closes its premium leg as
  data_gap (neither win nor loss). Its underlying episode still runs to
  its frozen exit so the underlying record is complete for every idea.

Wiring standards (scorecard §4 / gauntlet §6), as implemented here:
1. Subscription pinning: episodes live in the swing_c2_episodes table and
   are registered in active_observations.inventory(), so a chosen
   contract is pinned in the OPRA selection for its whole open life,
   exactly like open positions. (Selection itself reads the archive, so
   the retired book's failure mode — a pending candidate's contract
   falling out of the capped live selection before it could open —
   cannot occur; pinning covers selection -> scored exit.)
2. Chain-snapshot quote standard: see above; the <=5s stream freshness
   test is never used by this book.
3. Exit reasons on a cron-readable surface: every episode payload carries
   its frozen exit reason, and each lifecycle transition is appended as a
   swing_c2 event (readable through the cron-token events export).
4. Created / opened / closed counted separately (plus open and
   data_gap) in the swings_c2:state summary, recomputed from the table
   on every pass — "tracked" can never again mean "queued".

First readable premium verdict (frozen): >= 50 scored premium trades
spanning at least two consecutive 4-week windows. This module accrues
the trades; it renders no verdict and presents nothing as actionable.

Forward-only: on an empty state the scorer bootstraps the basket as of
the most recent completed rebalance from stored closes (no backfill of
earlier episodes), then stop-checks the sessions after it.
"""
import asyncio
import logging
import time
import uuid
from datetime import date as _date, timedelta

from sqlalchemy import Column, Float, Index, JSON, String, Table, select, update

from .market import day, number
from .store import events, identity, meta
from .swing_signals import sessions_between

VERSION = 'swings-c2-v1'
KIND = 'swing_c2'
STATE_KEY = 'swings_c2:state'
FEE = 0.65          # per contract, per leg, per side (frozen convention)
QTY = 1             # one contract per episode (shadow-book convention)
RANK_BACK = 65      # sessions: 12 weeks + skip week, per the gauntlet
RANK_SKIP = 5
MIN_HISTORY = 70    # sessions of closes required for eligibility
SERIES_SESSIONS = 200

episodes = Table('swing_c2_episodes', meta,
    Column('id', String(64), primary_key=True),
    Column('underlying', String(100), nullable=False),
    Column('status', String(24), nullable=False),
    Column('created', Float, nullable=False),
    Column('updated', Float, nullable=False),
    Column('payload', JSON, nullable=False))
Index('swing_c2_active', episodes.c.status, episodes.c.created)


# ---------------------------------------------------------------------------
# Pure helpers (frozen math; unit-tested without a database)

def wilder_atr(bars):
    """Wilder ATR(14) aligned to the symbol's own bar sequence, exactly as
    the gauntlet computed it: TR from the second bar, seed at index 14
    with mean(TR[1..14]), then atr = (atr*13 + TR)/14. None before 14."""
    n = len(bars)
    out = [None] * n
    if n < 15:
        return out
    tr = [None] * n
    for i in range(1, n):
        h, l, pc = bars[i]['h'], bars[i]['l'], bars[i - 1]['c']
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = sum(tr[1:15]) / 14.0
    out[14] = atr
    for i in range(15, n):
        atr = (atr * 13 + tr[i]) / 14.0
        out[i] = atr
    return out


def rank_universe(series, sessions, ci):
    """Eligible {symbol: rank_value} at master-session index ci.

    series: {symbol: [(session_date, close), ...]} (placeholder
    non-sessions already removed). Mirrors the gauntlet's
    eligible_and_rank: closes required at ci, ci-5 and ci-65, and at
    least MIN_HISTORY sessions present up to ci."""
    if ci - RANK_BACK < 0:
        return {}
    target, skip, back = sessions[ci], sessions[ci - RANK_SKIP], sessions[ci - RANK_BACK]
    out = {}
    for sym, rows in series.items():
        closes = dict(rows)
        if target not in closes or skip not in closes or back not in closes:
            continue
        if sum(1 for d, _ in rows if d <= target) < MIN_HISTORY:
            continue
        out[sym] = closes[skip] / closes[back] - 1.0
    return out


def basket_for(eligible):
    """Top decile as [(symbol, rank_value)] in rank order: rank desc,
    ties by symbol asc; k = max(1, round(10% of eligible)) — Python's
    round, as in the frozen study."""
    if not eligible:
        return []
    k = max(1, round(0.10 * len(eligible)))
    ranked = sorted(eligible.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:k]


def select_contract(snapshot, min_dte=14, max_dte=60, target_dte=30):
    """The retired swing book's contract standard applied to one archive
    snapshot: outright call, DTE in [min_dte, max_dte] from the snapshot
    date, |delta| in [0.30, 0.70] (delta must be present — a contract
    whose delta cannot be verified is not eligible), strike within
    +/-3% of the snapshot's underlying close, and an executable quote
    (bid > 0, ask >= bid). Deterministic preference: closest to the
    target DTE, then |delta| closest to 0.50, then strike closest to
    spot, then symbol. Returns the archive contract row or None."""
    spot = number(snapshot.get('underlying_close'))
    snap_date = snapshot.get('date')
    if not spot or spot <= 0 or not snap_date:
        return None
    start = _date.fromisoformat(snap_date)
    best = None
    for o in snapshot.get('contracts', []):
        if o.get('type') != 'call':
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
        if not (min_dte <= dte <= max_dte):
            continue
        if abs(strike / spot - 1.0) > 0.03:
            continue
        if not (0.30 <= abs(delta) <= 0.70):
            continue
        q = o.get('quote') or {}
        bid, ask = number(q.get('bid')), number(q.get('ask'))
        if bid is None or ask is None or bid <= 0 or ask < bid or ask <= 0:
            continue
        key = (abs(dte - target_dte), abs(abs(delta) - 0.50),
               abs(strike - spot), o.get('symbol') or '')
        if best is None or key < best[0]:
            best = (key, o)
    return best[1] if best else None


def exit_quote(snapshot, contract_symbol):
    """The held contract's quote in an exit snapshot. A quoted bid of
    exactly 0.00 is an executable exit (verified worthless); an absent
    contract or an absent quote is not a price at all. Returns
    (bid, ask, quote_ts) or None."""
    for o in snapshot.get('contracts', []):
        if o.get('symbol') == contract_symbol:
            q = o.get('quote') or {}
            bid, ask = number(q.get('bid')), number(q.get('ask'))
            if bid is None or ask is None or ask < bid:
                return None
            return bid, ask, q.get('ts')
    return None


# ---------------------------------------------------------------------------
# Stored-data readers

def master_sessions(now, back_days=500):
    """XNYS sessions up to and including today (ET)."""
    today = day(now)
    start = (_date.fromisoformat(today) - timedelta(days=back_days)).isoformat()
    return list(sessions_between(start, today))


def rebalance_dates(sessions):
    """Last session of each ISO week in the master list."""
    weeks = {}
    for i, d in enumerate(sessions):
        iso = _date.fromisoformat(d).isocalendar()
        weeks[(iso[0], iso[1])] = i
    return {sessions[i] for i in weeks.values()}


def daily_series(db, c, symbol, sessions):
    """The symbol's own daily bars labeled by master session, ascending,
    over the given session list (latest revision per session). Placeholder
    non-sessions (volume 0 with o == h == l == c — the gauntlet panel's
    halt rule) are dropped: no trade could ever have occurred on one."""
    if not sessions:
        return []
    from datetime import datetime
    from .market import NY
    first = datetime.fromisoformat(sessions[0]).replace(tzinfo=NY).timestamp()
    last = datetime.fromisoformat(sessions[-1]).replace(tzinfo=NY).timestamp() + 86400
    rows = c.execute(select(events).where(
        events.c.kind == 'daily', events.c.symbol == symbol,
        events.c.ts >= first, events.c.ts < last)
        .order_by(events.c.id.asc())).mappings()
    by_day = {}
    # Label each row by the session whose NY-midnight window contains it
    # (the daily_history.completed_rows convention).
    starts = [(datetime.fromisoformat(d).replace(tzinfo=NY).timestamp(), d) for d in sessions]
    for r in rows:
        d = None
        for start_ts, label_d in starts:
            if start_ts <= r['ts'] < start_ts + 86400:
                d = label_d
                break
        if d is None:
            continue
        by_day[d] = r['payload']  # ascending id: latest revision wins
    out = []
    for d in sessions:
        p = by_day.get(d)
        if not p:
            continue
        o, h, l, cl, v = (number(p.get(k)) for k in ('o', 'h', 'l', 'c', 'v'))
        if None in (o, h, l, cl, v) or cl <= 0:
            continue
        if v == 0 and o == h == l == cl:
            continue  # placeholder non-session (halt): not a session
        out.append({'date': d, 'o': o, 'h': h, 'l': l, 'c': cl, 'v': v})
    return out


def snapshot_on_or_after(db, c, symbol, date_str):
    """First chain_eod archive payload for the symbol dated on/after
    date_str, or None when the archive has not reached that date."""
    after = 0
    while True:
        rows = db.export_events(c, 'chain_eod', symbol=symbol, after_id=after, limit=200)
        if not rows:
            return None
        for r in rows:
            payload = r['payload']
            if (payload.get('date') or '') >= date_str:
                return payload
            after = r['id']
        if len(rows) < 200:
            return None


# ---------------------------------------------------------------------------
# Scorer

class SwingsC2:
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

    def _active(self, c):
        """Episodes whose underlying leg is still running."""
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status.in_(('open', 'data_gap'))).order_by(episodes.c.created)).scalars().all()
        return [p for p in rows if not p.get('underlying_exit')]

    def _counts(self, c):
        rows = c.execute(select(episodes.c.status, episodes.c.payload)).all()
        counts = {'created': 0, 'opened': 0, 'closed': 0, 'open': 0, 'data_gap': 0}
        for status, p in rows:
            counts['created'] += 1
            if p.get('entry'):
                counts['opened'] += 1
            if status == 'closed':
                counts['closed'] += 1
            elif status == 'open':
                counts['open'] += 1
            elif status == 'data_gap':
                counts['data_gap'] += 1
        return counts

    def _write_state(self, c, now, extra=None):
        state = {'version': VERSION, 'at': now, 'counts': self._counts(c), **(extra or {})}
        old = self.db.get(c, STATE_KEY, {})
        merged = {**old, **state}
        self.db.put(c, STATE_KEY, merged)
        return merged

    # -- underlying mechanics (frozen gauntlet rules) -------------------------

    def _trigger_exit(self, c, p, date_str, price, reason, now):
        p['underlying_exit'] = {'date': date_str, 'price': price, 'reason': reason}
        p['exit_trigger'] = {'reason': reason, 'underlying_date': date_str,
                             'underlying_price': price}
        entry = p['signal']['underlying_entry']
        p['underlying_exit']['r'] = (price - entry) / p['signal']['stopdist']
        if p['status'] == 'data_gap':
            # Premium leg already resolved as data_gap; the underlying
            # record is now complete and the episode is final.
            p['stage'] = 'done'
            self._event(c, p, 'underlying_closed_data_gap', now,
                        {'date': date_str, 'reason': reason})
        else:
            p['stage'] = 'awaiting_exit'
            self._event(c, p, 'exit_triggered', now,
                        {'date': date_str, 'reason': reason})
        self._save(c, p, now)

    def _check_stop(self, c, p, session_date, now, bars):
        """One session's catastrophe-stop check for one episode, given the
        symbol's own daily bars."""
        if session_date <= p['signal']['rebalance_date']:
            return
        bar = next((b for b in bars if b['date'] == session_date), None)
        if bar is None:
            return
        stop = p['signal']['stop']
        if bar['o'] <= stop:
            self._trigger_exit(c, p, session_date, bar['o'], 'stop', now)
        elif bar['l'] <= stop:
            self._trigger_exit(c, p, session_date, stop, 'stop', now)

    def _rebalance(self, c, reb_date, sessions, now):
        """Basket membership as of reb_date (already stop-checked)."""
        ci = sessions.index(reb_date)
        window = sessions[max(0, ci - (SERIES_SESSIONS - 1)):ci + 1]
        closes = {}
        bars_by_symbol = {}
        for sym in self.cfg.watch_symbols:
            bars = daily_series(self.db, c, sym, window)
            bars_by_symbol[sym] = bars
            closes[sym] = [(b['date'], b['c']) for b in bars]
        # Rank on master-calendar positions: indices are into the full
        # session list; per-symbol closes are keyed by session date.
        eligible = rank_universe(closes, sessions, ci)
        basket = basket_for(eligible)
        picked = {sym for sym, _ in basket}
        # Membership exits (stops for this date were checked by the caller).
        for p in self._active(c):
            if p['symbol'] in picked:
                continue
            bars = bars_by_symbol.get(p['symbol']) or daily_series(self.db, c, p['symbol'], window)
            past = [b for b in bars if b['date'] <= reb_date]
            if not past:
                continue
            last = past[-1]
            reason = 'rebal_out' if last['date'] == reb_date else 'rebal_out_stale'
            self._trigger_exit(c, p, last['date'], last['c'], reason, now)
        # New entries.
        active_symbols = {p['symbol'] for p in self._active(c)}
        created = []
        for rank_pos, (sym, value) in enumerate(basket, start=1):
            if sym in active_symbols:
                continue
            bars = bars_by_symbol[sym]
            entry_bar = next((b for b in bars if b['date'] == reb_date), None)
            if entry_bar is None:
                continue
            atr = wilder_atr(bars)
            idx = bars.index(entry_bar)
            atr_here = atr[idx] if idx < len(atr) else None
            if atr_here is None or entry_bar['c'] <= 0:
                continue  # the gauntlet skips picks it cannot price a stop for
            stopdist = 3.0 * atr_here
            p = {
                'id': identity('swing-c2-episode-v1', sym, reb_date),
                'version': VERSION, 'strategy': 'c2-weekly-momentum-12-1',
                'symbol': sym, 'underlying': sym, 'side': 'long',
                'status': 'open', 'stage': 'awaiting_entry',
                'premium_status': 'awaiting_entry',
                'created_at': now,
                'signal': {'rebalance_date': reb_date, 'rank_value': value,
                           'rank': rank_pos, 'eligible': len(eligible),
                           'basket_size': len(basket),
                           'underlying_entry': entry_bar['c'],
                           'atr14': atr_here, 'stopdist': stopdist,
                           'stop': entry_bar['c'] - stopdist},
                'contract': None, 'entry': None, 'exit_trigger': None,
                'exit': None, 'underlying_exit': None,
                'pnl': None, 'return_pct': None, 'outcome': None,
            }
            stmt = self.db.insert(episodes).values(
                id=p['id'], underlying=sym, status='open', created=now,
                updated=now, payload=p)
            c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
            created.append(sym)
            self._event(c, p, 'episode_created', now, {'date': reb_date})
        state = self.db.get(c, STATE_KEY, {})
        state['last_rebalance_date'] = reb_date
        state['last_basket'] = [{'symbol': s, 'rank_value': v} for s, v in basket]
        self.db.put(c, STATE_KEY, state)
        self.db.append(c, KIND, VERSION, 'BASKET', now,
                       {'event': 'rebalance', 'date': reb_date,
                        'eligible': len(eligible), 'basket': [s for s, _ in basket],
                        'created': created},
                       key=identity(KIND, 'rebalance', reb_date))
        return basket

    # -- premium pricing (chain archive only) ---------------------------------

    def _price_entries(self, c, now):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)).scalars().all()
        for p in rows:
            if p.get('entry') is not None or p['premium_status'] != 'awaiting_entry':
                continue
            snap = snapshot_on_or_after(self.db, c, p['symbol'],
                                        p['signal']['rebalance_date'])
            if snap is None:
                continue  # archive has not reached the signal date yet
            chosen = select_contract(snap, self.cfg.swing_min_dte,
                                     self.cfg.swing_max_dte,
                                     self.cfg.swing_target_dte)
            if chosen is None:
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                # The underlying leg still runs to its frozen exit (or has
                # already reached it) so its record completes either way.
                p['stage'] = 'done' if p.get('underlying_exit') else 'open'
                p['entry_data_gap'] = {'snapshot_date': snap.get('date'),
                                       'reason': 'no_eligible_executable_contract'}
                self._event(c, p, 'entry_data_gap', now,
                            {'date': p['signal']['rebalance_date'],
                             'snapshot_date': snap.get('date')})
                self._save(c, p, now, status='data_gap')
                continue
            q = chosen['quote']
            p['contract'] = {'symbol': chosen['symbol'], 'expiry': chosen['expiry'],
                             'strike': chosen['strike'], 'type': chosen['type']}
            p['entry'] = {
                'snapshot_date': snap['date'], 'snapshot_asof': snap.get('asof'),
                'snapshot_source': snap.get('source'),
                'underlying_close': snap.get('underlying_close'),
                'contract_symbol': chosen['symbol'], 'expiry': chosen['expiry'],
                'strike': chosen['strike'], 'delta': chosen.get('delta'),
                'bid': q.get('bid'), 'ask': q.get('ask'), 'quote_ts': q.get('ts'),
                'qty': QTY, 'commission': FEE, 'priced_at': now}
            p['premium_status'] = 'open'
            # An episode stopped out before its entry was priced goes
            # straight to exit pricing; it never sits in plain 'open'.
            p['stage'] = 'awaiting_exit' if p.get('underlying_exit') else 'open'
            self._event(c, p, 'entry_priced', now,
                        {'date': p['signal']['rebalance_date'],
                         'snapshot_date': snap['date'],
                         'contract': chosen['symbol'], 'ask': q.get('ask')})
            self._save(c, p, now)

    def _price_exits(self, c, now):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)).scalars().all()
        for p in rows:
            if p['stage'] != 'awaiting_exit' or not p.get('entry'):
                continue
            exit_date = p['underlying_exit']['date']
            snap = snapshot_on_or_after(self.db, c, p['symbol'], exit_date)
            if snap is None:
                continue  # archive has not reached the exit date yet
            found = exit_quote(snap, p['entry']['contract_symbol'])
            if found is None:
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'done'
                p['exit_data_gap'] = {'snapshot_date': snap.get('date'),
                                      'reason': 'held_contract_absent_or_unquoted'}
                self._event(c, p, 'exit_data_gap', now,
                            {'date': exit_date, 'snapshot_date': snap.get('date'),
                             'reason': p['exit_trigger']['reason']})
                self._save(c, p, now, status='data_gap')
                continue
            bid, ask, qts = found
            entry_ask = p['entry']['ask']
            pnl = (bid - entry_ask) * 100 * QTY - 2 * FEE
            p['exit'] = {
                'snapshot_date': snap['date'], 'snapshot_asof': snap.get('asof'),
                'snapshot_source': snap.get('source'),
                'contract_symbol': p['entry']['contract_symbol'],
                'bid': bid, 'ask': ask, 'quote_ts': qts,
                'qty': QTY, 'commission': FEE, 'priced_at': now,
                'reason': p['exit_trigger']['reason']}
            p['pnl'] = pnl
            p['return_pct'] = pnl / (entry_ask * 100 * QTY) if entry_ask else None
            p['outcome'] = 'win' if pnl > 0 else 'loss' if pnl < 0 else 'breakeven'
            p['premium_status'] = 'closed'
            p['stage'] = 'done'
            p['status'] = 'closed'
            self._event(c, p, 'exit_priced', now,
                        {'date': exit_date, 'snapshot_date': snap['date'],
                         'reason': p['exit_trigger']['reason'], 'pnl': pnl})
            self._save(c, p, now, status='closed')

    # -- driver ---------------------------------------------------------------

    def process(self, now):
        """One full pass. Each session / pricing step runs in its own
        transaction so one symbol's failure cannot roll back the book."""
        sessions = master_sessions(now)
        if not sessions:
            return
        completed = [d for d in sessions if d < day(now)]
        if not completed:
            return
        rebs = rebalance_dates(sessions)
        with self.db.tx() as c:
            state = self.db.get(c, STATE_KEY, {})
        watermark = state.get('last_session_processed')
        bootstrap = watermark is None
        if bootstrap:
            past_rebs = [d for d in completed if d in rebs]
            if not past_rebs:
                return
            first = past_rebs[-1]
            todo = [first] + [d for d in completed if d > first]
        else:
            todo = [d for d in completed if d > watermark]
        # Stop checks read series built lazily per symbol, outside the
        # per-session transactions (no nested transactions). Laziness
        # matters: on the bootstrap pass the episodes are created during
        # the loop, and their series must exist before their first stop
        # check — a series snapshot taken at pass start would miss them
        # and the advancing watermark would never revisit those sessions.
        window = sessions[-SERIES_SESSIONS:]
        series_by_symbol = {}

        def bars_for(sym):
            if sym not in series_by_symbol:
                with self.db.tx() as c:
                    series_by_symbol[sym] = daily_series(self.db, c, sym, window)
            return series_by_symbol[sym]
        for d in todo:
            with self.db.tx() as c:
                warm = {p['symbol'] for p in self._active(c)}
            for sym in warm:
                bars_for(sym)
            try:
                with self.db.tx() as c:
                    for p in self._active(c):
                        self._check_stop(c, p, d, now, bars_for(p['symbol']))
                    if d in rebs:
                        self._rebalance(c, d, sessions, now)
                    st = self.db.get(c, STATE_KEY, {})
                    st['last_session_processed'] = d
                    self.db.put(c, STATE_KEY, st)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'swings_c2 session %s failed; will retry next pass', d)
                break
        for step in (self._price_entries, self._price_exits):
            try:
                with self.db.tx() as c:
                    step(c, now)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'swings_c2 pricing step %s failed', step.__name__)
        with self.db.tx() as c:
            state = self._write_state(c, now)
        counts = state['counts']
        self.db.health('swings_c2', 'running',
                       'C2 forward premium scorer (paper/measurement): '
                       f"created {counts['created']}, opened {counts['opened']}, "
                       f"closed {counts['closed']}, open {counts['open']}, "
                       f"data_gap {counts['data_gap']}",
                       last_rebalance=state.get('last_rebalance_date'),
                       last_session=state.get('last_session_processed'),
                       **counts)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'swings-c2', self.owner, 300):
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
                    'swings_c2 worker failed; retrying')
            await asyncio.sleep(60)
