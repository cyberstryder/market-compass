"""Connors RSI(2) at SSC — forward premium scorer (2026-11-03).

Paper / measurement scope ONLY: no orders, no live sends. The one
outward surface is the display-only shadow alerts added by the
2026-11-13 addendum to the translation registration (Josh): episode
OPEN / CLOSE alerts to the #compass Discord channel (research
route), following the SH-SPY §6 pattern (own outbox, own flag,
default ON; see the push section below). They posted to the 0DTE
channel until 2026-11-25, when Josh moved every shadow book to
#compass so #0dte carries the 0DTE scanner funnel alone. This module shadow-fires the Connors RSI(2) candidate
exactly as frozen in the premium-translation registration
(files/connors-rsi2-translation-registration-2026-11-03.md) and scores
its premium translation forward from the EOD chain archive:

- Signal (verbatim C3): Wilder RSI(2) on closes, SMA(200) on closes;
  close_t > SMA(200)_t and RSI(2)_t <= 5 at the close; long only.
  Evaluated only where SMA(200), RSI(2) and ATR(14) are all defined.
  One counted position per symbol: a signal at session D occupies the
  symbol through D+2 (its own traded-session sequence); signals while
  occupied are skipped and counted. Occupancy attaches at signal
  acceptance, independent of the premium outcome.
- Sample rule (frozen): only signals dated on/after 2026-11-03 (ET)
  are sample. Earlier signals are never backfilled.
- Premium leg: one outright call per episode, DTE [5, 14] from the
  pricing snapshot's date, |delta| in [0.30, 0.70] targeting 0.50
  (preference: |delta - 0.50|, then expiry closest to 7 DTE, then
  strike closest to spot, then symbol). Entry at the ask of the first
  archive snapshot on/after the signal date; exit at the bid of the
  first archive snapshot on/after the exit session (the second
  session in the symbol's own sequence); $0.65/contract/leg/side.
  Calendar exit only — the 2.5xATR stop is the underlying
  cross-check's R unit, never an exit. Max loss = full debit + fees,
  stamped per episode as planned loss.
- G2 scoreability gate (frozen into the family): the chosen
  contract's spread must be <= max(15% of ask, $0.05) at entry
  pricing, else the episode is gate_excluded — a first-class counted
  outcome, never scored as a trade — with its underlying SSC outcome
  still tracked from daily bars so the gate itself is graded.
- data_gap is a first-class counted outcome (no eligible executable
  contract in the entry snapshot, or the held contract absent /
  unquoted in the exit snapshot). Pricing waits while the archive
  has not reached the target date; the first snapshot on/after the
  date decides (the swings_c2 handling). An exit bid of exactly 0.00
  is an executable exit (verified worthless).

Wiring standards (scorecard §4), as implemented here (mirroring
swings_c2):
1. Subscription pinning: episodes live in the connors_ssc_episodes
   table (declared in .store) and are registered in
   active_observations.inventory(), so a chosen contract is pinned
   in the OPRA selection for its whole open life.
2. Chain-snapshot quote standard: entries and exits price only from
   chain_eod snapshots; the <=5s stream freshness test is never
   consulted by this cell.
3. Exit reasons on a cron-readable surface: every episode payload
   carries its exit reason (ssc_close), and each lifecycle
   transition is appended as a connors_ssc event.
4. Created / opened / closed counted separately (plus open,
   data_gap, gate_excluded) in the connors_ssc:state summary,
   recomputed from the table on every pass, beside the reporting
   limbs (per-session census, pooled planned loss, ask-line
   expectancy split, underlying cross-check, gate-excluded
   underlying diagnostic).

The scorer renders no verdict: the registration's frozen terms
(two consecutive countable 4-week windows, >= 25 scored trades per
countable window, pooled n >= 50, pooled premium PF > 1.0, underlying
cross-check positive) are read from this accrued record.
"""
import asyncio
import logging
import time
import uuid
from datetime import date as _date, timedelta

import httpx
from sqlalchemy import select, update

from .market import day, number
from .store import identity
from .store import connors_push_outbox
from .store import connors_ssc_episodes as episodes
from .swing_signals import sessions_between
from .swings_c2 import daily_series, master_sessions, snapshot_on_or_after, wilder_atr

