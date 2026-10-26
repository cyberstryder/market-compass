"""Shadow iceberg detector (Josh, 2026-10-25).

Institutions work large options orders in small slices so the tape shows
dozens of 10-50 contract prints instead of one block. This module
reconstructs the parent order from the OPRA print stream that already
flows through the Collector: prints are clustered per contract into
rolling 1-minute buckets, ask/bid/mid-side classified against the live
NBBO, and a contract that shows institutional accumulation — size,
premium, ask-side aggression, persistence across sub-windows, premium
not conceding as size builds — fires ONE alert per accumulation episode.

SHADOW ONLY. It never trades, never blocks, never sends Discord in v1.
It logs alerts, follows each one to scored outcomes (+1h, session close,
next session close, expiry), and aggregates a scorecard whose
promotion-rule view (adopted 2026-10-25: >=40 scored alerts, positive
expectancy in two consecutive 4-week windows, measured) is checkable
from GET /api/iceberg-scorecard.

Hard constraints (standing incident lessons):
- AGGREGATES, NOT RAW PRINTS. No per-print rows are written anywhere by
  this module. Rolling state lives in memory (bounded, LRU-evicted);
  only fired alerts, a compact diag snapshot, and checkpoint scores are
  persisted (state rows, not events).
- FAULT ISOLATION. observe_* never raises and does NO database I/O —
  the stream read loop calls it inline. Anything unexpected is counted
  in `errors` and swallowed. Persistence happens in a periodic
  collector pass (drain + OI-cache refresh + scoring + diag flush).
- House data rule: an outcome that cannot be priced from a fresh quote
  is a data_gap, never a loss.
"""
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta

from .market import NY, day, number, session
from .store import identity

_OCC = re.compile(r"^O:([A-Z][A-Z0-9.]*?)(\d{6})([CP])(\d{8})$")

# Defaults (env-overridable via ICEBERG_* in config.py). Justification:
# the streams cover ~1,300 priority contracts on liquid names; a busy
# retail contract prints a handful of times an hour. An institutional
# parent worked in slices over half an hour is hundreds of contracts,
# tens of prints, five figures of premium, predominantly at the ask,
# spread across several 5-minute sub-windows rather than one burst, and
# the premium does not sag while they buy (they pay up, not down).
DEFAULTS = {
    'enabled': True,
    'min_contracts_30m': 500,
    'min_premium_30m': 25000.0,
    'min_prints_30m': 25,
    'min_ask_share': 0.65,
    'min_active_subwindows': 3,
    'vol_oi_ratio': 0.5,
    'no_concession_tol': 0.05,
    'cooldown_min': 30.0,
    'max_tracked': 5000,
}

HORIZONS = ('h1', 'close', 'next_close', 'expiry')

# pending_alerts bound: an alert fires at most once per contract per
# cooldown (30 min default) and the collector pass drains the queue
# every ~30 s, so a healthy queue holds a handful at most. The cap
# exists for the stalled/stopped-drain case: the deque used to be
# unbounded, so a detector whose drain never ran accumulated alerts
# forever (Task 003 item 6). 1024 is orders of magnitude above any
# plausible inter-drain burst; on overflow the deque drops the OLDEST
# undrained alerts and snapshot_diag's pending_alerts pins at the cap
# — the visible symptom of a stalled drain.
PENDING_ALERTS_MAXLEN = 1024


def _opt(cfg, name, default):
    value = getattr(cfg, 'iceberg_' + name, None) if cfg is not None else None
    return default if value is None else value


def parse_occ(sym):
    """'O:SPY251003C00745000' -> contract parts, or None."""
    m = _OCC.match(str(sym or ''))
    if not m:
        return None
    root, yymmdd, right, strike = m.groups()
    try:
        expiry = datetime.strptime(yymmdd, '%y%m%d').date().isoformat()
    except ValueError:
        return None
    return {'underlying': root, 'expiry': expiry,
            'type': 'call' if right == 'C' else 'put',
            'strike': int(strike) / 1000.0}


