import time

import pytest

from compass.store import Store
from compass import trend_bias as tb
from compass import pick_check as pc


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'tb.db'))
    store.initialize()
    yield store
    store.engine.dispose()


NOW = time.time()


class Cfg:
    ict_trend_bias = True
    ict_trend_bias_sma_len = 50
    ict_symbols = ('MNQ.c.0', 'MES.c.0')


def bars(close_last, n=60, start=100.0):
    return [{'ts': NOW - (n - i) * 86400, 'o': start, 'h': start + 1,
             'l': start - 1, 'c': start + (close_last - start) * i / (n - 1)}
            for i in range(n)]


def test_sma_math():
    assert tb.sma([1, 2, 3, 4, 5], 5) == 3.0
    assert tb.sma([1, 2, 3, 4, 10], 3) == pytest.approx((3 + 4 + 10) / 3)
    assert tb.sma([1, 2], 5) is None
    assert tb.sma([], 50) is None


def test_classify():
    assert tb.classify(105, 100) == 'bullish'
    assert tb.classify(95, 100) == 'bearish'
    assert tb.classify(100, 100) == 'bearish'  # strict: not above
    assert tb.classify(None, 100) is None
    assert tb.classify(100, 0) is None


def test_scan_disabled(db):
    class Off:
        ict_trend_bias = False
    with db.tx() as c:
        assert tb.scan(db, c, Off(), NOW) == {'ran': False, 'reason': 'disabled'}


def test_scan_regimes(db, monkeypatch):
    closes = {'MNQ.c.0': 31000.0, 'MES.c.0': 5900.0}
    monkeypatch.setattr(tb, '_daily_bars',
                        lambda s: bars(closes[s], start=closes[s] * 0.98))
    with db.tx() as c:
        out = tb.scan(db, c, Cfg(), NOW)
    assert out == {'ran': True, 'symbols': 2}
    with db.tx() as c:
        latest = db.get(c, 'trend_bias:latest')
    assert latest['rows']['MNQ.c.0']['regime'] == 'bullish'
    assert latest['rows']['MES.c.0']['regime'] == 'bullish'
    # flat/down-trending symbol -> bearish
    monkeypatch.setattr(tb, '_daily_bars',
                        lambda s: bars(closes[s], start=closes[s] * 1.02))
    with db.tx() as c:
        db.put(c, 'trend_bias:scanned_at', NOW - 100 * 3600)
        tb.scan(db, c, Cfg(), NOW)
        latest = db.get(c, 'trend_bias:latest')
    assert latest['rows']['MNQ.c.0']['regime'] == 'bearish'


def test_scan_no_bars_excluded(db, monkeypatch):
    monkeypatch.setattr(tb, '_daily_bars', lambda s: [])
    with db.tx() as c:
        tb.scan(db, c, Cfg(), NOW)
        latest = db.get(c, 'trend_bias:latest')
    assert latest['rows']['MNQ.c.0']['excluded'] == 'no_daily_bars'


def test_scan_throttled(db, monkeypatch):
    monkeypatch.setattr(tb, '_daily_bars', lambda s: bars(100.0))
    with db.tx() as c:
        assert tb.scan(db, c, Cfg(), NOW)['ran'] is True
        assert tb.scan(db, c, Cfg(), NOW + 60)['reason'] == 'throttled'


def test_display_shape(db, monkeypatch):
    monkeypatch.setattr(tb, '_daily_bars', lambda s: bars(100.0))
    with db.tx() as c:
        tb.scan(db, c, Cfg(), NOW)
        d = tb.display(db, c, NOW)
    assert d['regime_rows'] and d['rows'] == d['regime_rows']
    assert d['params'] == {'sma_len': 50}
    assert 'Context only' in d['note']


def test_pick_check_regime_counter_trend_note(db):
    with db.tx() as c:
        db.put(c, 'trend_bias:latest', {
            'at': NOW, 'sma_len': 50,
            'rows': {'MNQ.c.0': {'symbol': 'MNQ.c.0', 'regime': 'bullish',
                                 'sma': 30000.0, 'close': 30685.0,
                                 'dist_pct': 2.28, 'sma_len': 50,
                                 'at': NOW}}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    ict = out['pillars']['ict']
    assert ict['detail']['trend_bias']['verdict'] == 'info'
    assert 'counter-trend' in ict['detail']['trend_bias']['note']
    assert 'scalp framing' in ict['note']
    # info verdicts never move the score
    assert out['evidence_score'] == 0
    assert out['pillars_counted'] == 0


def test_pick_check_regime_with_trend_no_warning(db):
    with db.tx() as c:
        db.put(c, 'trend_bias:latest', {
            'at': NOW, 'sma_len': 50,
            'rows': {'MNQ.c.0': {'symbol': 'MNQ.c.0', 'regime': 'bearish',
                                 'sma': 31000.0, 'close': 30685.0,
                                 'dist_pct': -1.0, 'sma_len': 50,
                                 'at': NOW}}})
        out = pc.check(db, c, NOW, 'MNQ', 'short', 30685.25, 30664.25,
                       'stormzy')
    ict = out['pillars']['ict']
    assert 'with-trend' in ict['detail']['trend_bias']['note']
    assert 'scalp framing' not in ict['note']
