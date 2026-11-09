"""Claude's 4 gauntlet survivors as Compass detectors (2026-10-28).

Josh's swap decision (2026-10-28 22:55 CT): the four configurations that
passed the frozen house gauntlet on Claude's foundation sweep
(files/claude-survivors-gauntlet-2026-10-28.md) trade LIVE at 1 contract
each through TradersPost, replacing the TradingView Elite alerts Josh
retired himself. This amends the promotion policy's accumulate-first
step at Josh's explicit direction; every other detector's live/paper
state is untouched.

Definitions are the gauntlet's, verbatim — the streaming signal engine
below reproduced all four gate-arm trade lists EXACTLY over the
foundation year-1 bars before this module shipped (fidelity gate,
2026-10-28). Session = CME trade date (CT ts + 7h); ATR = mean true
range over the 14 sessions before the current one (daily aggregates,
needs 15 prior sessions); RTH VWAP from 08:30 CT; Asia low = running
min of lows with tod >= 17:00 or < 02:00 CT, frozen after 02:00;
c60 = close 60 bars back positionally (crosses sessions).

- S1 MNQ: RTH-VWAP z crosses up through +2.0, 09:00-15:00 CT -> SHORT.
  No stop/target; time exit 60 bars after the entry bar (flatten cap).
- S2 MCL: Asia-low pierce + close back above -> LONG, 02:00-15:00 CT.
  Stop 0.25*ATR, target 0.5*ATR (ATR in ticks at the signal bar).
- S3 MGC: 60-bar return z crosses down through -0.15, 02:00-08:00 CT
  -> LONG. No stop/target; time exit 30 bars after entry (flatten cap).
- S4 MGC: 60-bar return z crosses up through +0.15, 09:00-15:00 CT
  -> SHORT. Stop 0.25*ATR, target 0.25*ATR.

Live-vs-gauntlet construction differences (deliberate, itemized):
- Entries book at the live quote like every Compass futures trade (the
  gauntlet's gate arm repriced the detector entry by +1 tick adverse);
  TradersPost brackets are RELATIVE to the live fill, anchored by the
  same stop/target distances, tick-rounded (the gauntlet used float
  tick distances from the detector fill).
- Bracket distances are tick-rounded per the build spec.
- ICT gates that define OTHER detectors (regime filter, min-stop,
  min-RR, cooldown, brake shadow) do NOT apply — the frozen
  configurations have none. Applied: session/flatten gates, symbol
  mapping fail-closed, duplicate/hedging live-execution guards (shared
  with ict_paper both ways), and the pre-send freshness guard (full
  evaluation for S2/S4; a documented no-op for S1/S3, which have no
  levels to be beyond).
- News pause (added 2026-11-02 at Josh's direction — "fix and wire" —
  a knowing mid-review spec change, disclosed in the 50-fill review):
  ENTRIES are declined inside a high-impact economic-release window
  via the same should_pause_for_news gate the ICT paper book uses,
  with the same news_pause decline shape. Exits, stops, targets,
  brackets and the 15:45 flatten never consult it. The gate's parser
  was repaired the same day: the vendor's AM/PM event times had
  never parsed, so the ICT pause this mirrors could never fire.
- Safety-stop overlay (added 2026-11-04 at Josh's direction, after
  that morning's live S3 gold trade ran with no resting stop and he
  closed it manually — another knowing mid-review change, disclosed
  in the 50-fill review): horizon strategies' LIVE entries now carry
  a broker-resting DISASTER stop at SAFETY_STOP_ATR_MULT x ATR —
  stop-only, no take-profit; the timed engine exit remains the
  normal exit, and the resting stop is cancelled when the position
  closes exactly as an unfilled S2/S4 bracket leg is. The booked
  record keeps stop=None (the paper horizon construction is
  unchanged); the stop rides only on the live send copy, and
  engine.exits() honors it for live-sent positions so a broker-side
  safety fill is detected like a bracket fill. On top of that, a
  send-path invariant refuses — with a loud alert on the live ICT
  Discord channel — any live entry whose payload would carry no
  stop at all ('no_stop_attached'). S1/S3 were validated stop-less;
  this overlay is Josh's explicit safety direction, not a claim
  about their gauntlet construction.
- Signals are evaluated on every newly completed bar, but only bars
  whose close is within FRESH_SECONDS of now are traded; older catch-up
  bars after downtime advance the features and are counted as stale
  skips (fail-closed). Session history for the ATR seeds once from the
  bar-events store; after a contract roll the cross-session true range
  uses raw closes (the dataset is back-adjusted) — one TR term in a
  14-session mean, ~4 rolls/yr/root.
- Positions ride the engine's exits(): bracket trades exit on
  quote/bar stop-target or the 15:45 CT flatten; horizon trades carry
  flatten_at = min(time exit, 15:45 CT) and exit there at the last
  mark. Data-gap posture is the engine's (stale quote -> data_gap,
  counted as neither win nor loss; the flatten branch fires regardless).

Tripwire + review (Josh-approved design): state at survivors:state,
keyed to activated_at = the first production tick after deploy. On
every scan, combined realized P&L and fill counts are recomputed from
the closed TradersPost-taken survivor trades: P&L <= -$1,750 flips
the four to paper-only (live sends off, paper tracking continues);
>= 50 combined live fills (entry + exit fills of sent trades) sets
review_due. Both surface in /api/day-digest.

Per-strategy live gate (2026-11-09, Josh's decision): within a live
book, SURVIVOR_LIVE_STRATEGIES (default all four) selects which
strategies may queue TradersPost entries; a pulled strategy books
paper with a strategy-named skip reason. S2 was pulled this way
while S1/S3/S4 run to the 50-fill review. The gate is entry-only by
construction — engine.exits() sends live closes off each position's
own traderspost_sent flag — so an open live-sent position of a
pulled strategy still exits normally.
"""
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql_text

from .ict_common import ict_bars
from .instruments import future_spec
from .simulation import FILL_DESCRIPTION, FILL_VERSION

log = logging.getLogger('uvicorn.error')
CT = ZoneInfo("America/Chicago")

VERSION = 'survivors-v2'
STATE_KEY = 'survivors:state'
SCAN_KEY = 'survivors:scanned_at'
TRIPWIRE_LOSS = 1750.0
REVIEW_FILLS = 50
FRESH_SECONDS = 300
SCAN_INTERVAL = 20

STRATEGY_OF = {'S1': 'survivor_s1', 'S2': 'survivor_s2',
               'S3': 'survivor_s3', 'S4': 'survivor_s4'}
