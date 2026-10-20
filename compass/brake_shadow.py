"""Shadow evaluation of candidate futures brakes (Josh 2026-10-20).

After the 2026-10-20 bleed day (+$700 -> -$100 intraday on the live book),
a trade-stream replay of candidate guardrails found two survivors:

  1. strike3_0830 — a detector stands down after 3 consecutive realized
     losses, until the next 08:30 CT open. (The scope is Josh's: overnight
     strikes must not bench the NY session, so strikes are only counted
     from exits at/after the most recent 08:30 CT anchor.)
  2. corr_cap1 — at most one open position per correlation group per
     direction (equity indices MES/MNQ/MYM/M2K; currencies M6A/M6E;
     MGC/MCL standalone).

This module SHADOWS those brakes: on every submitted ICT paper trade it
records what the brakes would have done, plus entry-context marks (each
open position's unrealized R, the signal symbol's session-VWAP relation)
needed to test two further ideas later (win-conditioned correlation cap,
regime fade gate). It NEVER blocks, alters, or delays a trade. Anything
that goes wrong here is swallowed by the caller and logged to health.

Records live under `brake_shadow:<trade_id>` (the t1 leg id). Scoring
joins each record to its trade's realized outcome: a would-blocked trade
that was taken anyway and lost is a loss the brake would have avoided;
one that won is a win it would have missed.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .market import number

CT = ZoneInfo("America/Chicago")

# Same correlation groups as the TradersPost hedging guard in ict_paper.
GROUPS = {
    'MES': 'EQUITY', 'MNQ': 'EQUITY', 'MYM': 'EQUITY', 'M2K': 'EQUITY',
    'M6A': 'CURRENCY', 'M6E': 'CURRENCY',
}

STRIKE_LIMIT = 3


def root_of(symbol):
    return str(symbol or '').split('.')[0].upper()


def group_of(symbol):
    return GROUPS.get(root_of(symbol))


def last_0830_anchor(now):
    """Epoch ts of the most recent 08:30 CT at or before `now`."""
    d = datetime.fromtimestamp(now, CT)
    anchor = d.replace(hour=8, minute=30, second=0, microsecond=0)
    if d < anchor:
        anchor -= timedelta(days=1)
    return anchor.timestamp()


def session_start_1700(now):
    """Epoch ts of the most recent 17:00 CT (Globex open) at or before now."""
    d = datetime.fromtimestamp(now, CT)
    start = d.replace(hour=17, minute=0, second=0, microsecond=0)
    if d < start:
        start -= timedelta(days=1)
    return start.timestamp()


def strike_state(db, c, detector, now):
    """Consecutive realized losses for `detector` since the 08:30 anchor.

    Counts closed ICT trades in exit order; a win (pnl > 0) or a scratch
    (pnl == 0) breaks the streak. data_gap trades never count (Josh's
    rule: missing-data trades are not wins or losses).
    """
    anchor = last_0830_anchor(now)
    closed = []
    for key, trade in db.prefix(c, 'trade:ict-%s-' % detector).items():
        if not isinstance(trade, dict):
            continue
        if trade.get('status') != 'closed' or trade.get('data_gap'):
            continue
        exited = number(trade.get('exited_at'))
        if not exited or exited < anchor:
            continue
        closed.append((exited, number(trade.get('pnl')) or 0.0))
    closed.sort()
    strikes = 0
    for _exited, pnl in reversed(closed):
        if pnl < 0:
            strikes += 1
        else:
            break
    return {'strikes': strikes, 'would_block': strikes >= STRIKE_LIMIT,
            'anchor': anchor, 'closed_since_anchor': len(closed)}


def corr_state(db, c, symbol, side):
    """Open ICT positions in the signal's correlation group + direction."""
    group = group_of(symbol)
    blockers = []
    if group:
        for key, pos in db.prefix(c, 'position:ict:').items():
            if not isinstance(pos, dict) or pos.get('status') != 'open':
                continue
            if group_of(pos.get('symbol')) != group:
                continue
            if str(pos.get('side') or '').lower() != str(side).lower():
                continue
            blockers.append({'detector': pos.get('detector'),
                             'symbol': pos.get('symbol'),
                             'side': pos.get('side'),
                             'trade_id': pos.get('id')})
    return {'group': group, 'would_block': bool(blockers),
            'blockers': blockers}


