import copy
import json
import time

import httpx
from fastapi.testclient import TestClient

from compass.app import create_app
from compass.assistant_context import MAX_INPUT_BYTES, build_input
from compass.config import Config


def test_large_nested_reports_fit_without_losing_price_plan_or_risk():
    report = {'at': 123, 'coverage': 'full_window', 'counts': {'unresolved': 31},
              'records': [{'detail': 'quote path ' * 10000}] * 100}
    context = {
        'asof': 123, 'mode': 'SIMULATED', 'question_scope': {'symbols': ['SPY']},
        'quotes': {'SPY': {'bid': 100, 'ask': 101, 'ts': 122, 'source': 'test'}},
        'risk': {'risk:2026-09-17': {'realized': -390.5, 'entries': 40}},
        'limits': {'daily_realized_loss': 300},
        'spy_brief': {'decision': 'NO ENTRY', 'generated_at': 120, 'expires_at': 121},
        'tm_study': report, 'projects': report, 'research_admin': report,
    }
    before = copy.deepcopy(context)
    question = 'Is SPY eligible? ' + '📊' * 1900
    value, diagnostic = build_input(question, context)
    assert len(value.encode('utf-8')) <= MAX_INPUT_BYTES
    result = json.loads(value)
    assert result['question'] == question
    packed = result['market_context']
    for key in ('quotes', 'risk', 'limits', 'spy_brief'):
        assert packed[key] == context[key]
    assert 'research_admin' not in packed
    assert 'research_admin' in diagnostic['omitted_sections']
    assert 'projects' in diagnostic['trimmed_sections']
    assert packed['tm_study']['counts'] == {'unresolved': 31}
    assert context == before


def test_oversized_entry_evidence_is_unavailable_instead_of_partially_reassuring():
    context = {'quotes': {'SPY': {'bid': 100, 'source': 'x' * 10000, 'ts': 123}},
               'risk': {'detail': 'x' * 10000, 'realized': -400},
               'limits': {'daily_realized_loss': 300}}
    value, diagnostic = build_input('May I enter SPY?', context)
    packed = json.loads(value)['market_context']
    assert 'quotes' not in packed and 'risk' not in packed
    assert packed['limits'] == context['limits']
    assert packed['assistant_coverage']['entry_evidence_incomplete'] is True
    assert set(diagnostic['omitted_sections']) == {'quotes', 'risk'}


def test_total_budget_applies_across_individually_small_sections():
    from compass.assistant_context import ORDER
    context = {key: {'label': 'data ' * 590} for key in ORDER}
    question = '📊' * 2000
    value, diagnostic = build_input(question, context)
    assert len(value.encode('utf-8')) <= MAX_INPUT_BYTES
    assert json.loads(value)['question'] == question
    assert diagnostic['omitted_sections']


def test_ask_large_saved_reports_keep_spy_decision_and_send_one_bounded_request(tmp_path, monkeypatch):
    cfg = Config(local=True, role='web', db='sqlite:///' + str(tmp_path / 'ask.db'),
                 password='test-password-with-enough-length',
                 secret='test-signing-secret-with-enough-length', openai='fake-key')
    calls = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            calls.append(json)
            assert len(json['input'].encode('utf-8')) <= MAX_INPUT_BYTES
            return httpx.Response(200, json={'status': 'completed',
                'output': [{'content': [{'type': 'output_text', 'text': 'The saved plan says NO ENTRY.'}]}]})

    monkeypatch.setattr('compass.app.httpx.AsyncClient', lambda **kwargs: Client())
    # Ask must not build the duplicate administrative report at all.
    def unexpected_admin(*args):
        raise AssertionError('Ask should not build research admin')
    monkeypatch.setattr('compass.research_admin.snapshot', unexpected_admin)
    app = create_app(cfg)
    now = time.time()
    large = {'at': now, 'counts': {'unresolved': 7},
             'records': [{'path': 'saved observation ' * 2000}] * 100}
    brief = {'symbol': 'SPY', 'decision': 'NO ENTRY', 'generated_at': now - 3600,
             'expires_at': now - 1800, 'day': '2026-09-17'}
    with TestClient(app) as client:
        with app.state.db.tx() as c:
            app.state.db.put(c, 'tm-study-v1:report', large)
            app.state.db.put(c, 'spy-brief:latest', brief)
            app.state.db.put(c, 'risk:2026-09-17', {'realized': -390.5, 'entries': 40})
        client.post('/login', json={'password': cfg.password})
        result = client.post('/api/ask', json={'question': 'spy is climbing. Is an entry advisable?'})
        assert result.status_code == 200
        with app.state.db.tx() as c:
            health = app.state.db.get(c, 'health:assistant')
            assert health['context_size']['input_bytes'] <= MAX_INPUT_BYTES
            assert app.state.db.get(c, 'tm-study-v1:report') == large
    assert len(calls) == 1
    packed = json.loads(calls[0]['input'])['market_context']
    assert packed['spy_brief'] == brief
    assert packed['risk']['risk:2026-09-17']['realized'] == -390.5
    assert packed['question_scope']['symbols'] == ['SPY']
    assert 'respect its expiry, WAIT or NO ENTRY' in calls[0]['instructions']
    assert 'A SPY NO ENTRY decision is terminal' in calls[0]['instructions']
    assert 'Do not suggest waiting for a later breakout or candle' in calls[0]['instructions']