STRATEGY_TAG = {
    'S1': 'survivor_s1_rvwap_fade',
    'S2': 'survivor_s2_asia_low_fade',
    'S3': 'survivor_s3_london_mr60',
    'S4': 'survivor_s4_ny_mr60_fade',
}
STRATEGY_SYMBOL = {'S1': 'MNQ.c.0', 'S2': 'MCL.v.0',
                   'S3': 'MGC.v.0', 'S4': 'MGC.v.0'}
SURVIVOR_DETECTORS = frozenset(STRATEGY_OF.values())
SCAN_SYMBOLS = ('MNQ.c.0', 'MCL.v.0', 'MGC.v.0')
ROOT_STRATEGIES = {'MNQ': ('S1',), 'MCL': ('S2',), 'MGC': ('S3', 'S4')}
# Exit horizons (minutes) for the bracket-less strategies. S3 was
# 30 as registered; amended to 15 on 2026-11-05 at Josh's decision
# on the frozen horizon sweep's evidence (15m beat 30m under frozen
# limbs on the gauntlet panel; fork amendment — S3 fill evidence
# splits at the switch). See files/s3-horizon-switch-2026-11-05.md.
HORIZON = {'S1': 60, 'S3': 15}
DIRECTION = {'S1': -1, 'S2': 1, 'S3': 1, 'S4': -1}
# Safety-stop distance for the horizon strategies, as a multiple of
# the signal bar's ATR in ticks (the units S2/S4 express stop_ticks
# in). Derived from the frozen gauntlet trade sets' max-adverse-
# excursion distribution (guard-kept entries, foundation year-1;
# hidden_files/survivor-safety-stop-2026-11-04/mae_analysis.py):
# at 0.5 x ATR the stop would have bound on 1/65 S1 trades (1.5%)
# and 6/334 S3 trades (1.8%) — inside the ~1-2% disaster band. The
# next step down (0.25) binds ~9-10% (a strategy stop, not a
# disaster stop); 0.75 binds 0% / 0.6%. One constant per strategy:
# tighten here and every future S1/S3 live entry follows.
SAFETY_STOP_ATR_MULT = {'S1': 0.5, 'S3': 0.5}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ct_parts(ts):
    dt = datetime.fromtimestamp(ts, CT)
    tod = dt.hour * 60 + dt.minute
    # Trade date = CT calendar date of the instant 7h after the bar
    # (absolute arithmetic, DST-safe; matches the gauntlet's pandas).
    sdate = datetime.fromtimestamp(ts + 7 * 3600, CT).date().isoformat()
    return tod, sdate


# ---------------------------------------------------------------------
# Streaming signal engine (fidelity-verified against the gauntlet).
# ---------------------------------------------------------------------
class SurvivorFeed:
    """Feed 1-min bars ({ts,o,h,l,c,v}) in ascending ts order; feed()
    returns the signal dicts fired by that bar."""

    def __init__(self, root, tick):
        self.root = root
        self.tick = float(tick)
        self.strategies = ROOT_STRATEGIES.get(root, ())
        self.sdate = None
        self.s_hi = None
        self.s_lo = None
        self.s_bars = 0
        self.asia_lo = None
        self.rth_pv = 0.0
        self.rth_vol = 0.0
        self.history = []        # [{sdate, hi, lo, close, bars}]
        self.atr = None          # price units, current session
        self.closes = deque(maxlen=61)
        self.prev_close = None
        self.prev_asia_lo = None
        self.prev_z_rv = None
        self.prev_z_mr = None
        self.last_ts = None
        self.bar_index = -1
        self.seeded = False
        self.seed_attempt_at = 0

    def _roll_session(self, sdate):
        if self.sdate is not None:
            self.history.append({
                'sdate': self.sdate, 'hi': self.s_hi, 'lo': self.s_lo,
                'close': self.prev_close, 'bars': self.s_bars})
            self.history = self.history[-40:]
        self.sdate = sdate
        self.s_hi = None
        self.s_lo = None
        self.s_bars = 0
        self.asia_lo = None
        self.rth_pv = 0.0
        self.rth_vol = 0.0
        self._recompute_atr()

    def _recompute_atr(self):
        self.atr = None
        hist = [h for h in self.history if h['sdate'] != self.sdate]
        if len(hist) >= 15:
            trs = []
            for m in range(len(hist) - 14, len(hist)):
                hh = hist[m]
                pc = hist[m - 1]['close']
                trs.append(max(hh['hi'] - hh['lo'],
                               abs(hh['hi'] - pc), abs(hh['lo'] - pc)))
            self.atr = sum(trs) / len(trs)

    def feed(self, bar):
        ts = bar['ts']
        tod, sdate = _ct_parts(ts)
        if sdate != self.sdate:
            self._roll_session(sdate)
        h, l, c = bar['h'], bar['l'], bar['c']
        v = bar.get('v')
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = 0
        self.bar_index += 1
        idx = self.bar_index
        self.s_hi = h if self.s_hi is None else max(self.s_hi, h)
        self.s_lo = l if self.s_lo is None else min(self.s_lo, l)
        self.s_bars += 1
        if tod >= 1020 or tod < 120:
            self.asia_lo = l if self.asia_lo is None else min(self.asia_lo, l)
        if 510 <= tod < 960:
            self.rth_pv += c * v
            self.rth_vol += v
        self.closes.append(c)
        atr_t = (self.atr / self.tick) if self.atr else None
        z_rv = None
        if atr_t is not None and self.rth_vol > 0:
            z_rv = ((c - self.rth_pv / self.rth_vol) / self.tick) \
                / max(0.2 * atr_t, 1.0)
        z_mr = None
        if atr_t is not None and len(self.closes) == 61:
            z_mr = ((c - self.closes[0]) / self.tick) / max(atr_t, 1.0)
        signals = []
        if atr_t is not None:
            for strat in self.strategies:
                sig = None
                if strat == 'S1':
                    if (540 <= tod < 900 and z_rv is not None
                            and self.prev_z_rv is not None
                            and z_rv >= 2.0 and self.prev_z_rv < 2.0):
                        sig = {'strategy': 'S1', 'dir': -1, 'hold': 60}
                elif strat == 'S2':
                    if (120 <= tod < 900 and self.asia_lo is not None
                            and l <= self.asia_lo and c > self.asia_lo
                            and self.prev_close is not None
                            and self.prev_asia_lo is not None
                            and self.prev_close > self.prev_asia_lo):
                        sig = {'strategy': 'S2', 'dir': 1,
                               'stop_ticks': 0.25 * atr_t,
                               'target_ticks': 0.5 * atr_t}
                elif strat == 'S3':
                    if (120 <= tod < 480 and z_mr is not None
                            and self.prev_z_mr is not None
                            and z_mr <= -0.15 and self.prev_z_mr > -0.15):
                        sig = {'strategy': 'S3', 'dir': 1, 'hold': 15}
                elif strat == 'S4':
                    if (540 <= tod < 900 and z_mr is not None
                            and self.prev_z_mr is not None
                            and z_mr >= 0.15 and self.prev_z_mr < 0.15):
                        sig = {'strategy': 'S4', 'dir': -1,
                               'stop_ticks': 0.25 * atr_t,
                               'target_ticks': 0.25 * atr_t}
                if sig:
                    sig.update({'bar_index': idx, 'ts': ts, 'sdate': sdate,
                                'tod': tod, 'close': c, 'atr_t': atr_t})
                    signals.append(sig)
        self.prev_close = c
        self.prev_asia_lo = self.asia_lo
        self.prev_z_rv = z_rv
        self.prev_z_mr = z_mr
        self.last_ts = ts
        return signals

    def seed_history(self, sessions):
        """Install completed-session aggregates (cold start). Only
        sessions strictly before the in-progress one are used."""
        merged = {s['sdate']: s for s in self.history}
        for s in sessions:
            if self.sdate is None or s['sdate'] < self.sdate:
                merged[s['sdate']] = s
        self.history = [merged[k] for k in sorted(merged)][-40:]
        self._recompute_atr()

    def to_state(self):
        return {
            'version': VERSION, 'root': self.root, 'tick': self.tick,
            'sdate': self.sdate, 's_hi': self.s_hi, 's_lo': self.s_lo,
            's_bars': self.s_bars, 'asia_lo': self.asia_lo,
            'rth_pv': self.rth_pv, 'rth_vol': self.rth_vol,
            'history': self.history, 'atr': self.atr,
            'closes': list(self.closes), 'prev_close': self.prev_close,
            'prev_asia_lo': self.prev_asia_lo, 'prev_z_rv': self.prev_z_rv,
            'prev_z_mr': self.prev_z_mr, 'last_ts': self.last_ts,
            'bar_index': self.bar_index, 'seeded': self.seeded,
            'seed_attempt_at': self.seed_attempt_at,
        }

    @classmethod
    def from_state(cls, state):
        f = cls(state['root'], state['tick'])
        f.sdate = state['sdate']
        f.s_hi = state['s_hi']
        f.s_lo = state['s_lo']
        f.s_bars = state['s_bars']
        f.asia_lo = state['asia_lo']
        f.rth_pv = state['rth_pv']
        f.rth_vol = state['rth_vol']
        f.history = state['history']
        f.atr = state['atr']
        f.closes = deque(state['closes'], maxlen=61)
        f.prev_close = state['prev_close']
        f.prev_asia_lo = state['prev_asia_lo']
        f.prev_z_rv = state['prev_z_rv']
        f.prev_z_mr = state['prev_z_mr']
        f.last_ts = state['last_ts']
        f.bar_index = state['bar_index']
        if state.get('version') == VERSION:
            f.seeded = bool(state.get('seeded'))
            f.seed_attempt_at = state.get('seed_attempt_at') or 0
        # Rows written by an older version latched 'seeded' under a seed
        # query that silently installed zero sessions (collation bug);
        # force a re-seed under the fixed query. All other fields load
        # normally, and seed_history() merges safely on re-run.
        return f


