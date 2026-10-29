"""Close Momentum: last-45-minute continuation of the RTH day's direction.

Basis: Gao, Han, Li & Zhou (2018), "Market intraday momentum" — the first
half-hour return predicts the last half-hour return. Re-tested by Claude on
the desk's `data-foundation-v1` year (2025-10-27 → 2026-10-26, Databento
1-min, five micros): same sign on all five instruments; economically viable
only at full size, and robust only on YM (147 trades, +13.9 ticks gross per
trade, t = 3.2, both calendar halves positive, max DD two weeks of P&L).
CL carried one quarter, ES is marginal, NQ is NEGATIVE at every variant.
Report: claude-exchange/from-claude/2026-10-27-futures-method-search.md.

Rule (all times America/Chicago, one trade per symbol per session):
  s1 = sign(09:00 open − 08:30 open)        first-30-minute direction
  s2 = sign(15:00 open − 08:30 open)        day-so-far direction
  If s1 == s2 ≠ 0 at 15:00: enter in that direction at market.
  Exit: the engine's 15:45 session flatten. No stop or target is part of
  the edge (tested; a stop inside the 45-minute hold only adds friction),
  but ict_paper.submit() requires both, so a wide protective bracket is
  attached: stop 0.25 × daily ATR(14) (≈ $740 on YM; hit once in the test
  year), target 0.5 × daily ATR(14) (never hit). Daily ATR comes from the
  'daily' bar store; when absent, a fixed per-root point table is used.

Economics at the desk fill rule (2 ticks + fees), test year:
  YM  +$55 / trade,  +$8.1k / yr, max DD −$1,115, worst day −$674.
  MYM +$2.90 / trade — fees eat it; 5 MYM ≈ $14.70 / trade.
  NQ/MNQ: do not trade (−$82 / trade on NQ).

Status: SHADOW / paper-only. Promotion is under the adopted policy
(≥ 50 Tier-1 broker fills, edge surviving p90 stop-slippage repricing).
Evidence only. This module never alerts, never trades live.
"""
from datetime import datetime

from .market import number, CT
from .ict_common import ict_bars

SCAN_THROTTLE = 30            # seconds between scans (the window is 5 min wide)
ENTRY_HOUR, ENTRY_MINUTE = 15, 0
ENTRY_WINDOW_S = 5 * 60       # fire only between 15:00:00 and 15:05:00 CT
ATR_LEN = 14
STOP_ATR = 0.25
TARGET_ATR = 0.50

# Symbols the signal is cleared to run on (paper). YM first; MYM is the
# same signal at 1/10th size for accounts without a full-size seat.
# Everything else is research-only until the promotion gate says otherwise.
SYMBOLS = ('YM.c.0', 'MYM.c.0')

# Fallback stop/target distances in points when daily bars are missing.
# ≈ 0.25 / 0.5 of the test year's median daily ATR for each root.
FALLBACK_POINTS = {
    'YM': (150.0, 300.0), 'MYM': (150.0, 300.0),
    'ES': (18.0, 36.0), 'MES': (18.0, 36.0),
    'CL': (0.60, 1.20), 'MCL': (0.60, 1.20),
    'GC': (30.0, 60.0), 'MGC': (30.0, 60.0),
}


def _root(symbol):
    return str(symbol or '').split('.')[0].upper()


def _ct(ts):
    return datetime.fromtimestamp(ts, CT)