VERSION = 'connors-ssc-v1'
KIND = 'connors_ssc'
STATE_KEY = 'connors_ssc:state'
FEE = 0.65            # per contract, per leg, per side (frozen convention)
QTY = 1               # one contract per episode (shadow-book convention)
FREEZE_DATE = '2026-11-03'   # sample starts here (ET); never backfilled
MIN_DTE, MAX_DTE, TARGET_DTE = 5, 14, 7
DELTA_LO, DELTA_HI, DELTA_TARGET = 0.30, 0.70, 0.50
GATE_PCT, GATE_FLOOR = 0.15, 0.05   # G2: spread <= max(15% of ask, $0.05)
SMA_PERIOD = 200
RSI_MAX = 5.0
RISK_MULT = 2.5       # stop = 2.5 x ATR(14): the cross-check's R unit
SERIES_SESSIONS = 340  # trailing window for series work (>> SMA + seeds)
CENSUS_BACK_DAYS = 1100

# The episodes table is declared in .store (connors_ssc_episodes) so
# Store init's create_all creates it before this module is lazily
# imported (the bb1adfa lesson).


# ---------------------------------------------------------------------------
# Pure helpers (frozen math; unit-tested without a database)

def wilder_rsi2(closes):
    """Wilder RSI(2) aligned to the close sequence, exactly as the
    gauntlet computed it: seeded from the first two close changes
    (mean gain / mean loss over diffs 1-2); thereafter Wilder
    smoothing with alpha = 1/2. RSI = 100 when average loss is 0.
    Defined from index 2 onward (None before)."""
    n = len(closes)
    out = [None] * n
    if n < 3:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    ag = (gains[0] + gains[1]) / 2.0
    al = (losses[0] + losses[1]) / 2.0
    for i in range(2, n):
        if i > 2:
            ag = (ag + gains[i - 1]) / 2.0
            al = (al + losses[i - 1]) / 2.0
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def sma_series(closes, period=SMA_PERIOD):
    """Simple trailing mean including t; None before `period` closes."""
    out = [None] * len(closes)
    run = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= period:
            run -= closes[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def indicators(bars):
    """(atr, rsi, sma) arrays aligned to the bar sequence. ATR is
    swings_c2.wilder_atr — the same pinned Wilder arithmetic the
    gauntlet executed (TR from the second bar, seed mean(TR[1..14]))."""
    closes = [b['c'] for b in bars]
    return wilder_atr(bars), wilder_rsi2(closes), sma_series(closes)


def counted_trades(bars):
    """The gauntlet's counted family over one symbol's daily bars
    (ascending, placeholder sessions already removed): returns
    (trades, events) where events maps bar index -> 'accepted' |
    'skipped' for every raw signal, and trades are the accepted
    (counted) ones with their signal values. A counted trade entered
    at index t occupies the symbol through t+2."""
    atr, rsi, sma = indicators(bars)
    trades, events = [], {}
    open_until = -1
    for i, b in enumerate(bars):
        if sma[i] is None or rsi[i] is None or atr[i] is None:
            continue
        if not (b['c'] > sma[i] and rsi[i] <= RSI_MAX):
            continue
        if i <= open_until:
            events[i] = 'skipped'
            continue
        events[i] = 'accepted'
        trades.append({'index': i, 'date': b['date'], 'close': b['c'],
                       'rsi2': rsi[i], 'sma200': sma[i], 'atr14': atr[i],
                       'risk': RISK_MULT * atr[i]})
        open_until = i + 2
    return trades, events


def select_contract(snapshot, min_dte=MIN_DTE, max_dte=MAX_DTE,
                    target_dte=TARGET_DTE):
    """The registration's contract rule applied to one archive
    snapshot: outright call, DTE in [min_dte, max_dte] from the
    snapshot date, |delta| in [0.30, 0.70] (delta must be present),
    executable quote (bid > 0, ask >= bid). Deterministic preference:
    |delta| closest to 0.50, then expiry closest to the target DTE,
    then strike closest to spot, then symbol. Returns the archive
    contract row or None."""
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
        if not (DELTA_LO <= abs(delta) <= DELTA_HI):
            continue
        q = o.get('quote') or {}
        bid, ask = number(q.get('bid')), number(q.get('ask'))
        if bid is None or ask is None or bid <= 0 or ask < bid or ask <= 0:
            continue
        key = (abs(abs(delta) - DELTA_TARGET), abs(dte - target_dte),
               abs(strike - spot), o.get('symbol') or '')
        if best is None or key < best[0]:
            best = (key, o)
    return best[1] if best else None


def spread_gate(bid, ask):
    """G2 on the chosen pick: returns (spread, gate, passes). The 1e-9
    tolerance keeps IEEE dust (1.00 - 0.85 = 0.15000000000000002) from
    failing a quote that sits exactly on the frozen gate."""
    spread = ask - bid
    gate = max(GATE_PCT * ask, GATE_FLOOR)
    return spread, gate, spread <= gate + 1e-9


def exit_quote_iv(snapshot, contract_symbol):
    """The held contract's quote + IV in an exit snapshot. A quoted
    bid of exactly 0.00 is an executable exit (verified worthless);
    an absent contract or an absent quote is not a price at all.
    Returns (bid, ask, quote_ts, iv) or None."""
    for o in snapshot.get('contracts', []):
        if o.get('symbol') == contract_symbol:
            q = o.get('quote') or {}
            bid, ask = number(q.get('bid')), number(q.get('ask'))
            if bid is None or ask is None or ask < bid:
                return None
            return bid, ask, q.get('ts'), number(o.get('iv'))
    return None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Display-only shadow alerts (2026-11-13 addendum to the translation
# registration, Josh). Own outbox + own drainer, following the SH-SPY
# §6 precedent in sh_spy_options.py exactly: destination = the
# research route's configured webhook, resolved through
# alert_routes.destination. These alerts never enter the general
# alert pipeline. Scoring is byte-identical with the alert flag on
# or off (pinned by test); gate_excluded and data_gap episodes never
# alert (no premium trade exists).

PUSH_KIND = 'connors_push'
MAX_PUSH_ATTEMPTS = 10
REASON_LABEL = {'ssc_close': 'SSC CLOSE (D+2)'}


def _contract_label(payload):
    contract = payload.get('contract') or {}
    typ = 'C' if str(contract.get('type') or '').lower() == 'call' else 'P'
    strike = number(contract.get('strike'))
    strike_s = ('%g' % strike) if strike is not None else '?'
    return '%s %s %s%s' % (payload.get('symbol') or '?',
                           contract.get('expiry') or '?', strike_s, typ)


def _money(v):
    if v is None:
        return '—'
    return '+$%.2f' % v if v >= 0 else '-$%.2f' % abs(v)


def _fmt_num(v, digits=2):
    try:
        return '%.*f' % (digits, float(v))
    except (TypeError, ValueError):
        return '—'


def format_open_message(payload):
    """CONNORS SHADOW open alert (addendum labeling): identity line
    first, symbol, contract, entry ask, delta and DTE as the episode
    recorded them, shadow disclaimer. No live-trading language."""
    lines = [
        '🧪 **CONNORS SHADOW** — shadow forward test · display only',
        '📈 **OPEN LONG** · %s · signal %s' % (
            payload.get('symbol', '?'), payload.get('signal_date', '?')),
        'Contract `%s` · entry ask `$%.2f`' % (
            _contract_label(payload), payload.get('entry_ask', 0)),
        'Delta `%s` · DTE `%s` (frozen translation terms)' % (
            _fmt_num(payload.get('delta')), payload.get('dte', '—')),
        '_Shadow measurement only — not a bookable trade; nothing was booked or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Connors Shadow',
            'allowed_mentions': {'parse': []}}


def format_close_message(payload):
    """CONNORS SHADOW close alert: identity line, symbol, contract,
    exit bid + scored P&L + exit reason, underlying ruler, shadow
    disclaimer."""
    reason = REASON_LABEL.get(payload.get('reason'),
                              str(payload.get('reason', '?')).upper())
    bps = payload.get('underlying_bps')
    bps_s = ('%+.1f bps' % bps) if isinstance(bps, (int, float)) else '—'
    lines = [
        '🧪 **CONNORS SHADOW** — shadow forward test · display only',
        '📈 **CLOSE LONG** · %s — %s' % (payload.get('symbol', '?'), reason),
        'Contract `%s` · entry ask `$%.2f` → exit bid `$%.2f`' % (
            _contract_label(payload), payload.get('entry_ask', 0),
            payload.get('exit_bid', 0)),
        'Scored P&L **%s** · underlying ruler **%s**' % (
            _money(payload.get('pnl')), bps_s),
        '_Shadow measurement only — not a bookable trade; nothing was booked or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'Connors Shadow',
            'allowed_mentions': {'parse': []}}


def queue_alert(db, cfg, payload):
    """Queue one shadow alert in the module's own outbox. Called only
    AFTER the episode transaction has committed, in its own
    transaction. Returns True when queued (or when alerts are off —
    nothing to do). On failure the error lands on the
    health:connors_push marker and False is returned; the caller has
    already booked the episode, which never depends on this."""
    if not bool(getattr(cfg, 'connors_alerts', True)):
        return True
    try:
        row_id = identity('connors-push-v1', payload.get('episode_id', ''),
                          payload.get('type', ''), payload.get('at', 0))
        with db.tx() as c:
            c.execute(db.insert(connors_push_outbox).values(
                id=row_id, status='pending', created=time.time(),
                payload=payload, delivery={'attempts': 0})
                .on_conflict_do_nothing(index_elements=['id']))
        return True
    except Exception as e:
        try:
            db.health(PUSH_KIND, 'error',
                      'shadow alert queue failed: %s' % type(e).__name__)
        except Exception:
            pass
        logging.getLogger('uvicorn.error').exception(
            'connors_ssc: alert queue failed; episode already booked')
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
    """Attempt one pending shadow alert. Mirrors the fixed ict_push
    drainer semantics (as in sh_spy_options): any local failure
    (including a formatter error) fails the row with the cause
    recorded in its delivery — never strands a row in 'sending',
    never raises into the loop. Returns True if a row was attempted."""
    import re
    if not url or not re.fullmatch(
            r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+', url):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        row = c.execute(select(connors_push_outbox)
                        .where(connors_push_outbox.c.status == 'pending')
                        .order_by(connors_push_outbox.c.created).limit(1)
                        ).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        if d.get('next_attempt') and now < d['next_attempt']:
            return False
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(update(connors_push_outbox)
                  .where(connors_push_outbox.c.id == row['id'])
                  .values(status='sending', delivery=d))
        job = dict(row, delivery=d)

    status = 'failed'
    try:
        formatter = (format_close_message if job['payload'].get('type') == 'close'
                     else format_open_message)
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
        c.execute(update(connors_push_outbox)
                  .where(connors_push_outbox.c.id == job['id'])
                  .values(status=status, delivery=job['delivery']))
    if status == 'delivered':
        db.health(PUSH_KIND, 'delivered',
                  'Connors shadow alert confirmed by Discord', now)
    elif status == 'failed':
        db.health(PUSH_KIND, 'error',
                  'shadow alert delivery failed: %s'
                  % job['delivery'].get('error', 'attempts exhausted'))
    return True


async def run_push(db, cfg):
    """Drainer loop for the addendum shadow alerts (own outbox,
    research route destination). Never raises out of the loop."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if bool(getattr(cfg, 'connors_ssc', True)) and \
                        bool(getattr(cfg, 'connors_alerts', True)):
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


# Scorer

class ConnorsSSC:
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
        """Episodes whose underlying leg has not yet reached D+2."""
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status.in_(('open', 'data_gap', 'gate_excluded')))
            .order_by(episodes.c.created)).scalars().all()
        return [p for p in rows if not p.get('underlying_exit')]

    def _counts(self, c):
        rows = c.execute(select(episodes.c.status, episodes.c.payload)).all()
        counts = {'created': 0, 'opened': 0, 'closed': 0, 'open': 0,
                  'data_gap': 0, 'gate_excluded': 0}
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
        cross, gate_diag = [], []
        planned_open = 0.0
        for p in rows:
            if p['status'] == 'open' and p.get('planned_loss'):
                planned_open += p['planned_loss']
            if p['status'] == 'closed' and p.get('pnl') is not None:
                ask = p['entry']['ask']
                bucket = ('le_1.00' if ask <= 1.00 else
                          '1.00_5.00' if ask <= 5.00 else 'gt_5.00')
                ask_lines[bucket]['n'] += 1
                ask_lines[bucket]['net'] += p['pnl']
                if p.get('underlying_exit'):
                    cross.append(p['underlying_exit']['bps'])
            if p['status'] == 'gate_excluded' and p.get('underlying_exit'):
                gate_diag.append(p['underlying_exit']['bps'])

        def agg(vals):
            return {'n': len(vals),
                    'mean_bps': (sum(vals) / len(vals)) if vals else None}
        state = {'version': VERSION, 'at': now, 'counts': self._counts(c),
                 'planned_loss_open': round(planned_open, 2),
                 'ask_lines': ask_lines,
                 'underlying_crosscheck': agg(cross),
                 'gate_excluded_underlying': agg(gate_diag),
                 **(extra or {})}
        old = self.db.get(c, STATE_KEY, {})
        merged = {**old, **state}
        self.db.put(c, STATE_KEY, merged)
        return merged

    # -- underlying mechanics ---------------------------------------------------

    def _stamp_underlying_exit(self, c, p, exit_bar, now):
        """D+2 reached in the symbol's own sequence: record the
        underlying SSC outcome (bps + R) for every episode, whatever
        its premium state — the cross-check and the gate diagnostic
        both read it."""
        entry = p['signal']['close']
        price = exit_bar['c']
        p['underlying_exit'] = {
            'date': exit_bar['date'], 'price': price, 'reason': 'ssc_close',
            'bps': (price - entry) / entry * 1e4,
            'r': (price - entry) / p['signal']['risk']}
        if p['premium_status'] == 'data_gap':
            p['stage'] = 'done'
            self._event(c, p, 'underlying_closed_data_gap', now,
                        {'date': exit_bar['date'], 'reason': 'ssc_close'})
        elif p['premium_status'] == 'gate_excluded':
            p['stage'] = 'done'
            self._event(c, p, 'underlying_closed_gate_excluded', now,
                        {'date': exit_bar['date'], 'reason': 'ssc_close'})
        else:
            p['exit_trigger'] = {'reason': 'ssc_close',
                                 'underlying_date': exit_bar['date'],
                                 'underlying_price': price}
            if p['premium_status'] == 'open':
                p['stage'] = 'awaiting_exit'
            self._event(c, p, 'exit_triggered', now,
                        {'date': exit_bar['date'], 'reason': 'ssc_close'})
        self._save(c, p, now)

    def _create_episode(self, c, sym, trade, now):
        p = {
            'id': identity('connors-ssc-episode-v1', sym, trade['date']),
            'version': VERSION, 'strategy': 'connors-rsi2-ssc',
            'symbol': sym, 'underlying': sym, 'side': 'long',
            'status': 'open', 'stage': 'awaiting_entry',
            'premium_status': 'awaiting_entry',
            'created_at': now,
            'signal': {'date': trade['date'], 'close': trade['close'],
                       'rsi2': trade['rsi2'], 'sma200': trade['sma200'],
                       'atr14': trade['atr14'], 'risk': trade['risk'],
                       'seq_index': trade['index']},
            'contract': None, 'entry': None, 'exit_trigger': None,
            'exit': None, 'underlying_exit': None,
            'planned_loss': None, 'pnl': None, 'pnl_mid': None,
            'friction_gap': None, 'return_pct': None, 'outcome': None,
        }
        stmt = self.db.insert(episodes).values(
            id=p['id'], underlying=sym, status='open', created=now,
            updated=now, payload=p)
        c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        self._event(c, p, 'episode_created', now,
                    {'date': trade['date'], 'rsi2': trade['rsi2'],
                     'close': trade['close']})
        return p

    def _process_session(self, c, d, sessions, now):
        """One completed session: underlying exits due in each
        symbol's own sequence, then signal evaluation at the close.
        Returns the session census."""
        ci = sessions.index(d)
        window = sessions[max(0, ci - (SERIES_SESSIONS - 1)):ci + 1]
        active = {}
        for p in self._active(c):
            active.setdefault(p['symbol'], []).append(p)
        census = {'date': d, 'signals': 0, 'created': 0, 'skipped': 0,
                  'created_symbols': []}
        for sym in self.cfg.watch_symbols:
            bars = daily_series(self.db, c, sym, window)
            if not bars or bars[-1]['date'] != d:
                continue  # symbol did not trade this master session
            dates = [b['date'] for b in bars]
            idx = len(bars) - 1
            # Underlying exits: an episode exits at the bar two
            # positions after its entry bar in this same sequence.
            for p in active.get(sym, []):
                if p['signal']['date'] not in dates:
                    continue
                entry_pos = dates.index(p['signal']['date'])
                if idx - entry_pos >= 2:
                    self._stamp_underlying_exit(c, p, bars[entry_pos + 2], now)
            # Signal evaluation at this close. counted_trades replays
            # the window; only the outcome AT this index is consumed
            # (occupancy at the tail is window-independent: it depends
            # only on the prior two sessions, all inside the window).
            trades, events = counted_trades(bars)
            outcome = events.get(idx)
            if outcome == 'skipped':
                census['signals'] += 1
                census['skipped'] += 1
            elif outcome == 'accepted':
                census['signals'] += 1
                trade = next(t for t in trades if t['index'] == idx)
                self._create_episode(c, sym, trade, now)
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

    def _depth_census(self, c, now):
        """Bootstrap depth census: stored daily-bar depth per watch
        symbol over the long window; >= SMA_PERIOD bars = eligible."""
        today = day(now)
        start = (_date.fromisoformat(today)
                 - timedelta(days=CENSUS_BACK_DAYS)).isoformat()
        long_sessions = list(sessions_between(start, today))
        eligible, ineligible = [], {}
        for sym in self.cfg.watch_symbols:
            bars = daily_series(self.db, c, sym, long_sessions)
            if len(bars) >= SMA_PERIOD:
                eligible.append(sym)
            else:
                ineligible[sym] = len(bars)
        census = {'as_of': today, 'eligible': eligible,
                  'eligible_count': len(eligible),
                  'ineligible': ineligible,
                  'ineligible_count': len(ineligible)}
        self.db.append(c, KIND, VERSION, 'CENSUS', now,
                       {'event': 'bootstrap', 'date': today, **census},
                       key=identity(KIND, 'bootstrap', VERSION))
        st = self.db.get(c, STATE_KEY, {})
        st['depth_census'] = census
        self.db.put(c, STATE_KEY, st)
        return census

    # -- premium pricing (chain archive only) ---------------------------------

    def _price_entries(self, c, now, alerts_out=None):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)).scalars().all()
        for p in rows:
            if p.get('entry') is not None or p['premium_status'] != 'awaiting_entry':
                continue
            snap = snapshot_on_or_after(self.db, c, p['symbol'],
                                        p['signal']['date'])
            if snap is None:
                continue  # archive has not reached the signal date yet
            chosen = select_contract(snap)
            if chosen is None:
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'done' if p.get('underlying_exit') else 'underlying_only'
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
                p['stage'] = 'done' if p.get('underlying_exit') else 'underlying_only'
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
            p['stage'] = 'awaiting_exit' if p.get('underlying_exit') else 'open'
            self._event(c, p, 'entry_priced', now,
                        {'date': p['signal']['date'],
                         'snapshot_date': snap['date'],
                         'contract': chosen['symbol'], 'ask': ask})
            self._save(c, p, now)
            if alerts_out is not None:
                # Display-only shadow alert payload (2026-11-13
                # addendum): collected here, queued by process()
                # only after this transaction commits.
                alerts_out.append({
                    'type': 'open', 'episode_id': p['id'],
                    'symbol': p['symbol'], 'contract': p['contract'],
                    'entry_ask': ask, 'delta': p['entry']['delta'],
                    'dte': (_date.fromisoformat(p['entry']['expiry'])
                            - _date.fromisoformat(
                                p['entry']['snapshot_date'])).days,
                    'signal_date': p['signal']['date'], 'at': now})

    def _price_exits(self, c, now, alerts_out=None):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)).scalars().all()
        for p in rows:
            if p['stage'] != 'awaiting_exit' or not p.get('entry'):
                continue
            exit_date = p['underlying_exit']['date']
            snap = snapshot_on_or_after(self.db, c, p['symbol'], exit_date)
            if snap is None:
                continue  # archive has not reached the exit date yet
            found = exit_quote_iv(snap, p['entry']['contract_symbol'])
            if found is None:
                p['status'] = 'data_gap'
                p['premium_status'] = 'data_gap'
                p['stage'] = 'done'
                p['exit_data_gap'] = {
                    'snapshot_date': snap.get('date'),
                    'reason': 'held_contract_absent_or_unquoted'}
                self._event(c, p, 'exit_data_gap', now,
                            {'date': exit_date, 'snapshot_date': snap.get('date'),
                             'reason': 'ssc_close'})
                self._save(c, p, now, status='data_gap')
                continue
            bid, ask, qts, iv = found
            entry_ask = p['entry']['ask']
            pnl = (bid - entry_ask) * 100 * QTY - 2 * FEE
            entry_mid = (p['entry']['bid'] + entry_ask) / 2.0
            pnl_mid = ((bid + ask) / 2.0 - entry_mid) * 100 * QTY - 2 * FEE
            p['exit'] = {
                'snapshot_date': snap['date'], 'snapshot_asof': snap.get('asof'),
                'snapshot_source': snap.get('source'),
                'contract_symbol': p['entry']['contract_symbol'],
                'bid': bid, 'ask': ask, 'quote_ts': qts, 'iv': iv,
                'qty': QTY, 'commission': FEE, 'priced_at': now,
                'reason': 'ssc_close'}
            p['pnl'] = pnl
            p['pnl_mid'] = pnl_mid
            p['friction_gap'] = pnl_mid - pnl
            p['return_pct'] = pnl / (entry_ask * 100 * QTY) if entry_ask else None
            p['outcome'] = 'win' if pnl > 0 else 'loss' if pnl < 0 else 'breakeven'
            p['premium_status'] = 'closed'
            p['stage'] = 'done'
            p['status'] = 'closed'
            self._event(c, p, 'exit_priced', now,
                        {'date': exit_date, 'snapshot_date': snap['date'],
                         'reason': 'ssc_close', 'pnl': pnl})
            self._save(c, p, now, status='closed')
            if alerts_out is not None:
                alerts_out.append({
                    'type': 'close', 'episode_id': p['id'],
                    'symbol': p['symbol'], 'contract': p['contract'],
                    'entry_ask': entry_ask, 'exit_bid': bid, 'pnl': pnl,
                    'reason': 'ssc_close',
                    'underlying_bps': (p.get('underlying_exit') or {})
                    .get('bps'),
                    'at': now})

    # -- driver ---------------------------------------------------------------

    def process(self, now):
        """One full pass. Sessions and pricing steps run in their own
        transactions so one symbol's failure cannot roll back the book."""
        sessions = master_sessions(now)
        if not sessions:
            return
        completed = [d for d in sessions if d < day(now)]
        if not completed:
            return
        with self.db.tx() as c:
            state = self.db.get(c, STATE_KEY, {})
        watermark = state.get('last_session_processed')
        bootstrap = watermark is None and not state.get('depth_census')
        if bootstrap:
            with self.db.tx() as c:
                self._depth_census(c, now)
            todo = [d for d in completed if d >= FREEZE_DATE]
        else:
            todo = [d for d in completed
                    if d >= FREEZE_DATE and (watermark is None or d > watermark)]
        for d in todo:
            try:
                with self.db.tx() as c:
                    self._process_session(c, d, sessions, now)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'connors_ssc session %s failed; will retry next pass', d)
                break
        alerts_out = []
        for step in (self._price_entries, self._price_exits):
            step_alerts = []
            try:
                with self.db.tx() as c:
                    step(c, now, step_alerts)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'connors_ssc pricing step %s failed', step.__name__)
            else:
                # Only alerts whose episode transaction committed are
                # queued (queueing itself happens after, in queue_alert).
                alerts_out.extend(step_alerts)
        for payload in alerts_out:
            queue_alert(self.db, self.cfg, payload)
        with self.db.tx() as c:
            state = self._write_state(c, now)
        counts = state['counts']
        census = state.get('depth_census') or {}
        self.db.health('connors_ssc', 'running',
                       'Connors RSI(2) SSC forward premium scorer '
                       '(paper/measurement): '
                       f"created {counts['created']}, opened {counts['opened']}, "
                       f"closed {counts['closed']}, open {counts['open']}, "
                       f"data_gap {counts['data_gap']}, "
                       f"gate_excluded {counts['gate_excluded']}",
                       last_session=state.get('last_session_processed'),
                       eligible=census.get('eligible_count'),
                       **counts)

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'connors-ssc', self.owner, 300):
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
                    'connors_ssc worker failed; retrying')
            await asyncio.sleep(60)
