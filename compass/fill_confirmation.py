"""Broker fill confirmation for the live survivors book (2026-11-02).

THE DEFECT THIS CLOSES: survivors.py booked a live position the moment
its TradersPost webhook POST returned — but that response is ACCEPTANCE
ONLY ({success, id, payload echo}); fill price/time are not on the wire
(traderspost.py's own docstring: fills "arrive via later reconciliation,
never faked here" — and no reconciliation existed). On 2026-11-02 the
S3 MGC London trade was rejected at Tradovate on BOTH legs ("Access
token is expired") while Compass booked and closed it +$42 modeled, and
the day-digest printed it as a live trade; the one true trade (S4)
filled at broker prices that differed from the book's modeled prices.
The 50-fill review counter and the −$1,750 tripwire read from that
book, so phantom rows corrupted both.

STATUS SOURCE (investigated 2026-11-02, see the build note): TradersPost
exposes NO self-serve status API for signal executions — their public
API is a waitlist product for building trading apps, and no Compass
config carries a TradersPost API credential (webhook URLs only).
Per-signal execution detail (fills, rejection reasons) exists in the
TradersPost web UI (Strategies → Signals → trade detail); that is where
the 2026-11-02 exhibits were verified. So the confirmation source of
record is a broker-verified VERDICT recorded through the cron-token
guarded admin endpoint (POST /api/admin/survivor-fill-verdict), with
provenance (source / recorded_by / recorded_at) on every verdict. If a
real status API ever ships, a poller writes through record_verdict —
the single seam — and nothing downstream changes.

Known executor failure mode: the Tradovate connection's access token
cycles roughly every 80 minutes; a lapse rejects orders at the BROKER
after the webhook accepted them (the S3 case). Verdicts must therefore
be read at the executor after sends, and absence of a verdict is never
treated as a fill.

RULES:
- Only broker-confirmed fills open/close live accounting, increment the
  50-fill counter, or move the tripwire (account_live in survivors.py
  reads the projections derived here).
- A sent leg with no verdict is PENDING: counted as neither fill nor
  P&L, kept on the reconciliation list forever, escalated once per leg
  after CONFIRM_WINDOW_S — never silently promoted, never dropped.
- Verdicts are first-wins per leg; an explicit force=True re-verdict
  (endpoint only) is the correction path and is audit-evented.
- Confirmed P&L is GROSS (confirmed exit − entry prices × qty ×
  multiplier × direction), the basis Josh's tripwire/review accounting
  uses (+$222 for S4); broker fees/net live in the verdict provenance.
"""
import time

from .instruments import future_spec

VERDICT_PREFIX = 'fill_verdict:'
VERDICTS = ('filled', 'rejected', 'no_order')
LEGS = ('entry', 'exit')

# Bounded confirmation window: a send unverdicted after this long is
# escalated (health + audit event) but STAYS pending — the window bounds
# silence, not truth. 4h covers a full session leg plus the Tradovate
# token cycle many times over; normal verdicts land in minutes.
CONFIRM_WINDOW_S = 4 * 3600

CORRECTION_FLAG = 'fill_correction_20261102_at'
_PROJECTION_FIELDS = ('fill_entry_status', 'fill_entry_price',
                      'fill_entry_at', 'fill_entry_reason',
                      'fill_exit_status', 'fill_exit_price',
                      'fill_exit_at', 'fill_exit_reason',
                      'live_status', 'live_confirmed_pnl')


def verdict_key(trade_id, leg):
    return VERDICT_PREFIX + str(trade_id) + ':' + str(leg)


def get_verdicts(db, c, trade_id):
    out = {}
    for leg in LEGS:
        v = db.get(c, verdict_key(trade_id, leg))
        if isinstance(v, dict):
            out[leg] = v
    return out


def _audit(db, c, now, payload):
    try:
        db.append(c, 'survivor_event', 'survivors', '', now, payload)
    except Exception:
        pass


