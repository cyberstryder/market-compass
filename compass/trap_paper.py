"""Paper trading for bull/bear trap credit spreads.

When the trap detector flags a failed breakout, this module opens a simulated
credit spread so the user can watch the strategy's performance over time:

- bull_trap (failed upside break, fade down) -> bear call spread
- bear_trap (failed downside break, fade up) -> bull put spread

Spread spec: 5% wide, short leg at-the-money, 14-21 DTE (Friday nearest 17
DTE), 1 contract, held to expiration. P&L settles at expiration from the
underlying close (intrinsic value) — no stop/target management.

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
