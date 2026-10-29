"""ICE shadow scorecard tests.

The fire-list expectations below were generated from the VERIFIED study
harness (hidden_files/ice-stocks-viability-2026-10-29, run_study_rsi)
run over the deterministic LCG fixture series defined here, and the
production port was additionally proven fire-for-fire identical to the
study on 1.35M foundation bars before deploy (fidelity_gate.py in the
goal hidden files). These tests pin that equivalence in CI.
"""
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from compass import day_digest, ice_shadow
from compass.ice_shadow import (IceShadow, SeriesState, SymbolState,
                                resample_5m, score_fire)
from compass.store import Store, ice_shadow_fires

ET = ZoneInfo('America/New_York')


# ---------------------------------------------------------------------
# Deterministic fixture (integer LCG math only — stable everywhere).
# ---------------------------------------------------------------------
def lcg_bars(n, seed=20261029):
    x = seed

    def rnd():
        nonlocal x
        x = (1103515245 * x + 12345) % (1 << 31)
        return x / (1 << 31) - 0.5

    bars = []
    price = 100.0
    for i in range(n):
        drift = 0.9 * (bars[-1][3] - bars[-2][3]) if len(bars) >= 2 else 0.0
        step = rnd() * 0.60 + drift * 0.1
        o = price
        c = price + step
        h = max(o, c) + abs(rnd()) * 0.22
        low = min(o, c) - abs(rnd()) * 0.22
        bars.append((o, h, low, c))
        price = c
    return bars


# Study-harness fire lists for lcg_bars(2600) (idx, variant, side).
FIRES_1M = [(371, 'max6rsi', 'long'), (399, 'max6rsi', 'short'),
            (401, 'max6rsi', 'short'), (430, 'max6rsi', 'short'),
            (451, 'max6rsi', 'short'), (505, 'max6rsi', 'long'),
            (550, 'max6rsi', 'short'), (596, 'max6rsi', 'short'),
            (612, 'base', 'long'), (618, 'max6rsi', 'long'),
            (703, 'base', 'short'), (704, 'max6rsi', 'short'),
            (774, 'base', 'long'), (795, 'max6rsi', 'short'),
            (798, 'max6rsi', 'short'), (799, 'max6rsi', 'short'),
            (878, 'base', 'long'), (879, 'max6rsi', 'long'),
            (904, 'max6rsi', 'short'), (964, 'max6rsi', 'long'),
            (965, 'base', 'short'), (1008, 'max6rsi', 'short'),
            (1013, 'max6rsi', 'short'), (1083, 'max6rsi', 'short'),
            (1143, 'max6rsi', 'long'), (1175, 'max6rsi', 'long'),
            (1198, 'max6rsi', 'short'), (1287, 'max6rsi', 'short'),
            (1514, 'base', 'long'), (1523, 'max6rsi', 'short'),
            (1540, 'max6rsi', 'short'), (1687, 'max6rsi', 'long'),
            (1691, 'max6rsi', 'long'), (1750, 'max6rsi', 'short'),
            (1769, 'max6rsi', 'long'), (1794, 'max6rsi', 'short'),
            (1905, 'max6rsi', 'short'), (1961, 'max6rsi', 'short'),
            (1983, 'max6rsi', 'short'), (2178, 'max6rsi', 'short'),
            (2268, 'max6rsi', 'short'), (2279, 'max6rsi', 'short'),
            (2427, 'max6rsi', 'short'), (2481, 'max6rsi', 'long'),
            (2493, 'max6rsi', 'short'), (2539, 'max6rsi', 'short'),
            (2542, 'max6rsi', 'short'), (2557, 'max6rsi', 'short'),
            (2558, 'max6rsi', 'short'), (2572, 'max6rsi', 'short'),
            (2573, 'max6rsi', 'short')]
FIRES_5M = [(288, 'max6rsi', 'short'), (336, 'max6rsi', 'short'),
            (338, 'base', 'long'), (340, 'max6rsi', 'long'),
            (467, 'max6rsi', 'short'), (469, 'max6rsi', 'short'),
            (478, 'max6rsi', 'short'), (484, 'max6rsi', 'short'),
            (489, 'max6rsi', 'short')]