# ---------------------------------------------------------------------
# Tripwire / review state.
# ---------------------------------------------------------------------
def get_state(db, c):
    st = db.get(c, STATE_KEY)
    return st if isinstance(st, dict) else None


def ensure_state(db, c, now):
    st = get_state(db, c)
    if st is None:
        st = {'version': VERSION, 'activated_at': now,
              'activated_iso': _iso(now), 'live': True,
              'tripped_at': None, 'trip_reason': None,
              'combined_live_pnl': 0.0, 'live_fills': 0,
              'live_trades_closed': 0, 'review_due': False,
              'review_due_at': None, 'updated_at': now}
        db.put(c, STATE_KEY, st)
        try:
            db.append(c, 'survivor_event', 'survivors', '', now,
                      {'event': 'activated', 'at': now})
        except Exception:
            pass
    return st


ALL_STRATEGIES = frozenset(STRATEGY_OF)


def live_strategies(cfg):
    """Strategy ids whose live entry sends are enabled by config
    (SURVIVOR_LIVE_STRATEGIES; Josh's per-strategy pull, 2026-11-09).
    A cfg without the attribute yields all four — the pre-gate
    behavior, in the same getattr-default style as the
    traderspost_enabled reads at the send sites. Consulted ONLY at
    entry: exits never read this set (engine.exits() sends live
    closes off the position's own traderspost_sent flag), so pulling
    a strategy can never strand an open live position."""
    s = getattr(cfg, 'survivor_live_strategies', None)
    if s is None:
        return ALL_STRATEGIES
    return s