def record_verdict(db, c, trade_id, leg, verdict, now, price=None, at=None,
                   reason=None, source=None, recorded_by=None, force=False):
    """Record one leg's broker truth. Returns (verdict_row, created).

    Validation: the trade must exist; 'filled' requires a numeric price.
    First verdict wins unless force=True. The projection is re-applied to
    the trade (and its position copy) in the same transaction so every
    consumer sees the confirmed truth immediately.
    """
    if leg not in LEGS:
        raise ValueError('leg must be one of %r' % (LEGS,))
    if verdict not in VERDICTS:
        raise ValueError('verdict must be one of %r' % (VERDICTS,))
    trade = db.get(c, 'trade:' + str(trade_id))
    if not isinstance(trade, dict):
        raise ValueError('unknown trade %r' % (trade_id,))
    if verdict == 'filled':
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            raise ValueError('a filled verdict requires the broker fill price')
    key = verdict_key(trade_id, leg)
    existing = db.get(c, key)
    if isinstance(existing, dict) and not force:
        return existing, False
    row = {'trade_id': str(trade_id), 'leg': leg, 'verdict': verdict,
           'price': float(price) if verdict == 'filled' else None,
           'at': float(at) if isinstance(at, (int, float)) else None,
           'reason': reason, 'source': source,
           'recorded_by': recorded_by, 'recorded_at': now}
    if isinstance(existing, dict):
        row['supersedes'] = {'verdict': existing.get('verdict'),
                             'recorded_at': existing.get('recorded_at'),
                             'recorded_by': existing.get('recorded_by')}
    db.put(c, key, row)
    _audit(db, c, now, {'event': 'fill_verdict', 'trade': trade_id,
                        'leg': leg, 'verdict': verdict,
                        'price': row['price'], 'reason': reason,
                        'source': source, 'recorded_by': recorded_by,
                        'forced': bool(force and existing)})
    apply_projection(db, c, trade, get_verdicts(db, c, trade_id), now)
    return row, True


def _leg_projection(verdict_row):
    """(status, price, at, reason) for one leg from its verdict row."""
    if not isinstance(verdict_row, dict):
        return 'pending', None, None, None
    v = verdict_row.get('verdict')
    if v == 'filled':
        return 'filled', verdict_row.get('price'), verdict_row.get('at'), \
            verdict_row.get('reason')
    if v in ('rejected', 'no_order'):
        return v, None, None, verdict_row.get('reason')
    return 'pending', None, None, None


def derive_live(trade, verdicts):
    """Pure projection: trade row + verdict rows → confirmation fields.

    Returns a dict with the _PROJECTION_FIELDS. Trades that were never
    sent and carry no verdict project to live_status 'not_sent' with no
    leg fields (paper-only rows stay untouched in spirit: consumers key
    off live_status)."""
    out = {f: None for f in _PROJECTION_FIELDS}
    sent = bool(trade.get('traderspost_sent'))
    entry_v = (verdicts or {}).get('entry')
    exit_v = (verdicts or {}).get('exit')
    if not sent and not entry_v:
        out['live_status'] = 'not_sent'
        return out
    e_status, e_price, e_at, e_reason = _leg_projection(entry_v)
    if not sent and e_status == 'pending':
        e_status = 'pending'  # verdicted trade whose send flag is off
    out['fill_entry_status'] = e_status
    out['fill_entry_price'] = e_price
    out['fill_entry_at'] = e_at
    out['fill_entry_reason'] = e_reason
    closed = trade.get('status') == 'closed' or trade.get('exited_at')
    if e_status in ('rejected', 'no_order'):
        out['live_status'] = 'not_filled'
        if closed and exit_v:
            x_status, x_price, x_at, x_reason = _leg_projection(exit_v)
            out['fill_exit_status'] = x_status
            out['fill_exit_price'] = x_price
            out['fill_exit_at'] = x_at
            out['fill_exit_reason'] = x_reason
        return out
    if e_status != 'filled':
        out['live_status'] = 'pending'
        return out
    # Entry confirmed filled.
    if not closed:
        out['live_status'] = 'confirmed_open'
        return out
    x_status, x_price, x_at, x_reason = _leg_projection(exit_v)
    out['fill_exit_status'] = x_status
    out['fill_exit_price'] = x_price
    out['fill_exit_at'] = x_at
    out['fill_exit_reason'] = x_reason
    if x_status == 'filled':
        spec = future_spec(trade.get('symbol') or '')
        mult = (spec or {}).get('multiplier')
        qty = trade.get('qty') or 1
        if mult and e_price is not None and x_price is not None:
            direction = 1 if trade.get('side') == 'long' else -1
            out['live_confirmed_pnl'] = round(
                (x_price - e_price) * direction * qty * mult, 2)
        out['live_status'] = 'confirmed_closed'
    elif x_status in ('rejected', 'no_order'):
        # Entry filled at the broker but the Compass exit never closed
        # it there — the position state at the broker is UNKNOWN from
        # here. Counted as neither (no P&L, entry fill only) and flagged
        # distinctly so it cannot hide inside the pending bucket.
        out['live_status'] = 'exit_unconfirmed'
    else:
        out['live_status'] = 'pending_exit'
    return out


