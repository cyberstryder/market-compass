from sqlalchemy import select
from compass.session_trace import tgt_trace
from compass.secondary import Secondary, pending
from compass.config import Config
from test_secondary import db, candidate, quote, NOW


def test_tgt_trace_distinguishes_window_availability_from_capture_freshness(db,monkeypatch):
    worker=Secondary(db,Config(local=True))
    candidate_row=candidate(NOW,symbol='TGT')
    with db.tx() as c:
        db.put(c,'quote:TGT',quote(NOW-10))
        worker.review(c,candidate_row,{},NOW)
        waiting=c.execute(select(pending)).mappings().one()
        worker.review(c,candidate_row,{},NOW+61,waiting)
    monkeypatch.setattr('compass.store.time.time',lambda:NOW+31)
    with db.tx() as c: db.append(c,'quote','test','TGT',NOW+30,quote(NOW+30))
    monkeypatch.setattr('compass.store.time.time',lambda:NOW+70)
    with db.tx() as c:
        db.append(c,'quote','test','TGT',NOW+58,quote(NOW+58))
        result=tgt_trace(db,c,NOW+80)[0]
        assert result['usable_by_deadline']==1
        assert result['usable_at_capture']==0
        assert result['frozen_quote']['age_at_check']==71
        assert result['archive_delay_max']==12
        assert result['verdict']=='insufficient_data'