def _session_ts(now, hour, minute):
    """Epoch of today's (CT calendar day of `now`) hour:minute."""
    d = _ct(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return d.timestamp()


def _open_at(bars, ts):
    """Open of the 1-min bar whose timestamp is exactly `ts`, else None."""
    for b in bars:
        if b['ts'] == ts:
            return number(b['o'])
        if b['ts'] > ts:
            break
    return None


def _sign(x):
    if x is None:
        return 0
    return (x > 0) - (x < 0)


def signal(bars, now):
    """Pure signal from 1-min bars. Returns {'direction', 's1', 's2', 'o830',
    'o900', 'o1500', 'entry'} or None.

    `bars` are ascending dicts with ts/o/h/l/c. `now` must fall inside the
    entry window [15:00, 15:05) CT; the 15:00 bar must exist (its open is
    known the moment it starts forming)."""
    t1500 = _session_ts(now, ENTRY_HOUR, ENTRY_MINUTE)
    if not (t1500 <= now < t1500 + ENTRY_WINDOW_S):
        return None
    o830 = _open_at(bars, _session_ts(now, 8, 30))
    o900 = _open_at(bars, _session_ts(now, 9, 0))
    o1500 = _open_at(bars, t1500)
    if None in (o830, o900, o1500):
        return None
    s1 = _sign(o900 - o830)
    s2 = _sign(o1500 - o830)
    if s1 == 0 or s1 != s2:
        return None
    return {'direction': 'long' if s1 > 0 else 'short', 's1': s1, 's2': s2,
            'o830': o830, 'o900': o900, 'o1500': o1500, 'entry': o1500}


def daily_atr(db, c, symbol, length=ATR_LEN):
    """ATR(length) from the 'daily' bar store (prior sessions only). None if short."""
    try:
        rows = db.recent(c, 'daily', symbol, limit=length + 5)
    except Exception:
        return None
    bars = []
    for r in rows or []:
        p = r.get('payload') or {}
        h, l, cl = number(p.get('h')), number(p.get('l')), number(p.get('c'))
        if None in (h, l, cl):
            continue
        bars.append((r['ts'], h, l, cl))
    bars = sorted(set(bars))
    if len(bars) < length + 1:
        return None
    bars = bars[-(length + 1):]
    trs = []
    for i in range(1, len(bars)):
        _, h, l, _ = bars[i]
        pc = bars[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def trade_levels(sig, atr_pts, root):
    """Attach the protective bracket. Returns submit()-shaped levels or None."""
    entry = number(sig.get('entry'))
    if not entry:
        return None
    if atr_pts and atr_pts > 0:
        stop_d, tgt_d = STOP_ATR * atr_pts, TARGET_ATR * atr_pts
        basis = 'daily_atr'
    else:
        fb = FALLBACK_POINTS.get(root)
        if not fb:
            return None
        stop_d, tgt_d = fb
        basis = 'fallback_points'
    d = 1 if sig['direction'] == 'long' else -1
    return {'direction': sig['direction'], 'entry': entry,
            'stop': entry - d * stop_d, 'target': entry + d * tgt_d,
            'target_kind': 'protective_only', 'stop_basis': basis,
            'risk_pts': stop_d, 'reward_pts': tgt_d,
            'level_kind': 'close_momentum', 'level_price': sig['o830'],
            's1': sig['s1'], 's2': sig['s2'],
            'o830': sig['o830'], 'o900': sig['o900'], 'o1500': sig['o1500']}


def scan(db, c, cfg, now):
    """Throttled close-momentum scan. One submission per symbol per session."""
    if not getattr(cfg, 'ict_close_momentum', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'ict_close_momentum:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    t1500 = _session_ts(now, ENTRY_HOUR, ENTRY_MINUTE)
    if not (t1500 <= now < t1500 + ENTRY_WINDOW_S):
        db.put(c, 'ict_close_momentum:scanned_at', now)
        return {'ran': False, 'reason': 'outside_window'}
    symbols = tuple(getattr(cfg, 'ict_close_momentum_symbols', ()) or SYMBOLS)
    day_iso = _ct(now).date().isoformat()
    fired = 0
    signals = 0
    declined = 0
    for symbol in symbols:
        seen_key = 'ict_close_momentum:fired:%s:%s' % (symbol, day_iso)
        if db.get(c, seen_key):
            continue
        bars = ict_bars(db, c, symbol)
        if len(bars) < 60:
            continue
        sig = signal(bars, now)
        if not sig:
            # No-signal days are recorded once so the fire-rate audit can
            # distinguish "skipped by rule" from "never evaluated".
            if _open_at(bars, t1500) is not None:
                db.put(c, seen_key, {'at': now, 'no_signal': True})
            continue
        signals += 1
        levels = trade_levels(sig, daily_atr(db, c, symbol), _root(symbol))
        if not levels:
            continue
        from .ict_paper import submit as _paper_submit
        res = _paper_submit(db, c, cfg, now, 'close_momentum', symbol, levels)
        if res.get('submitted'):
            fired += 1
            db.put(c, seen_key, {'at': now, 'submitted': True})
            db.append(c, 'ict_close_momentum_signal', 'ict_futures', symbol,
                      now, dict(levels),
                      key='ictcm:%s:%s' % (symbol, day_iso))
        else:
            declined += 1
            # Decline reasons that cannot clear today (market_closed, past
            # flatten, symbol gate) are terminal for the day; transient ones
            # (quote freshness, existing position) get retried inside the
            # 5-minute window by leaving seen_key unset.
            if res.get('reason') in ('symbol_not_allowed', 'paper_disabled',
                                     'market_closed', 'past_flatten',
                                     'unknown_detector', 'not_a_future',
                                     'bad_signal', 'levels_inverted'):
                db.put(c, seen_key, {'at': now, 'declined': res.get('reason')})
            try:
                from .ict_declines import record_decline as _record_decline
                _record_decline(db, c, now, 'close_momentum', symbol, res,
                                {**levels, 'signal_ts': t1500})
            except Exception:
                pass
    db.put(c, 'ict_close_momentum:scanned_at', now)
    db.put(c, 'ict_close_momentum:latest', {'at': now, 'fired': fired,
                                            'signals': signals,
                                            'declined': declined})
    return {'ran': True, 'fired': fired, 'signals': signals,
            'declined': declined}


def display(db, c, now, limit=50):
    latest = db.get(c, 'ict_close_momentum:latest', {})
    rows = db.recent(c, 'ict_close_momentum_signal', limit=limit)
    return {'asof': latest.get('at'), 'fired': latest.get('fired', 0),
            'symbols': list(SYMBOLS),
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'], **s['payload']}
                        for s in rows],
            'note': 'Close Momentum (first-30 → last-45). Shadow / paper only.'}
