"""Tests for the /api/state two-tier split (fast 3s tier vs slow 5min tier).

The monolithic /api/state snapshot grew large enough to block the browser
main thread on parse+render. The fast tier must stay small and exclude the
heavy study/matrix/report sections; the slow tier must carry them intact.
"""
import time

from fastapi.testclient import TestClient

from compass.app import create_app
from compass.config import Config

# Keys that must live in the slow tier only.
HEAVY_KEYS = ['scanner', 'projects', 'obsidian', 'forward_acceptance',
              'tm_study', 'swing_study', 'spy_study', 'secondary',
              'setup_study', 'option_ideas', 'swing_ideas', 'matrix',
              'greek_diagnostics', 'levels', 'exposure', 'research_admin',
              'notes', 'storage']

# Keys that must stay in the fast 3s tier.
FAST_KEYS = ['asof', 'markets', 'health', 'quotes', 'trades', 'positions',
             'operating_policy', 'delivery', 'futures', 'risk', 'limits',
             'ai_configured', 'quote_checks']


def _seed(app, now):
    # A 500-strike matrix: the _cells-suffixed fields are stripped by the
    # snapshot builder, the rest must survive into the slow tier.
    strikes = [{'strike': 100 + i, 'gex': 1.5, 'vex': 0.5, 'gex_cells': 12,
                'note': 'x' * 200} for i in range(500)]
    with app.state.db.tx() as c:
        app.state.db.put(c, 'matrix:SPY',
                          {'symbol': 'SPY', 'strikes': strikes,
                           'expirations': [{'expiry': '2026-10-02'}],
                           'source_ts': now, 'received': now})
        app.state.db.put(c, 'tm-study-v1:report',
                          {'at': now, 'counts': {'prospective': 3},
                           'records': [{'path': 'y' * 1000}] * 20})
        app.state.db.put(c, 'forward-acceptance-v1:report',
                          {'reliability': {'at': now}, 'blob': 'z' * 20000})


def _seeded_client(tmp_path):
    cfg = Config(local=True, role='web', db='sqlite:///' + str(tmp_path / 'tiers.db'),
                 password='long-test-password-123')
    return create_app(cfg), cfg


def test_fast_tier_excludes_heavy_sections(tmp_path):
    app, cfg = _seeded_client(tmp_path)
    with TestClient(app) as client:
        client.post('/login', json={'password': cfg.password})
        _seed(app, time.time())
        r = client.get('/api/state')
        assert r.status_code == 200
        data = r.json()
        for key in HEAVY_KEYS:
            assert key not in data, f'{key} must not be in the fast tier'
        for key in FAST_KEYS:
            assert key in data, f'{key} must stay in the fast tier'
        # The seeded matrix alone serializes to >150KB; the fast tier must
        # stay well under the 200KB budget.
        size = len(r.content)
        assert size < 200_000, f'fast tier too large: {size} bytes'


def test_slow_tier_carries_heavy_sections_intact(tmp_path):
    app, cfg = _seeded_client(tmp_path)
    now = time.time()
    with TestClient(app) as client:
        client.post('/login', json={'password': cfg.password})
        _seed(app, now)
        r = client.get('/api/state/studies')
        assert r.status_code == 200
        data = r.json()
        for key in HEAVY_KEYS:
            assert key in data, f'{key} must be in the slow tier'
        # Matrix strikes survive with _cells fields stripped, others intact.
        spy = data['matrix']['matrix:SPY']
        assert len(spy['strikes']) == 500
        assert all('gex_cells' not in row for row in spy['strikes'])
        assert spy['strikes'][0]['note'] == 'x' * 200
        assert spy['freshness']
        # Study reports arrive unmodified.
        assert data['tm_study']['at'] == now
        assert data['tm_study']['counts'] == {'prospective': 3}
        assert data['forward_acceptance']['blob'] == 'z' * 20000
        assert 'research_admin' in data
        # Fast-tier live keys are not duplicated here (frontend merges).
        assert 'asof' not in data
        assert 'quotes' not in data


def test_tiers_merge_to_full_snapshot(tmp_path):
    """Fast + slow tiers together cover every key the old monolith served."""
    app, cfg = _seeded_client(tmp_path)
    with TestClient(app) as client:
        client.post('/login', json={'password': cfg.password})
        _seed(app, time.time())
        fast = client.get('/api/state').json()
        slow = client.get('/api/state/studies').json()
        merged = {**slow, **fast}
        for key in HEAVY_KEYS + FAST_KEYS:
            assert key in merged, f'{key} missing from merged tiers'
