from compass.provider_coverage import compare


def test_absent_source_and_missing_collection_are_distinct():
    report=compare(0,240,[0,60,180],[0,180],300,True)
    assert report['provider_absent_minutes']==[120]
    assert report['collector_missing_minutes']==[60]
    assert report['entry_gate_unchanged'] is True


def test_partial_response_cannot_establish_provider_absence():
    report=compare(0,240,[0],[0,180],300,False)
    assert report['provider_absent_minutes']==report['stored_only_minutes']==[]
    assert report['status']=='incomplete_provider_response'


def test_inventory_deduplicates_and_respects_closed_minute_window():
    report=compare(60,180,[0,60,60,120,180],[60,120,180],300,True)
    assert report['provider_bars']==report['stored_bars']==2
    assert report['provider_absent_minutes']==report['collector_missing_minutes']==[]