def account_live(db, c, st, now):
    """Recompute combined live P&L / fills from BROKER-CONFIRMED live
    survivor trades only; apply the tripwire and the 50-fill review
    flag. Pure function of the trade records + fill verdicts, so it is
    idempotent across restarts. Returns the (possibly updated) state.

    Fill confirmation (2026-11-02, compass/fill_confirmation.py): a
    TradersPost webhook acceptance is NOT a fill. A trade counts only
    when its legs carry broker-verified verdicts — confirmed_closed
    contributes its CONFIRMED P&L (broker fill prices, gross) and 2
    fills; confirmed_open / pending_exit / exit_unconfirmed contribute
    the 1 confirmed entry fill and no P&L. Sent-but-unconfirmed and
    rejected/no-order trades count as NEITHER and are tallied in
    live_pending / live_not_filled for audit visibility. Before this
    fix the counters read the modeled book: on 2026-11-02 that printed
    a phantom S3 rejection (+$42) as live P&L and S4 at modeled prices
    (+$181 vs the broker's +$222)."""
    from . import fill_confirmation as fc
    pnl = 0.0
    fills = 0
    closed_sent = 0
    pending = 0
    not_filled = 0
    try:
        trades = db.prefix(c, 'trade:ict-survivor_')
    except Exception:
        trades = {}
    for t in trades.values():
        if not isinstance(t, dict) or not t.get('id'):
            continue
        verdicts = fc.get_verdicts(db, c, t['id'])
        if not t.get('traderspost_sent') and not verdicts:
            continue
        proj = fc.derive_live(t, verdicts)
        status = proj['live_status']
        if status == 'confirmed_closed':
            closed_sent += 1
            pnl += proj['live_confirmed_pnl'] or 0.0
            fills += 2          # confirmed entry fill + exit fill
        elif status in ('confirmed_open', 'pending_exit',
                        'exit_unconfirmed'):
            fills += 1          # confirmed entry fill only, so far
        elif status == 'not_filled':
            not_filled += 1
        elif status == 'pending':
            pending += 1
    changed = False
    if st.get('combined_live_pnl') != round(pnl, 2) \
            or st.get('live_fills') != fills \
            or st.get('live_trades_closed') != closed_sent \
            or st.get('live_pending') != pending \
            or st.get('live_not_filled') != not_filled:
        st['combined_live_pnl'] = round(pnl, 2)
        st['live_fills'] = fills
        st['live_trades_closed'] = closed_sent
        st['live_pending'] = pending
        st['live_not_filled'] = not_filled
        changed = True
    if st.get('live') and pnl <= -TRIPWIRE_LOSS:
        st['live'] = False
        st['tripped_at'] = now
        st['trip_reason'] = (
            'combined live P&L %.2f <= -%.2f across %d closed live trades'
            % (pnl, TRIPWIRE_LOSS, closed_sent))
        changed = True
        try:
            db.append(c, 'survivor_event', 'survivors', '', now,
                      {'event': 'tripwire_tripped', 'at': now,
                       'combined_live_pnl': round(pnl, 2),
                       'live_trades_closed': closed_sent})
        except Exception:
            pass
        try:
            db.health('survivors', 'tripped', st['trip_reason'])
        except Exception:
            pass
        log.error('SURVIVORS TRIPWIRE TRIPPED: %s', st['trip_reason'])
    if not st.get('review_due') and fills >= REVIEW_FILLS:
        st['review_due'] = True
        st['review_due_at'] = now
        changed = True
        try:
            db.append(c, 'survivor_event', 'survivors', '', now,
                      {'event': 'review_due', 'at': now,
                       'live_fills': fills})
        except Exception:
            pass
        log.info('SURVIVORS REVIEW DUE: %d combined live fills', fills)
    if changed:
        st['updated_at'] = now
        db.put(c, STATE_KEY, st)
    return st


# ---------------------------------------------------------------------
# Live-execution guard (duplicate / correlated-hedging): a faithful
# port of the keyed-lookup scan in ict_paper.submit's TradersPost
# block — same symbol + ANY registered detector + any direction is a
# duplicate; a correlated symbol in the opposite direction is hedging.
# The reverse direction needs no code here: survivor detectors are
# registered in ict_paper.STRATEGY_TAGS, so ICT submits already scan
# survivor positions with this same logic.
# ---------------------------------------------------------------------
_CORRELATED = {
    'MES': 'EQUITY', 'MNQ': 'EQUITY', 'MYM': 'EQUITY', 'M2K': 'EQUITY',
    'M6A': 'CURRENCY', 'M6E': 'CURRENCY',
}


def guard_check(db, c, trade):
    """Return a skip-reason string when an open position duplicates or
    hedges `trade` under the house live-execution guards; else None."""
    from .ict_paper import STRATEGY_TAGS
    symbol = trade['symbol']
    side = trade['side']
    own_id = trade['id']
    root = symbol.split('.')[0] if symbol else ''
    group = _CORRELATED.get(root)
    symbols_to_check = [symbol]
    if group:
        for sym_root, grp in _CORRELATED.items():
            if grp == group and sym_root != root:
                for suffix in ('.c.0', '.v.0'):
                    symbols_to_check.append('%s%s' % (sym_root, suffix))
    for other_detector in STRATEGY_TAGS:
        for check_symbol in symbols_to_check:
            for suffix in ('', ':t1', ':t2', ':trail'):
                pos_key = 'position:ict:%s:%s%s' % (
                    other_detector, check_symbol, suffix)
                pos = db.get(c, pos_key, {}) or {}
                if pos.get('id') == own_id:
                    continue
                if pos.get('status') == 'open':
                    pos_side = (pos.get('side') or '').lower()
                    if check_symbol == symbol:
                        return 'skip %s: already open for %s' % (
                            symbol, other_detector)
                    if group and pos_side and pos_side != side:
                        return 'skip %s %s: hedging vs %s %s (%s)' % (
                            symbol, side, check_symbol, pos_side, group)
    return None


# ---------------------------------------------------------------------
# Submit.
# ---------------------------------------------------------------------
def _round_to_tick(price, tick):
    return round(round(price / tick) * tick, 10)