# Study component values at the final bar of the 1M fixture.
FINAL_COMPONENTS = {'tsi': -33.8622128156, 'dmi': -33.5857578285,
                    'sq20': -0.3429432991, 'sq32': -1.2033068001,
                    'qs50': -0.0415163577, 'qs24': -0.039409844,
                    'macd': -0.8132766695, 'rsi': 36.6912570041,
                    'atr': 0.2481250075, 'a25u': 0.0,
                    'a48d': 97.9166666667}


def run_series(bars):
    st = SeriesState()
    fires = []
    snap = None
    for i, (o, h, low, c) in enumerate(bars):
        snap, fs = st.feed(i * 60, o, h, low, c)
        fires += [(i, v, s) for v, s in fs]
    return fires, snap


def test_fire_list_matches_study_1m():
    fires, snap = run_series(lcg_bars(2600))
    assert fires == FIRES_1M
    for key, want in FINAL_COMPONENTS.items():
        assert snap[key] == pytest.approx(want, rel=1e-7, abs=1e-9), key


def test_fire_list_matches_study_5m():
    bars1 = [(i * 60, o, h, low, c)
             for i, (o, h, low, c) in enumerate(lcg_bars(2600))]
    bars5 = resample_5m(bars1)
    assert len(bars5) == 520
    st = SeriesState()
    fires = []
    for j, (ts, o, h, low, c) in enumerate(bars5):
        _snap, fs = st.feed(ts, o, h, low, c)
        fires += [(j, v, s) for v, s in fs]
    assert fires == FIRES_5M


def test_state_roundtrip_preserves_fires():
    bars = lcg_bars(2600)
    whole, _ = run_series(bars)
    st = SeriesState()
    part = []
    for i, (o, h, low, c) in enumerate(bars[:1300]):
        _s, fs = st.feed(i * 60, o, h, low, c)
        part += [(i, v, s) for v, s in fs]
    blob = json.loads(json.dumps(ice_shadow._enc(st.to_dict())))
    st2 = SeriesState.from_dict(ice_shadow._dec(blob))
    for i, (o, h, low, c) in enumerate(bars[1300:], start=1300):
        _s, fs = st2.feed(i * 60, o, h, low, c)
        part += [(i, v, s) for v, s in fs]
    assert part == whole == FIRES_1M


def test_warmup_gate():
    # No fire may occur before the 250-bar warmup, whatever the series.
    fires, _ = run_series(lcg_bars(2600))
    assert min(i for i, _v, _s in fires) >= 250


# Study-harness vote counts (base bull/bear, max6rsi bull/bear) at
# fixture indices — the counts stored on every fire row so the
# accumulated evidence can be re-cut by threshold/side later.
VOTE_COUNTS = {371: (1, 0, 3, 0), 612: (3, 0, 1, 1),
               2178: (0, 2, 0, 3), 2599: (0, 0, 0, 2)}


def test_vote_counts_match_study():
    st = SeriesState()
    for i, (o, h, low, c) in enumerate(lcg_bars(2600)):
        snap, _fires = st.feed(i * 60, o, h, low, c)
        if i in VOTE_COUNTS:
            bb, bs, rb, rs = VOTE_COUNTS[i]
            assert (snap['base_bull'], snap['base_bear']) == (bb, bs)
            assert (snap['bull_rsi'], snap['bear_rsi']) == (rb, rs)


# ---------------------------------------------------------------------
# Scorer (study CtxStocks.score2 semantics).
# ---------------------------------------------------------------------
def et_ts(day, minute_of_day):
    y, m, d = day
    dt = datetime(y, m, d, tzinfo=ET).timestamp()
    return dt + (570 + minute_of_day) * 60  # 09:30 ET + minute


def make_seg(day=(2026, 10, 13), n=200, next_day_bars=0):
    seg = []
    for i in range(n):
        ts = et_ts(day, i)
        seg.append((ts, 100.0, 100.0, 100.0, 100.0))
    for j in range(next_day_bars):
        ts = et_ts((2026, 10, 14), j)
        seg.append((ts, 50.0, 50.0, 50.0, 50.0))
    return seg


