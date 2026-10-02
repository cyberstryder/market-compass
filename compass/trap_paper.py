"""Paper trading for bull/bear trap credit spreads.

When the trap detector flags a failed breakout, this module opens a simulated
credit spread so the user can watch the strategy's performance over time:

- bull_trap (failed upside break, fade down) -> bear call spread
- bear_trap (failed downside break, fade up) -> bull put spread

Spread spec: 5% wide, short leg at-the-money, 14-21 DTE (Friday nearest 17
DTE), 1 contract, held to expiration. P&L settles at expiration from the
underlying close (intrinsic value) — no stop/target management.

Early-exit study: every daily scan records a mark-to-market snapshot of each
open spread (chain mids when available, Black-Scholes fallback). The snapshot
trail lets exit_study() compare hold-to-expiration against early-exit rules
(50%/25% of max profit, 7 DTE, 50% of max loss) once trades close.

Entry timing: the trap is detected after the close on trap day. The paper
spread is entered on the first scan where the trap's sessions_since_break >= 1,
using that session's close as the entry underlying price.

Pricing: uses the stored option chain mid-price when a fresh chain exists for
the symbol; otherwise falls back to Black-Scholes with IV estimated from
20-day realized volatility (x1.3, floored at 15%). The pricing source is
recorded on the position for transparency.

Gating: TRAP_PAPER_ENABLED (default false) AND the global paper_trading flag
must both be on. Max 3 concurrent trap spreads (the user's 3-position cap),
one open spread per symbol.

Positions live under the `trap_spread:` prefix (NOT `position:`) so the
engine's exits() loop never touches them — settlement is expiration-only via
settle(). Trade records are `trade:trap-<symbol>-<entry_day>` with
strategy='trap-spread' for attribution.

No alerts are sent; entries/exits log `paper_decision` rows only.
"""
import math
from datetime import date, timedelta

from .market import number
from . import paper_risk
from .smoothers_blackscholes import bs_price

STRATEGY_TAG = 'trap-spread'
MAX_CONCURRENT = 3
WIDTH_PCT = 0.05
TARGET_DTE = 17
MIN_DTE, MAX_DTE = 14, 21
FEE_PER_SPREAD = 1.32  # 2 legs x open/close, consistent with Smoothers accounting


def paper_enabled(cfg):
    """True only when the trap paper flag AND global paper trading are on."""
    return bool(getattr(cfg, 'trap_paper', False)) and \
        bool(getattr(cfg, 'paper_trading', False))


def _pick_expiry(entry_day):
    """Friday 14-21 days after entry, nearest to 17 DTE."""
    d = date.fromisoformat(entry_day)
    best, best_dist = None, None
    for days_out in range(MIN_DTE, MAX_DTE + 1):
        cand = d + timedelta(days=days_out)
        if cand.weekday() != 4:  # Friday
            continue
        dist = abs(days_out - TARGET_DTE)
        if best is None or dist < best_dist:
            best, best_dist = cand, dist
    if best is not None:
        return best.isoformat()
    return (d + timedelta(days=TARGET_DTE)).isoformat()


def _estimate_iv(bars):
    """IV fallback: 1.3x 20-day realized vol, floored at 15%, capped at 200%."""
    closes = [number(b.get('c')) for b in bars[-21:] if number(b.get('c'))]
    if len(closes) < 10:
        return 0.30
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not rets:
        return 0.30
    var = sum(r * r for r in rets) / len(rets)
    rv = math.sqrt(var * 252)
    return max(0.15, min(2.0, rv * 1.3))


def _credit_from_chain(db, c, symbol, kind, short_k, long_k, expiry, S):
    """Best-effort credit from stored chain mids. Returns None if unusable."""
    try:
        chain = db.get(c, 'chain:' + symbol, {}) or {}
        contracts = chain.get('contracts') or []
        if not contracts:
            return None
        # Match expiry first (exact), then closest strikes.
        by_exp = [o for o in contracts if str(o.get('expiry')) == expiry]
        if not by_exp:
            return None
        otype = 'CALL' if kind == 'bear_call' else 'PUT'
        cands = [o for o in by_exp
                 if str(o.get('type', '')).upper().startswith(otype[0])
                 and number(o.get('multiplier')) == 100]
        if not cands:
            return None
        def mid(o):
            q = db.get(c, 'quote:' + str(o.get('symbol')), {}) or {}
            b, a = number(q.get('bid')), number(q.get('ask'))
            if b and a and a >= b:
                return (b + a) / 2
            return None
        short_c = min(cands, key=lambda o: abs(number(o.get('strike')) - short_k))
        long_c = min(cands, key=lambda o: abs(number(o.get('strike')) - long_k))
        sm, lm = mid(short_c), mid(long_c)
        if sm is None or lm is None or sm <= lm:
            return None
        return sm - lm
    except Exception:
        return None