def open_marks(db, c):
    """Snapshot of open ICT positions with unrealized R at the last bar close.

    R is measured against each position's own initial stop distance, so a
    mark of +1.0 means the position has run one full risk unit in favor —
    the data the win-conditioned correlation cap needs.
    """
    from .ict_common import ict_bars
    marks = []
    last_close = {}
    for key, pos in db.prefix(c, 'position:ict:').items():
        if not isinstance(pos, dict) or pos.get('status') != 'open':
            continue
        sym = pos.get('symbol')
        entry = number(pos.get('entry'))
        stop = number(pos.get('stop'))
        mark = None
        if sym not in last_close:
            last_close[sym] = None
            try:
                bars = ict_bars(db, c, sym)
                if bars:
                    last_close[sym] = number(bars[-1].get('c'))
            except Exception:
                last_close[sym] = None
        mark = last_close[sym]
        unr = None
        if mark and entry and stop and entry != stop:
            dirn = 1 if str(pos.get('side')).lower() == 'long' else -1
            unr = round(dirn * (mark - entry) / abs(entry - stop), 3)
        marks.append({'detector': pos.get('detector'), 'symbol': sym,
                      'side': pos.get('side'), 'entry': entry,
                      'stop': stop, 'mark': mark, 'unrealized_r': unr,
                      'trade_id': pos.get('id')})
    return marks


def vwap_context(db, c, symbol, now, atr_v):
    """Session VWAP (from the 17:00 CT Globex open) and price relation."""
    from .ict_common import ict_bars
    out = {'vwap': None, 'last_close': None, 'dist_atr': None,
           'above_vwap': None}
    try:
        bars = ict_bars(db, c, symbol)
    except Exception:
        return out
    if not bars:
        return out
    start = session_start_1700(now)
    win = [b for b in bars if number(b.get('ts')) and b['ts'] >= start]
    if not win:
        win = bars[-60:]
    num = 0.0
    den = 0.0
    for b in win:
        tp = (number(b.get('h')) + number(b.get('l')) + number(b.get('c'))) / 3.0
        vol = number(b.get('v')) or number(b.get('volume')) or 0.0
        if vol > 0:
            num += tp * vol
            den += vol
        else:
            num += tp
            den += 1.0
    if den <= 0:
        return out
    vwap = num / den
    last_close = number(win[-1].get('c'))
    out['vwap'] = round(vwap, 4)
    out['last_close'] = last_close
    out['above_vwap'] = bool(last_close and last_close >= vwap)
    if atr_v:
        out['dist_atr'] = round((last_close - vwap) / atr_v, 3)
    return out


def evaluate(db, c, now, detector, symbol, side, atr_v=None):
    """Full shadow record for one submitted signal. Read-only."""
    strikes = strike_state(db, c, detector, now)
    corr = corr_state(db, c, symbol, side)
    return {
        'ts': now,
        'detector': detector,
        'symbol': symbol,
        'side': side,
        'strike3_0830': strikes,
        'corr_cap1': corr,
        'open_marks': open_marks(db, c),
        'vwap': vwap_context(db, c, symbol, now, atr_v),
    }


def record(db, c, shadow, trade_id, traderspost_sent=None):
    shadow = dict(shadow)
    shadow['trade_id'] = trade_id
    if traderspost_sent is not None:
        shadow['traderspost_sent'] = bool(traderspost_sent)
    db.put(c, 'brake_shadow:' + trade_id, shadow)
    return shadow


def summarize(db, c, since_ts):
    """Aggregate shadow records against realized trade outcomes.

    Per brake: would-blocked trades that closed — wins missed and losses
    avoided in dollars — plus still-open (pending) and not-taken counts.
    """
    def _blank():
        return {'would_block': 0, 'closed': 0, 'wins_missed': 0.0,
                'losses_avoided': 0.0, 'pending': 0}
    out = {'since': since_ts, 'records': 0, 'trades_closed': 0,
           'baseline_pnl': 0.0,
           'strike3_0830': _blank(), 'corr_cap1': _blank()}
    for key, rec in db.prefix(c, 'brake_shadow:').items():
        if not isinstance(rec, dict) or (number(rec.get('ts')) or 0) < since_ts:
            continue
        out['records'] += 1
        trade = db.get(c, 'trade:' + str(rec.get('trade_id')), {}) or {}
        status = trade.get('status')
        pnl = number(trade.get('pnl'))
        if status == 'closed' and pnl is not None:
            out['trades_closed'] += 1
            out['baseline_pnl'] += pnl
        for brake in ('strike3_0830', 'corr_cap1'):
            b = rec.get(brake) or {}
            if not b.get('would_block'):
                continue
            agg = out[brake]
            agg['would_block'] += 1
            if status == 'closed' and pnl is not None:
                agg['closed'] += 1
                if pnl > 0:
                    agg['wins_missed'] += pnl
                elif pnl < 0:
                    agg['losses_avoided'] += -pnl
            elif status == 'open':
                agg['pending'] += 1
    for brake in ('strike3_0830', 'corr_cap1'):
        agg = out[brake]
        agg['wins_missed'] = round(agg['wins_missed'], 2)
        agg['losses_avoided'] = round(agg['losses_avoided'], 2)
        agg['net'] = round(agg['losses_avoided'] - agg['wins_missed'], 2)
    out['baseline_pnl'] = round(out['baseline_pnl'], 2)
    return out
