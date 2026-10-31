"""Executable-cost ranking for the Smoothers shadow board (2026-10-31).

Josh approved the Smoothers plan 2026-10-30: the weekly board stops
presenting the A+/A/B/WATCH quality tiers as an actionable ranking (on
the graded record the tiers are premium-INVERTED: A+ -$26.49/pick vs
WATCH +$75.85) and is instead ordered by what a pick costs to trade —
the quantity that separated the graded book's halves by +$129.85/pick
in-sample (cheap half +$78.73 vs dear half -$51.12; cutoff-simulation.md
Part 2). The quality score/tier stay stored on every payload for audit;
they no longer order or headline the board.

Cost key (exactly the graded construction):
    key = entry spread % + entry theta %/day * E[days-to-exit]
    theta %/day = |greeks.theta| / entry ask * 100
    E[days-to-exit] = 1.908 (the graded book's own exit-day mean)

All DTE classes compete head-to-head (Josh confirmed 2026-10-30 — no
expiry-class separation): an 11-DTE contract's lower daily rent is a
real cost advantage, and a 2-DTE contract's ~40%/day rent is a real
cost, and the key prices both as such.

Inputs are the pick's publication-batch entry quote (the refreshed
`quote` dict from refresh_entry_quotes: status/bid/ask/midpoint/
spread_pct/greeks, AlpacaQuotes.read_snapshot shape). A pick whose
entry quote is unusable has no key: it is unrankable, sits below the
ranked board, and is flagged quote-missing rather than silently tiered
(the graded weeks had 3 such picks: ABNB, GOOGL, TMO).
"""
import statistics

from .market import number

EXPECTED_DAYS = 1.908
# The quote layer itself marks spreads above 20% 'wide_spread'; the
# board's friction flag is the plan's relative rule (above the week's
# own median), computed in annotate().
WIDE_SPREAD_STATUS = 'wide_spread'


def spread_pct(quote):
    """Entry spread as % of midpoint, from the quote's own spread_pct
    when present, else derived from bid/ask. None when unusable."""
    q = quote or {}
    if q.get('status') not in ('available', WIDE_SPREAD_STATUS):
        return None
    value = number(q.get('spread_pct'))
    if value is not None and value >= 0:
        return float(value)
    bid, ask = number(q.get('bid')), number(q.get('ask'))
    if bid is None or ask is None or ask <= 0 or bid < 0 or bid > ask:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return float((ask - bid) / mid * 100)


def rent_pct_day(quote):
    """Theta rent as % of the entry ask per day. None when the quote
    carries no usable theta or ask."""
    q = quote or {}
    if q.get('status') not in ('available', WIDE_SPREAD_STATUS):
        return None
    ask = number(q.get('ask'))
    greeks = q.get('greeks') or {}
    theta = number(greeks.get('theta'))
    if ask is None or ask <= 0 or theta is None:
        return None
    return float(abs(theta) / ask * 100)


def cost_key(quote):
    """The executable-cost key for one entry quote, or None."""
    spread = spread_pct(quote)
    rent = rent_pct_day(quote)
    if spread is None or rent is None:
        return None
    return spread + rent * EXPECTED_DAYS


def annotate(signals):
    """Attach entry_spread_pct / entry_rent_pct_day / cost_key /
    cost_rank / friction fields to each signal dict in place and
    return the list reordered: ranked picks by cost ascending
    (deterministic ticker/direction tie-break), then unrankable picks
    (quote-missing) in ticker order.

    Friction rule (the plan's): a pick whose entry spread is above the
    week's median entry spread is flagged; the reason names 'wide
    spread', plus 'high rent' when its rent is also above the week's
    median rent. Medians are over the week's rankable picks.
    """
    rows = list(signals)
    for p in rows:
        q = p.get('quote') or {}
        p['entry_spread_pct'] = spread_pct(q)
        p['entry_rent_pct_day'] = rent_pct_day(q)
        p['cost_key'] = (p['entry_spread_pct'] + p['entry_rent_pct_day'] * EXPECTED_DAYS
                         if p['entry_spread_pct'] is not None
                         and p['entry_rent_pct_day'] is not None else None)
        p['cost_rank'] = None
        p['friction'] = False
        p['friction_reason'] = None
    ranked = [p for p in rows if p['cost_key'] is not None]
    unranked = [p for p in rows if p['cost_key'] is None]
    ranked.sort(key=lambda p: (p['cost_key'], str(p.get('ticker') or ''),
                               str(p.get('direction') or '')))
    for idx, p in enumerate(ranked, start=1):
        p['cost_rank'] = idx
    if ranked:
        median_spread = statistics.median(p['entry_spread_pct'] for p in ranked)
        median_rent = statistics.median(p['entry_rent_pct_day'] for p in ranked)
        for p in ranked:
            if p['entry_spread_pct'] > median_spread:
                p['friction'] = True
                reasons = ['wide spread']
                if p['entry_rent_pct_day'] > median_rent:
                    reasons.append('high rent')
                p['friction_reason'] = ' + '.join(reasons)
    unranked.sort(key=lambda p: (str(p.get('ticker') or ''),
                                 str(p.get('direction') or '')))
    return ranked + unranked
