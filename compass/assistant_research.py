"""Question-scoped research evidence; strategy admission is a separate concern."""
import re

VERSION = 'ask-research-v3'
POLICY = (
    'Use research_request to answer the actual question, not a different strategy-entry question. '
    'For a market direction question, lead with a bullish, bearish, mixed or insufficient-evidence lean, '
    'the requested horizon, and qualitative confidence justified by the evidence. Never guarantee rise/fall '
    'or invent a probability. Mixed is appropriate for conflicting evidence, not simply an untriggered strategy. '
    'Assess current price versus VWAP, EMA alignment and slope, recent completed-bar structure, volume, '
    'and available exposure/flow with their timestamps and limitations. Distinguish intraday momentum '
    'from the forecast for the remaining session. Do not treat an opening candle as current momentum. '
    'A scheduled SPY WAIT/NO ENTRY is only that strategy state, never a veto on directional analysis. '
    'Being inside the premarket range does not by itself establish a neutral trend. '
    'A future candle that has not closed is not missing or stale data; use the latest completed bars. '
    'Missing option quotes prevent contract pricing, not an underlying directional thesis. '
    'Quote availability, contract listings, liquidity, bid/ask spreads and open interest alone are not directional evidence. '
    'Available or liquid calls do not establish bullishness; available or liquid puts do not establish bearishness. '
    'The requested option side expresses the question, not observed buying, institutional conviction or a forecast. '
    'Support any directional lean with independent, timestamped underlying trend, price structure or contextualized flow evidence '
    'appropriate to the requested horizon. Intraday VWAP, EMAs and nearby GEX cannot establish a multi-month LEAPS thesis. '
    'When horizon-appropriate directional evidence is absent, state insufficient directional evidence; do not infer a lean '
    'from contract availability or disguise that inference with words such as plausible or conditional. '
    'For quote-only or verification requests, report the requested facts without adding an unsolicited market thesis. '
    'Saved brief option availability is historical to that brief, not proof of a current provider outage. '
    'For market_research, do not add automated-entry eligibility, paper risk limits, or a required '
    '15-minute breakout. Only discuss those when the question requests that strategy or entry review. '
    'Context omissions are assistant packaging limits, not provider failures or evidence that data does not exist. '
    'Describe only missing evidence material to the question, after using the available evidence. '
    'Do not claim the supplied context is complete. If fresh direction evidence is insufficient, say why. '
    'Use concise human-readable times and sensible price precision; do not print internal flags, field paths '
    'or epoch timestamps in the answer. Group source attribution instead of repeating it in every bullet. '
)


def request_profile(question):
    q = question.lower()
    entry = bool(re.search(r'\b(paper|simulat\w*|automat\w*|eligib\w*|blocked|skipped|enter|entry|entries|fills?)\b', q))
    scheduled = bool(re.search(r'\b(morning brief|scheduled|premarket breakout|spy plan|no entry)\b', q))
    return dict(version=VERSION, mode='scheduled_plan' if scheduled else 'entry_review' if entry else 'market_research',
                entry_review_requested=entry, scheduled_plan_requested=scheduled,
                basis='Question scope only; not a signal, prediction or execution authorization.')


def scope_context(question, context):
    """Copy before reducing context; never edit saved plans or source observations."""
    result = dict(context)
    profile = request_profile(question)
    result['research_request'] = profile
    not_requested = []
    if profile['mode'] == 'market_research':
        for key in ('risk', 'positions', 'limits', 'trades', 'quote_checks'):
            if key in result:
                result.pop(key)
                not_requested.append(key)
        brief = result.get('spy_brief')
        if isinstance(brief, dict):
            # Keep the dated level reference, not unrelated plan admission or premium status.
            result['spy_brief'] = {k: brief[k] for k in ('symbol', 'day', 'generated_at', 'phase') if k in brief}
            result['spy_brief'].update(
                basis='Archived scheduled-brief level reference, not a current direction assessment or entry gate.',
                chart_levels=[r for r in brief.get('chart_levels', [])
                    if not any(word in str(r.get('label', '')).lower() for word in ('stop', 'target'))])
    return result, not_requested


def mentioned_futures(question, quotes):
    from .instruments import future_root
    words = set(re.findall(r'[A-Za-z][A-Za-z0-9.!@]*', question.upper()))
    return [symbol for symbol in quotes if '@' in symbol and
            (future_root(symbol) in words or symbol in words or symbol.split('@')[0] in words)]
