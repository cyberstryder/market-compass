"""Rumers Box: prior-day RTH high/low open-reaction system.

Box = prior trading day's RTH (09:30-16:00 ET) high/low. In the first
30 minutes after the next open, trade the reaction:
  CONTINUATION: two consecutive closes beyond a box edge (open may gap
    beyond the edge already) -> trade the break direction.
  REVERSAL: a spike beyond a box edge that reclaims back inside (false
    breakout) -> trade back toward the opposite edge.
First signal of the day per symbol wins.

CONFIRMATION: volume above the 20-bar average OR price on the
direction-consistent side of session VWAP (at least one must hold).
VWAP/ATR/volume are computed from bars known at the trigger bar only —
a later scan never lets the rest of the day rewrite confirmation.

Stop: continuation -> beyond the broken box edge; reversal -> beyond the
spike extreme (both plus STOP_BUF_ATR_FRAC x ATR).
Target: continuation -> 2R; reversal -> closer of 2R and the opposite box
edge, requiring >= MIN_TARGET_R (1.5) else the setup is skipped.

Evidence only. No alerts. Paper fills via ict_paper only when both
ICT_FUTURES_PAPER_ENABLED and the detector flag are on.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .market import number, day
from .ict_common import ict_bars, atr, session_vwap, _bars_from_recent
from .instruments import future_root
from .ict_paper import submit as _paper_submit

NY = ZoneInfo("America/New_York")

SCAN_THROTTLE = 120
SUPPORTED_ROOTS = ('NQ', 'YM')
WINDOW_MINUTES = 30
VOL_LOOKBACK = 20
STOP_BUF_ATR_FRAC_DEFAULT = 0.25
RR_DEFAULT = 2.0
MIN_TARGET_R_DEFAULT = 1.5
ATR_PERIOD = 14
BAR_LIMIT = 10000
MIN_RTH_BARS = 200


def _ny(ts):
    return datetime.fromtimestamp(ts, NY)


def rth_sessions(bars):
    """1-min-ish bars grouped by NY date, RTH (09:30-16:00 ET) only."""
    by_day = {}
    for b in bars:
        d = _ny(b['ts'])
        if d.hour < 9 or (d.hour == 9 and d.minute < 30):
            continue
        if d.hour >= 16:
            continue
        by_day.setdefault(d.date().isoformat(), []).append(b)
    return by_day


def prior_box(bars, today):
    """(box_hi, box_lo, box_date) from the most recent complete prior RTH
    session. None when unavailable."""
    for d in sorted(rth_sessions(bars), reverse=True):
        if d >= today:
            continue
        sess = rth_sessions(bars)[d]
        if len(sess) < MIN_RTH_BARS:
            continue
        return max(b['h'] for b in sess), min(b['l'] for b in sess), d
    return None


def in_window(ts):
    """True for RTH bars in the first 30 minutes after the 09:30 ET open."""
    d = _ny(ts)
    return d.hour == 9 and 30 <= d.minute < 30 + WINDOW_MINUTES


def detect_signal(bars, box_hi, box_lo, today,
                  stop_buf_atr_frac=STOP_BUF_ATR_FRAC_DEFAULT,
                  rr=RR_DEFAULT, min_target_r=MIN_TARGET_R_DEFAULT,
                  vwap_min_bars=20):
    """First-30-min open reaction. Returns signal dict or None.

    Pure function (no db). VWAP and ATR are computed from bars available at
    the trigger bar only (no lookahead): a later scan must not let the rest
    of the day's volume/prices rewrite the confirmation.
    """
    sess = rth_sessions(bars).get(today)
    if not sess:
        return None
    win = [b for b in sess if in_window(b['ts'])]
    if len(win) < 3:
        return None
    setup = None
    for i in range(1, len(win)):
        b, prev = win[i], win[i - 1]
        # continuation: two consecutive closes beyond an edge
        if b['c'] > box_hi and prev['c'] > box_hi:
            setup = ('continuation', 'long', b, box_hi)
            break
        if b['c'] < box_lo and prev['c'] < box_lo:
            setup = ('continuation', 'short', b, box_lo)
            break
        # reversal: spike beyond the edge, close back inside
        if b['h'] > box_hi and b['c'] < box_hi:
            setup = ('reversal', 'short', b, b['h'])
            break
        if b['l'] < box_lo and b['c'] > box_lo:
            setup = ('reversal', 'long', b, b['l'])
            break
    if setup is None:
        return None
    kind, direction, trig, ref = setup
    entry = trig['c']
    # only bars known at the trigger bar feed confirmation and sizing
    known = [b for b in bars if b['ts'] <= trig['ts']]
    atr_v = atr(known[-60:], ATR_PERIOD) if len(known) >= ATR_PERIOD + 1 else None
    buf = (stop_buf_atr_frac or 0.0) * (atr_v or 0.0)
    # confirmation: volume above 20-bar average OR VWAP alignment
    hist_v = [b.get('v') or 0 for b in known[:-1]]
    avg_v = (sum(hist_v[-VOL_LOOKBACK:]) / VOL_LOOKBACK
             if len(hist_v) >= VOL_LOOKBACK else 0)
    vol_ok = bool(avg_v and (trig.get('v') or 0) > avg_v)
    vwap = session_vwap(known, trig['ts'], min_bars=vwap_min_bars)
    vwap = number(vwap)
    vwap_ok = bool(vwap and ((direction == 'long' and entry >= vwap) or
                             (direction == 'short' and entry <= vwap)))
    if not (vol_ok or vwap_ok):
        return None
    if direction == 'long':
        stop = ref - buf  # continuation: broken edge; reversal: spike extreme
        risk = entry - stop
    else:
        stop = ref + buf
        risk = stop - entry
    if risk is None or risk <= 0:
        return None
    if kind == 'continuation':
        target, target_kind = (entry + rr * risk, 'rr') \
            if direction == 'long' else (entry - rr * risk, 'rr')
    else:
        opp = box_lo if direction == 'short' else box_hi
        opp_dist = (entry - opp) if direction == 'short' else (opp - entry)
        if opp_dist is not None and opp_dist > 0 and opp_dist < rr * risk:
            if opp_dist < min_target_r * risk:
                return None
            target = entry - opp_dist if direction == 'short' \
                else entry + opp_dist
            target_kind = 'opposite_edge'
        else:
            target = entry - rr * risk if direction == 'short' \
                else entry + rr * risk
            target_kind = 'rr'
    reward = abs(target - entry)
    return {'direction': direction, 'signal_ts': trig['ts'],
            'level_price': (box_hi + box_lo) / 2.0, 'level_kind': 'rumers_box',
            'setup': kind, 'box_hi': box_hi, 'box_lo': box_lo,
            'entry': entry, 'stop': stop, 'target': target,
            'target_kind': target_kind, 'risk_pts': risk,
            'reward_pts': reward, 'rr': reward / risk,
            'vol_ok': vol_ok, 'vwap_ok': vwap_ok, 'vwap': vwap, 'atr': atr_v}


def scan(db, c, cfg, now):
    """Throttled Rumers Box scan over supported symbols."""
    if not getattr(cfg, 'ict_rumers_box', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'rumers_box:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    stop_buf = number(getattr(cfg, 'ict_rb_stop_buf_atr_frac',
                              STOP_BUF_ATR_FRAC_DEFAULT))
    if stop_buf is None:
        stop_buf = STOP_BUF_ATR_FRAC_DEFAULT
    rr = number(getattr(cfg, 'ict_rb_rr', RR_DEFAULT)) or RR_DEFAULT
    min_tr = number(getattr(cfg, 'ict_rb_min_target_r', MIN_TARGET_R_DEFAULT))
    if min_tr is None:
        min_tr = MIN_TARGET_R_DEFAULT
    try:
        vwap_min = int(number(getattr(cfg, 'ict_rb_vwap_min_bars', 20)) or 20)
    except (TypeError, ValueError):
        vwap_min = 20
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ())
               if future_root(s) in SUPPORTED_ROOTS]
    today = day(now)
    rows, excluded = {}, {}
    n_signals = 0
    for symbol in symbols:
        window = ict_bars(db, c, symbol, limit=BAR_LIMIT)
        bars = window or _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))
        if not bars:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_bars', 'at': now}
            continue
        box = prior_box(bars, today)
        if box is None:
            excluded['no_prior_box'] = excluded.get('no_prior_box', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_prior_box',
                            'at': now}
            continue
        box_hi, box_lo, box_date = box
        base = {'symbol': symbol, 'concept': 'rumers_box', 'at': now,
                'box_hi': box_hi, 'box_lo': box_lo, 'box_date': box_date}
        if (db.get(c, 'rumers_box:traded:' + symbol, {}) or {}).get('date') == today:
            excluded['already_traded_today'] = \
                excluded.get('already_traded_today', 0) + 1
            rows[symbol] = {**base, 'excluded': 'already_traded_today'}
            continue
        vwap = session_vwap(bars, now, min_bars=vwap_min)
        sig = detect_signal(bars, box_hi, box_lo, today,
                            stop_buf_atr_frac=stop_buf, rr=rr,
                            min_target_r=min_tr, vwap_min_bars=vwap_min)
        if sig is None:
            excluded['no_setup'] = excluded.get('no_setup', 0) + 1
            rows[symbol] = {**base, 'excluded': 'no_setup', 'vwap': number(vwap)}
            continue
        row = {**base, **sig}
        rows[symbol] = row
        payload = {k: v for k, v in row.items() if k != 'at'}
        db.append(c, 'rumers_box_signal', 'yt_methods', symbol, now,
                  payload, key='rb:%s:%s' % (symbol, today))
        db.put(c, 'rumers_box:traded:' + symbol,
               {'date': today, 'signal_ts': sig['signal_ts'],
                'setup': sig['setup'], 'direction': sig['direction'], 'at': now})
        _paper_submit(db, c, cfg, now, 'rumers_box', symbol, sig)
        n_signals += 1
    db.put(c, 'rumers_box:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'params': {'stop_buf_atr_frac': stop_buf, 'rr': rr,
                       'min_target_r': min_tr, 'vwap_min_bars': vwap_min},
            'status': 'running' if rows else 'idle'})
    db.put(c, 'rumers_box:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent box signals."""
    latest = db.get(c, 'rumers_box:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'rumers_box_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only. No auto-admission, no alerts; '
                    'simulated paper fills only when ICT_FUTURES_PAPER_ENABLED '
                    'is on. No live orders.'}
