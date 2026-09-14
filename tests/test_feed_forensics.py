from types import SimpleNamespace
from compass.feed_forensics import quote_reason, clock_fields, futures_gaps
from compass.setup_study import SetupStudy
from compass.engine import spec
from test_setup_study import db, cfg, NOW, quote, signal, rows
from test_quote_coverage import archive


def test_quote_failures_keep_receipt_and_source_clocks_separate():
    q=quote()
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW,received=NOW+1))=='usable'
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW,received=NOW+6))=='stale_or_invalid_at_receipt'
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW,received=NOW-1))=='future_at_receipt'
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW+1,received=NOW+2))=='timestamp_mismatch'


def test_clock_fields_omit_market_payloads_and_credentials():
    result=clock_fields({'api_key':'secret','data':{'spot':100,'snapshotTime':'2026-09-14T20:00:00Z'},
        'freshness':{'stale':True,'refreshSeconds':300,'credentials':'secret'},'cached':True})
    assert result=={'data.snapshotTime':'2026-09-14T20:00:00Z','freshness.stale':True,
        'freshness.refreshSeconds':300,'cached':True}


def test_gap_report_separates_late_arrivals_and_preserves_outcome(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        study.tick(c,NOW+20)
        before=rows(c)
        archive(db,c,NOW+10,symbol='MESZ6@1',received=NOW+30)
        result=futures_gaps(db,c,NOW+40)
        row=result['rows'][0]
        assert row['stored_after_finish']==1 and row['available_by_finish']=={}
        assert row['first_next_quote']['reason']=='stale_or_invalid_at_receipt'
        assert rows(c)==before and rows(c)[0]['status']=='unresolved'