def submit_survivor(db, c, cfg, now, strat, symbol, sig):
    """Book one survivor signal as a 1-contract paper position and, when
    the tripwire state is live, queue the TradersPost entry. Mirrors
    ict_paper.submit's record shapes and send gating, minus the ICT-only
    gates (see module docstring)."""
    from . import ict_paper
    from .futures import futures_session
    detector = STRATEGY_OF[strat]
    strategy = STRATEGY_TAG[strat]
    d = sig['dir']
    side = 'long' if d > 0 else 'short'
    entry = sig['close']
    signal_ts = sig['ts']
    sess = futures_session(now, symbol)
    if not sess.get('is_open'):
        return {'submitted': False, 'reason': 'market_closed'}
    # News pause (2026-11-02, Josh: "fix and wire"): decline ENTRIES
    # inside a high-impact economic-release window, mirroring the ICT
    # paper gate — same repaired parser, same decline shape; scan()
    # records the decline under reason 'news_pause'. ENTRIES ONLY:
    # exits, stops, targets, brackets and the 15:45 flatten never
    # pass through this path. Mid-review spec change at Josh's
    # direction (Friday 2026-11-06 NFP protection), disclosed in the
    # 50-fill review. Intel errors proceed without the filter,
    # exactly as the ICT path does.
    try:
        from .tradermatrix_intel import should_pause_for_news
        pause, event = should_pause_for_news(db, c)
        if pause:
            return {'submitted': False, 'reason': 'news_pause',
                    'event': event.get('title') if event else
                    'high-impact news'}
    except Exception:
        pass
    flat = sess['flatten_at']
    if now >= flat:
        return {'submitted': False, 'reason': 'past_flatten'}
    spec = future_spec(symbol)
    if not spec:
        return {'submitted': False, 'reason': 'not_a_future'}
    pos_key_base = 'position:ict:%s:%s' % (detector, symbol)
    for suffix in ('', ':t1', ':t2', ':trail'):
        existing = db.get(c, pos_key_base + suffix)
        if existing and existing.get('status') == 'open':
            return {'submitted': False, 'reason': 'existing_position'}
    tick = spec['tick']
    bracket = strat in ('S2', 'S4')
    stop = target = None
    stop_ticks_r = target_ticks_r = None
    if bracket:
        stop_ticks_r = int(round(sig['stop_ticks']))
        target_ticks_r = int(round(sig['target_ticks']))
        if stop_ticks_r < 1 or target_ticks_r < 1:
            return {'submitted': False, 'reason': 'bracket_too_tight'}
        stop = _round_to_tick(entry - d * stop_ticks_r * tick, tick)
        target = _round_to_tick(entry + d * target_ticks_r * tick, tick)
        if (d > 0 and not (stop < entry < target)) \
                or (d < 0 and not (target < entry < stop)):
            return {'submitted': False, 'reason': 'levels_inverted'}
    # Safety stop (horizon strategies, 2026-11-04 overlay): computed
    # from the signal entry exactly like a bracket stop, but kept OFF
    # filled['stop'] — the booked record stays stop=None so the paper
    # book's horizon construction (exits on flatten_at only) is
    # untouched. It reaches the broker only inside the live send copy
    # far below; exits() honors it there via traderspost_sent. A
    # computation that cannot produce a valid level leaves it None,
    # and the send-path invariant then refuses the live entry.
    safety_stop = safety_ticks = None
    if strat in SAFETY_STOP_ATR_MULT:
        _ss_ticks = int(round(SAFETY_STOP_ATR_MULT[strat]
                              * sig['atr_t']))
        if _ss_ticks >= 1:
            _ss = _round_to_tick(entry - d * _ss_ticks * tick, tick)
            if (d > 0 and _ss < entry) or (d < 0 and _ss > entry):
                safety_stop = _ss
                safety_ticks = _ss_ticks
    # The stop price the live payload must carry, whichever family
    # produced it. None only for a horizon strategy whose safety stop
    # did not compute — the invariant's refusal case.
    send_stop = stop if bracket else safety_stop
    flatten_at = flat
    if strat in HORIZON:
        # Exit at the close of the bar HORIZON bars after the entry bar:
        # signal bar [ts, ts+60), entry bar starts ts+60, so the exit
        # bar closes at ts + (hold + 2) * 60. Capped at the flatten.
        time_exit = signal_ts + (HORIZON[strat] + 2) * 60
        if time_exit <= now:
            return {'submitted': False, 'reason': 'stale_signal'}
        flatten_at = min(time_exit, flat)
    booked = ict_paper._book_entry_price(db, c, symbol, side, entry, spec, now)
    entry_booked = booked['price']
    if bracket:
        risk_pts = abs(entry - stop)
        risk_basis = 'stop_distance'
    else:
        risk_pts = sig['atr_t'] * tick
        risk_basis = 'atr_nominal'
    per_unit_risk = risk_pts * spec['multiplier'] + 2 * spec['fee'] \
        + 2 * tick * spec['multiplier']
    from . import paper_risk
    from .futures import risk_day
    base_id = 'ict-%s-%s-%d' % (detector, symbol, int(now))
    trade_id = base_id + '-t1'
    filled = {
        'id': trade_id, 'group_id': base_id, 'detector': detector,
        'strategy': strategy, 'symbol': symbol, 'asset': 'future',
        'side': side, 'direction': side, 'qty': 1, 'quantity': 1,
        'leg': 't1', 'is_trailer': False, 'tier': None,
        'entry': entry_booked, 'signal_price': entry,
        'stop': stop, 'target': target,
        'multiplier': spec['multiplier'], 'tick': tick, 'fee': spec['fee'],
        'status': 'open', 'entered_at': now, 'decided_at': now,
        'signal_time': signal_ts, 'signal_ts': signal_ts,
        'flatten_at': flatten_at, 'strategy_version': VERSION,
        'time_barrier': None, 'underlying': None, 'underlying_side': None,
        'underlying_invalidation': None,
        'entry_price_source': booked['source'],
        'entry_quote_ts': booked['quote_ts'],
        'entry_slip_ticks': booked['slip_ticks'],
        'initial_risk': per_unit_risk * 1, 'risk_basis': risk_basis,
        'fill_model': FILL_DESCRIPTION,
        'fill_version': FILL_VERSION,
        'risk_day': risk_day(now), 'risk_policy': paper_risk.VERSION,
        'paper_portfolio': paper_risk.portfolio('future'),
        'source': 'survivors',
        'signal_rr': (target_ticks_r / stop_ticks_r) if bracket else None,
        'signal_level': None,
        'last_quote_ts': 0, 'last_bar_checked': signal_ts - 60,
        'mark': entry_booked, 'unrealized': -2 * spec['fee'],
        'survivor_strategy': strat, 'signal_tod': sig['tod'],
        'signal_sdate': sig['sdate'], 'atr_ticks': sig['atr_t'],
        'no_bracket': not bracket,
    }
    if bracket:
        filled['stop_ticks'] = stop_ticks_r
        filled['target_ticks'] = target_ticks_r
    if strat in SAFETY_STOP_ATR_MULT:
        # Recorded on the booked trade like the GEX stamp: evidence
        # for exits() (live-sent rows only) and for the review — the
        # paper exit logic never reads it (see engine.exits).
        filled['safety_stop'] = safety_stop
        filled['safety_stop_ticks'] = safety_ticks
    if booked['source'] == 'signal_fallback':
        filled['entry_unverified'] = True
    # Discord live label (ict_push): the entry alert is queued below,
    # BEFORE the TradersPost send block runs, so decide here whether
    # this booking will be queued to TradersPost — evaluating exactly
    # the send block's own gates, in its order: master switch,
    # survivors LIVE state, per-strategy live set, duplicate/hedge
    # guard, symbol mapping, freshness guard. Every check is
    # read-only; the send block below
    # re-evaluates them unchanged and stays the only place that acts.
    # The no-stop invariant is part of the mirror: a booking whose
    # payload would carry no stop (send_stop None) will be REFUSED by
    # the send block, so it is never labeled a live send here — the
    # refusal alert below is its live-channel notice instead.
    filled['live_send'] = False
    if getattr(cfg, 'traderspost_enabled', False) \
            and send_stop is not None:
        _st = get_state(db, c)
        if _st and _st.get('live') \
                and strat in live_strategies(cfg) \
                and guard_check(db, c, filled) is None:
            from . import traderspost as _tp
            _mapped = _tp.map_symbol(symbol)
            if not (_mapped == symbol and '.' in symbol):
                try:
                    from . import freshness_guard as _fg
                    _guard = _fg.evaluate(db, c, cfg, filled, now)
                except Exception:
                    _guard = {'decision': 'send'}
                if _guard.get('decision') != 'skip':
                    filled['live_send'] = True
    # GEX context stamp (Build #2, 2026-11-02): evidence only — the
    # stamp never feeds the fill counter, the tripwire, or any gate.
    # MCL (S2) has no vendor GEX proxy, so its rows simply carry none.
    try:
        from . import tm_gex
        _gex = tm_gex.gex_ctx(db, c, symbol, entry_booked, tick, now)
        if _gex is not None:
            filled['gex_ctx'] = _gex
    except Exception:
        pass
    db.put(c, pos_key_base + ':t1', filled)
    db.put(c, 'trade:' + trade_id, filled)
    db.append(c, 'paper_decision', 'survivors', symbol, now,
              {**filled, 'status': 'entered'}, 'entry:' + trade_id)
    try:
        from . import ict_push
        ict_push.maybe_queue(db, c, cfg, filled)
    except Exception as e:
        # Booking must never block on a push failure — but a silent
        # swallow makes a queue failure invisible at this seam.
        # Mirror the marker ict_paper.submit already writes for the
        # same call ('ict_push_queue'), so the failure surfaces in
        # health instead of vanishing. Written on THIS connection,
        # not via db.health(): health() opens its own transaction,
        # which deadlocks against the booking transaction's writes
        # on sqlite and needlessly couples the marker to a second
        # connection on Postgres. Same row shape health() writes;
        # it commits (or rolls back) with the booking itself.
        try:
            prev = db.get(c, 'health:ict_push_queue') or {}
            db.put(c, 'health:ict_push_queue',
                   {**prev, 'name': 'ict_push_queue', 'status': 'error',
                    'detail': '%s: %s' % (type(e).__name__,
                                          str(e)[:200]),
                    'checked_at': time.time()})
        except Exception:
            pass
    try:
        risk = paper_risk.account(db, c, now, 'future', persist=True)
        risk['entries'] = risk.get('entries', 0) + 1
        db.put(c, paper_risk.key(now, 'future'), risk)
    except Exception:
        pass
    result = {'submitted': True, 'id': filled['id'], 'detector': detector,
              'strategy': strategy, 'symbol': symbol, 'side': side,
              'entry': entry_booked, 'stop': stop, 'target': target,
              'safety_stop': safety_stop,
              'qty': 1, 'traderspost_sent': False,
              'traderspost_queued': False}
    if not getattr(cfg, 'traderspost_enabled', False):
        return result
    # ---- live send path (mirrors ict_paper.submit's TradersPost block;
    # the send is QUEUED in this transaction and dispatched after the
    # tick commits; dispatch writes traderspost_sent onto both copies).
    from . import traderspost
    trade = filled
    skip_reason = None
    guard = None
    st = get_state(db, c)
    if not (st and st.get('live')):
        skip_reason = 'skip %s: survivor live sends off (%s)' % (
            symbol, 'tripwire tripped' if st and st.get('tripped_at')
            else 'state not initialized')
    if skip_reason is None and strat not in live_strategies(cfg):
        # Per-strategy pull (2026-11-09): the book stays live, this
        # strategy's entries book paper only — the same skip stamping
        # as the book-level-off case above. ENTRIES only: exits for
        # an already-open live-sent position of this strategy are
        # sent by engine.exits() off the position's traderspost_sent
        # flag and never consult this gate.
        skip_reason = 'skip %s: survivor %s live sends off ' \
            '(not in SURVIVOR_LIVE_STRATEGIES)' % (symbol, strat)
    if skip_reason is None:
        skip_reason = guard_check(db, c, trade)
    if skip_reason is None:
        mapped = traderspost.map_symbol(symbol)
        if mapped == symbol and '.' in symbol:
            skip_reason = 'skip %s: no TradersPost symbol mapping' % symbol
    if skip_reason is None:
        try:
            from . import freshness_guard as _fg
            guard = _fg.evaluate(db, c, cfg, trade, now)
        except Exception as e:
            guard = {'decision': 'send', 'evaluated': False, 'reason': None,
                     'note': 'error',
                     'error': '%s: %s' % (type(e).__name__, str(e)[:200])}
        if guard.get('decision') == 'skip':
            skip_reason = 'skip %s: freshness guard (%s)' % (
                symbol, guard.get('reason'))
    keys = ('trade:' + trade_id, pos_key_base + ':t1')
    if skip_reason:
        for key in keys:
            t = db.get(c, key)
            if t:
                t = dict(t)
                t['traderspost_sent'] = False
                t['traderspost_skip_reason'] = skip_reason
                if guard:
                    t['freshness_guard'] = guard
                db.put(c, key, t)
        result['traderspost_skip_reason'] = skip_reason
        log.info('Survivor TradersPost send skipped: %s', skip_reason)
        try:
            db.health('survivors', 'degraded', skip_reason)
        except Exception:
            pass
        return result
    if guard is not None:
        for key in keys:
            t = db.get(c, key)
            if t:
                t = dict(t)
                t['freshness_guard'] = guard
                db.put(c, key, t)
    trade = db.get(c, 'trade:' + trade_id) or dict(filled)
    trade = dict(trade)
    trade['is_first_leg'] = True
    if not bracket:
        # Horizon send copy: attach the safety stop as a stop-only
        # protective bracket (build_payload's safety_stop_only
        # branch). ONLY the copy changes — the booked record keeps
        # stop=None / no_bracket=True, the paper horizon construction.
        # When the safety stop did not compute, the copy stays
        # bracket-less and the invariant below refuses the send.
        trade['stop'] = safety_stop
        trade['target'] = None
        trade['no_bracket'] = False
        trade['safety_stop_only'] = True
    # No-naked-entry invariant (2026-11-04, Josh: "I don't want any
    # orders that are solely market"): a live entry is queued ONLY
    # when the payload it will send carries a stop price. Checked
    # against the built payload itself — the ground truth of what
    # TradersPost would receive — so it covers every strategy,
    # present and future, whatever its record shape. A payload that
    # fails to build at all keeps the pre-existing invalid-payload
    # marking below (bracket family only; for a horizon trade a
    # failed build means the safety stop is missing, which IS the
    # refusal case). The refusal is stamped like every other skip
    # (both copies, digest-visible) and additionally raises a loud
    # alert on the live ICT channel — a live entry that did not go
    # out is exactly what that channel exists for. By construction
    # the trade was never sent: fill_confirmation projects it
    # 'not_sent' and the live accounting ignores it.
    _payload = traderspost.build_payload(None, trade)
    _has_stop = bool(_payload and _payload.get('stopLoss'))
    if not _has_stop and (_payload is not None or not bracket):
        refusal = 'skip %s: no_stop_attached' % symbol
        for key in keys:
            t = db.get(c, key)
            if t:
                t = dict(t)
                t['traderspost_sent'] = False
                t['traderspost_skip_reason'] = refusal
                db.put(c, key, t)
        result['traderspost_skip_reason'] = refusal
        result['send_refused'] = 'no_stop_attached'
        log.error('Survivor live send REFUSED — no stop attached '
                  '(%s %s %s)', strat, symbol, trade_id)
        try:
            db.health('survivors', 'degraded', refusal)
        except Exception:
            pass
        try:
            from . import ict_push
            ict_push.maybe_queue_send_refusal(db, c, cfg, trade,
                                              'no_stop_attached')
        except Exception:
            log.exception('Survivor send-refusal alert failed')
        return result
    queued = traderspost.queue_signal(
        db, c, trade, trade_key='trade:' + trade_id,
        position_key=pos_key_base + ':t1')
    if queued:
        result['traderspost_queued'] = True
        return result
    for key in keys:
        t = db.get(c, key)
        if t:
            t = dict(t)
            t['traderspost_sent'] = False
            t['traderspost_skip_reason'] = \
                'send_failed: invalid payload at queue time'
            db.put(c, key, t)
    result['traderspost_skip_reason'] = 'send_failed: invalid payload'
    return result