def _bucket(minute):
    return {'minute': minute, 'prints': 0, 'contracts': 0.0,
            'premium': 0.0, 'ask': 0.0, 'bid': 0.0, 'mid': 0.0,
            'other': 0.0}


class _State:
    __slots__ = ('symbol', 'parts', 'buckets', 'day', 'day_contracts',
                 'day_premium', 'last_price', 'last_ts', 'cooldown_until',
                 'burst', 'sigs', 'src_contracts')

    def __init__(self, symbol, parts):
        self.symbol = symbol
        self.parts = parts
        self.buckets = deque(maxlen=31)
        self.day = None
        self.day_contracts = 0.0
        self.day_premium = 0.0
        self.last_price = None
        self.last_ts = 0.0
        self.cooldown_until = 0.0
        self.burst = deque(maxlen=256)      # (ts, exchange) recent prints
        self.sigs = {}                      # dedupe: (ts,px,size) -> src
        self.src_contracts = {}             # this-episode contracts by src


class IcebergDetector:
    """In-memory accumulation detector fed from OptionBuffer.offer taps."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        # Kill switch: the Config field is `iceberg_detector`
        # (config.py, env ICEBERG_DETECTOR_ENABLED). This used to read
        # _opt(cfg, 'enabled') -> cfg.iceberg_enabled, a field Config
        # does not have, so the value was always None and `enabled`
        # always fell back to the default True — the env switch did
        # nothing (Task 003 item 6, confirmed behaviorally). The legacy
        # name stays as a fallback for callers that pass iceberg_enabled.
        _det = _opt(cfg, 'detector', None)
        if _det is None:
            _det = _opt(cfg, 'enabled', DEFAULTS['enabled'])
        self.enabled = bool(_det)
        self.min_contracts = float(_opt(cfg, 'min_contracts_30m',
                                         DEFAULTS['min_contracts_30m']))
        self.min_premium = float(_opt(cfg, 'min_premium_30m',
                                       DEFAULTS['min_premium_30m']))
        self.min_prints = int(_opt(cfg, 'min_prints_30m',
                                   DEFAULTS['min_prints_30m']))
        self.min_ask_share = float(_opt(cfg, 'min_ask_share',
                                        DEFAULTS['min_ask_share']))
        self.min_active = int(_opt(cfg, 'min_active_subwindows',
                                   DEFAULTS['min_active_subwindows']))
        self.vol_oi_ratio = float(_opt(cfg, 'vol_oi_ratio',
                                        DEFAULTS['vol_oi_ratio']))
        self.concession_tol = float(_opt(cfg, 'no_concession_tol',
                                         DEFAULTS['no_concession_tol']))
        self.cooldown_s = float(_opt(cfg, 'cooldown_min',
                                     DEFAULTS['cooldown_min'])) * 60.0
        self.max_tracked = int(_opt(cfg, 'max_tracked',
                                    DEFAULTS['max_tracked']))
        self._lock = threading.Lock()
        self.contracts = OrderedDict()      # symbol -> _State (LRU)
        self.nbbo = {}                      # symbol -> (ts, bid, ask)
        self.oi_cache = {}                  # symbol -> open interest
        self.pending_alerts = deque(maxlen=PENDING_ALERTS_MAXLEN)
        self.started_at = time.time()
        self.prints_seen = 0
        self.quotes_seen = 0
        self.dup_prints = 0
        self.unparsed = 0
        self.errors = 0
        self.alerts_fired = 0
        self.last_print_ts = None
        self.last_alert_at = None

    # -- stream taps (called inline from the buffer; NEVER raise) ------

    def note_error(self):
        self.errors += 1

    def observe_quote(self, symbol, stamp, bid, ask):
        try:
            if not self.enabled:
                return
            b, a = number(bid), number(ask)
            if b is None or a is None or b <= 0 or a < b:
                return
            with self._lock:
                self.nbbo[symbol] = (stamp, b, a)
                self.quotes_seen += 1
        except Exception:
            self.errors += 1

    def observe_trade(self, symbol, stamp, item):
        """One accepted trade print. Returns the fired alert or None."""
        try:
            if not self.enabled or not isinstance(item, dict):
                return None
            return self._observe_trade(symbol, stamp, item)
        except Exception:
            self.errors += 1
            return None

    def _observe_trade(self, symbol, stamp, item):
        price, size = number(item.get('p')), number(item.get('s'))
        if price is None or size is None or price <= 0 or size <= 0:
            return None
        parts = parse_occ(symbol)
        if parts is None:
            self.unparsed += 1
            return None
        src = str(item.get('src') or 'massive')
        with self._lock:
            st = self.contracts.get(symbol)
            if st is None:
                st = _State(symbol, parts)
                self.contracts[symbol] = st
                while len(self.contracts) > self.max_tracked:
                    old, _ = self.contracts.popitem(last=False)
                    self.nbbo.pop(old, None)
            else:
                self.contracts.move_to_end(symbol)
            # Cross-source echo dedupe: both streams report the same
            # exchange trade; identical (ts, price, size) from the OTHER
            # source inside 10 s is the same print, counted once.
            sig = (round(stamp, 3), price, size)
            for old in [k for k in st.sigs if stamp - k[0] > 10]:
                del st.sigs[old]
            if sig in st.sigs and st.sigs[sig] != src:
                self.dup_prints += 1
                return None
            st.sigs[sig] = src
            # Day rollover for the volume-vs-OI totals.
            today = day(stamp)
            if st.day != today:
                st.day = today
                st.day_contracts = 0.0
                st.day_premium = 0.0
            minute = int(stamp // 60)
            if not st.buckets or minute - st.buckets[-1]['minute'] >= 31:
                st.buckets.clear()
                st.buckets.append(_bucket(minute))
            while st.buckets[-1]['minute'] < minute:
                st.buckets.append(_bucket(st.buckets[-1]['minute'] + 1))
            b = st.buckets[-1]
            side = self._classify(symbol, stamp, price)
            premium = price * size * 100.0
            b['prints'] += 1
            b['contracts'] += size
            b['premium'] += premium
            b[side] += size
            st.day_contracts += size
            st.day_premium += premium
            st.last_price = price
            st.last_ts = stamp
            st.src_contracts[src] = st.src_contracts.get(src, 0.0) + size
            exchange = item.get('x')
            st.burst.append((stamp, exchange))
            self.prints_seen += 1
            self.last_print_ts = stamp
            return self._evaluate(st, stamp)

    def _classify(self, symbol, stamp, price):
        nb = self.nbbo.get(symbol)
        if not nb or stamp - nb[0] > 5:
            return 'other'
        _ts, bid, ask = nb
        if price >= ask - 1e-9:
            return 'ask'
        if price <= bid + 1e-9:
            return 'bid'
        if bid < price < ask:
            return 'mid'
        return 'ask' if (ask - price) <= (price - bid) else 'bid'

    # -- detection ------------------------------------------------------

    def _evaluate(self, st, now):
        if now < st.cooldown_until:
            return None
        buckets = list(st.buckets)
        c30 = sum(b['contracts'] for b in buckets)
        p30 = sum(b['premium'] for b in buckets)
        n30 = sum(b['prints'] for b in buckets)
        if (c30 < self.min_contracts or p30 < self.min_premium
                or n30 < self.min_prints):
            return None
        ask = sum(b['ask'] for b in buckets)
        bid = sum(b['bid'] for b in buckets)
        mid = sum(b['mid'] for b in buckets)
        classified = ask + bid + mid
        if classified <= 0:
            return None
        ask_share = ask / classified
        if ask_share < self.min_ask_share:
            return None
        # Persistence: accumulation in >= min_active of the six 5-minute
        # sub-windows, including the most recent one.
        groups = [0.0] * 6
        base = buckets[0]['minute']
        for b in buckets:
            idx = min(5, (b['minute'] - base) // 5)
            groups[idx] += b['contracts']
        active = sum(1 for g in groups if g > 0)
        last_idx = min(5, (buckets[-1]['minute'] - base) // 5)
        if active < self.min_active or groups[last_idx] <= 0:
            return None
        # No concession: the buyer is not getting filled cheaper as size
        # builds — late-window VWAP holds vs early-window VWAP.
        def _vwap(rows):
            c = sum(r['contracts'] for r in rows)
            return (sum(r['premium'] for r in rows) / (c * 100.0)) if c else None
        early, late = _vwap(buckets[:10]), _vwap(buckets[-10:])
        if early and late and late < early * (1 - self.concession_tol):
            return None
        # Extra confirmation (any one): notable volume vs open interest,
        # or a multi-exchange burst in the trailing seconds.
        oi = self.oi_cache.get(st.symbol)
        vol_oi = (st.day_contracts / oi) if oi else None
        burst_ex = len({x for t, x in st.burst
                        if now - t <= 2.0 and x is not None})
        reasons = []
        if vol_oi is not None and vol_oi >= self.vol_oi_ratio:
            reasons.append('vol_oi %.2f' % vol_oi)
        if burst_ex >= 3:
            reasons.append('burst %d exchanges' % burst_ex)
        if not reasons:
            return None
        c5 = sum(b['contracts'] for b in buckets[-5:])
        score = round(0.35 * min(1.0, c30 / (4 * self.min_contracts))
                      + 0.25 * ask_share
                      + 0.20 * min(1.0, p30 / (4 * self.min_premium))
                      + 0.20 * (active / 6.0), 3)
        nb = self.nbbo.get(st.symbol)
        mark_mid = ((nb[1] + nb[2]) / 2.0) if nb else None
        episode_start = buckets[0]['minute'] * 60
        alert = {
            'id': identity('iceberg_alert', st.symbol, episode_start),
            'kind': 'iceberg_alert',
            'symbol': st.symbol,
            'underlying': st.parts['underlying'],
            'expiry': st.parts['expiry'],
            'strike': st.parts['strike'],
            'type': st.parts['type'],
            'side': 'bullish' if st.parts['type'] == 'call' else 'bearish',
            'alert_ts': now,
            'day': day(now),
            'week': _iso_week(now),
            'est_parent_contracts': int(c30),
            'est_parent_premium': round(p30, 2),
            'prints': int(n30),
            'ask_share': round(ask_share, 3),
            'score': score,
            'reasons': reasons,
            'mark': st.last_price,
            'mid': round(mark_mid, 4) if mark_mid else None,
            'underlying_at_alert': None,
            'window': {
                'contracts_5m': int(c5), 'contracts_30m': int(c30),
                'premium_30m': round(p30, 2), 'prints_30m': int(n30),
                'ask_share_30m': round(ask_share, 3),
                'active_subwindows': active,
                'burst_exchanges': burst_ex,
                'day_contracts': int(st.day_contracts),
                'oi': oi, 'vol_oi': round(vol_oi, 3) if vol_oi else None,
                'sources': dict(st.src_contracts),
            },
            'scores': {},
            'mfe_pct': None, 'mae_pct': None,
            'und_mfe_pct': None, 'und_mae_pct': None,
            'status': 'open',
        }
        st.cooldown_until = now + self.cooldown_s
        st.src_contracts = {}
        self.pending_alerts.append(alert)
        self.alerts_fired += 1
        self.last_alert_at = now
        return alert

    # -- periodic-pass interface (collector thread; DB lives outside) ---

    def drain_alerts(self):
        with self._lock:
            out = list(self.pending_alerts)
            self.pending_alerts.clear()
            return out

    def refresh_oi(self, mapping):
        with self._lock:
            self.oi_cache.update(mapping)
            # Bound the cache: chain snapshots cover far more contracts
            # than the detector tracks; only when it balloons do we cut
            # it back to the tracked set (never prune eagerly — an OI
            # entry may legitimately arrive before the first print).
            if len(self.oi_cache) > 20000:
                for sym in [s for s in self.oi_cache
                            if s not in self.contracts]:
                    del self.oi_cache[sym]

    def tracked_underlyings(self):
        with self._lock:
            return sorted({st.parts['underlying']
                           for st in self.contracts.values()})

    def snapshot_diag(self, now):
        with self._lock:
            return {
                'at': now, 'enabled': self.enabled,
                'started_at': self.started_at,
                'prints_seen': self.prints_seen,
                'quotes_seen': self.quotes_seen,
                'dup_prints': self.dup_prints,
                'unparsed': self.unparsed,
                'errors': self.errors,
                'alerts_fired': self.alerts_fired,
                'pending_alerts': len(self.pending_alerts),
                'contracts_tracked': len(self.contracts),
                'last_print_ts': self.last_print_ts,
                'last_alert_at': self.last_alert_at,
                'thresholds': {
                    'min_contracts_30m': self.min_contracts,
                    'min_premium_30m': self.min_premium,
                    'min_prints_30m': self.min_prints,
                    'min_ask_share': self.min_ask_share,
                    'min_active_subwindows': self.min_active,
                    'vol_oi_ratio': self.vol_oi_ratio,
                    'cooldown_min': self.cooldown_s / 60.0,
                },
            }


def _iso_week(t):
    iso = datetime.fromtimestamp(t, NY).isocalendar()
    return '%04d-W%02d' % (iso[0], iso[1])


# ----------------------------------------------------------------------
# Outcome scoring (periodic pass; owns DB reads/writes via caller's tx)
# ----------------------------------------------------------------------

def _session_close(day_iso):
    try:
        sess = session(day_iso)
    except Exception:
        return None
    return sess[1] if sess else None


def _checkpoint_ts(alert, horizon):
    if horizon == 'h1':
        return alert['alert_ts'] + 3600
    if horizon == 'close':
        return _session_close(alert['day'])
    if horizon == 'next_close':
        d = datetime.fromisoformat(alert['day']).date()
        for i in range(1, 8):
            close = _session_close((d + timedelta(days=i)).isoformat())
            if close:
                return close
        return None
    if horizon == 'expiry':
        return _session_close(alert['expiry'])
    return None


def _fresh_mid(db, c, symbol, target, horizon, now):
    """Contract mid from the latest quote, or (None, reason).

    Quotes persist as latest-state only, so a checkpoint is priced from
    the freshest quote at pass time and accepted only inside a window
    around its target; anything else is a data_gap, never a guess.
    """
    q = db.get(c, 'quote:' + symbol) or {}
    bid, ask, qt = number(q.get('bid')), number(q.get('ask')), number(q.get('ts'))
    if bid is None or ask is None or qt is None or bid <= 0 or ask < bid:
        return None, 'no_quote'
    lo = target - (900 if horizon == 'h1' else 3600)
    hi = target + (900 if horizon == 'h1' else 6 * 3600)
    if qt < lo:
        return None, 'stale_quote'
    if qt > hi and now > hi:
        return None, 'late_quote'
    return (bid + ask) / 2.0, None


def _underlying_close_at(db, c, underlying, day_iso, before_ts):
    bars = db.get(c, 'bar_window:' + underlying) or []
    best = None
    for row in bars:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        if day(row[0]) != day_iso or row[0] > before_ts:
            continue
        if best is None or row[0] > best[0]:
            best = row
    return number(best[4]) if best else None


def _score_alert(db, c, alert, now):
    changed = False
    symbol = alert['symbol']
    base = number(alert.get('mid')) or number(alert.get('mark'))
    if alert.get('underlying_at_alert') is None:
        px = _underlying_close_at(db, c, alert['underlying'],
                                  alert['day'], alert['alert_ts'])
        if px:
            alert['underlying_at_alert'] = px
            changed = True
    # Running excursion from any reasonably fresh quote.
    q = db.get(c, 'quote:' + symbol) or {}
    bid, ask, qt = number(q.get('bid')), number(q.get('ask')), number(q.get('ts'))
    if (base and bid and ask and qt and now - qt <= 3600 and bid > 0):
        ret = ((bid + ask) / 2.0 / base - 1) * 100.0
        for field, better in (('mfe_pct', max), ('mae_pct', min)):
            cur = alert.get(field)
            alert[field] = round(ret, 2) if cur is None else round(
                better(cur, ret), 2)
            changed = True
    und0 = number(alert.get('underlying_at_alert'))
    if und0:
        uq = db.get(c, 'quote:' + alert['underlying']) or {}
        upx, uqt = number(uq.get('bid')), number(uq.get('ts'))
        ask_u = number(uq.get('ask'))
        if upx and ask_u and uqt and now - uqt <= 3600:
            move = (((upx + ask_u) / 2.0) / und0 - 1) * 100.0
            if alert['side'] == 'bearish':
                move = -move
            for field, better in (('und_mfe_pct', max), ('und_mae_pct', min)):
                cur = alert.get(field)
                alert[field] = round(move, 2) if cur is None else round(
                    better(cur, move), 2)
                changed = True
    scores = alert.setdefault('scores', {})
    for horizon in HORIZONS:
        if horizon in scores:
            continue
        target = _checkpoint_ts(alert, horizon)
        if target is None:
            scores[horizon] = {'status': 'data_gap', 'reason': 'no_session',
                               'evaluated_at': now}
            changed = True
            continue
        if now < target:
            continue
        entry = {'target_ts': target}
        if horizon == 'expiry':
            close_px = _underlying_close_at(db, c, alert['underlying'],
                                            alert['expiry'], target + 6 * 3600)
            if close_px is not None:
                intrinsic = (max(0.0, close_px - alert['strike'])
                             if alert['type'] == 'call'
                             else max(0.0, alert['strike'] - close_px))
                entry.update(status='scored', basis='intrinsic',
                             value=round(intrinsic, 4),
                             underlying=close_px, evaluated_at=now)
                if base:
                    entry['contract_ret_pct'] = round(
                        (intrinsic / base - 1) * 100.0, 2)
                scores[horizon] = entry
                changed = True
                continue
        mid, reason = _fresh_mid(db, c, symbol, target, horizon, now)
        if mid is not None and base:
            entry.update(status='scored', basis='quote_mid',
                         value=round(mid, 4), evaluated_at=now,
                         contract_ret_pct=round((mid / base - 1) * 100.0, 2))
            uq = db.get(c, 'quote:' + alert['underlying']) or {}
            ub, ua = number(uq.get('bid')), number(uq.get('ask'))
            if ub and ua and und0:
                move = (((ub + ua) / 2.0) / und0 - 1) * 100.0
                entry['underlying_ret_pct'] = round(
                    -move if alert['side'] == 'bearish' else move, 2)
            scores[horizon] = entry
            changed = True
        elif now > target + (1800 if horizon in ('h1', 'close') else 3600):
            # Grace expired and still unquotable: a data gap, not a loss.
            scores[horizon] = {'status': 'data_gap', 'reason': reason,
                               'target_ts': target, 'evaluated_at': now}
            changed = True
    if alert.get('status') == 'open' and len(scores) == len(HORIZONS):
        alert['status'] = 'complete'
        changed = True
    return changed


def score_pass(db, c, cfg, now):
    """Score every open alert whose checkpoints are due. Returns counts."""
    updated = 0
    rows = db.prefix(c, 'iceberg_alert:')
    for key, alert in rows.items():
        if not isinstance(alert, dict):
            continue
        try:
            if _score_alert(db, c, alert, now):
                db.put(c, key, alert)
                updated += 1
        except Exception:
            continue
    return {'alerts': len(rows), 'updated': updated}


# ----------------------------------------------------------------------
# Scorecard (read API)
# ----------------------------------------------------------------------

def _horizon_stats(alerts, horizon):
    scored, gaps, pending = [], 0, 0
    for a in alerts:
        s = (a.get('scores') or {}).get(horizon)
        if not s:
            pending += 1
        elif s.get('status') == 'scored' and s.get('contract_ret_pct') is not None:
            scored.append(s)
        else:
            gaps += 1
    rets = [s['contract_ret_pct'] for s in scored]
    und = [s['underlying_ret_pct'] for s in scored
           if s.get('underlying_ret_pct') is not None]
    return {
        'scored': len(scored), 'data_gap': gaps, 'pending': pending,
        'mean_contract_ret_pct': round(sum(rets) / len(rets), 2) if rets else None,
        'median_contract_ret_pct': (round(sorted(rets)[len(rets) // 2], 2)
                                    if rets else None),
        'pct_ge_50': (round(100.0 * sum(1 for r in rets if r >= 50) / len(rets), 1)
                      if rets else None),
        'mean_underlying_ret_pct': (round(sum(und) / len(und), 2)
                                    if und else None),
    }


def _monday(day_iso):
    d = datetime.fromisoformat(day_iso).date()
    return d - timedelta(days=d.weekday())


def promotion_view(alerts):
    """The adopted rule, checkable: >=40 scored alerts and positive
    expectancy (mean contract return at session close) in two
    CONSECUTIVE 4-week windows. Windows are 4-week blocks anchored at
    the Monday of the first alert's week. `meets_bar` uses the pair's
    combined scored count (>=40) with both windows positive;
    `meets_bar_each_window` is the stricter per-window reading."""
    if not alerts:
        return {'windows': [], 'alerts_total': 0, 'scored_total': 0,
                'two_consecutive_positive': False, 'meets_bar': False,
                'meets_bar_each_window': False}
    anchor = min(_monday(a['day']) for a in alerts)
    blocks = {}
    for a in alerts:
        idx = (_monday(a['day']) - anchor).days // 28
        blocks.setdefault(idx, []).append(a)
    windows = []
    for idx in sorted(blocks):
        rows = blocks[idx]
        stats = _horizon_stats(rows, 'close')
        start = anchor + timedelta(days=28 * idx)
        windows.append({
            'window': start.isoformat(), 'alerts': len(rows),
            'scored_close': stats['scored'],
            'expectancy_close_pct': stats['mean_contract_ret_pct'],
            'positive': bool(stats['mean_contract_ret_pct'] is not None
                             and stats['mean_contract_ret_pct'] > 0),
        })
    two_pos = any(w['positive'] and nxt['positive']
                  for w, nxt in zip(windows, windows[1:]))
    meets = any(w['positive'] and nxt['positive']
                and w['scored_close'] + nxt['scored_close'] >= 40
                for w, nxt in zip(windows, windows[1:]))
    meets_each = any(w['positive'] and nxt['positive']
                     and w['scored_close'] >= 40 and nxt['scored_close'] >= 40
                     for w, nxt in zip(windows, windows[1:]))
    return {'windows': windows,
            'alerts_total': len(alerts),
            'scored_total': sum(w['scored_close'] for w in windows),
            'two_consecutive_positive': two_pos,
            'meets_bar': meets, 'meets_bar_each_window': meets_each}


def summarize(db, c, now, days=90):
    rows = [a for a in db.prefix(c, 'iceberg_alert:').values()
            if isinstance(a, dict) and (number(a.get('alert_ts')) or 0) >= now - days * 86400]
    rows.sort(key=lambda a: a.get('alert_ts') or 0)
    by_underlying = {}
    for a in rows:
        by_underlying.setdefault(a['underlying'], []).append(a)
    return {
        'alerts': len(rows),
        'horizons': {h: _horizon_stats(rows, h) for h in HORIZONS},
        'promotion': promotion_view(rows),
        'by_underlying': {
            u: {'alerts': len(v),
                'close': _horizon_stats(v, 'close'),
                'expiry': _horizon_stats(v, 'expiry')}
            for u, v in sorted(by_underlying.items())},
        'recent': rows[-20:],
    }


def status(db, c, now):
    rows = db.prefix(c, 'iceberg_alert:')
    return {
        'diag': db.get(c, 'iceberg:diag', {}),
        'health': db.get(c, 'health:iceberg_detector', {}),
        'alerts_total': len(rows),
        'alerts_open': sum(1 for a in rows.values()
                           if isinstance(a, dict) and a.get('status') == 'open'),
        'alerts_complete': sum(1 for a in rows.values()
                               if isinstance(a, dict) and a.get('status') == 'complete'),
    }
