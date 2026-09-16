"""Full cohorts, portable SQL, and pagination that cannot silently drop trials."""
import pytest
from fastapi.testclient import TestClient

from compass import futures_assessment, futures_variants
from compass.app import create_app
from compass.setup_reporting import record_page
from compass.setup_study import report_for, trials
from test_setup_study import NOW, cfg, db
from test_smoothers_postgres_handoff import pg


@pytest.fixture(params=['sqlite', 'postgres'])
def report_db(request):
    return request.getfixturevalue('db' if request.param == 'sqlite' else 'pg')


def trial(id, started=NOW-10, **overrides):
    return dict(dict(id=f'{id:064d}', source_id=str(id), symbol='SPY', strategy='breakout',
        side='long', version='setup-outcomes-v3', asset='stock', fill_version='sampled-bracket-v2',
        started=started, finished=None, status='excluded', alerted=True, pnl=None,
        r_multiple=None, elapsed_seconds=None), **overrides)


def insert(c, rows):
    for offset in range(0, len(rows), 500):
        c.execute(trials.insert(), [dict({k: row[k] for k in
            ('id', 'source_id', 'symbol', 'strategy', 'side', 'version', 'status', 'started', 'finished')},
            payload=row) for row in rows[offset:offset+500]])


def sorted_groups(report):
    return sorted(report['groups'], key=lambda g: str(sorted(g.items())))


def test_full_summary_includes_older_trials_past_ten_thousand(report_db):
    rows = [trial(i) for i in range(10001)]
    for i, status in enumerate(('closed', 'closed', 'closed', 'open', 'unresolved', 'excluded')):
        p = trial(20000+i, NOW-60, symbol='MESZ6@1', asset='future', status=status,
            alerted=i % 2 == 0, pnl=(10, -5, 0)[i] if i < 3 else None,
            r_multiple=(2, -1, 0)[i] if i < 3 else None, elapsed_seconds=60 if i < 3 else None,
            exit_reason='target' if i == 0 else 'stop' if i == 1 else None,
            fill_version=None if i == 0 else 'sampled-bracket-v2')
        if i:
            p['entry_variants'] = dict(version=futures_variants.VERSION,
                repeated='excluded' if i == 5 else 'selected',
                first_per_trend='unknown' if i == 4 else 'selected', pullback_reset='skipped')
            p['market_assessment'] = dict(version=futures_assessment.VERSION,
                state='unknown' if i == 4 else 'trend', proposed_selection='candidate')
        rows.append(p)
    with report_db.tx() as c:
        insert(c, rows+[trial(30000, NOW-30*86400-1), trial(30001, NOW+1)])
        result = report_for(c, NOW)
        assert result['count'] == 10007 and not result['truncated']
        assert result['coverage'] == 'full_window' and result['limit'] is None
        assert len(result['records']) == 100 and result['records_has_more']
        assert result['records'][0]['id'] == trial(10000)['id']
        assert sum(g['total'] for g in result['groups']) == 10007
        assert sum(g['closed'] for g in result['groups']) == 3
        assert sum(g['wins'] for g in result['groups']) == 1
        assert sum(g['losses'] for g in result['groups']) == 1
        assert sum(g['unresolved'] for g in result['groups']) == 1
        for key, model in (('entry_variants', futures_variants), ('market_assessment', futures_assessment)):
            expected = model.report(rows)
            assert result[key]['pre_activation_trials'] == expected['pre_activation_trials'] == 1
            assert sorted_groups(result[key]) == sorted_groups(expected)


def test_filtered_pages_reach_every_record_with_equal_times_and_new_arrivals(report_db):
    rows = [trial(i, alerted=i % 3 == 0) for i in range(650)]
    with report_db.tx() as c:
        insert(c, rows+[trial(1000, NOW-30*86400-1)])
        page = record_page(c, trials, NOW, cohort='alerted')
        assert page['total'] == 217 and len(page['records']) == 100
        first = page
        found = [p['id'] for p in page['records']]
        insert(c, [trial(1001, NOW+1)])
        while page['next_cursor']:
            page = record_page(c, trials, NOW+5, cohort='alerted', cursor=page['next_cursor'])
            assert page['asof'] == NOW and page['total'] == 217
            found.extend(p['id'] for p in page['records'])
        assert len(found) == len(set(found)) == 217
        assert found == sorted((p['id'] for p in rows if p['alerted']), reverse=True)
        assert record_page(c, trials, NOW+5, cohort='alerted', asof=NOW)['records'] == first['records']
        assert record_page(c, trials, NOW+5, cohort='alerted')['total'] == 218
        assert record_page(c, trials, NOW, cohort='quiet')['total'] == 433
        with pytest.raises(ValueError):
            record_page(c, trials, NOW+5, cohort='quiet', cursor=first['next_cursor'])


def test_record_pages_require_owner_and_reject_invalid_bounds(tmp_path, cfg):
    cfg.role='web';cfg.db='sqlite:///'+str(tmp_path/'web.db')
    cfg.password='test-password-long-enough';cfg.secret='test-secret-long-enough'
    app=create_app(cfg)
    with TestClient(app) as client:
        assert client.get('/api/setup-study/records').status_code == 401
        client.post('/login', json={'password':cfg.password})
        for query in ('limit=0', 'limit=101', 'cohort=bad', 'asof=nan', 'asof=inf', 'asof=-1'):
            assert client.get('/api/setup-study/records?'+query).status_code == 422
        for query in ('cursor=bad', 'cursor=W10=', 'asof=9999999999999'):
            assert client.get('/api/setup-study/records?'+query).status_code == 400
        with app.state.db.tx() as c:
            insert(c, [trial(1)])
        response=client.get('/api/setup-study/records',params={'asof':NOW,'limit':1})
        assert response.status_code == 200
        assert response.json()['records'][0]['id'] == trial(1)['id']
        assert response.json()['has_more'] is False
        assert 'no-store' in response.headers['cache-control']