# ---------------------------------------------------------------------
# Seeding (one-time ATR warm-up from the bar-events store).
#
# Access-path contract (2026-10-30, second seed fix): every events
# query below is served by the (kind, symbol, ts) index. Bar reads use
# EQUALITY on (kind, symbol) with a range on ts only. Symbol discovery
# is one DISTINCT lookup over the root's subtree, ranged in DATABASE
# collation (the index's own collation, so the range is index-served).
# Two earlier shapes failed in production: (1) prefix bounds in
# database order with a '\uffff' cap silently matched nothing, because
# glibc/ICU collation sorts '\uffff' below letters; (2) the COLLATE
# "C" repair (5ba7b6c) returned the right rows but the glibc-collated
# index cannot serve a codepoint range, so the query seq-scanned the
# ~45M-row events table and died on statement timeout (verified live
# 2026-10-30). Neither idiom may come back here.
# ---------------------------------------------------------------------
_SEED_DISCOVER_SQL = (
    "SELECT DISTINCT symbol FROM events WHERE kind='bar' "
    "AND symbol >= :lo AND symbol < :hi AND ts >= :tlo")
_SEED_BARS_SQL = (
    "SELECT ts, payload FROM events WHERE kind='bar' "
    "AND symbol = :symbol AND ts >= :tlo ORDER BY ts")