def apply_projection(db, c, trade, verdicts, now):
    """Stamp derive_live's fields onto the trade row and its survivor
    position copy. Returns the projection dict."""
    proj = derive_live(trade, verdicts)
    changed = any(trade.get(f) != proj[f] for f in _PROJECTION_FIELDS)
    if changed:
        trade.update(proj)
        trade['fill_checked_at'] = now
        db.put(c, 'trade:' + str(trade.get('id')), trade)
        # Survivor position copies live under the ICT tranche key shape
        # position:ict:<detector>:<symbol>:t1 (submit_survivor).
        pkey = 'position:ict:%s:%s:t1' % (trade.get('detector'),
                                          trade.get('symbol'))
        pos = db.get(c, pkey)
        if isinstance(pos, dict) and pos.get('id') == trade.get('id'):
            pos.update(proj)
            pos['fill_checked_at'] = now
            db.put(c, pkey, pos)
    return proj


def _entry_dispatched_at(db, c, trade):
    claim = db.get(c, 'traderspost_send:entry:' + str(trade.get('id')))
    if isinstance(claim, dict) and claim.get('order_sent_at'):
        return claim['order_sent_at']
    return trade.get('entered_at') or trade.get('order_sent_at')


def _exit_dispatched_at(db, c, trade):
    claim = db.get(c, 'traderspost_send:close:' + str(trade.get('id')))
    if isinstance(claim, dict) and claim.get('order_sent_at'):
        return claim['order_sent_at']
    return trade.get('exited_at')


def reconcile(db, c, now):
    """Settle pending survivor sends against recorded verdicts.

    Survivors only (trade:ict-survivor_*). Stamps projections, escalates
    legs pending past CONFIRM_WINDOW_S exactly once per leg (audit event
    + a returned flag the caller surfaces as health), and returns a
    summary the accounting + digest read. Pending legs are NEVER
    auto-resolved here: with no status API to poll, reconciliation means
    projecting the verdicts that exist and keeping the rest visibly
    pending."""
    summary = {'checked': 0, 'pending': 0, 'pending_exit': 0,
               'not_filled': 0, 'confirmed_open': 0, 'confirmed_closed': 0,
               'exit_unconfirmed': 0, 'overdue_legs': []}
    try:
        trades = db.prefix(c, 'trade:ict-survivor_')
    except Exception:
        trades = {}
    for trade in (trades or {}).values():
        if not isinstance(trade, dict) or not trade.get('id'):
            continue
        verdicts = get_verdicts(db, c, trade['id'])
        if not trade.get('traderspost_sent') and not verdicts:
            continue
        summary['checked'] += 1
        proj = apply_projection(db, c, trade, verdicts, now)
        status = proj['live_status']
        if status in summary:
            summary[status] += 1
        fresh = db.get(c, 'trade:' + str(trade['id'])) or trade
        for leg, dispatched in (
                ('entry', _entry_dispatched_at(db, c, fresh)),
                ('exit', _exit_dispatched_at(db, c, fresh))):
            if leg == 'exit' and status not in ('pending_exit',
                                                'exit_unconfirmed'):
                continue
            if leg == 'entry' and status != 'pending':
                continue
            if not dispatched or now - dispatched <= CONFIRM_WINDOW_S:
                continue
            flag = 'fill_%s_overdue_at' % leg
            if fresh.get(flag):
                continue
            fresh[flag] = now
            db.put(c, 'trade:' + str(trade['id']), fresh)
            summary['overdue_legs'].append(
                {'trade': trade['id'], 'leg': leg})
            _audit(db, c, now,
                   {'event': 'fill_unconfirmed_overdue',
                    'trade': trade['id'], 'leg': leg,
                    'pending_s': round(now - dispatched, 1),
                    'window_s': CONFIRM_WINDOW_S})
    return summary


