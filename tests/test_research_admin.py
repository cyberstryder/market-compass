"""Research inventory must not turn collected or missing evidence into results."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from compass.app import create_app
from compass.config import Config
from compass.flow_recovery import freshness
from compass.research_admin import activity, build
from compass.store import events, discord_jobs

NOW = datetime(2026, 9, 16, 18, tzinfo=timezone.utc).timestamp()


def stream(report, key):
    return next(row for row in report['streams'] if row['id'] == key)


def metrics(row):
    return {item['label']: item['value'] for item in row['metrics']}


def test_worker_update_during_snapshot_is_not_a_stale_worker():
    assert activity({'at': NOW+0.5}, NOW) == 'observing'
    assert activity({'at': NOW+60}, NOW) == 'stale'
    assert activity({'at': NOW-181}, NOW) == 'stale'
    # Source-time validation remains strict even within the heartbeat grace.
    assert freshness({'source_ts': NOW+0.5, 'received': NOW}, NOW)['status'] == 'clock_error'


def test_missing_evidence_is_unknown_not_completed_or_profitable():
    report = build({'asof': NOW}, {}, {})
    assert len({row['id'] for row in report['streams']}) == 15
    assert report['flow_research']['records_rated_by_compass'] is None
    assert report['flow_research']['all_record_outcomes'] is None
    assert report['counts']['with_measured_results'] == 0
    assert metrics(stream(report, 'intraday'))['Closed outcomes'] is None
    assert stream(report, 'morning-expirations')['measured'] is None
    assert stream(report, 'morning')['stage'] == 'awaiting_evidence'


@pytest.mark.parametrize('source_age,vendor_stale,swing_blocked', [
    (30, False, False), (180, False, False), (301, False, True),
    (30, True, True), (-1, False, True),
])
def test_poll_collection_and_entry_freshness_are_separate(source_age, vendor_stale, swing_blocked):
    flow = {'received': NOW-2, 'source_ts': NOW-source_age, 'vendor_stale': vendor_stale,
            'unique_rows': 1200, 'total': 1200, 'recovery': {'estimated_gap': 0}}
    flow['flow_freshness'] = freshness(flow, NOW)
    report = build({'asof': NOW, 'matrix': {'matrix:unusual_activity': flow},
                    'swing_ideas': {'enabled': True, 'worker': {'at': NOW}, 'counts': {}}},
                   {}, {'matrix': False, 'scanner': True, 'swing_alerts': True})
    # The web process need not carry the collector's provider credentials.
    assert stream(report, 'tm-flow')['stage'] == 'observing'
    assert metrics(stream(report, 'tm-flow'))['Unique records stored today'] == 1200
    assert report['flow_research']['coverage'] == 'awaiting_first_report'
    blocked = 'Current flow timing prevents new swing confirmation.'
    assert (blocked in stream(report, 'swing_ideas')['issues']) == swing_blocked


def test_candidate_counts_never_become_trades_and_studies_keep_their_windows():
    report = build({'asof': NOW,
        'forward_acceptance': {'at': NOW, 'since': NOW-86400,
            'swing_technical_candidates': 285, 'swing_without_flow': 285},
        'swing_ideas': {'counts': {}, 'worker': {'at': NOW}, 'enabled': True},
        'option_ideas': {'counts': {'pending': 2, 'closed': 4, 'unresolved': 3, 'excluded': 1}},
        'setup_study': {'groups': [
            {'symbol': 'SPY', 'total': 9, 'closed': 4, 'wins': 2, 'unresolved': 3},
            {'symbol': 'ESU6', 'total': 7, 'closed': 3, 'wins': 1, 'unresolved': 2}],
            'truncated': True, 'window_days': 30}}, {}, {})
    assert report['swing_audit']['technical_candidates'] == 285
    assert metrics(stream(report, 'swing_ideas'))['Idea records stored'] == 0
    assert '90-day' in stream(report, 'swing_ideas')['window']
    assert '30-day' in stream(report, 'option_ideas')['window']
    assert stream(report, 'option_ideas')['measured'] == 4
    assert metrics(stream(report, 'intraday'))['Closed outcomes'] == 4
    assert metrics(stream(report, 'futures'))['Closed outcomes'] == 3
    assert any('10,000' in issue for issue in stream(report, 'futures')['issues'])


def test_original_sender_and_delivery_health_do_not_imply_research_readiness():
    report = build({'asof': NOW,
        'projects': {'native_programs': {'morning_sender': 'original',
            'morning': {'at': NOW, 'expiration_tracks': 15, 'expiration_samples': 80}}},
        'delivery': {'pending': 2, 'routes': [{'route': 'options_ideas', 'channel': 'ideas',
            'destination_mode': 'dedicated', 'pending': 2,
            'health': {'status': 'connected'}}]}},
        {'native_report': {'at': NOW, 'expiration_comparison': {
            'collected': 8, 'pairs': [{'left': 'baseline', 'right': 'zero_dte', 'paired': 0}]}}},
        {'swing_alerts': True})
    assert stream(report, 'morning')['alerts']['owner'] == 'original'
    assert stream(report, 'morning-expirations')['measured'] == 0
    assert stream(report, 'morning-expirations')['needs_attention']
    assert stream(report, 'swing_ideas')['alerts']['health'] == 'connected'
    assert any('confirmation' in i for i in stream(report, 'swing_ideas')['issues'])
    assert report['counts']['discord_pending'] == 2


def test_reports_use_saved_clock_and_complete_pairs_not_sample_slots():
    report = build({'asof': NOW,
        'projects': {'native_programs': {'morning': {'expiration_samples': 900}}}},
        {'spy': {'latest': {'generated_at': NOW-3600, 'decision': 'WAIT'}, 'checks_today': 4},
         'native_report': {'at': NOW-400, 'expiration_comparison': {
             'collected': 12, 'pairs': [{'left': 'baseline', 'right': 'zero_dte', 'paired': 3}]}}},
        {'spy': True})
    assert stream(report, 'spy')['checked_at'] == NOW-3600
    assert stream(report, 'morning-expirations')['measured'] == 3
    assert stream(report, 'morning-expirations')['stage'] == 'stale'
    assert report['counts']['with_measured_results'] == 1


def test_admin_api_requires_login_and_only_reads_saved_evidence(tmp_path):
    cfg = Config(db='sqlite:///'+str(tmp_path/'web.db'), local=True, role='web',
        password='test-password-for-tests-only', secret='test-signing-secret-for-tests-only',
        stocks=('SPY',), futures=(), alpaca_key='', alpaca_secret='', massive='',
        databento='', matrix='', discord='', openai='')
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get('/api/research-admin').status_code == 401
        assert client.get('/api/research-admin?download=true').status_code == 401
        assert client.post('/login', json={'password': cfg.password}).status_code == 200
        db = app.state.db

        def stored_counts():
            with db.tx() as c:
                return tuple(c.execute(select(func.count()).select_from(t)).scalar_one()
                             for t in (events, discord_jobs))

        before = stored_counts()
        response = client.get('/api/research-admin?download=true')
        assert response.status_code == 200
        assert response.headers['content-disposition'].endswith('"compass-research-status.json"')
        assert 'no-store' in response.headers['cache-control']
        report = response.json()
        assert report['version'] == 'research-admin-v2'
        assert report['storage']['database'] == 'sqlite'
        assert report['flow_research']['all_record_outcomes'] is None
        assert stored_counts() == before
        assert cfg.password not in response.text and cfg.secret not in response.text
        assert client.get('/api/state').json()['research_admin']['version'] == report['version']
        assert client.get('/static/research-admin.js').status_code == 200


def test_closed_position_cache_entries_are_not_counted_as_open_options():
    report = build({'asof': NOW, 'positions': [
        {'asset': 'option', 'status': 'closed'},
        {'asset': 'option', 'status': 'open'},
        {'asset': 'future', 'status': 'open'}], 'trades': [
        {'asset': 'option', 'status': 'closed'},
        {'asset': 'option', 'status': 'open'}]}, {}, {})
    values = metrics(stream(report, 'zero-dte'))
    assert values['Open option positions'] == 1
    assert values['Option trades in recent view'] == 2


def test_placeholder_summaries_are_not_observed_zero_cohorts():
    data = {'asof': NOW, 'setup_study': {'groups': [], 'records': [], 'count': 0},
            'secondary': {'report_at': None, 'window': {'reviewed': 0},
                          'counts': {'supported': 0, 'rejected': 0}}}
    report = build(data, {}, {})
    assert metrics(stream(report, 'intraday'))['Closed outcomes'] is None
    assert metrics(stream(report, 'secondary'))['Reviewed in report'] is None
    assert stream(report, 'intraday')['coverage']['sections'][0]['status'] == 'unobserved'
    data['setup_study'].update(at=NOW, coverage='full_window', version='setup-outcomes-v3')
    data['secondary']['report_at'] = NOW
    report = build(data, {}, {})
    assert metrics(stream(report, 'intraday'))['Closed outcomes'] == 0
    assert metrics(stream(report, 'secondary'))['Reviewed in report'] == 0


def test_nested_report_limits_prevent_claiming_complete_coverage():
    report = build({'asof': NOW}, {'native_report': {'at': NOW-12,
        'morning': {'signals': 80, 'coverage': {'complete': 70, 'incomplete': 10}},
        'morning_truncated': {'signals': False, 'candidates': True,
            'research_bars': True, 'source_inventory': False}}}, {})
    c = stream(report, 'morning-expirations')['coverage']
    sections = {s['name']: s for s in c['sections']}
    assert not c['complete_scope']
    assert sections['Native report: signals']['status'] == 'complete_scope'
    assert sections['Native report: research bars']['status'] == 'truncated'
    assert sections['Native report: receipts']['status'] == 'coverage_unknown'
    assert c['report_age_seconds'] == 12
    assert c['source_age_seconds'] is None
    assert c['states'] == {'complete': 70, 'incomplete': 10}


def test_window_totals_and_unflagged_preview_are_distinct():
    report = build({'asof': NOW, 'option_ideas': {
        'counts': {'closed': 300, 'unresolved': 20}, 'records': [{}]*100},
        'obsidian': {'total_ideas': 59, 'total_events': 116,
            'ideas': [{}]*59, 'events': [{}]*100}}, {}, {})
    scopes = stream(report, 'option_ideas')['coverage']['sections']
    assert scopes[0]['status'] == 'complete_scope'
    assert scopes[1]['status'] == 'coverage_unknown'
    assert stream(report, 'option_ideas')['measured'] == 300
    obs = {s['name']: s for s in stream(report, 'obsidian')['coverage']['sections']}
    assert obs['Ideas detail']['truncated'] is False
    assert obs['Events detail']['truncated'] is True


def test_all_active_cards_have_versioned_protocols_without_resetting_existing_studies():
    report = build({'asof': NOW}, {}, {})
    assert len(report['planned']) == 2
    for row in report['streams']:
        p = row['protocol']
        assert p['primary'] and p['model'] and p['evaluation']
        if row['id'] in ('tm-flow', 'swing-flow-study', 'spy'):
            assert p['status'] == 'existing_frozen_protocol'
            assert p['frozen_on'] == '2026-09-16'
        else:
            assert all(p.get(k) for k in ('population', 'entry', 'contract', 'exit', 'costs', 'exclusions', 'uncertainty'))
    # A response must not mutate the canonical definition or a later response.
    stream(report, 'morning')['protocol']['primary'] = 'changed'
    assert stream(build({'asof': NOW}, {}, {}), 'morning')['protocol']['primary'] != 'changed'


def test_sustained_option_policy_has_separate_definition_and_evaluation():
    from compass.research_protocols import protocol
    old=protocol('option_ideas')
    current=stream(build({'asof':NOW},{},{}),'option_ideas')['protocol']
    assert old['frozen_on']=='2026-09-17' and old['model'].endswith('quote-continuity-v1')
    assert current['frozen_on']=='2026-09-24' and current['model'].endswith('quote-continuity-v2')
    assert '2026-09-25' in current['evaluation'] and 'earlier' in current['evaluation']