# Dated-contract event symbols: <ROOT><month code><year>[@<iid>],
# e.g. MNQU6@4242 — the shape ict_common._DATED_WINDOW matches for
# bar_window state keys.
_DATED_SYMBOL = re.compile(r'^([A-Z]+)([FGHJKMNQUVXZ])(\d{1,4})(@.*)?$')


def _root_successor(root):
    """Smallest string strictly above every string that starts with
    root (its last letter stepped). Subtree bounds must hold in the
    database collation as well as in codepoint order: unlike
    root+'\\uffff', stepping the root's own final letter bounds the
    subtree under both orderings, because the first position where a
    subtree member and the successor differ is that letter itself."""
    return root[:-1] + chr(ord(root[-1]) + 1)


def _dated_symbols(c, root, lo_ts):
    """Full dated-contract event symbols for root with bars at/after
    lo_ts, discovered through the (kind, symbol, ts) index. Keeps the
    contract set the old prefix seed aggregated: dated keys only —
    never the bare root, never dotted alias keys ('MNQ.c.0')."""
    rows = c.execute(sql_text(_SEED_DISCOVER_SQL),
                     {'lo': root, 'hi': _root_successor(root),
                      'tlo': lo_ts})
    out = []
    for row in rows:
        m = dict(row._mapping) if hasattr(row, '_mapping') else dict(row)
        sym = m.get('symbol') or ''
        if sym == root or '.' in sym:
            continue
        dm = _DATED_SYMBOL.match(sym)
        if dm and dm.group(1) == root:
            out.append(sym)
    return sorted(out)


def _payload(row):
    p = row
    if isinstance(p, str):
        import json
        try:
            p = json.loads(p)
        except Exception:
            return {}
    return p or {}


def seed_history(db, c, feed, root, now):
    """Aggregate the last ~26 days of stored 1-min bars into completed
    sessions and install them in the feed. Sessions with < 800 bars are
    partial (holidays/outages) and skipped, like the dataset's degraded
    handling. Returns the number of complete sessions found.

    Reads run one dated contract at a time with (kind, symbol)
    equality + a ts range — see the access-path contract above. Bars
    are merged across contracts and re-sorted by ts before
    aggregating, preserving the old single-scan semantics exactly
    (session close = the last bar by ts, whichever contract printed
    it)."""
    lo_ts = now - 26 * 86400
    bars = []
    for sym in _dated_symbols(c, root, lo_ts):
        rows = c.execute(sql_text(_SEED_BARS_SQL),
                         {'symbol': sym, 'tlo': lo_ts})
        for row in rows:
            m = dict(row._mapping) if hasattr(row, '_mapping') \
                else dict(row)
            p = _payload(m.get('payload'))
            ts = m.get('ts')
            if ts is None or p.get('h') is None or p.get('l') is None \
                    or p.get('c') is None:
                continue
            bars.append((ts, p['h'], p['l'], p['c']))
    bars.sort(key=lambda b: b[0])
    sessions = {}
    for ts, h, l, close in bars:
        _, sdate = _ct_parts(ts)
        s = sessions.setdefault(sdate, {'sdate': sdate, 'hi': h,
                                        'lo': l, 'close': close,
                                        'bars': 0})
        s['hi'] = max(s['hi'], h)
        s['lo'] = min(s['lo'], l)
        s['close'] = close  # bars are ts-ordered: last write wins
        s['bars'] += 1
    cutoff = feed.sdate or _ct_parts(now)[1]
    complete = [s for s in sessions.values()
                if s['bars'] >= 800 and s['sdate'] < cutoff]
    complete.sort(key=lambda s: s['sdate'])
    feed.seed_history(complete[-20:])
    return len(complete)