def _modeled_credit(S, kind, T_years, iv):
    """Black-Scholes spread credit: short ATM leg minus long 5%-OTM leg."""
    width = S * WIDTH_PCT
    if kind == 'bear_call':
        short_k, long_k = S, S + width
        otype = 'CALL'
    else:  # bull_put
        short_k, long_k = S, S - width
        otype = 'PUT'
    short_p = bs_price(S, short_k, T_years, iv, otype)
    long_p = bs_price(S, long_k, T_years, iv, otype)
    credit = max(0.01, short_p - long_p)
    return credit, short_k, long_k, width


def _intrinsic_value(spread, underlying_close):
    """Spread value at expiration (European cash settlement)."""
    width = spread['width']
    if spread['spread_kind'] == 'bull_put':
        return max(0.0, min(width, spread['short_strike'] - underlying_close))
    return max(0.0, min(width, underlying_close - spread['short_strike']))


def _mark_spread(db, c, spread, S, dte, bars_for):
    """Current cost to buy back the spread. Returns (mark, source) or (None, None).

    Prefers stored chain mids for the spread's exact strikes/expiry; falls
    back to Black-Scholes with the entry IV (or a fresh realized-vol estimate).
    """
    chain_mark = _credit_from_chain(
        db, c, spread['symbol'], spread['spread_kind'],
        spread['short_strike'], spread['long_strike'],
        spread['expiry'], S)
    if chain_mark is not None:
        return chain_mark, 'chain'
    try:
        T = max(dte, 1) / 365.0
        iv = spread.get('model_iv') or _estimate_iv(bars_for(spread['symbol']) or [])
        otype = 'CALL' if spread['spread_kind'] == 'bear_call' else 'PUT'
        short_p = bs_price(S, spread['short_strike'], T, iv, otype)
        long_p = bs_price(S, spread['long_strike'], T, iv, otype)
        return max(0.0, short_p - long_p), 'bs_model'
    except Exception:
        return None, None


def mark_open(db, c, cfg, now, today, close_for, bars_for):
    """Daily mark-to-market snapshot for every open trap spread.

    today: 'YYYY-MM-DD'. close_for(symbol, day_str) -> close or None.
    bars_for(symbol) -> daily bars (for the IV fallback).
    Writes one paper_decision row per open spread ('mtm:<trade_id>:<day>').
    Returns the snapshot list. Never raises.
    """
    snaps = []
    for key, p in list(db.prefix(c, 'trap_spread:').items()):
        if not isinstance(p, dict) or p.get('status') != 'open':
            continue
        try:
            S = close_for(p['symbol'], today)
            if not S:
                continue
            dte = (date.fromisoformat(p['expiry']) - date.fromisoformat(today)).days
            if dte < 0:
                continue  # settle() owns expired spreads
            mark, src = _mark_spread(db, c, p, S, dte, bars_for)
            if mark is None:
                continue
            qty = p.get('qty', 1)
            unreal = (p['credit'] - mark) * 100 * qty - FEE_PER_SPREAD
            max_profit = p['credit'] * 100 * qty - FEE_PER_SPREAD
            snap = {
                'trade_id': p['id'], 'strategy': STRATEGY_TAG,
                'symbol': p['symbol'], 'day': today,
                'underlying': round(S, 2), 'dte_remaining': dte,
                'mark': round(mark, 2),
                'unrealized_pnl': round(unreal, 2),
                'max_profit': round(max_profit, 2),
                'max_loss': p.get('max_loss'),
                'price_source': src,
            }
            db.append(c, 'paper_decision', 'trap_paper', p['symbol'], now,
                      {**snap, 'status': 'mtm'}, 'mtm:%s:%s' % (p['id'], today))
            snaps.append(snap)
        except Exception:
            continue
    return snaps


def open_count(db, c):
    n = 0
    for key, p in db.prefix(c, 'trap_spread:').items():
        if isinstance(p, dict) and p.get('status') == 'open':
            n += 1
    return n


def has_open(db, c, symbol):
    for key, p in db.prefix(c, 'trap_spread:').items():
        if isinstance(p, dict) and p.get('status') == 'open' \
                and p.get('symbol') == symbol:
            return True
    return False


def submit(db, c, cfg, now, trap_event, entry_price, bars):
    """Open a paper credit spread for a fresh trap event.

    trap_event: the stored breakout event dict (kind bull_trap/bear_trap).
    entry_price: underlying price to enter at (T+1 close).
    bars: daily bars for IV estimation fallback.
    Returns {'submitted': bool, 'reason': str, ...}. Never raises.
    """
    try:
        return _submit(db, c, cfg, now, trap_event, entry_price, bars)
    except Exception as e:
        return {'submitted': False, 'reason': 'error: %s' % e}


