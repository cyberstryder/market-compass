"""Request progress and facts supplied to the answer, not model reasoning."""
import asyncio
import json
import logging
import math

from fastapi import HTTPException


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def evidence_summary(assistant_input):
    # Read the final budgeted payload: never advertise a fact dropped upstream.
    context = json.loads(assistant_input)['market_context']
    coverage = context.get('assistant_coverage', {})
    limited = set(coverage.get('trimmed_sections', []))
    omitted = set(coverage.get('omitted_sections', []))
    cards = []
    for symbol in context.get('question_scope', {}).get('symbols', [])[:8]:
        quote = context.get('quotes', {}).get(symbol) or {}
        technical = context.get('technical_context', {}).get(symbol) or {}
        swing = context.get('swing_technical_context', {}).get(symbol) or {}

        def add(label, section, fields, source, at=None, time_label='As of', note=None):
            values = [{'label': title, 'value': value} for title, value in fields if value is not None]
            state = 'not_included' if section in omitted else 'missing' if not values else 'limited' if section in limited or len(values) < len(fields) else 'included'
            cards.append(dict(symbol=symbol, label=label, status=state, values=values,
                              source=str(source or 'Source not recorded')[:160], at=number(at),
                              time_label=time_label, note=str(note)[:300] if note else None))

        option_data = context.get('option_research', {})
        if option_data:
            option = option_data.get('symbols', {}).get(symbol, {})
            req = option_data.get('request', {})
            add('Requested options', 'option_research',
                [('From DTE', req.get('min_dte')), ('Through DTE', req.get('max_dte')),
                 ('Sampled contracts', len(option.get('candidates', [])) if option.get('status')=='available' else None)],
                option.get('source'), option.get('fetched_at'), 'Fetched',
                str(option.get('status', option_data.get('status', req.get('status')))) +
                '; bounded chain sample; each quote has its own source time.')

        bid, ask = number(quote.get('bid')), number(quote.get('ask'))
        usable = bid is not None and ask is not None and 0 < bid <= ask
        add('Two-sided quote', 'quotes', [('Bid', bid if usable else None), ('Ask', ask if usable else None)],
            quote.get('source'), quote.get('ts'), note=None if usable else 'No usable two-sided quote in this request.')
        add('VWAP', 'technical_context', [('VWAP', number(technical.get('vwap')))],
            'Compass minute bars', technical.get('asof'))
        add('Short EMAs', 'technical_context', [('EMA 9', number(technical.get('ema9'))),
            ('EMA 21', number(technical.get('ema21')))], 'Compass minute bars', technical.get('asof'))
        add('Intraday structure', 'technical_context', [('Prior 5-bar high', number(technical.get('prior5_high'))),
            ('Prior 5-bar low', number(technical.get('prior5_low')))], 'Compass minute bars', technical.get('asof'))
        bias = technical.get('htf15_bias')
        add('15-minute bias', 'technical_context', [('Bias', {1: 'Bullish', -1: 'Bearish', 0: 'Neutral'}.get(bias))],
            'Compass completed bars', technical.get('asof'))
        add('Weekly context', 'swing_technical_context', [('Weekly close', number(swing.get('weekly_close'))),
            ('10-week SMA', number(swing.get('weekly_sma10')))], 'Alpaca daily bars / Compass',
            swing.get('computed_at'), 'Computed',
            'Completed weeks through ' + str(swing['weekly_through']) if swing.get('weekly_through') else 'Completed-week source date not available.')
    return dict(asof=context.get('asof'), symbols=context.get('question_scope', {}).get('symbols', [])[:8],
                cards=cards, limited=bool(limited or omitted),
                note='Data supplied to this answer. Source times may be older than this request; inclusion does not establish trade eligibility.')


async def event_stream(operation, *, timeout=90, heartbeat=10):
    """One request, one provider call; cancel work if its stream is closed."""
    queue = asyncio.Queue(maxsize=8)

    async def run():
        try:
            async with asyncio.timeout(timeout):
                result = await operation(queue.put_nowait)
            queue.put_nowait({'type': 'complete', **result})
        except HTTPException as error:
            queue.put_nowait({'type': 'error', 'detail': error.detail, 'status': error.status_code})
        except TimeoutError:
            queue.put_nowait({'type': 'error', 'detail': 'The answer took too long. Please try again.', 'status': 504})
        except Exception as error:
            logging.getLogger('uvicorn.error').warning('Assistant request failed: %s', type(error).__name__)
            queue.put_nowait({'type': 'error', 'detail': 'Unable to finish this answer. Please try again.', 'status': 500})

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), heartbeat)
            except TimeoutError:
                event = {'type': 'heartbeat'}
            yield json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
            if event['type'] in ('complete', 'error'):
                break
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

