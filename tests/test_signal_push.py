"""Tests for gap continuation and day-board push wiring."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.gap_continuation import format_message as gap_format
from compass.day_trading_board import format_message as board_format, _a_plus_rows


def test_gap_format_message():
    payload = {'symbol': 'NVDA', 'direction': 'up', 'gap_pct': 0.025,
               'pillars': {'price': 'supportive', 'dealer': 'supportive',
                           'flow': 'neutral', 'sector': 'supportive'}}
    msg = gap_format(payload)
    assert 'NVDA' in msg['content']
    assert 'LONG' in msg['content']
    assert '+2.50%' in msg['content']
    assert len(msg['content']) <= 1900


def test_gap_format_short():
    payload = {'symbol': 'AMD', 'direction': 'down', 'gap_pct': -0.04,
               'pillars': {}}
    msg = gap_format(payload)
    assert 'SHORT' in msg['content']


def test_board_a_plus_filter():
    board = {'rows': [
        {'symbol': 'A', 'total': 6, 'day_pct': 0.03, 'components': {}},
        {'symbol': 'B', 'total': 5, 'day_pct': -0.01, 'components': {}},
        {'symbol': 'C', 'total': 4, 'day_pct': 0.05, 'components': {}},
    ]}
    rows = _a_plus_rows(board)
    assert [r['symbol'] for r in rows] == ['A', 'B']
    assert _a_plus_rows({}) == []
    assert _a_plus_rows(None) == []


def test_board_format_message():
    payload = {'session': '2026-10-05', 'action': 'built',
               'setups': [
                   {'symbol': 'NVDA', 'total': 6, 'day_pct': 0.031,
                    'components': {'flow': 1, 'sector': 1, 'mover': 1,
                                   'earnings': 1, 'economic': 1, 'radar': 1}},
                   {'symbol': 'AMD', 'total': 5, 'day_pct': -0.012,
                    'components': {'flow': 1, 'sector': 0, 'mover': 0,
                                   'earnings': 1, 'economic': 1, 'radar': 1}},
               ]}
    msg = board_format(payload)
    assert 'NVDA' in msg['content']
    assert 'AMD' in msg['content']
    assert '6/6' in msg['content']
    assert len(msg['content']) <= 1900
