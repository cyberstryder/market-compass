"""Freshness guard for live ICT futures sends (Josh 2026-10-26).

Approved for go-live 2026-10-26 09:30 CT in the configuration fitted by
files/freshness-guard-refit-2026-10-26.md. The guard runs at exactly
one point: inside ict_paper.submit(), after every existing gate has
passed and the detector is not paper-only — i.e. where the trade would
otherwise queue a TradersPost send. It gates the LIVE SEND ONLY; the
paper trade books normally either way, so a skipped trade stays on the
book as a measurable counterfactual (the paper-only carve-out's
philosophy, applied per-signal instead of per-detector).

Two tests, both read off a FRESH resolved quote taken at the decision
point (the booking's own machinery — ict_paper._book_entry_price:
exact `quote:<alias>` key, else ict_common.resolve_quote_key onto the
dated contract, exits()' per-symbol freshness ages, ask for a long /
bid for a short, last/price mark as fallback):

1. LEVEL RULE (reason guard_level_target / guard_level_stop): the
   fresh quote is already beyond the trade's APPROVED absolute stop or
   target — long: quote >= target or quote <= stop; short mirrored.
   Levels are the approved signal levels on the trade record, NOT
   booked-anchored levels: the rule asks "did the thesis print before
   we entered?", the one question relative TradersPost brackets cannot
   ask (they re-anchor past a breached level and open the dead trade).
2. RESIDUAL CAP (reason guard_adverse_cap): the fresh quote has moved
   adversely from the BOOKED entry (trade['entry'], the price booked
   this submit) by strictly more than 10% of |target - booked entry|.
   Adverse is per side: a long is hurt by a falling quote, a short by
   a rising one. 10% is the re-fit grid's kink — far outside typical
   dispatch latency (median 1-2 ticks), inside the flush tail.

Fail-open by design: the guard is an edge filter, not a safety gate.
Kill switch off, no fresh quote readable at the decision point, a
malformed trade record, or ANY internal error — the send proceeds and
the verdict records that the guard was not evaluated (upstream gates
and the data-gap machinery own bad data). evaluate() never raises.

Kill switch: FRESHNESS_GUARD_ENABLED (config.freshness_guard),
default TRUE — Josh approved it live; the env var is the no-deploy
rollback. Production needs no var set.
"""
from .market import number

CAP_FRACTION = 0.10

REASON_LEVEL_STOP = 'guard_level_stop'
REASON_LEVEL_TARGET = 'guard_level_target'
REASON_ADVERSE_CAP = 'guard_adverse_cap'


def _read_decision_quote(db, c, symbol, side, booked_entry, now):
    """Fresh resolved quote read at the send decision point.

    Delegates to ict_paper._book_entry_price so the guard reads quotes
    with EXACTLY the booking's resolution, freshness ages and side
    convention (ask=long / bid=short, last/price mark fallback). The
    booked entry is passed as the reference price; the returned slip
    is not used (the cap is computed here, in target-distance units).
    A result with source 'signal_fallback' means no fresh quote was
    readable — the caller treats that as not evaluated.
    """
    from . import ict_paper
    from .instruments import future_spec
    spec = future_spec(symbol) or {'tick': 0}
    return ict_paper._book_entry_price(db, c, symbol, side,
                                       booked_entry, spec, now)


def _verdict(**kw):
    v = {'decision': 'send', 'evaluated': False, 'reason': None,
         'fresh_quote': None, 'quote_ts': 0, 'booked_entry': None,
         'stop': None, 'target': None, 'adverse_pct': None,
         'note': None}
    v.update(kw)
    return v


def evaluate(db, c, cfg, trade, now):
    """Evaluate the freshness guard for one live-bound booked trade.

    `trade` is the booked t1 record: entry = booked entry, stop/target
    = the approved signal levels, side/symbol as submitted. Returns a
    verdict dict (see _verdict): decision 'send' or 'skip'; when the
    guard could not run, evaluated=False and 'note' says why
    ('disabled' / 'no_fresh_quote' / 'bad_trade' / 'error' + 'error').
    adverse_pct is the measured adverse move as a PERCENT of target
    distance (10.0 == the cap), adverse-positive, favorable = negative;
    None when no fresh quote was read. Never raises.
    """
    try:
        if not getattr(cfg, 'freshness_guard', True):
            return _verdict(note='disabled')
        side = str((trade or {}).get('side') or '').lower()
        symbol = (trade or {}).get('symbol')
        booked = number((trade or {}).get('entry'))
        stop = number((trade or {}).get('stop'))
        target = number((trade or {}).get('target'))
        base = {'booked_entry': booked, 'stop': stop, 'target': target}
        if side not in ('long', 'short') or not symbol \
                or booked is None or stop is None or target is None:
            return _verdict(note='bad_trade', **base)
        read = _read_decision_quote(db, c, symbol, side, booked, now)
        if read['source'] == 'signal_fallback':
            return _verdict(note='no_fresh_quote', **base)
        quote = read['price']
        base.update({'evaluated': True, 'fresh_quote': quote,
                     'quote_ts': read['quote_ts']})
        # 1. Level rule: has the thesis already printed?
        if side == 'long':
            if quote >= target:
                return _verdict(decision='skip',
                                reason=REASON_LEVEL_TARGET, **base)
            if quote <= stop:
                return _verdict(decision='skip',
                                reason=REASON_LEVEL_STOP, **base)
        else:
            if quote <= target:
                return _verdict(decision='skip',
                                reason=REASON_LEVEL_TARGET, **base)
            if quote >= stop:
                return _verdict(decision='skip',
                                reason=REASON_LEVEL_STOP, **base)
        # 2. Residual adverse cap: >10% of target distance, strictly.
        # The fraction is rounded to 9dp before the strict comparison:
        # prices are tick-quantized, and raw float dust (100.0 - 99.6 =
        # 0.4000000000000057) must not push an exactly-10% move over.
        direction = 1 if side == 'long' else -1
        adverse_pts = (booked - quote) * direction
        distance = abs(target - booked)
        if distance > 0:
            frac = round(adverse_pts / distance, 9)
            base['adverse_pct'] = round(frac * 100.0, 4)
            if frac > CAP_FRACTION:
                return _verdict(decision='skip',
                                reason=REASON_ADVERSE_CAP, **base)
        return _verdict(**base)
    except Exception as e:
        return _verdict(note='error',
                        error='%s: %s' % (type(e).__name__,
                                          str(e)[:200]))