def test_scorer_basic_long_short():
    seg = make_seg()
    seg[6] = (seg[6][0], 100.0, 100.0, 100.0, 100.0)   # entry open 100
    seg[35] = (seg[35][0], 0, 0, 0, 104.0)
    seg[65] = (seg[65][0], 0, 0, 0, 96.0)
    seg[125] = (seg[125][0], 0, 0, 0, 100.0)
    seg[199] = (seg[199][0], 0, 0, 0, 102.0)
    fdate = ice_shadow._et_date(seg[5][0])
    out = score_fire(seg, 5, +1, 2.0, 60, fdate)
    assert out[30] == ('scored', 2.0, seg[35][0], 104.0)
    assert out[60][1] == -2.0
    assert out[120][1] == 0.0
    assert out['session'][1] == 1.0
    out = score_fire(seg, 5, -1, 2.0, 60, fdate)
    assert out[30][1] == -2.0 and out['session'][1] == -1.0


def test_scorer_session_ignores_next_day_bars():
    seg = make_seg(next_day_bars=50)
    seg[199] = (seg[199][0], 0, 0, 0, 102.0)
    out = score_fire(seg, 5, +1, 2.0, 60, ice_shadow._et_date(seg[5][0]))
    assert out['session'][1] == 1.0  # last close of the fire date, not 50.0


def test_scorer_gap_drop():
    seg = make_seg()
    seg = seg[:41] + [(ts + 20 * 60, o, h, l, c)
                      for ts, o, h, l, c in seg[41:]]
    out = score_fire(seg, 5, +1, 2.0, 60, ice_shadow._et_date(seg[5][0]))
    assert out[30][0] == 'scored'
    assert out[60] == ('dropped', 'gap')
    assert out[120] == ('dropped', 'gap')


def test_scorer_pending_and_zero_atr_and_session_order():
    seg = make_seg(n=40)
    fdate = ice_shadow._et_date(seg[5][0])
    out = score_fire(seg, 5, +1, 2.0, 60, fdate)
    assert out[30][0] == 'scored'
    assert out[60] == ('pending',) and out[120] == ('pending',)
    out = score_fire(seg, 5, +1, 0.0, 60, fdate)
    assert all(v == ('dropped', 'zero_atr') for v in out.values())
    # Fire on the session's last bar: no same-session exit after entry.
    seg = make_seg()
    out = score_fire(seg, 199, +1, 2.0, 60, ice_shadow._et_date(seg[5][0]))
    assert out['session'] == ('dropped', 'session_order')


def test_resample_buckets():
    bars = [(i * 60, float(i), float(i) + 2, float(i) - 2, float(i) + 1)
            for i in range(12)]
    out = resample_5m(bars)
    # Like the study's pandas resample, the trailing partial bucket is
    # emitted (bars 10-11 form a 2-bar bucket).
    assert len(out) == 3
    assert out[0] == (0, 0.0, 6.0, -2.0, 5.0)
    assert out[1] == (300, 5.0, 11.0, 3.0, 10.0)
    assert out[2] == (600, 10.0, 13.0, 8.0, 12.0)


# ---------------------------------------------------------------------
# Engine integration: seed -> detect -> resolve against a sqlite store.
# ---------------------------------------------------------------------
DAYS = [(2026, 10, 5), (2026, 10, 6), (2026, 10, 7), (2026, 10, 8),
        (2026, 10, 9), (2026, 10, 12), (2026, 10, 13)]


