"""Bounded, explicitly incomplete previews for the research assistant.

Budget UTF-8 bytes rather than guessing four characters per token. This leaves
ample room for instructions and the 6,000-token output on the default model.
The dashboard and its stored research records are never modified.
"""
import json

from .assistant_research import scope_context

MAX_INPUT_BYTES = 52_000
SECTION_BYTES = 3_000
# These facts must travel intact or be explicitly unavailable. In particular,
# never retain a price while silently trimming its timestamp or a risk veto.
INTACT = {'operating_policy', 'asof', 'asof_ct', 'mode', 'markets', 'question_scope', 'limits',
          'risk', 'positions', 'quotes', 'levels', 'spy_brief', 'option_research',
          'technical_context', 'swing_technical_context', 'exposure', 'research_request'}
ORDER = ('operating_policy', 'asof', 'asof_ct', 'mode', 'markets', 'question_scope', 'research_request',
         'option_research', 'technical_context', 'swing_technical_context',
         'quotes', 'levels', 'exposure', 'matrix', 'spy_brief', 'research', 'flow',
         'limits', 'risk', 'positions', 'health',
         'scanner', 'option_ideas',
         'swing_ideas', 'projects', 'secondary', 'alerts', 'trades',
         'spy_study', 'tm_study', 'swing_study', 'setup_study',
         'forward_acceptance', 'obsidian', 'quote_checks', 'delivery', 'futures',
         'storage', 'workers', 'greek_diagnostics', 'notes')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def size(value):
    return len(encode(value).encode('utf-8'))


def preview(value, rows=8, depth=6):
    """Limit nested expansion, including large reports inside a single row."""
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:1000] + ' [text truncated]'
    if not isinstance(value, (dict, list)):
        return value
    if depth == 0:
        return {'assistant_omitted': True, 'reason': 'nested detail limit'}
    if isinstance(value, list):
        return [preview(item, rows, depth - 1) for item in value[:rows]]
    # Keep timestamps, source labels, decisions, counts and coverage flags ahead
    # of large child collections regardless of their insertion order.
    ordered = sorted(value, key=lambda key: isinstance(value[key], (dict, list)))
    return {key: preview(value[key], rows, depth - 1) for key in ordered[:48]}


def build_input(question, context):
    context, not_requested = scope_context(question, context)
    selected = {}
    coverage = {
        'basis': 'Bounded assistant preview; saved dashboard reports remain complete as stored.',
        'warning': 'Missing or trimmed evidence is unknown, never zero or proof that an entry is allowed.',
        'trimmed_sections': [], 'omitted_sections': [],
        'not_requested_sections': not_requested,
        'omission_reason': 'Assistant context budget; not a provider outage.',
    }
    selected['assistant_coverage'] = coverage
    # Reserve room for coverage labels even when every optional section is cut.
    budget = MAX_INPUT_BYTES - size({'question': question, 'market_context': selected}) - 6000
    for key in ORDER:
        if key not in context:
            continue
        original = context[key]
        cap = min(budget, 14_000 if key == 'option_research' else 12_000 if key in ('technical_context', 'swing_technical_context') else 10_000 if key == 'spy_brief' else 8000 if key == 'exposure' else 6000 if key in ('quotes', 'matrix', 'levels') else SECTION_BYTES)
        candidate = original
        trimmed = False
        if size(candidate) > cap and key not in INTACT:
            for rows in (8, 3, 1):
                candidate = preview(original, rows)
                trimmed = True
                if size(candidate) <= cap:
                    break
        cost = size({key: candidate})
        if size(candidate) > cap or cost > budget:
            coverage['omitted_sections'].append(key)
            continue
        selected[key] = candidate
        budget -= cost
        if trimmed:
            coverage['trimmed_sections'].append(key)
    coverage['omitted_sections'].extend(key for key in context if key not in ORDER)
    coverage['entry_evidence_incomplete'] = (context['research_request']['mode'] != 'market_research'
        and any(key in INTACT for key in coverage['omitted_sections']))
    coverage['market_evidence_omitted'] = [key for key in coverage['omitted_sections']
        if key in ('quotes', 'levels', 'technical_context', 'swing_technical_context', 'exposure', 'matrix', 'flow')]
    coverage['section_sizes'] = {key: {'original_bytes': size(context[key]),
        'supplied_bytes': size(selected[key]) if key in selected else 0}
        for key in ORDER if key in context}
    result = encode({'question': question, 'market_context': selected})
    if len(result.encode('utf-8')) > MAX_INPUT_BYTES:
        # Fail closed if a future schema change exhausts even the label reserve.
        raise ValueError('Assistant context could not fit safely')
    return result, {'input_bytes': len(result.encode('utf-8')),
                    'trimmed_sections': coverage['trimmed_sections'],
                    'omitted_sections': coverage['omitted_sections']}

