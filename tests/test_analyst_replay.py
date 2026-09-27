import json

import pytest

from compass import analyst_replay as ar


class FakeDB:
    """Synthetic in-memory db: only the reads the harness uses."""

    def __init__(self, rows):
        self._rows = list(rows)

    def recent(self, c, kind, symbol=None, limit=100, since=None):
        out = [r for r in self._rows
               if r['kind'] == kind and (symbol is None or r['symbol'] == symbol)]
        out.sort(key=lambda r: r['ts'], reverse=True)
        return out[:limit]

    def get(self, c, key, default=None):
        return default


DAY = '2026-09-24'
PICK_TS = ar.end_of_day_ts(DAY)


def pick(ticker='MRNA', direction='long', pnl=None):
    return {'ticker': ticker, 'direction': direction, 'date': DAY,
            'source': 'test', 'entry': None, 'pnl': pnl, 'status': 'open',
            'replayable': True}


def row(kind, symbol, ts, payload):
    return {'kind': kind, 'symbol': symbol, 'ts': ts, 'payload': payload}


def apex_row(ts, signal='tested_holding', role='support'):
    return row('apex_magnet_signal', 'MRNA', ts,
               {'signal': signal, 'role': role, 'magnet': 165.0, 'spot': 170.0,
                'distance_pct': 0.029, 'vs_flip': 'above'})


# --- anti-lookahead (the critical test) ---------------------------------------

def test_evidence_after_pick_ts_is_never_counted():
    db = FakeDB([apex_row(PICK_TS + 3600)])
    got = ar._replay_apex(db, None, pick(), PICK_TS)
    assert got['state'] == 'no_data'


def test_evidence_exactly_at_pick_ts_counts():
    db = FakeDB([apex_row(PICK_TS)])
    got = ar._replay_apex(db, None, pick(), PICK_TS)
    assert got['state'] == 'agrees'


def test_stale_evidence_outside_lookback_ignored():
    db = FakeDB([apex_row(PICK_TS - ar.LOOKBACK_S - 1)])
    got = ar._replay_apex(db, None, pick(), PICK_TS)
    assert got['state'] == 'no_data'


def test_future_tape_and_breakout_rows_ignored():
    db = FakeDB([
        row('tape_confirmation', 'MRNA', PICK_TS + 10,
            {'side': 'long', 'max_score': 95}),
        row('breakout_event', 'MRNA', PICK_TS + 10,
            {'direction': 'up', 'pattern': 'range_break'}),
        row('gap_continuation', 'MRNA', PICK_TS + 10,
            {'direction': 'up', 'gap_pct': 0.03}),
    ])
    r = ar.replay_pick(db, None, pick())
    assert r['pillars']['tape']['state'] == 'no_data'
    assert r['pillars']['breakouts']['state'] == 'no_data'
    assert r['pillars']['gap']['state'] == 'no_data'
    assert r['agreeing'] == 0


# --- direction mapping per pillar ---------------------------------------------

@pytest.mark.parametrize('direction,role,signal,expected', [
    ('long', 'support', 'tested_holding', 'agrees'),
    ('long', 'support', 'approaching', 'agrees'),
    ('long', 'resistance', 'tested_holding', 'disagrees'),
    ('long', 'support', 'broke_through', 'disagrees'),
    ('short', 'resistance', 'approaching', 'agrees'),
    ('short', 'resistance', 'tested_holding', 'agrees'),
    ('short', 'support', 'tested_holding', 'disagrees'),
    ('short', 'resistance', 'broke_through', 'disagrees'),
])
def test_apex_direction_mapping(direction, role, signal, expected):
    db = FakeDB([apex_row(PICK_TS - 100, signal=signal, role=role)])
    got = ar._replay_apex(db, None, pick(direction=direction), PICK_TS)
    assert got['state'] == expected


def test_tape_same_direction_agrees_opposite_disagrees():
    db = FakeDB([row('tape_confirmation', 'MRNA', PICK_TS - 100,
                     {'side': 'long', 'max_score': 92})])
    assert ar._replay_tape(db, None, pick('MRNA', 'long'), PICK_TS)['state'] == 'agrees'
    got = ar._replay_tape(db, None, pick('MRNA', 'short'), PICK_TS)
    assert got['state'] == 'disagrees'
    assert got['detail']['reason'] == 'only_opposing_flow_confirmed'


def test_tape_no_rows_is_no_data():
    assert ar._replay_tape(FakeDB([]), None, pick(), PICK_TS)['state'] == 'no_data'


@pytest.mark.parametrize('direction,event_dir,expected', [
    ('long', 'up', 'agrees'), ('long', 'down', 'disagrees'),
    ('short', 'down', 'agrees'), ('short', 'up', 'disagrees'),
])
def test_breakout_direction_mapping(direction, event_dir, expected):
    db = FakeDB([row('breakout_event', 'MRNA', PICK_TS - 100,
                     {'direction': event_dir, 'pattern': 'range_break'})])
    got = ar._replay_breakouts(db, None, pick(direction=direction), PICK_TS)
    assert got['state'] == expected


