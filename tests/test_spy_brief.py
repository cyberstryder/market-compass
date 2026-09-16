import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from compass.alerts import dispatch
from compass.config import Config
from compass.index_reference import collect, prior_session, reference_pair, yahoo_close
from compass.market import NY, day, session
from compass.spy_brief import BriefWorker, build, delivery_payload
from compass.store import Store
from compass.vendor_freshness import CACHE_POLICY


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=NY).timestamp()


OPEN = at('2026-09-16T09:30:00')
NOW = OPEN + 907


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'brief.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def seed(db, now=NOW):
    opening = session(day(now))[0]
    pm = opening - 5.5 * 3600
    rows = []
    for i in range(int((now - pm) // 60)):
        t = pm + i * 60
        price = 760 if t < opening else 761
        rows.append([t, price, price + .5, price - .5, price, 100, price])
    d = datetime.fromtimestamp(now, NY).date() - timedelta(days=1)
    with db.tx() as c:
        n = 0
        while n < 15:
            if session(d.isoformat()):
                db.append(c, 'daily', 'fixture', 'SPY', session(d.isoformat())[0],
                          {'o': 760, 'h': 762, 'l': 758, 'c': 760, 'v': 100})
                n += 1
            d -= timedelta(days=1)
        db.put(c, 'bar_window:SPY', rows)
        db.put(c, 'quote:SPY', {'ts': now-1, 'bid': 760.99, 'ask': 761.01, 'bid_size': 1, 'ask_size': 1})
        contracts = []
        for kind in ('call', 'put'):
            ticker = 'O:SPY260916' + ('C' if kind == 'call' else 'P') + '00761000'
            q = {'ts': now-1, 'bid': 1.95, 'ask': 2.05, 'bid_size': 5, 'ask_size': 5}
            contracts.append({'symbol': ticker, 'underlying': 'SPY', 'expiry': day(now),
                'strike': 761, 'type': kind, 'multiplier': 100, 'quote': q, 'delta': .5 if kind == 'call' else -.5,
                'iv': .2, 'gamma': .1, 'oi': 100 if kind == 'call' else 200})
            db.put(c, 'quote:' + ticker, q)
        db.put(c, 'chain:SPY', {'asof': now-1, 'source': 'fixture', 'complete': True, 'contracts': contracts})
        db.put(c, 'apex:SPY', {'source_ts': now-60, 'received': now-1, 'cache_policy': CACHE_POLICY,
            'expirations': [day(now)], 'levels': [
                {'price': 759, 'score': 100, 'kind': 'apex', 'net_gex': -1e9, 'oi': 120000},
                {'price': 762, 'score': 90, 'kind': 'apex', 'net_gex': 1e8, 'oi': 20000},
                {'price': 761.5, 'score': None, 'kind': 'gamma_flip'}]})
        db.put(c, 'research:gamma_market', {'received': now-1, 'items': [{'symbol': 'SPY',
            'source_ts': now-60, 'data': {'totalGEX': 1e9, 'totalCallGEX': 2e9, 'totalPutGEX': -1e9,
                'gammaFlipLevel': 761.6, 'expirationsUsed': [day(now)]}}]})
        db.put(c, 'index_reference:SPY', reference_pair(760, 7560, prior_session(now), now, 'SYNTHETIC FIXTURE'))


def report(db, now=NOW):
    with db.tx() as c:
        return build(db, c, now, 'opening' if now >= OPEN else 'preopen')


def test_confirmed_plan_has_0dte_quotes_and_dated_index_estimates(db):
    seed(db)
    r = report(db)
    assert r['decision'] == 'CALL SETUP CONFIRMED'
    assert r['plans']['call']['entry_reference'] == 761
    assert r['plans']['call']['stop'] == pytest.approx(760.2)
    assert r['plans']['call']['target'] == pytest.approx(762.6)
    assert r['options']['call']['expiry'] == '2026-09-16'
    assert r['options']['call']['max_debit_with_entry_fee'] == pytest.approx(205.65)
    high = r['chart_levels'][0]
    assert high['spx_estimate'] == pytest.approx(760.5 * 7560 / 760)
    assert high['xsp_estimate'] == pytest.approx(high['spx_estimate'] / 10)
    assert 'Vendor and Compass GEX signs disagree' in r['gamma']['discrepancies']
    assert r['gamma']['vendor_call_gex'] == 2e9 and r['gamma']['vendor_put_gex'] == -1e9
    assert any('Overview/Apex flips differ' in v for v in r['gamma']['discrepancies'])


def test_premarket_does_not_claim_open_or_fresh_option_premiums(db):
    now = OPEN - 600
    seed(db, now)
    r = report(db, now)
    assert r['context']['regular']['open'] is None
    assert r['context']['first15_close'] is None
    assert 'CONFIRMED' not in r['decision']
    assert r['options']['call']['status'] == 'unavailable'
    assert 'ask' not in r['options']['call']
    assert 'closed' in r['options']['call']['reason']


@pytest.mark.parametrize('failure', ['minute_gap', 'pm_gap', 'future_quote', 'stale_bars'])
def test_confirmation_requires_completed_and_usable_price_evidence(db, failure):
    seed(db)
    with db.tx() as c:
        rows = db.get(c, 'bar_window:SPY')
        if failure == 'minute_gap': rows = [r for r in rows if r[0] != OPEN + 300]
        if failure == 'pm_gap': rows = rows[100:]
        if failure in ('future_quote', 'stale_bars'):
            rows = [r for r in rows if r[0] < OPEN]
            q = db.get(c, 'quote:SPY'); q['ts'] = NOW+10 if failure == 'future_quote' else NOW-600
            db.put(c, 'quote:SPY', q)
        db.put(c, 'bar_window:SPY', rows)
    assert 'CONFIRMED' not in report(db)['decision']


def test_stale_gamma_and_index_reference_are_withheld_independently(db):
    seed(db)
    with db.tx() as c:
        apex = db.get(c, 'apex:SPY'); apex['source_ts'] = NOW-86400
        db.put(c, 'apex:SPY', apex)
        ref = db.get(c, 'index_reference:SPY'); ref['reference_day'] = '2026-09-14'
        db.put(c, 'index_reference:SPY', ref)
    r = report(db)
    assert not r['gamma']['ranked'] and r['gamma']['flip'] == 761.6
    assert r['gamma']['flip_source'] == 'overview'
    assert r['mapping']['status'] == 'unavailable'
    assert all(v['spx_estimate'] is None for v in r['chart_levels'])
    assert r['decision'] == 'CALL SETUP CONFIRMED'


def test_next_day_options_never_substitute_and_wide_quotes_flagged(db):
    seed(db)
    with db.tx() as c:
        chain = db.get(c, 'chain:SPY')
        chain['contracts'][0]['expiry'] = '2026-09-17'
        put = chain['contracts'][1]
        put['quote']['ask'] = 4
        db.put(c, 'quote:' + put['symbol'], put['quote'])
        db.put(c, 'chain:SPY', chain)
    r = report(db)
    assert r['options']['call']['status'] == 'unavailable'
    assert r['options']['put']['status'] == 'wide_spread'


def test_scheduler_persists_dedup_and_does_not_replay_missed_windows(db):
    seed(db, OPEN-600)
    cfg = Config(local=True)
    w = BriefWorker(db, cfg)
    assert w.tick(OPEN-600)['phase'] == 'preopen'
    assert BriefWorker(db, cfg).tick(OPEN-590) is None
    assert w.tick(OPEN-299) is None
    seed(db)
    assert w.tick(NOW)['phase'] == 'opening'
    assert w.tick(OPEN+1025) is None
    with db.tx() as c:
        assert len(db.recent(c, 'alert')) == 2
        assert not db.prefix(c, 'position:')


def test_opening_waits_for_late_bar_then_sends_data_gap_before_deadline(db):
    seed(db, OPEN-600)
    w = BriefWorker(db, Config(local=True))
    assert w.tick(OPEN+905) is None
    r = w.tick(OPEN+1010)
    assert r and r['decision'].startswith('WAIT')
    assert w.tick(OPEN+1020) is None


def test_holiday_disabled_and_dst_scheduling(db):
    w = BriefWorker(db, Config(local=True))
    assert w.tick(at('2026-09-07T09:20:00')) is None
    assert BriefWorker(db, Config(local=True, spy_morning_brief=False)).tick(OPEN-600) is None
    assert prior_session(at('2026-09-08T09:20:00')) == '2026-09-04'
    summer = session('2026-07-06')[0]
    winter = session('2026-12-07')[0]
    assert datetime.fromtimestamp(summer-600, NY).hour == datetime.fromtimestamp(winter-600, NY).hour == 9
    assert datetime.fromtimestamp(summer, timezone.utc).hour != datetime.fromtimestamp(winter, timezone.utc).hour


def test_discord_preserves_full_plan_and_expiry_banner(db):
    seed(db)
    r = report(db)
    row = {'id': 1, 'ts': NOW, 'payload': r, 'source': 'spy_brief', 'symbol': 'SPY'}
    body = delivery_payload(row, NOW)
    assert len(body['embeds']) == 4
    assert sum(len(e.get('title','')) + len(e.get('description','')) + len(e.get('footer',{}).get('text','')) for e in body['embeds']) <= 6000
    assert all(len(e['description']) <= 4096 for e in body['embeds'])
    assert 'DATED PLAN' in delivery_payload(row, OPEN+1025)['content']
    seen = []
    async def send():
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'id': '12345'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await dispatch(client, db, 'https://discord.com/api/webhooks/fake/fake', row)
    asyncio.run(send())
    assert len(seen[0]['embeds']) == 4 and seen[0]['allowed_mentions'] == {'parse': []}


def test_index_fallback_uses_matching_raw_daily_closes_and_daily_cache(db):
    class Collector:
        cfg = Config(local=True, massive='fixture-only')
        def __init__(self): self.db, self.calls = db, []
        async def get(self, url, **kwargs):
            self.calls.append(url)
            if 'massive.com' in url: raise ValueError('No index entitlement')
            symbol, close = ('^GSPC', 7560) if '%5E' in url else ('SPY', 760)
            return {'chart': {'result': [{'meta': {'symbol': symbol},
                'timestamp': [session('2026-09-15')[0]],
                'indicators': {'quote': [{'close': [close]}], 'adjclose': [{'adjclose': [1]}]}}]}}
    c = Collector()
    result = asyncio.run(collect(c, OPEN-600))
    assert result['ratio'] == pytest.approx(7560/760)
    assert len(c.calls) == 3
    assert asyncio.run(collect(c, OPEN-590)) == result and len(c.calls) == 3
    with pytest.raises(ValueError):
        yahoo_close({'chart': {'result': []}}, '^GSPC', '2026-09-15')


def test_preview_api_requires_authentication(db):
    from compass.app import create_app
    app = create_app(Config(local=True, db=str(db.engine.url), password='long-local-password', role='web'))
    with TestClient(app) as client:
        response = client.get('/api/spy-morning-brief', follow_redirects=False)
        assert response.status_code in (401, 303, 307)
