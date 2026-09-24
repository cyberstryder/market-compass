import pytest
from compass.option_continuity import assess,assess_sustained,cohorts,SUSTAINED_VERSION
from compass.store import events
from test_option_ideas import db,cfg,seed,signal,rows,NOW,CALL,q,contract
from compass.option_ideas import OptionIdeas
from tests.test_smoothers_postgres_handoff import pg


def test_continuity_requires_observed_history_not_just_current_quote(db,cfg):
    with db.tx() as c:
        seed(db,c)
        c.execute(events.delete().where(events.c.kind=='quote'))
        service=OptionIdeas(db,cfg);service.queue(c,signal(),NOW);service.tick(c,NOW)
        p=rows(c)[0]
        assert p['status']=='pending' and p['waiting_reason']=='Waiting for option quote continuity'
        service.tick(c,NOW+121)
        report=cohorts(rows(c))
        assert report['continuity_excluded']==1
        assert sum(r['opened'] for r in report['cohorts'])==0


def test_late_archive_and_interior_gaps_do_not_qualify(db,cfg):
    with db.tx() as c:
        seed(db,c)
        assert assess(db,c,CALL,NOW)['ready']
        c.execute(events.update().where(events.c.kind=='quote').values(received=NOW+1))
        assert not assess(db,c,CALL,NOW)['ready']


def test_open_observations_are_not_completed_and_exclusions_are_separate():
    report=cohorts([dict(status=s,collection_version='v3',opened_at=1) for s in ('open','closed','unresolved')]+[dict(status='excluded')])
    cohort=next(x for x in report['cohorts'] if x['version']=='v3')
    assert cohort['opened']==3 and cohort['open']==1 and cohort['closed']==1 and cohort['unresolved']==1


def test_interior_silence_rejects_despite_fresh_last_quote(db,cfg):
    with db.tx() as c:
        seed(db,c)
        c.execute(events.delete().where(events.c.kind=='quote'))
        from test_option_ideas import q
        for offset in (25,24,23,22,1):
            db.append(c,'quote','massive',CALL,NOW-offset,q(NOW-offset,2,2.05))
            c.execute(events.update().where(events.c.symbol==CALL,events.c.ts==NOW-offset).values(received=NOW-offset+.1))
        quality=assess(db,c,CALL,NOW)
        assert quality['samples']==5 and quality['max_gap_seconds']==21 and not quality['ready']


def archive(db,c,stamps,symbol=CALL,source='massive',delay=.1):
    for stamp in stamps:
        db.append(c,'quote',source,symbol,stamp,q(stamp,2,2.05))
        c.execute(events.update().where(events.c.symbol==symbol,events.c.ts==stamp,
            events.c.source==source).values(received=stamp+delay))


@pytest.mark.parametrize('database',('db','pg'))
@pytest.mark.parametrize('older',[False,True])
def test_short_burst_cannot_hide_missing_history_or_a_recent_drought(request,database,older):
    db=request.getfixturevalue(database)
    with db.tx() as c:
        if older:archive(db,c,[NOW-offset for offset in range(295,179,-5)])
        archive(db,c,[NOW-offset for offset in (25,20,15,10,5)])
        assert assess(db,c,CALL,NOW)['ready']
        quality=assess_sustained(db,c,CALL,NOW)
        assert quality['short_window_ready'] and not quality['ready']
        assert quality['reason']==('recent_quote_drought' if older else 'insufficient_sustained_history')


def test_fresh_opra_recovery_can_supply_sustained_history_during_stream_silence(db):
    with db.tx() as c:
        # Original-time OPRA evidence counts even when the Massive socket is quiet.
        archive(db,c,[NOW-offset for offset in range(95,30,-5)])
        archive(db,c,[NOW-offset for offset in range(30,0,-5)],source='alpaca_opra_recovery')
        quality=assess_sustained(db,c,CALL,NOW)
        assert quality['ready'] and quality['sustained']['span_seconds']==90
        assert quality['version']==SUSTAINED_VERSION


@pytest.mark.parametrize('delay',[6,1000])
def test_stale_or_not_yet_recorded_recovery_cannot_bridge_a_drought(db,delay):
    with db.tx() as c:
        archive(db,c,[NOW-offset for offset in range(95,0,-5) if offset not in (80,75,70)])
        archive(db,c,[NOW-80,NOW-75,NOW-70],source='alpaca_opra_recovery',delay=delay)
        quality=assess_sustained(db,c,CALL,NOW)
        assert quality['short_window_ready'] and quality['reason']=='recent_quote_drought'
        assert quality['sustained']['max_gap_seconds']==20


def test_drought_remains_visible_after_a_new_good_burst_then_ages_out(db):
    with db.tx() as c:
        archive(db,c,[NOW-offset for offset in range(295,179,-5)])
        archive(db,c,[NOW-offset for offset in range(95,0,-5)])
        assert assess_sustained(db,c,CALL,NOW)['reason']=='recent_quote_drought'
        # New usable quotes eventually replace the old drought in the five-minute review.
        archive(db,c,[NOW+offset for offset in range(0,200,5)])
        quality=assess_sustained(db,c,CALL,NOW+200)
        assert quality['ready'] and quality['sustained']['droughts']==0