def _submit(db, c, cfg, now, trap_event, entry_price, bars):
    if not paper_enabled(cfg):
        return {'submitted': False, 'reason': 'paper_disabled'}
    kind = trap_event.get('kind')
    if kind == 'bull_trap':
        spread_kind = 'bear_call'
    elif kind == 'bear_trap':
        spread_kind = 'bull_put'
    else:
        return {'submitted': False, 'reason': 'not_a_trap'}
    symbol = trap_event.get('symbol')
    S = number(entry_price)
    if not symbol or not S:
        return {'submitted': False, 'reason': 'bad_input'}
    if has_open(db, c, symbol):
        return {'submitted': False, 'reason': 'existing_position'}
    if open_count(db, c) >= MAX_CONCURRENT:
        return {'submitted': False, 'reason': 'max_positions'}
    entry_day = trap_event.get('entry_day') or ''
    trade_id = 'trap-%s-%s' % (symbol, entry_day)
    if db.get(c, 'trade:' + trade_id):
        return {'submitted': False, 'reason': 'duplicate'}

    expiry = _pick_expiry(entry_day)
    dte = (date.fromisoformat(expiry) - date.fromisoformat(entry_day)).days
    T = max(dte, 1) / 365.0

    # Price the spread: real chain first, BS model fallback.
    price_source = 'chain'
    credit = _credit_from_chain(
        db, c, symbol, spread_kind, S,
        S * (1 + WIDTH_PCT) if spread_kind == 'bear_call' else S * (1 - WIDTH_PCT),
        expiry, S)
    if credit is None:
        price_source = 'bs_model'
        iv = _estimate_iv(bars or [])
        credit, short_k, long_k, width = _modeled_credit(S, spread_kind, T, iv)
    else:
        width = S * WIDTH_PCT
        short_k = S
        long_k = S + width if spread_kind == 'bear_call' else S - width
        iv = None

    pos_key = 'trap_spread:%s:%s' % (symbol, entry_day)
    spread = {
        'id': trade_id, 'strategy': STRATEGY_TAG,
        'symbol': symbol, 'asset': 'option',
        'spread_kind': spread_kind, 'trap_kind': kind,
        'trap_id': trap_event.get('id'),
        'status': 'open',
        'entry_day': entry_day, 'entered_at': now,
        'entry_underlying': S,
        # Ledger columns: entry = credit received per share, exit = cost to
        # close per share (intrinsic at expiration). P&L = (entry-exit)*100-fees.
        'entry': round(credit, 2),
        'short_strike': round(short_k, 2), 'long_strike': round(long_k, 2),
        'width': round(width, 2),
        'credit': round(credit, 2),
        'credit_total': round(credit * 100, 2),
        'expiry': expiry, 'dte': dte,
        'qty': 1,
        'price_source': price_source,
        'model_iv': round(iv, 4) if iv else None,
        'max_loss': round((width - credit) * 100, 2),
        'source': 'trap_paper',
    }
    db.put(c, pos_key, spread)
    db.put(c, 'trade:' + trade_id, spread)
    risk = paper_risk.account(db, c, now, 'option', persist=True)
    risk['entries'] = risk.get('entries', 0) + 1
    db.put(c, paper_risk.key(now, 'option'), risk)
    db.append(c, 'paper_decision', 'trap_paper', symbol, now,
              {**spread, 'status': 'entered'}, 'entry:' + trade_id)
    return {'submitted': True, 'reason': 'entered', 'trade_id': trade_id,
            'strategy': STRATEGY_TAG, 'credit': spread['credit'],
            'price_source': price_source}


def settle(db, c, cfg, now, today, close_for):
    """Expire-settle open spreads whose expiry has arrived.

    today: 'YYYY-MM-DD'. close_for(symbol, day_str) -> underlying close or None.
    Returns list of settled trade dicts. Never raises.
    """
    settled = []
    for key, p in list(db.prefix(c, 'trap_spread:').items()):
        if not isinstance(p, dict) or p.get('status') != 'open':
            continue
        try:
            if today < p.get('expiry', ''):
                continue
            underlying = close_for(p['symbol'], p['expiry'])
            if underlying is None:
                continue
            intrinsic = _intrinsic_value(p, underlying)
            pnl = (p['credit'] - intrinsic) * 100 * p.get('qty', 1) - FEE_PER_SPREAD
            p.update(status='closed', exit_underlying=round(underlying, 2),
                     exited_at=now, exit_day=today,
                     exit_reason='expired',
                     exit=round(intrinsic, 2),
                     intrinsic_value=round(intrinsic, 2),
                     pnl=round(pnl, 2))
            db.put(c, key, p)
            db.put(c, 'trade:' + p['id'], p)
            risk = paper_risk.account(db, c, now, 'option', persist=True)
            risk['realized'] = risk.get('realized', 0) + p['pnl']
            db.put(c, paper_risk.key(now, 'option'), risk)
            db.append(c, 'paper_decision', 'trap_paper', p['symbol'], now,
                      {**p, 'status': 'exited'}, 'exit:' + p['id'])
            settled.append(p)
        except Exception:
            continue
    return settled


