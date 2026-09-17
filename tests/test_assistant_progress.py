import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from compass.app import create_app
from compass.assistant_context import build_input
from compass.assistant_progress import evidence_summary, event_stream
from compass.config import Config


def test_evidence_uses_only_budgeted_facts_and_distinguishes_computation_time():
    context = {'asof': 100, 'question_scope': {'symbols': ['TSLA']},
        'quotes': {'TSLA': {'bid': 367.9, 'ask': 367.99, 'ts': 95, 'source': 'alpaca'}},
        'technical_context': {'TSLA': {'asof': 90, 'vwap': 368.53, 'ema9': 368.12,
                                     'htf15_bias': 1, 'prior5_high': 368.46, 'prior5_low': 367.73}},
        'swing_technical_context': {'TSLA': {'computed_at': 98, 'weekly_close': 365.44,
                                          'weekly_sma10': 351.482, 'weekly_through': '2026-09-11'}}}
    summary = evidence_summary(build_input('TSLA?', context)[0])
    cards = {card['label']: card for card in summary['cards']}
    assert cards['Two-sided quote']['at'] == 95
    assert cards['VWAP']['values'] == [{'label': 'VWAP', 'value': 368.53}]
    assert cards['Short EMAs']['status'] == 'limited'  # EMA 21 is absent, not zero.
    assert cards['15-minute bias']['values'][0]['value'] == 'Bullish'
    assert cards['Weekly context']['time_label'] == 'Computed'
    assert cards['Weekly context']['at'] == 98
    assert '2026-09-11' in cards['Weekly context']['note']
    context['quotes']['TSLA']['detail'] = 'x' * 10000
    summary = evidence_summary(build_input('TSLA?', context)[0])
    quote = summary['cards'][0]
    assert quote['status'] == 'not_included' and quote['values'] == []
    assert summary['limited'] is True


def test_progress_arrives_before_answer_and_cancels_the_pending_operation():
    async def check():
        cancelled = asyncio.Event()
        async def operation(notify):
            notify({'type': 'stage', 'stage': 'gathering'})
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        events = event_stream(operation, heartbeat=.01)
        first = json.loads(await asyncio.wait_for(anext(events), .5))
        assert first == {'type': 'stage', 'stage': 'gathering'}
        assert json.loads(await anext(events))['type'] == 'heartbeat'
        await events.aclose()
        assert cancelled.is_set()
    asyncio.run(check())


def test_progress_timeout_and_unexpected_errors_never_complete_or_leak_details():
    async def check():
        async def slow(notify): await asyncio.Event().wait()
        rows = [json.loads(row) async for row in event_stream(slow, timeout=.01)]
        assert rows[-1]['type'] == 'error' and rows[-1]['status'] == 504
        async def broken(notify): raise RuntimeError('private database details')
        rows = [json.loads(row) async for row in event_stream(broken)]
        assert rows[-1]['status'] == 500 and 'private' not in rows[-1]['detail']
    asyncio.run(check())


@pytest.mark.parametrize('provider_status', ['completed', 'incomplete', 'rejected'])
def test_streamed_endpoint_keeps_auth_limits_and_complete_answer_rules(tmp_path, monkeypatch, provider_status):
    cfg = Config(local=True, role='web', db='sqlite:///' + str(tmp_path / 'ask.db'),
                 password='test-password-with-enough-length', secret='test-signing-secret-with-enough-length', openai='fake-key', stocks=('TSLA',))
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            calls.append(json)
            if provider_status == 'rejected':
                return httpx.Response(400, json={'error': {'code': 'context_length_exceeded', 'message': 'too large'}})
            return httpx.Response(200, json={'status': provider_status,
                'incomplete_details': {'reason': 'max_output_tokens'} if provider_status == 'incomplete' else None,
                'output': [{'content': [{'type': 'output_text', 'text': 'Recorded TSLA context.'}]}]})
    monkeypatch.setattr('compass.app.httpx.AsyncClient', lambda **kwargs: Client())
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.post('/api/ask/stream', json={'question': 'TSLA?'}).status_code == 401
        client.post('/login', json={'password': cfg.password})
        assert client.post('/api/ask/stream', json={'question': 'TSLA?'}, headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        with app.state.db.tx() as c:
            app.state.db.put(c, 'scanner_features:TSLA', {'asof': time.time(), 'vwap': 368.53})
        result = client.post('/api/ask/stream', json={'question': 'TSLA?'})
        assert result.headers['content-type'].startswith('application/x-ndjson')
        assert result.headers['cache-control'] == 'no-store'
        rows = [json.loads(line) for line in result.text.splitlines()]
        assert [r.get('stage') for r in rows if r['type'] == 'stage'] == ['gathering', 'preparing', 'generating']
        data = next(r for r in rows if r['type'] == 'context')
        assert next(c for c in data['cards'] if c['label'] == 'VWAP')['values'][0]['value'] == 368.53
        assert rows[-1]['type'] == ('complete' if provider_status == 'completed' else 'error')
        if provider_status != 'completed': assert 'answer' not in rows[-1]
        assert len(calls) == 1
        with app.state.db.tx() as c:
            app.state.db.put(c, 'assistant:budget', {'day': int(time.time() // 86400), 'count': 100})
        blocked = client.post('/api/ask/stream', json={'question': 'TSLA?'})
        assert json.loads(blocked.text.splitlines()[-1])['status'] == 429
        assert len(calls) == 1


def test_browser_stream_reader_handles_split_utf8_errors_and_interrupted_connections():
    if not shutil.which('node'): pytest.skip('Node runs this parser check in CI')
    subprocess.run(['node', '--test', 'tests/assistant_stream.test.cjs'], cwd=Path(__file__).parents[1], check=True, capture_output=True, text=True)