def idx_ts(idx):
    return et_ts(DAYS[idx // 390], idx % 390)


def put_bars(db, sym, bars, lo, hi):
    with db.tx() as c:
        for i in range(lo, hi):
            o, h, low, cl = bars[i]
            db.append(c, 'bar', 'alpaca', sym, idx_ts(i),
                      {'o': o, 'h': h, 'l': low, 'c': cl, 'v': 100,
                       'vw': cl})


def test_engine_seed_detect_resolve(tmp_path):
    db = Store(f'sqlite:///{tmp_path}/ice.db')
    db.initialize()
    cfg = SimpleNamespace(ice_shadow_enabled=True, watch_symbols=('TESTX',))
    eng = IceShadow(db, cfg)
    bars = lcg_bars(2600)
    put_bars(db, 'TESTX', bars, 0, 2000)
    with db.tx() as c:
        eng.tick(c, idx_ts(1999) + 120)  # seed tick (fires suppressed)
        assert db.get(c, 'ice_shadow:state:TESTX') is not None
        assert db.get(c, 'ice_shadow:worker') is not None
        assert c.execute(select(ice_shadow_fires)).fetchall() == []
    put_bars(db, 'TESTX', bars, 2000, 2600)
    with db.tx() as c:
        eng.tick(c, idx_ts(2599) + 120)  # detect + resolve
        rows = c.execute(
            select(ice_shadow_fires).order_by(ice_shadow_fires.c.fire_ts)
        ).mappings().all()
    assert len(rows) == 17  # 12 x 1M + 5 x 5M post-seed study fires
    by_key = {(r['timeframe'], r['fire_ts']): r for r in rows}
    first = by_key[('1M', idx_ts(2178))]
    assert first['variant'] == 'max6rsi' and first['side'] == 'short'
    assert first['status'] == 'done'
    assert first['r60'] == pytest.approx(-7.7608325143, abs=1e-9)
    p = first['payload']
    # Raw vote counts stored on the row (study counts at this bar:
    # max6rsi bull 0 / bear 3 — a threshold-3 short fire).
    assert p['bull_votes'] == 0 and p['bear_votes'] == 3
    assert p['atr'] == pytest.approx(0.2668146854, abs=1e-9)
    assert p['entry']['price'] == bars[2179][0]
    assert p['horizons']['30']['r'] == pytest.approx(-1.118795555, abs=1e-9)
    assert p['horizons']['120']['r'] == pytest.approx(-0.4271118617, abs=1e-9)
    assert p['horizons']['session']['r'] == pytest.approx(-1.1479379005,
                                                          abs=1e-9)
    crossed = by_key[('1M', idx_ts(2268))]
    assert crossed['payload']['horizons']['120'] == {
        'status': 'dropped', 'reason': 'gap'}  # window crosses the close
    assert crossed['status'] == 'done'
    day7 = by_key[('1M', idx_ts(2427))]
    assert day7['status'] == 'open'  # its session has not closed yet
    five = by_key[('5M', idx_ts(2335))]
    assert five['variant'] == 'max6rsi' and five['side'] == 'short'

    day6_start = datetime(2026, 10, 12, tzinfo=ET).timestamp()
    with db.tx() as c:
        summary = ice_shadow.summarize(db, c, idx_ts(2599) + 120, day6_start)
        section = day_digest._ice_shadow(db, c, day6_start)
    v = summary['variants']['max6rsi']
    assert v['fires_total'] == 17 and v['fires_today'] == 4
    assert summary['variants']['base']['fires_total'] == 0
    # The three completed day-6 1M fires contribute scored +60m outcomes
    # (their exact values are asserted on the rows above); same-day
    # Oct-13 fires add more as their +60m exits print.
    assert v['h60']['n'] >= 3
    assert v['h60']['mean_r'] is not None
    text = '\n'.join(section['lines'])
    assert 'ICE Max6+RSI (shadow): 4 fires today · 17 to date' in text
    assert '+60m: n=' in text and 'Shadow evidence only' in text


def test_disabled_flag_noops(tmp_path):
    db = Store(f'sqlite:///{tmp_path}/ice.db')
    db.initialize()
    cfg = SimpleNamespace(ice_shadow_enabled=False, watch_symbols=('TESTX',))
    eng = IceShadow(db, cfg)
    with db.tx() as c:
        eng.tick(c, idx_ts(100))
        assert db.get(c, 'ice_shadow:worker') is None


def test_config_default_on(monkeypatch):
    monkeypatch.delenv('ICE_SHADOW_ENABLED', raising=False)
    import dataclasses
    from compass.config import Config
    field = {f.name: f for f in dataclasses.fields(Config)}[
        'ice_shadow_enabled']
    assert field.default_factory() is True