# Raised per-transaction ceilings for the one-off seed load. The pool
# default statement_timeout is 30 s, sized for tick work; the seed is
# a bounded 26-day read that only ever runs a handful of times per
# feed, so it gets headroom — inside its OWN transaction only.
_SEED_STATEMENT_TIMEOUT_MS = 120000
_SEED_TRANSACTION_TIMEOUT = '240s'


def _seed_isolated(db, symbol, feed, root, now):
    """Run one feed's seed on a dedicated pooled connection in its own
    transaction, then bank the outcome in a separate small transaction.

    The first production seed ran inside the tick's shared transaction:
    its statement timeout aborted the whole tick transaction, the
    feeds later in scan order never got an attempt, and the aborted
    transaction never persisted seed_attempt_at — so scan() retried
    every tick (~80 s) instead of on the 600 s cadence. Isolation
    contains all three failures: a seed error rolls back only the
    seed transaction, and the attempt stamp (or, on success, the
    installed history itself) is committed by the bank transaction
    even if the surrounding tick segment later aborts. Returns True
    when the seed completed (feed.seeded set)."""
    feed.seed_attempt_at = now
    ok = False
    try:
        with db.tx() as sc:
            if sc.dialect.name == 'postgresql':
                sc.execute(sql_text(
                    'SET LOCAL statement_timeout = %d'
                    % _SEED_STATEMENT_TIMEOUT_MS))
                version = sc.execute(sql_text(
                    "SELECT current_setting('server_version_num')::int"
                )).scalar()
                if version and version >= 170000:
                    # The pool sets a 60 s session transaction_timeout
                    # on PG 17+; raise it for this transaction only.
                    sc.execute(sql_text(
                        "SET LOCAL transaction_timeout = '%s'"
                        % _SEED_TRANSACTION_TIMEOUT))
            n_seeded = seed_history(db, sc, feed, root, now)
        feed.seeded = True
        ok = True
        if n_seeded:
            log.info('Survivors: seeded %s ATR history with %d '
                     'sessions', root, n_seeded)
    except Exception as e:
        log.warning('Survivors: seed failed for %s: %s', root, e)
    try:
        with db.tx() as pc:
            db.put(pc, 'survivor_engine:' + symbol, feed.to_state())
    except Exception as e:
        log.warning('Survivors: seed state bank failed for %s: %s',
                    root, e)
    return ok


# ---------------------------------------------------------------------
# Scan (engine tick entry point).
# ---------------------------------------------------------------------
def scan(db, c, cfg, now):
    if not getattr(cfg, 'survivor_enabled', False):
        return {'ran': False, 'reason': 'disabled'}
    if not getattr(cfg, 'ict_futures_paper', False):
        return {'ran': False, 'reason': 'ict_futures_paper_disabled'}
    last = db.get(c, SCAN_KEY)
    if isinstance(last, dict) and now - (last.get('at') or 0) < SCAN_INTERVAL:
        return {'ran': False, 'reason': 'throttled'}
    st = ensure_state(db, c, now)
    # Fill confirmation (2026-11-02): settle the book against broker
    # verdicts BEFORE accounting reads it — one-time correction of the
    # 2026-11-02 record, then the standing reconciliation pass
    # (projections + overdue-pending escalation). Isolated like every
    # other scan unit: a confirmation failure must never silence the
    # detectors, and accounting below fails closed on its own (it
    # derives from verdicts directly).
    try:
        from . import fill_confirmation as _fc
        _fc.apply_history_correction_2026_11_02(db, c, st, now)
        _rec = _fc.reconcile(db, c, now)
        if _rec.get('overdue_legs'):
            try:
                db.health('survivors_fill', 'degraded',
                          '%d live send leg(s) unconfirmed past the '
                          'confirmation window: %s'
                          % (len(_rec['overdue_legs']),
                             ', '.join('%s/%s' % (o['trade'], o['leg'])
                                       for o in _rec['overdue_legs'])))
            except Exception:
                pass
    except Exception:
        log.exception('survivors fill reconciliation failed')
    st = account_live(db, c, st, now)
    summary = {'ran': True, 'at': now, 'live': bool(st.get('live')),
               'symbols': 0, 'signals': 0, 'submitted': 0, 'sent': 0,
               'stale_skips': 0, 'declined': {}}
    for symbol in SCAN_SYMBOLS:
        root = symbol.split('.')[0]
        spec = future_spec(symbol)
        if not spec:
            continue
        summary['symbols'] += 1
        state = db.get(c, 'survivor_engine:' + symbol)
        feed = SurvivorFeed.from_state(state) \
            if isinstance(state, dict) else SurvivorFeed(root, spec['tick'])
        if not feed.seeded and now - feed.seed_attempt_at >= 600:
            # Isolated on purpose: the seed must never run inside this
            # tick transaction (see _seed_isolated).
            _seed_isolated(db, symbol, feed, root, now)
        bars = ict_bars(db, c, symbol, limit=10000)
        fresh_signals = []
        for b in bars:
            if feed.last_ts is not None and b['ts'] <= feed.last_ts:
                continue
            for s in feed.feed(b):
                summary['signals'] += 1
                if now - (s['ts'] + 60) <= FRESH_SECONDS:
                    fresh_signals.append(s)
                else:
                    summary['stale_skips'] += 1
        db.put(c, 'survivor_engine:' + symbol, feed.to_state())
        for s in fresh_signals:
            try:
                res = submit_survivor(db, c, cfg, now, s['strategy'],
                                      symbol, s)
            except Exception as e:
                log.error('Survivor submit failed (%s %s): %s',
                          s['strategy'], symbol, e)
                res = {'submitted': False, 'reason': 'error'}
            if res.get('submitted'):
                summary['submitted'] += 1
                if res.get('traderspost_queued'):
                    summary['sent'] += 1
            else:
                reason = res.get('reason') or 'declined'
                summary['declined'][reason] = \
                    summary['declined'].get(reason, 0) + 1
    db.put(c, SCAN_KEY, {'at': now, **summary})
    return summary