def attribution(db, c):
    """Per-strategy paper P&L over closed trap spread trades. Read-only."""
    out = {'trades': 0, 'wins': 0, 'losses': 0, 'realized': 0.0,
           'total_credit': 0.0}
    for key, trade in db.prefix(c, 'trade:trap-').items():
        if not isinstance(trade, dict) or trade.get('status') != 'closed':
            continue
        out['trades'] += 1
        pnl = number(trade.get('pnl')) or 0.0
        out['realized'] += pnl
        out['total_credit'] += number(trade.get('credit_total')) or 0.0
        if pnl > 0:
            out['wins'] += 1
        elif pnl < 0:
            out['losses'] += 1
    out['realized'] = round(out['realized'], 2)
    out['total_credit'] = round(out['total_credit'], 2)
    return out


def _mtm_trail(db, c):
    """All MTM snapshots grouped by trade_id, each trail sorted by day."""
    from sqlalchemy import select
    from .store import events
    rows = c.execute(
        select(events.c.key, events.c.payload)
        .where(events.c.kind == 'paper_decision',
               events.c.source == 'trap_paper',
               events.c.key.like('mtm:%'))).all()
    trails = {}
    for key, payload in rows:
        if not isinstance(payload, dict):
            continue
        tid = payload.get('trade_id')
        if not tid:
            continue
        trails.setdefault(tid, []).append(payload)
    for tid in trails:
        trails[tid].sort(key=lambda s: s.get('day', ''))
    return trails


def exit_study(db, c):
    """Hold-to-expiration vs early-exit rules, replayed over MTM trails.

    Rules (first snapshot satisfying the condition wins):
      hold_to_expiry: actual settled P&L (baseline, needs no MTM data)
      take_50: unrealized >= 50% of max profit
      take_25: unrealized >= 25% of max profit
      exit_7dte: <= 7 DTE remaining (dodge expiration gamma)
      stop_50: unrealized <= -50% of max loss
    Returns per-rule {trades, wins, win_rate, avg_pnl, total_pnl} and
    baseline_only (closed trades with no MTM history). Early-exit rules cover
    only trades with an MTM trail; a rule that never triggers on a trail
    falls back to the settled outcome (i.e. the position was held).
    Read-only.
    """
    trails = _mtm_trail(db, c)
    rules = ['hold_to_expiry', 'take_50', 'take_25', 'exit_7dte', 'stop_50']
    agg = {r: {'trades': 0, 'wins': 0, 'total_pnl': 0.0} for r in rules}
    baseline_only = 0

    def record(rule, pnl):
        a = agg[rule]
        a['trades'] += 1
        a['total_pnl'] += pnl
        if pnl > 0:
            a['wins'] += 1

    for key, trade in db.prefix(c, 'trade:trap-').items():
        if not isinstance(trade, dict) or trade.get('status') != 'closed':
            continue
        tid = trade.get('id')
        settled = number(trade.get('pnl')) or 0.0
        record('hold_to_expiry', settled)
        snaps = trails.get(tid) or []
        if not snaps:
            baseline_only += 1
            continue
        # Simulate each early-exit rule over the snapshot trail.
        exits = {}
        for s in snaps:
            unreal = number(s.get('unrealized_pnl')) or 0.0
            max_profit = number(s.get('max_profit')) or 0.0
            max_loss = number(s.get('max_loss')) or 0.0
            dte = s.get('dte_remaining')
            if 'take_50' not in exits and max_profit > 0 \
                    and unreal >= 0.5 * max_profit:
                exits['take_50'] = unreal
            if 'take_25' not in exits and max_profit > 0 \
                    and unreal >= 0.25 * max_profit:
                exits['take_25'] = unreal
            if 'exit_7dte' not in exits and dte is not None and dte <= 7:
                exits['exit_7dte'] = unreal
            if 'stop_50' not in exits and max_loss > 0 \
                    and unreal <= -0.5 * max_loss:
                exits['stop_50'] = unreal
        # A rule that never triggers falls back to the settled outcome.
        for r in ('take_50', 'take_25', 'exit_7dte', 'stop_50'):
            record(r, exits.get(r, settled))

    out = {}
    for r, a in agg.items():
        n = a['trades']
        out[r] = {
            'trades': n,
            'wins': a['wins'],
            'win_rate': round(a['wins'] / n, 3) if n else 0.0,
            'avg_pnl': round(a['total_pnl'] / n, 2) if n else 0.0,
            'total_pnl': round(a['total_pnl'], 2),
        }
    out['baseline_only'] = baseline_only
    return out
