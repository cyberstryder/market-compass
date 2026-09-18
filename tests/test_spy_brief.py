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
        assert len(db.recent(c, 'alert')) == 4
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


def test_chart_companion_uses_frozen_levels_and_honest_timeframes(db):
    from compass.spy_chart import companion, prompt, delivery_payload as chart_payload
    seed(db)
    r = report(db); r['id'] = 'spy-brief:2026-09-16:opening'
    p = companion(r)
    text = prompt(p, NOW)
    assert '1-minute and 15-minute' in text and 'no 1-minute entry trigger' in text
    assert 'SPX=SPY×' in text and '2026-09-15' in text and 'XSP=SPX/10' in text
    for level in r['chart_levels']:
        assert level['label']+': '+f"{level['spy']:.2f}" in text
        assert f"{level['spx_estimate']:.2f}" in text and f"{level['xsp_estimate']:.2f}" in text
    assert 'static snapshot' in text and 'preserve all my other drawings' in text
    assert p['parent_plan'] == r['id'] and p['id'] != r['id']
    row = {'id': 2, 'payload': p, 'symbol': 'SPY', 'source': 'spy_brief', 'ts': NOW}
    body = chart_payload(row, NOW)
    assert len(body['content']) < 2000
    assert len(body['embeds'][0]['description']) < 4096
    assert 'EXPIRED / REFERENCE ONLY' in prompt(p, OPEN+1025)
    r['mapping'] = {'status': 'unavailable'}
    for level in r['chart_levels']: level['spx_estimate'] = level['xsp_estimate'] = None
    assert 'draw SPY only' in prompt(r, NOW)


