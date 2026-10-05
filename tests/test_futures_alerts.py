import sys, time
sys.path.insert(0, '/home/hatch/workspace/market-compass/market-compass-push')
from compass.alert_format import message_for

NOW = 1791218000

def row(symbol, payload):
    return {'id': 1, 'ts': NOW, 'source': 'engine', 'symbol': symbol, 'payload': payload}

def test_futures_paper_exit_compact():
    msg = message_for(row('SIL.v.0', {
        'status': 'closed', 'asset': 'future', 'side': 'long', 'strategy': 'ict-aoi-zones',
        'entry': 61.30, 'stop': 61.25, 'target': 61.41, 'exit': 61.24,
        'qty': 1, 'initial_risk': 66.04, 'pnl': -63.00, 'exit_reason': 'stop',
    }), NOW)
    assert 'FUTURES | PAPER EXIT | SIL · micro silver · LONG' in msg
    assert 'Stopped out' in msg
    assert '61.30' in msg and '61.24' in msg
    assert '-$63.00' in msg
    assert 'AOI Zones' in msg
    assert 'no broker order' in msg
    for junk in ('Ref:', 'Event #', 'ict-aoi-zones', 'SIL.v.0', 'Instrument:', 'Source:'):
        assert junk not in msg, junk

def test_futures_paper_entry_compact():
    msg = message_for(row('MNQ.c.0', {
        'status': 'entered', 'asset': 'future', 'side': 'long', 'strategy': 'ict-aoi-fade',
        'entry': 61.30, 'stop': 61.25, 'target': 61.41, 'qty': 1,
    }), NOW)
    assert 'FUTURES | PAPER ENTRY | MNQ · micro Nasdaq · LONG' in msg
    assert 'AOI 50/20 Fade' in msg
    assert 'Entry 61.30' in msg and 'stop 61.25' in msg and 'target 61.41' in msg
    assert 'Ref:' not in msg and 'Event #' not in msg

def test_futures_no_data_compact():
    msg = message_for(row('MCL.v.0', {
        'status': 'management_blocked', 'asset': 'future', 'side': 'long', 'strategy': 'ict-aoi-fade',
        'entry': 61.30, 'stop': 61.25, 'target': 61.41, 'qty': 1,
        'reason': 'No fresh exit quote; position remains unresolved',
    }), NOW)
    assert 'FUTURES | NO DATA | MCL · micro crude · LONG' in msg
    assert 'DATA BLOCKED' not in msg
    assert 'No fresh price' in msg
    assert 'entry 61.30' in msg and 'stop 61.25' in msg and 'target 61.41' in msg
    assert 'AOI 50/20 Fade' in msg
    assert 'Ref:' not in msg and 'Event #' not in msg

def test_futures_target_hit_reason():
    msg = message_for(row('MES.c.0', {
        'status': 'closed', 'asset': 'future', 'side': 'short', 'strategy': 'ict-turtle-soup',
        'entry': 100.0, 'exit': 99.0, 'pnl': 50.0, 'exit_reason': 'target', 'qty': 1,
    }), NOW)
    assert 'Target hit' in msg
    assert 'Turtle Soup' in msg
    assert '+$50.00' in msg

def test_futures_flatten_reason():
    msg = message_for(row('MGC.v.0', {
        'status': 'closed', 'asset': 'future', 'side': 'long', 'strategy': 'ict-continuation',
        'entry': 50.0, 'exit': 50.5, 'pnl': 10.0, 'exit_reason': 'session_flatten', 'qty': 1,
    }), NOW)
    assert 'Session flatten (15:45 CT)' in msg
