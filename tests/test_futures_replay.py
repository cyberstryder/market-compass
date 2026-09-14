from types import SimpleNamespace
from compass.futures_replay import ReplayBars


def test_replay_batches_preserve_every_bar_and_publish_current_bar(monkeypatch):
    now=1800000000
    monkeypatch.setattr('compass.futures_replay.time.time',lambda:now)
    monkeypatch.setattr('compass.futures_replay.time.monotonic',lambda:1)
    writes=[]
    buffer=ReplayBars(SimpleNamespace(bars=lambda source,rows:writes.append((source,list(rows)))))
    rows=[('MGCZ6@1',now-86400+i*60,{'c':float(i)}) for i in range(1001)]
    for row in rows: buffer.add(row)
    assert [len(batch) for source,batch in writes]==[500,500]
    current=('MGCZ6@1',now-60,{'c':123.0})
    buffer.add(current)
    assert [row for source,batch in writes for row in batch]==rows+[current]
    assert all(source=='databento' for source,batch in writes)
    assert buffer.pending==[]


def test_partial_replay_flushes_on_heartbeat_without_a_new_bar(monkeypatch):
    clock=[1.0]
    monkeypatch.setattr('compass.futures_replay.time.monotonic',lambda:clock[0])
    monkeypatch.setattr('compass.futures_replay.time.time',lambda:1800000000)
    writes=[]
    buffer=ReplayBars(SimpleNamespace(bars=lambda source,rows:writes.extend(rows)))
    bar=('MCLV6@1',1700000000,{})
    buffer.add(bar)
    buffer.flush_due()
    assert writes==[]
    clock[0]+=2
    buffer.flush_due()
    buffer.flush()
    assert writes==[bar]
