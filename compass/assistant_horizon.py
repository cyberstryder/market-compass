"""Keep intraday evidence out of long-horizon answers; fail closed on absent history."""
from .readiness import clock

INTRADAY = ('technical_context','scanner','exposure','matrix','levels','spy_brief',
            'flow','alerts','option_ideas','spy_study','setup_study','tm_study')


def long_only(context):
    req=(context.get('option_research') or {}).get('request') or {}
    return req.get('status')=='requested' and req.get('min_dte',0)>=365


def restrict(context):
    if not long_only(context):
        return context
    return {key:value for key,value in context.items() if key not in INTRADAY}


def missing_history_answer(context):
    """A quote lookup cannot fill in missing completed daily/weekly evidence.

    Only applies to a single long-horizon request with no ready longer-term
    technical evidence for any requested symbol. Mixed horizons keep their
    existing research route. This is a data-availability response, not a veto on
    research or a forecast from a failed strategy gate.
    """
    if not long_only(context):return None
    options=context['option_research'];req=options['request']
    symbols=context.get('question_scope',{}).get('symbols',[])
    if not symbols:return None
    history=context.get('swing_technical_context') or {}
    if any((history.get(s) or {}).get('status')=='ready' for s in symbols):return None
    lines=['**Insufficient long-term directional evidence.** The supplied completed daily/weekly history is not ready. '
           'I cannot support a bullish or bearish LEAPS conclusion from this context. '
           'Intraday VWAP, EMAs, GEX and option availability do not establish a multi-month outlook.',
           f"Requested {req.get('side') or 'call/put'} expirations: {req['expiry_start']} through {req['expiry_end']} "
           f"({req['min_dte']}–{req['max_dte']} calendar days)."]
    for symbol in symbols:
        item=options.get('symbols',{}).get(symbol,{})
        rows=item.get('candidates',[])
        expiries=', '.join(sorted({r['expiry'] for r in rows})) or 'none returned'
        lines.append(f"**{symbol}:** returned expirations {expiries}. Bounded sample: {len(rows)} contracts; "
                     f"{sum(r.get('quote_status')=='fresh' for r in rows)} fresh, "
                     f"{sum(r.get('quote_status')=='stale' for r in rows)} stale, "
                     f"{sum(r.get('quote_status') not in ('fresh','stale') for r in rows)} unavailable quotes "
                     'at lookup. This is pricing evidence only, not directional conviction.')
        for row in rows:
            lines.append(f"- {row['symbol']}: {row.get('quote_status','unavailable')}; "
                         f"bid/ask {row.get('bid')} / {row.get('ask')}; "
                         f"source {row.get('source',item.get('source','unavailable'))}; "
                         f"quote time {clock(row.get('quote_ts')) or 'unavailable'}.")
        if not rows:lines.append('Contract lookup: '+str(item.get('status',options.get('status','unavailable')))+'.')
    lines.append('Needed for a directional assessment: completed daily/weekly trend history and independent evidence relevant to the holding horizon. '
                 'Research remains available; paper-entry limits do not prohibit analysis.')
    return '\n\n'.join(lines)
