from compass.option_continuity import assess,cohorts
from compass.store import events
from test_option_ideas import db,cfg,seed,signal,rows,NOW,CALL
from compass.option_ideas import OptionIdeas


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