# ---------------------------------------------------------------------
# One-time correction of the 2026-11-02 record (broker-verified facts).
#
# S3: entry AND exit sends were accepted by the TradersPost webhook and
#     rejected at Tradovate — "Access token is expired" (read in Josh's
#     TradersPost account, 2026-11-02). The +$42 modeled close was a
#     phantom.
# S2: NEVER SENT — the duplicate/hedging guard skipped it ("already open
#     for turtle_soup"); traderspost_sent is False on the row and no
#     broker order exists (account checked 2026-11-02 13:51 CT). The
#     verdict records that no order exists, with the true reason.
# S4: the day's one true trade — entry filled 3971.30 at 09:11:25 CT,
#     exit (bracket target) filled 3949.10 at 13:50:16 CT, +$222.00
#     gross; the account's Day P&L showed +$219.42 net of $2.58 fees.
#     Compass's modeled row carried 3967.5 → 3949.1 (+$181).
# Modeled values stay on the rows untouched (they are the PAPER record);
# every live consumer reads the confirmed projection instead.
# ---------------------------------------------------------------------

_CORRECTION_SOURCE = 'traderspost_ui_broker_verification_2026-11-02'
_TOKEN_REASON = ('Tradovate error: Access token is expired — both legs '
                 'rejected at the broker after webhook acceptance '
                 '(verified in TradersPost account, 2026-11-02)')
_CORRECTIONS = (
    ('ict-survivor_s3-MGC.v.0-1793607435-t1', 'entry', 'rejected',
     None, None, _TOKEN_REASON),
    ('ict-survivor_s3-MGC.v.0-1793607435-t1', 'exit', 'rejected',
     None, None, _TOKEN_REASON),
    ('ict-survivor_s2-MCL.v.0-1793607435-t1', 'entry', 'no_order',
     None, None,
     'Never sent — duplicate/hedging guard skip (MCL.v.0 already open '
     'for turtle_soup); no order exists at the broker (account verified '
     '2026-11-02 13:51 CT). Recorded so the row is marked not-filled, '
     'not left as an unexplained paper close.'),
    ('ict-survivor_s4-MGC.v.0-1793632218-t1', 'entry', 'filled',
     3971.30, 1793632285.0,
     'Broker fill verified in TradersPost account, 2026-11-02 '
     '(09:11:25 CT).'),
    ('ict-survivor_s4-MGC.v.0-1793632218-t1', 'exit', 'filled',
     3949.10, 1793649016.0,
     'Bracket target Limit Buy filled at the broker, verified in '
     'TradersPost account, 2026-11-02 (13:50:16 CT). Gross +$222.00; '
     'account Day P&L +$219.42 net of $2.58 fees.'),
)


def apply_history_correction_2026_11_02(db, c, state, now):
    """Write the 2026-11-02 verdicts once (state-flag guarded).

    Idempotent by construction: record_verdict is first-wins, and the
    flag stops re-scanning. Returns the number of verdicts created."""
    if not isinstance(state, dict) or state.get(CORRECTION_FLAG):
        return 0
    created = 0
    for trade_id, leg, verdict, price, at, reason in _CORRECTIONS:
        trade = db.get(c, 'trade:' + trade_id)
        if not isinstance(trade, dict):
            continue  # fresh/test DB without the production rows
        _, was_created = record_verdict(
            db, c, trade_id, leg, verdict, now, price=price, at=at,
            reason=reason, source=_CORRECTION_SOURCE,
            recorded_by='fill_confirmation_correction')
        created += 1 if was_created else 0
    state[CORRECTION_FLAG] = now
    db.put(c, 'survivors:state', state)
    _audit(db, c, now, {'event': 'fill_correction_20261102_applied',
                        'verdicts_created': created})
    return created