def test_two_messages_are_atomic_deduplicated_and_individually_acknowledged(db):
    from compass.alerts import DeliveryWorker, outbox_status
    seed(db)
    worker = BriefWorker(db, Config(local=True))
    worker.tick(NOW); worker.tick(NOW+1)
    with db.tx() as c:
        rows = sorted(db.recent(c, 'alert'), key=lambda r: r['id'])
        assert [r['payload']['status'] for r in rows] == ['spy_morning_brief', 'spy_chart_prompt']
        assert rows[1]['payload']['parent_plan'] == rows[0]['key']
    sent = []; fail_chart = True
    def handler(request):
        if request.method == 'GET': return httpx.Response(200, json={'id': '123'})
        p = json.loads(request.content); sent.append(p)
        if 'message 2 of 2' in p['content'] and fail_chart:
            return httpx.Response(429, json={'retry_after': 1})
        return httpx.Response(200, json={'id': str(1000+len(sent))})
    async def run():
        nonlocal fail_chart
        delivery = DeliveryWorker(db, Config(local=True, discord='https://discord.com/api/webhooks/123/fixture'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await delivery.tick(client, NOW)
            await delivery.tick(client, NOW+1)
            with db.tx() as c: assert outbox_status(db, c, NOW+1)['pending'] == 1
            fail_chart = False
            await delivery.tick(client, NOW+3)
    asyncio.run(run())
    assert len(sent) == 3
    assert sum('message 1 of 2' in p['content'] for p in sent) == 1
    with db.tx() as c: assert outbox_status(db, c, NOW+3)['pending'] == 0


def seed_checks(db, now, closes=None, missing=(), pm_gap=0):
    """Default every completed candle to inside the PM range; change selected blocks."""
    seed(db, now)
    closes = closes or {}
    with db.tx() as c:
        rows = db.get(c, 'bar_window:SPY')
        for row in rows:
            if row[0] >= OPEN:
                minute = (int((row[0]-OPEN)//900)+1)*15
                price = closes.get(minute, 760)
                row[1:5] = [price, price+.1, price-.1, price]
        rows = [r for r in rows[pm_gap:] if r[0] not in missing]
        db.put(c, 'bar_window:SPY', rows)


@pytest.mark.parametrize('minute', [30, 45, 60])
@pytest.mark.parametrize('price,side', [(761, 'call'), (759, 'put')])
def test_later_confirmation_uses_current_candle_and_stops_after_first_setup(db, minute, price, side):
    from compass.store import leases
    worker = BriefWorker(db, Config(local=True))
    for m in range(15, minute+1, 15):
        now = OPEN+m*60+7
        seed_checks(db, now, {minute: price})
        r = worker.tick(now)
        if m < minute:
            assert r['decision'].startswith('WAIT')
            assert r['next_check_at'] == OPEN+(m+15)*60
    assert r['decision'] == side.upper() + ' SETUP CONFIRMED'
    assert r['confirmation_kind'] == 'later' and r['session_complete']
    assert r['check_minute'] == minute and r['next_check_at'] is None
    assert r['context']['first15_close'] == 760
    assert r['context']['confirmation_candle']['close'] == price
    assert r['context']['confirmation_candle']['start'] == OPEN+(minute-15)*60
    assert r['plans'][side]['entry_reference'] == price
    sign = 1 if side == 'call' else -1
    assert r['plans'][side]['stop'] == pytest.approx(price-sign*.8)
    assert r['plans'][side]['target'] == pytest.approx(price+sign*1.6)
    assert r['frozen_premarket']['high'] == 760.5
    assert r['frozen_premarket']['low'] == 759.5
    with db.tx() as c:
        rows = db.recent(c, 'alert')
        assert len(rows) == minute//15*2
        assert sum(x['payload'].get('decision') in ('CALL SETUP CONFIRMED','PUT SETUP CONFIRMED') for x in rows) == 2
        assert not db.prefix(c, 'position:')
        c.execute(leases.update().values(until=0))
    restarted = BriefWorker(db, Config(local=True))
    assert restarted.tick(now+1) is None
    for m in range(minute+15, 61, 15):
        seed_checks(db, OPEN+m*60+7, {m: 758})
        assert restarted.tick(OPEN+m*60+7) is None
    with db.tx() as c: assert len(db.recent(c,'alert')) == minute//15*2


def test_opening_confirmation_prevents_every_followup(db):
    seed(db)
    worker = BriefWorker(db, Config(local=True))
    assert worker.tick(NOW)['decision'] == 'CALL SETUP CONFIRMED'
    for minute in (30,45,60):
        seed_checks(db, OPEN+minute*60+7, {minute:759})
        assert worker.tick(OPEN+minute*60+7) is None
    with db.tx() as c: assert len(db.recent(c,'alert')) == 2


def test_all_waits_end_with_no_entry_and_each_check_has_two_unique_messages(db):
    worker = BriefWorker(db, Config(local=True))
    for minute in (15,30,45,60):
        now = OPEN+minute*60+7
        seed_checks(db, now)
        r = worker.tick(now)
        assert worker.tick(now+1) is None
        assert r['decision'].startswith('NO ENTRY' if minute==60 else 'WAIT')
    assert r['session_complete'] and r['next_check_at'] is None
    assert worker.tick(OPEN+75*60+7) is None
    with db.tx() as c:
        rows = sorted(db.recent(c,'alert'), key=lambda x:x['id'])
        assert len(rows) == 8 and len({x['key'] for x in rows}) == 8
        for i in range(0,8,2):
            assert rows[i+1]['payload']['parent_plan'] == rows[i]['key']
        assert db.get(c,'spy-brief:latest')['decision'].startswith('NO ENTRY')
    seed_checks(db,OPEN+3617,{60:761})
    with db.tx() as c:
        preview=build(db,c,OPEN+3617)
    assert preview['decision_state']=='reference_only' and preview['session_complete']
    assert preview['next_check_at'] is None


@pytest.mark.parametrize('touch', [759.5,760.5])
def test_touching_frozen_boundary_does_not_confirm(db, touch):
    worker = BriefWorker(db, Config(local=True))
    seed_checks(db,NOW); worker.tick(NOW)
    seed_checks(db,OPEN+1807,{30:touch})
    assert worker.tick(OPEN+1807)['decision_state'] == 'waiting'


def test_no_opening_record_means_no_followup_or_invented_confirmation(db):
    seed_checks(db,OPEN+1807,{30:761})
    assert BriefWorker(db,Config(local=True)).tick(OPEN+1807) is None
    with db.tx() as c:
        assert not db.recent(c,'alert')
        preview=build(db,c,OPEN+1807)
    assert preview['decision'].startswith('WAIT')
    assert 'recorded opening WAIT required' in preview['data_blocks']


def test_missed_check_is_not_replayed_or_silently_skipped(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW); worker.tick(NOW)
    seed_checks(db,OPEN+1925,{30:761})
    for at in (OPEN+1799,OPEN+1804,OPEN+1925):
        assert worker.tick(at) is None
    for minute in (45,60):
        seed_checks(db,OPEN+minute*60+7,{30:761,minute:761})
        r=worker.tick(OPEN+minute*60+7)
        assert 'earlier scheduled check missing' in r['data_blocks']
        assert r['decision_state'] == ('no_entry' if minute==60 else 'waiting')
    with db.tx() as c: assert db.get(c,'spy-brief:2026-09-16:followup_30') is None


@pytest.mark.parametrize('missing', [OPEN+600,OPEN+1740])
def test_old_or_current_minute_gaps_remain_blocking_until_recovered(db,missing):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW); worker.tick(NOW)
    seed_checks(db,OPEN+1807,{30:761},missing=(missing,))
    first_attempt=worker.tick(OPEN+1807)
    if missing>=OPEN+900:
        assert first_attempt is None
        r=worker.tick(OPEN+1910)
    else:
        r=first_attempt
        assert r['context']['confirmation_candle']['complete']
    assert r['decision'].startswith('WAIT')
    assert not r['context']['confirmation_history_complete']
    seed_checks(db,OPEN+2707,{30:761,45:761})
    assert worker.tick(OPEN+2707)['decision']=='CALL SETUP CONFIRMED'


def test_followup_waits_briefly_for_late_closing_bar(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW); worker.tick(NOW)
    seed_checks(db,OPEN+1807,{30:761},missing=(OPEN+1740,))
    assert worker.tick(OPEN+1807) is None
    seed_checks(db,OPEN+1817,{30:761})
    r=worker.tick(OPEN+1817)
    assert r['decision']=='CALL SETUP CONFIRMED' and r['expires_at']==OPEN+1925


def test_premarket_coverage_can_recover_only_with_same_frozen_range(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW,pm_gap=58)
    first=worker.tick(NOW)
    assert 'premarket coverage incomplete' in first['data_blocks']
    seed_checks(db,OPEN+1807,{30:761},pm_gap=58)
    assert worker.tick(OPEN+1807)['decision'].startswith('WAIT')
    seed_checks(db,OPEN+2707,{30:761,45:761})
    r=worker.tick(OPEN+2707)
    assert r['decision']=='CALL SETUP CONFIRMED'
    assert r['frozen_premarket']==first['frozen_premarket']


def test_data_block_keeps_breakout_price_evidence_and_exact_missing_minutes(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW,{15:761},pm_gap=34)
    first=worker.tick(NOW)
    assert first['decision'].startswith('WAIT')
    assert first['price_condition']=='call_breakout'
    assert first['data_status']=='blocked'
    evidence=first['context']['premarket_coverage_evidence']
    assert evidence['missing_count']==len(evidence['missing_minutes'])==34
    assert evidence['required_bars']==297
    assert evidence['provider_omission_status']=='not_verified'
    assert first['context']['premarket']['bars']==296
    with db.tx() as c:
        row=next(r for r in db.recent(c,'alert') if r['payload']['status']=='spy_morning_brief')
    payload=json.dumps(delivery_payload(row,NOW))
    assert 'close above PM high' in payload and 'entry blocked by data checks' in payload
    # Later data recovery cannot rewrite the report already sent.
    seed_checks(db,OPEN+1807,{30:759})
    with db.tx() as c:
        saved=db.get(c,'spy-brief:'+day(NOW)+':opening')
    assert saved['decision']==first['decision']
    assert saved['context']['premarket_coverage_evidence']==evidence


def test_recovered_range_discrepancy_blocks_confirmation_and_keeps_drawn_levels(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW,pm_gap=58); worker.tick(NOW)
    for minute in (30,45,60):
        now=OPEN+minute*60+7
        seed_checks(db,now,{minute:762})
        with db.tx() as c:
            bars=db.get(c,'bar_window:SPY'); bars[0][2]=761.5
            db.put(c,'bar_window:SPY',bars)
        r=worker.tick(now)
        assert r['context']['premarket_complete']
        assert r['context']['premarket']['high']==760.5
        assert r['context']['premarket_observed']['high']==761.5
        assert 'recovered premarket range differs from frozen levels' in r['data_blocks']
        assert next(x['spy'] for x in r['chart_levels'] if x['label']=='PM high')==760.5
        assert r['decision_state']==('no_entry' if minute==60 else 'waiting')


def test_legacy_opening_wait_can_continue_without_changing_saved_levels(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW); first=worker.tick(NOW)
    first.pop('frozen_premarket')
    with db.tx() as c: db.put(c,first['id'],first)
    seed_checks(db,OPEN+1807,{30:759})
    r=worker.tick(OPEN+1807)
    assert r['decision']=='PUT SETUP CONFIRMED'
    assert r['frozen_premarket']['low']==759.5


def test_later_messages_preserve_expiry_bounds_and_separate_tracking(db):
    from compass.spy_chart import companion,prompt,delivery_payload as chart_payload
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW); worker.tick(NOW)
    seed_checks(db,OPEN+1807,{30:761})
    r=worker.tick(OPEN+1807)
    p=companion(r)
    row={'id':9,'payload':r,'source':'spy_brief','symbol':'SPY','ts':OPEN+1807}
    body=delivery_payload(row,OPEN+1807)
    assert 'LATER CHECK' in body['content']
    assert 'Later 15m check' in body['embeds'][0]['description']
    text=prompt(p,OPEN+1807)
    assert '09:00' in text and 'Confirmation close: 761.00' in text
    assert 'Valid until Sep 16 09:02:05 CT' in text
    assert 'EXPIRED / REFERENCE ONLY' in prompt(p,OPEN+1925)
    assert 'DATED PLAN' in delivery_payload(row,OPEN+1925)['content']
    assert sum(len(e.get('title',''))+len(e.get('description',''))+len(e.get('footer',{}).get('text','')) for e in body['embeds'])<=6000
    assert len(chart_payload({**row,'payload':p},OPEN+1807)['embeds'][0]['description'])<4096


def test_followup_session_schedule_honors_dst_and_early_close_sessions():
    from compass.spy_confirmation import scheduled_phase,clock
    for d in ('2026-07-06','2026-12-07','2026-11-27'):
        opening=session(d)[0]
        for minute in (15,30,45,60):
            at=opening+minute*60+7
            assert scheduled_phase(opening,at)==('opening' if minute==15 else 'followup_'+str(minute))
            assert clock(opening+minute*60)=={15:'08:45 CT',30:'09:00 CT',45:'09:15 CT',60:'09:30 CT'}[minute]


def test_opening_preview_cannot_bypass_a_changed_saved_premarket_range(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,NOW); worker.tick(NOW)
    seed_checks(db,NOW+10,{15:761})
    with db.tx() as c:
        bars=db.get(c,'bar_window:SPY'); bars[0][2]=762
        db.put(c,'bar_window:SPY',bars)
        r=build(db,c,NOW+10)
    assert r['decision'].startswith('WAIT')
    assert r['context']['premarket']['high']==760.5
    assert 'recovered premarket range differs from frozen levels' in r['data_blocks']