@pytest.mark.parametrize('direction,event_dir,expected', [
    ('long', 'up', 'agrees'), ('long', 'down', 'disagrees'),
    ('short', 'down', 'agrees'), ('short', 'up', 'disagrees'),
])
def test_gap_direction_mapping(direction, event_dir, expected):
    db = FakeDB([row('gap_continuation', 'MRNA', PICK_TS - 100,
                     {'direction': event_dir, 'gap_pct': 0.025})])
    got = ar._replay_gap(db, None, pick(direction=direction), PICK_TS)
    assert got['state'] == expected


def test_full_pick_counts_agreeing_pillars():
    db = FakeDB([
        apex_row(PICK_TS - 100),
        row('tape_confirmation', 'MRNA', PICK_TS - 200,
            {'side': 'long', 'max_score': 90}),
        row('breakout_event', 'MRNA', PICK_TS - 300,
            {'direction': 'up', 'pattern': 'range_break'}),
        # no gap row -> no_data
    ])
    r = ar.replay_pick(db, None, pick())
    assert r['agreeing'] == 3
    assert r['pillars']['gap']['state'] == 'no_data'


# --- foxy loader ---------------------------------------------------------------

FOXY = {'fills': [
    {'ticker': 'MRNA', 'date': '2026-09-24', 'side': 'Bought',
     'contract': "OCT 16 26 190 Call", 'price': 11.24, 'qty': 1,
     'status': 'open'},
    {'ticker': 'META', 'date': '2026-09-21', 'side': 'Bought',
     'contract': "OCT 16 26 700 Put", 'price': 15.9, 'qty': 1,
     'status': 'open'},
    {'ticker': 'WMT', 'date': '2026-09-22', 'side': 'Sold',
     'contract': "JAN 15 27 110 Call", 'price': 6.9, 'qty': 10,
     'status': 'closed', 'pnl': 1794.5},
    {'ticker': 'HOOD', 'date': '2026-09-18', 'side': 'Sold',
     'contract': "OCT 16 26 125 Call", 'price': 6.2, 'qty': 2,
     'status': 'open', 'note': 'covered calls, not directional'},
]}


def test_foxy_loader_maps_calls_puts_and_skips_covered(tmp_path):
    p = tmp_path / 'foxy.json'
    p.write_text(json.dumps(FOXY))
    picks = ar.load_foxy_fills(str(p))
    by_ticker = {x['ticker']: x for x in picks}
    assert 'HOOD' not in by_ticker  # covered-call sale skipped
    assert by_ticker['MRNA']['direction'] == 'long'
    assert by_ticker['MRNA']['replayable'] is True
    assert by_ticker['MRNA']['entry'] == 11.24
    assert by_ticker['META']['direction'] == 'short'
    wmt = by_ticker['WMT']
    assert wmt['direction'] == 'long'          # closing a long call position
    assert wmt['replayable'] is False         # no entry date: outcome stats only
    assert wmt['pnl'] == 1794.5


# --- win/loss split ------------------------------------------------------------

def test_outcome_never_invented():
    assert ar.outcome(pick(pnl=10)) == 'win'
    assert ar.outcome(pick(pnl=-10)) == 'loss'
    assert ar.outcome(pick(pnl=0)) == 'loss'   # win is strictly pnl > 0
    assert ar.outcome(pick(pnl=None)) == 'unknown'
    assert ar.outcome(pick()) == 'unknown'


def test_summarize_splits_by_known_pnl_only():
    db = FakeDB([apex_row(PICK_TS - 100)])
    picks = [pick(pnl=100), pick(pnl=-50), pick(pnl=None)]
    results = ar.replay_picks(db, None, picks)
    s = ar.summarize(results, picks)
    assert s['n_replayed'] == 3
    assert s['by_outcome']['win']['n'] == 1
    assert s['by_outcome']['loss']['n'] == 1
    assert s['by_outcome']['unknown']['n'] == 1
    assert s['by_outcome']['win']['mean_agreeing'] == 1.0


def test_closed_fills_counted_in_outcome_stats_not_replay(tmp_path):
    p = tmp_path / 'foxy.json'
    p.write_text(json.dumps(FOXY))
    picks = ar.load_foxy_fills(str(p))
    results = ar.replay_picks(FakeDB([]), None, picks)
    assert all(r['pick']['ticker'] != 'WMT' for r in results)
    s = ar.summarize(results, picks)
    cw = s['closed_without_entry_replay']
    assert cw['n'] == 1 and cw['wins'] == 1 and cw['losses'] == 0
