from types import SimpleNamespace
import pytest
from compass.futures_ingress import IngressProbe


def test_probe_distinguishes_silent_sources_from_blocking_writes():
    now=[100.0]; emitted=[]
    probe=IngressProbe('CME',clock=lambda:now[0],monotonic=lambda:now[0],emit=emitted.append)
    def quote(stamp):
        return SimpleNamespace(ts_event=int(stamp*1e9),ts_recv=int(stamp*1e9),
            levels=[SimpleNamespace(bid_px=100,ask_px=101,bid_sz=1,ask_sz=1)])
    callback=probe.wrap(lambda r: probe.quote('ESZ6@1',r))
    callback(quote(100));now[0]=120;callback(quote(120))
    row=probe.symbols['ESZ6@1']
    assert row['max_source_gap']==row['max_arrival_gap']==20
    assert row['max_event_age']==probe.max_callback_seconds==0
    def write(): now[0]+=7
    slow=probe.wrap(lambda r: (probe.quote('ESZ6@1',r),probe.writing('ESZ6@1',write)))
    now[0]=130;slow(quote(129))
    assert row['max_write_seconds']==probe.max_callback_seconds==7
    assert row['write_calls_completed']==row['selected']==1
    assert emitted[0]['stream']=='CME'


def test_probe_counts_invalid_unsampled_quotes_without_writing_or_swallowing_errors():
    now=[100.0]
    probe=IngressProbe('NYMEX',clock=lambda:now[0],monotonic=lambda:now[0])
    r=SimpleNamespace(ts_event=100000000000,levels=[SimpleNamespace(bid_px=100,ask_px=100,bid_sz=0,ask_sz=1)])
    probe.wrap(lambda r:probe.quote('MCLV6@1',r))(r)
    assert probe.symbols['MCLV6@1']['invalid_bbo']==1
    assert probe.symbols['MCLV6@1']['selected']==0
    def failed(): raise RuntimeError('fixture')
    with pytest.raises(RuntimeError): probe.writing('MCLV6@1',failed)
    assert probe.symbols['MCLV6@1']['write_calls_completed']==0


def test_recent_window_resets_without_erasing_connection_peak():
    now=[100.0]; rows=[]
    p=IngressProbe('COMEX',clock=lambda:now[0],monotonic=lambda:now[0],emit=rows.append)
    def r(t):return SimpleNamespace(ts_event=int(t*1e9),levels=[SimpleNamespace(bid_px=1,ask_px=2,bid_sz=1,ask_sz=1)])
    f=p.wrap(lambda r:p.quote('MGC',r))
    now[0]=130;f(r(120))
    now[0]=160;f(r(160))
    assert rows[0]['window_max_event_age']==10
    assert rows[1]['window_max_event_age']==0
    assert rows[1]['symbols']['MGC']['max_event_age']==10
    assert rows[0]['symbols']['MGC']['records']==1