def test_repeated_timestamps_and_truncated_archive_do_not_qualify(db):
    with db.tx() as c:
        for source in ('massive','alpaca_opra_recovery','massive_rest'):
            archive(db,c,[NOW-offset for offset in (25,20,15,10,5)],source=source)
        quality=assess_sustained(db,c,CALL,NOW)
        assert quality['sustained']['samples']==5 and not quality['ready']
        archive(db,c,[NOW-200+i*.01 for i in range(1201)])
        assert assess_sustained(db,c,CALL,NOW)['reason']=='archive_truncated'


@pytest.mark.parametrize('drought',[False,True])
def test_selection_prefers_sustained_alternative_and_freezes_entry_evidence(db,cfg,drought):
    alternative='O:SPY260918C00101000'
    with db.tx() as c:
        seed(db,c)
        c.execute(events.delete().where(events.c.symbol==CALL))
        # Both contracts pass the original 30-second check. The nearer strike
        # either has an old drought or poorer sustained cadence.
        offsets=([295,290,285] if drought else list(range(145,29,-12)))+[25,20,15,10,5]
        archive(db,c,[NOW-offset for offset in offsets])
        archive(db,c,[NOW-offset for offset in range(95,0,-5)],symbol=alternative)
        chain=db.get(c,'chain:SPY')
        chain['contracts'].append({**contract(),'symbol':alternative,'strike':101})
        db.put(c,'chain:SPY',chain)
        db.put(c,'quote:'+alternative,q(NOW,2,2.05))
        db.put(c,'options:subscriptions',dict(at=NOW,symbols=[CALL,alternative]))
        worker=OptionIdeas(db,cfg);worker.queue(c,signal(),NOW);worker.tick(c,NOW)
        p=rows(c)[0]
        assert p['status']=='open' and p['contract']['symbol']==alternative
        assert p['continuity_policy']==SUSTAINED_VERSION
        assert p['continuity_checks'][alternative]['sustained']['span_seconds']==90
        if drought:assert p['continuity_checks'][CALL]['reason']=='recent_quote_drought'
        else:assert p['continuity_checks'][CALL]['ready']


@pytest.mark.parametrize('gap',[15,17.835])
def test_observed_ttd_gap_exceeds_sustained_limit_despite_recent_activity(db,gap):
    with db.tx() as c:
        archive(db,c,[NOW+offset for offset in range(-295,-164,5)])
        archive(db,c,[NOW-165+gap+offset for offset in range(0,120,5)])
        archive(db,c,[NOW-offset for offset in range(30,0,-5)])
        quality=assess_sustained(db,c,CALL,NOW)
        assert quality['short_window_ready']
        assert quality['ready']==(gap==15)
        if gap>15:assert quality['reason']=='recent_quote_drought'


def test_drought_blocks_entry_until_deadline_without_changing_existing_results(db,cfg):
    with db.tx() as c:
        seed(db,c)
        archive(db,c,[NOW-295,NOW-290,NOW-285])
        worker=OptionIdeas(db,cfg);worker.queue(c,signal(),NOW);worker.tick(c,NOW)
        p=rows(c)[0]
        assert p['status']=='pending' and not db.recent(c,'alert')
        worker.tick(c,NOW+121)
        p=rows(c)[0]
        assert p['status']=='excluded' and p['pnl'] is None
        old=dict(status='unresolved',opened_at=NOW-500,continuity_policy='quote-continuity-v1',
            collection_version='option-reliability-v7',pnl=None)
        report=cohorts([old,p])
        assert report['continuity_excluded']==1 and report['candidates']==1
        assert report['continuity_rejections']=={'recent_quote_drought':1}
        assert {r['policy']:r['unresolved'] for r in report['continuity_cohorts']}=={
            'quote-continuity-v1':1,SUSTAINED_VERSION:0}
        assert old['pnl'] is None and old['status']=='unresolved'


def test_policy_reasons_count_candidates_once_and_keep_contract_checks_separate():
    from copy import deepcopy
    rows=[dict(status='excluded',continuity_policy=SUSTAINED_VERSION,exit_reason=reason)
        for reason in ['Fresh underlying quote after the trigger required']*2+['No recent eligible listed contract']]
    rows += [dict(status='excluded',continuity_policy='quote-continuity-v1',exit_reason='Waiting for option quote continuity',
        continuity_checks={'A':{'ready':False,'reason':'recent_quote_drought'},'B':{'ready':False,'reason':'recent_quote_drought'}}),
        dict(status='pending',continuity_policy=SUSTAINED_VERSION,waiting_reason='Waiting for a fresh, liquid streamed option quote'),
        dict(status='excluded'),dict(status='closed',continuity_policy=SUSTAINED_VERSION,opened_at=NOW)]
    original=deepcopy(rows)
    report=cohorts(rows);policies={p['policy']:p for p in report['continuity_cohorts']}
    assert policies[SUSTAINED_VERSION]['exclusion_reasons']=={
        'Fresh underlying quote after the trigger required':2,'No recent eligible listed contract':1}
    assert policies[SUSTAINED_VERSION]['waiting_reasons']=={'Waiting for a fresh, liquid streamed option quote':1}
    assert policies[SUSTAINED_VERSION]['blocked_without_saved_quote_check']==4
    assert policies['quote-continuity-v1']['quote_check_rejections']=={'recent_quote_drought':2}
    assert policies['unversioned']['exclusion_reasons']=={'Not recorded':1}
    for policy in policies.values():
        assert sum(policy['exclusion_reasons'].values())==policy['excluded']
        assert sum(policy['waiting_reasons'].values())==policy['pending']
    assert rows==original and report['continuity_rejections']=={}
