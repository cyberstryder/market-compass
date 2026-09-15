import copy
import json
from pathlib import Path
from compass.morning_gaps import EVIDENCE, chart_gaps, legacy_trace


def test_chart_evidence_exact_scope_preserves_unclassified_and_conflicts():
    assert len(EVIDENCE['records']) == 7
    assert sum(len(r['minutes']) for r in EVIDENCE['records']) == 48
    for r in EVIDENCE['records']:
        s={k:r[k] for k in ('ticker','signal_at_ms','script_version')}
        coverage={'unreceived_elapsed_minutes':r['minutes']}
        before=copy.deepcopy(coverage)
        assert chart_gaps(s,coverage)['chart_absent_minutes']==r['minutes']
        assert coverage==before
        assert not chart_gaps({**s,'signal_at_ms':s['signal_at_ms']+60000},coverage)['chart_absent_minutes']
        assert not chart_gaps({**s,'script_version':'1.2.0'},coverage)['chart_absent_minutes']
        result=chart_gaps(s,{'unreceived_elapsed_minutes':[61]})
        assert result['unclassified_minutes']==[61]
        assert result['evidence_disagrees_minutes']==r['minutes']


def test_legacy_native_packet_recovery_separate_from_research():
    fixture=json.loads((Path(__file__).parent/'fixtures/morning_history.json').read_text())
    s={**fixture['signal']['signal'],'script_version':'1.2.0'}
    event=copy.deepcopy(fixture['checkpoint']);event['signal']=s
    start=s['signal_at_ms']
    event['stock_bars']=[{**event['stock_bars'][0],'open_at_ms':start}]
    event['observation']['horizon_min']=30
    envelopes=[{'payload':{'payload':event},'sha256':'a'*64,'source_received':1.}]
    r=legacy_trace(s,{'unreceived_elapsed_minutes':[1,2]},envelopes,None,[])
    assert r['native_packet_recoverable_minutes']==[1]
    assert r['not_in_retained_packets_or_matching_research']==[2]
    assert r['classification']=='native_packet_import_mismatch'
    link={'session':s,'record':{'source_signal_id':s['signal_id'],'at_ms':start,'price':s['price']}}
    research=[{'open_at_ms':start+60000}]
    r=legacy_trace(s,{'unreceived_elapsed_minutes':[2]},envelopes,link,research)
    assert r['research_only_available_minutes']==[2]
    assert r['classification']=='checkpoint_receipts_end_before_60m'
    link['session']={**s,'stream_id':'different'}
    assert legacy_trace(s,{'unreceived_elapsed_minutes':[2]},envelopes,link,research)['research_only_available_minutes']==[]
    assert legacy_trace(s,{'unreceived_elapsed_minutes':[2]},[],None,[])['classification']=='no_retained_checkpoint'


def test_feed_cohort_uses_open_time_not_creation_time():
    from compass.forward_audit import feed_validation, FEED_FIX_AT
    rows=[{'created_at':FEED_FIX_AT-100,'opened_at':FEED_FIX_AT+1,'observation_model':'paired-recorded-quotes-v2','status':'closed'},
          {'created_at':FEED_FIX_AT-100,'opened_at':FEED_FIX_AT-1,'observation_model':'paired-recorded-quotes-v2','status':'unresolved'},
          {'created_at':FEED_FIX_AT+1,'status':'excluded'}]
    r=feed_validation(rows)
    assert r['opened']==1 and r['closed']==1 and r['unresolved']==0
