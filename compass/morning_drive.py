"""Morning-drive fade (Riley Coleman 5-step checklist, mechanical skeleton).

Fade an "unhealthy" opening drive on NQ futures:
  1. Drive = RTH open (09:30 ET) to session extreme. Unhealthy when the drive
     covers >= DRIVE_FRAC (0.50) of the trailing AVG_DAYS (14) average RTH
     daily range.
  2. Entry window 09:40-10:30 ET. Trigger = a bar closes beyond the prior
     2-bar extreme against the drive (momentum reclaim): for an up-drive,
     close < min(low[i-1], low[i-2]) -> short; for a down-drive,
     close > max(high[i-1], high[i-2]) -> long. First trigger in the window wins.
  3. Stop beyond the session (drive) extreme. Target fixed 2R.
  4. Time stop: flat at 12:00 ET (implemented via flatten_at on the paper
     position; the engine exits() loop session-flattens it).
  5. Max 1 trade per day per symbol.

CONVENTION NOTES:
- "Daily range" is the RTH (09:30-16:00 ET) high-low range, averaged over
  the prior AVG_DAYS (14) complete sessions (needs all 14).
- The drive is measured open-to-extreme over RTH bars **up to the trigger
  bar only** (anti-lookahead): a scan run later in the day must not let
  post-trigger highs/lows rewrite the drive extreme or the stop.
- Stop sits exactly at the as-of-trigger drive extreme (no buffer) per the
  literal checklist; STOP_BUF_ATR_FRAC exists as a knob (default 0.0).
- The independent backtest of this mechanical skeleton (Ground Truth A13)
  lost money net of costs; the trader attributes edge to discretionary
  overlays NOT modeled here. Evidence only.

Evidence only. No alerts. Paper fills via ict_paper only when both
ICT_FUTURES_PAPER_ENABLED and the detector flag are on.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .market import number, day
from .ict_common import ict_bars, atr, _bars_from_recent
from .instruments import future_root
from .ict_paper import submit as _paper_submit

NY = ZoneInfo("America/New_York")

SCAN_THROTTLE = 120
SUPPORTED_ROOTS = ('NQ',)
DRIVE_FRAC_DEFAULT = 0.50
AVG_DAYS_DEFAULT = 14
MIN_HISTORY_SESSIONS_DEFAULT = 14  # spec: 14-day average, all 14 required
WINDOW_START_DEFAULT = (9, 40)
WINDOW_END_DEFAULT = (10, 30)
TIME_STOP_DEFAULT = (12, 0)
RR_DEFAULT = 2.0
STOP_BUF_ATR_FRAC_DEFAULT = 0.0
ATR_PERIOD = 14
BAR_LIMIT = 30000


def _ny(ts):
    return datetime.fromtimestamp(ts, NY)


def _rth_bars(bars):
    """Bars within 09:30-16:00 ET, grouped by NY date."""
    by_day = {}
    for b in bars:
        d = _ny(b['ts'])
        if d.hour < 9 or (d.hour == 9 and d.minute < 30):
            continue
        if d.hour >= 16:
            continue
        by_day.setdefault(d.date().isoformat(), []).append(b)
    return by_day


def avg_rth_range(bars, avg_days=AVG_DAYS_DEFAULT,
                  min_sessions=MIN_HISTORY_SESSIONS_DEFAULT, today=None):
    """Mean RTH high-low range over prior sessions. None when insufficient."""
    by_day = _rth_bars(bars)
    ranges = []
    for d in sorted(by_day):
        if today and d >= today:
            continue
        sess = by_day[d]
        if len(sess) < 200:  # incomplete session (1-min bars ~390/day)
            continue
        ranges.append(max(b['h'] for b in sess) - min(b['l'] for b in sess))
    ranges = ranges[-avg_days:]
    if len(ranges) < min_sessions:
        return None
    return sum(ranges) / len(ranges)


def drive_upto(sess, upto_idx, avg_range, drive_frac=DRIVE_FRAC_DEFAULT):
    """Opening-drive state using only sess[0..upto_idx] (anti-lookahead).

    `sess` is one RTH session's bars in ascending order. Direction is up
    when the open-to-high excursion dominates, else down.
    """
    sub = sess[:upto_idx + 1]
    o = sub[0]['o']
    hi = max(b['h'] for b in sub)
    lo = min(b['l'] for b in sub)
    if hi - o >= o - lo:
        direction, extreme = 'up', hi
    else:
        direction, extreme = 'down', lo
    drive_range = abs(extreme - o)
    unhealthy = bool(avg_range and avg_range > 0
                     and drive_range >= drive_frac * avg_range)
    return {'direction': direction, 'extreme': extreme, 'open': o,
            'drive_range': drive_range, 'avg_range': avg_range,
            'unhealthy': unhealthy}


def drive_state(bars, today, avg_range, drive_frac=DRIVE_FRAC_DEFAULT):
    """Opening-drive assessment for `today` (NY date str).

    Convenience wrapper: drive over the whole available session. Prefer
    find_trigger() for signal work (it evaluates the drive as of each
    candidate trigger bar instead).
    """
    sess = _rth_bars(bars).get(today)
    if not sess:
        return None
    return drive_upto(sess, len(sess) - 1, avg_range, drive_frac)


def in_window(ts, start=WINDOW_START_DEFAULT, end=WINDOW_END_DEFAULT):
    d = _ny(ts)
    t = (d.hour, d.minute)
    return (start[0], start[1]) <= t <= (end[0], end[1])


def find_trigger(sess, avg_range, drive_frac=DRIVE_FRAC_DEFAULT,
                 start=WINDOW_START_DEFAULT, end=WINDOW_END_DEFAULT):
    """Sequential scan: first window bar where the drive-so-far is unhealthy
    AND the momentum-reclaim trigger fires against it.

    Against an up-drive: close < min(low[i-1], low[i-2]) -> short.
    Against a down-drive: close > max(high[i-1], high[i-2]) -> long.

    Returns (trig_idx, direction, drive, ever_unhealthy) where drive is the
    as-of-trigger drive state and ever_unhealthy reports whether the drive
    was unhealthy at any window bar (lets the caller distinguish
    drive_too_small from no_trigger).
    """
    ever_unhealthy = False
    for i in range(2, len(sess)):
        b = sess[i]
        if not in_window(b['ts'], start, end):
            continue
        drive = drive_upto(sess, i, avg_range, drive_frac)
        if not drive['unhealthy']:
            continue
        ever_unhealthy = True
        if drive['direction'] == 'up':
            if b['c'] < min(sess[i - 1]['l'], sess[i - 2]['l']):
                return i, 'short', drive, True
        else:
            if b['c'] > max(sess[i - 1]['h'], sess[i - 2]['h']):
                return i, 'long', drive, True
    return None, None, None, ever_unhealthy


def detect_trigger(bars, today, drive,
                   start=WINDOW_START_DEFAULT, end=WINDOW_END_DEFAULT):
    """First momentum-reclaim trigger inside the entry window.

    Legacy wrapper kept for compatibility: evaluates the trigger against a
    precomputed full-session `drive`. New code should use find_trigger(),
    which evaluates the drive as of each candidate bar (no lookahead).
    """
    sess = _rth_bars(bars).get(today)
    if not sess or len(sess) < 3:
        return None, None
    fade = 'short' if drive['direction'] == 'up' else 'long'
    for i in range(2, len(sess)):
        b = sess[i]
        if not in_window(b['ts'], start, end):
            continue
        if fade == 'short':
            if b['c'] < min(sess[i - 1]['l'], sess[i - 2]['l']):
                return i, 'short'
        else:
            if b['c'] > max(sess[i - 1]['h'], sess[i - 2]['h']):
                return i, 'long'
    return None, None


def build_signal(sess, trig_idx, direction, drive,
                 rr=RR_DEFAULT, stop_buf_atr_frac=STOP_BUF_ATR_FRAC_DEFAULT,
                 time_stop=TIME_STOP_DEFAULT):
    """Entry/stop/target/2R from the trigger bar."""
    trig = sess[trig_idx]
    entry = trig['c']
    atr_v = atr(sess[:trig_idx + 1], ATR_PERIOD)
    buf = (stop_buf_atr_frac or 0.0) * (atr_v or 0.0)
    risk_dist = None
    if direction == 'short':
        stop = drive['extreme'] + buf
        risk = stop - entry
    else:
        stop = drive['extreme'] - buf
        risk = entry - stop
    if risk is None or risk <= 0:
        return None
    target = entry - rr * risk if direction == 'short' else entry + rr * risk
    d = _ny(trig['ts'])
    flat = d.replace(hour=time_stop[0], minute=time_stop[1],
                     second=0, microsecond=0)
    return {'direction': direction, 'signal_ts': trig['ts'],
            'level_price': entry, 'level_kind': 'morning_drive_fade',
            'drive_direction': drive['direction'],
            'drive_extreme': drive['extreme'], 'rth_open': drive['open'],
            'drive_range': drive['drive_range'], 'avg_range': drive['avg_range'],
            'entry': entry, 'stop': stop, 'target': target,
            'risk_pts': risk, 'reward_pts': rr * risk, 'rr': rr,
            'flatten_at': flat.timestamp(),
            'atr': atr_v}


def _window_end_idx(sess, end=WINDOW_END_DEFAULT):
    """Index of the last session bar at or before the entry-window end."""
    idx = None
    for i, b in enumerate(sess):
        d = _ny(b['ts'])
        if (d.hour, d.minute) <= (end[0], end[1]):
            idx = i
    return idx


def scan(db, c, cfg, now):
    """Throttled morning-drive-fade scan over supported symbols."""
    if not getattr(cfg, 'ict_morning_drive', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'morning_drive:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    drive_frac = number(getattr(cfg, 'ict_md_drive_frac', DRIVE_FRAC_DEFAULT)) \
        or DRIVE_FRAC_DEFAULT
    avg_days = int(number(getattr(cfg, 'ict_md_avg_days', AVG_DAYS_DEFAULT))
                   or AVG_DAYS_DEFAULT)
    rr = number(getattr(cfg, 'ict_md_rr', RR_DEFAULT)) or RR_DEFAULT
    stop_buf = number(getattr(cfg, 'ict_md_stop_buf_atr_frac',
                              STOP_BUF_ATR_FRAC_DEFAULT))
    if stop_buf is None:
        stop_buf = STOP_BUF_ATR_FRAC_DEFAULT
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
        avg_range = avg_rth_range(bars, avg_days=avg_days, today=today)
        if avg_range is None:
            excluded['insufficient_history'] = \
                excluded.get('insufficient_history', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'insufficient_history',
                            'at': now}
            continue
        sess = _rth_bars(bars).get(today)
        if not sess:
            excluded['no_rth_open'] = excluded.get('no_rth_open', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_rth_open', 'at': now}
            continue
        trig_idx, direction, drive, ever_unhealthy = find_trigger(
            sess, avg_range, drive_frac=drive_frac)
        # Snapshot for display: as-of-trigger when one fired, else as of the
        # entry-window end (never later bars — no lookahead).
        snap = drive
        if snap is None:
            widx = _window_end_idx(sess)
            snap = drive_upto(sess, widx if widx is not None else len(sess) - 1,
                              avg_range, drive_frac)
        base = {'symbol': symbol, 'concept': 'morning_drive', 'at': now,
                'drive_direction': snap['direction'],
                'drive_range': snap['drive_range'], 'avg_range': avg_range,
                'unhealthy': snap['unhealthy']}
        if not ever_unhealthy:
            excluded['drive_too_small'] = excluded.get('drive_too_small', 0) + 1
            rows[symbol] = {**base, 'excluded': 'drive_too_small'}
            continue
        if (db.get(c, 'morning_drive:traded:' + symbol, {}) or {}).get('date') == today:
            excluded['already_traded_today'] = \
                excluded.get('already_traded_today', 0) + 1
            rows[symbol] = {**base, 'excluded': 'already_traded_today'}
            continue
        sess = _rth_bars(bars)[today]
        if trig_idx is None:
            excluded['no_trigger'] = excluded.get('no_trigger', 0) + 1
            rows[symbol] = {**base, 'excluded': 'no_trigger'}
            continue
        sig = build_signal(sess, trig_idx, direction, drive, rr=rr,
                           stop_buf_atr_frac=stop_buf)
        if sig is None:
            excluded['bad_levels'] = excluded.get('bad_levels', 0) + 1
            rows[symbol] = {**base, 'excluded': 'bad_levels'}
            continue
        row = {**base, **sig}
        rows[symbol] = row
        payload = {k: v for k, v in row.items() if k != 'at'}
        db.append(c, 'morning_drive_signal', 'yt_methods', symbol, now,
                  payload, key='md:%s:%s' % (symbol, today))
        db.put(c, 'morning_drive:traded:' + symbol,
               {'date': today, 'signal_ts': sig['signal_ts'],
                'direction': direction, 'at': now})
        _paper_submit(db, c, cfg, now, 'morning_drive', symbol, sig,
                      flatten_at=sig['flatten_at'])
        n_signals += 1
    db.put(c, 'morning_drive:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'params': {'drive_frac': drive_frac, 'avg_days': avg_days,
                       'rr': rr, 'stop_buf_atr_frac': stop_buf},
            'status': 'running' if rows else 'idle'})
    db.put(c, 'morning_drive:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent fade signals."""
    latest = db.get(c, 'morning_drive:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'morning_drive_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only (mechanical skeleton of a '
                    'discretionary method; independent backtest of the '
                    'skeleton was negative). No auto-admission, no alerts; '
                    'simulated paper fills only when ICT_FUTURES_PAPER_ENABLED '
                    'is on. No live orders.'}
