"""ICE shadow scorecard (Josh, 2026-10-29): forward evidence for the two
ICE direction-shift variants that passed the frozen stocks viability
study (files/ice-stocks-viability-2026-10-29.md) — base ICE v2.2.2 and
Max6+RSI. Max6 failed the frozen gates and is NOT tracked here.

SHADOW ONLY: this module detects fires on live stock bars and scores
each fire's hypothetical underlying outcome. It never alerts, never
books, never enters, never spends premium, never touches TradersPost.
Rows in ice_shadow_fires_v1 are evidence, nothing else.

Frozen semantics (do not re-derive): the study's verified
implementations — hidden_files/ice-viability-2026-10-27/SPEC.md,
hidden_files/ice-rsi-viability-2026-10-29/SPEC.md (RSI(18) persistent
LEVEL vote, >= 64 bull / <= 46 bear, added to the Max6 vote count) and
hidden_files/ice-stocks-viability-2026-10-29/SPEC_STOCKS.md. The port
below reproduces the study harness's fire lists exactly on foundation
data (fidelity gate, 2026-10-29: 26,891 fires across AAPL/ACB/NVDA/SERV
x {1M, 5M} x both variants, zero missing / zero extra).

- base: TSI(22,13,8) / DMI Osc(22) / TTM(20) zero-cross or strict flip
  votes + Aroon(25) cross-within-3-bars + QStick(50) cross; fire at
  >= 3 same-direction votes.
- max6rsi: TSI / DMI / TTM(32) / QStick(24) / MACD(WildersAvg 6-24)
  crosses + Aroon(48) LEVEL vote (Up >= 70 / Down >= 70) + RSI level
  vote; fire at >= 3.
- Every qualifying bar is a fire (no dedup/thinning); 250-bar warmup
  per series; RTH bars only (09:30 <= ET < 16:00); 5M series resampled
  from the 1M bars into epoch-aligned 300s buckets over minutes present.
- Scoring (underlying-first, identical construction): entry = open of
  the bar after the fire bar; R = ATR(14) Wilder at the fire bar of the
  cell timeframe; horizons +30/+60/+120 trading minutes and the session
  close (16:00 ET on the fire date); a fire is dropped from a horizon
  when the exit index runs past the series or any bar-start step inside
  the held window exceeds 15 minutes of missing bars.

KNOWN OPEN DEPENDENCY (desk Task 008, Josh owes two TOS checks): the
real ThinkOrSwim DMI_Oscillator(22, isCumulative=yes) may drift
one-sided instead of oscillating. The frozen study — and this build —
use the oscillating ratio definition 100*(+DI - -DI)/(+DI + -DI). The
DMI oscillator is isolated in DmiOscillator below so a definition swap
is a one-class change.

Live-only deviations from the study, by construction:
- The study's two mechanical symbol-day exclusions (degraded <195-bar
  days, >35% overnight split-guard jumps) are knowable only in
  hindsight, so live detection does not apply them; the scoring gap and
  session rules ARE applied exactly.
- A fire whose ATR is exactly 0 (a dead-flat series) is recorded but
  its horizons resolve as dropped ('zero_atr'); the study's R division
  is undefined there and no foundation symbol exhibits it.
- Detector state is seeded from the trailing ~8 days of stored bars on
  first sight of a symbol (fires suppressed while seeding); the
  250-bar warmup makes the seed immaterial, as in the study.

State: per-symbol streaming indicator state persists at
ice_shadow:state:<SYM> (NaN/Infinity sentinel-encoded — Postgres JSONB
rejects bare NaN). Fires live in the dedicated ice_shadow_fires_v1
table so retention is managed independently of the events store.
Expected volume ~2,300 rows/day across both variants; that is the
study's fire rate and is intended.
"""
import json
import logging
import math
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, func

from .store import identity, ice_shadow_fires

LOG = logging.getLogger('uvicorn.error')
ET = ZoneInfo('America/New_York')

VERSION = 'ice-shadow-v1'
WARMUP = 250
VARIANTS = ('base', 'max6rsi')
HORIZONS = (30, 60, 120, 'session')
TICK_SECONDS = 20
SEED_DAYS = 8
SEED_BUDGET = 4
RESOLVE_CAP = 400
STALE_SECONDS = 7 * 86400

NAN = float('nan')
INF = float('inf')


# ------------------------------------------------------------ json safe --

def _enc(value):
    """Sentinel-encode NaN/Infinity floats for Postgres JSONB storage."""
    if isinstance(value, float):
        if value != value:
            return {'$f': 'nan'}
        if value == INF:
            return {'$f': 'inf'}
        if value == -INF:
            return {'$f': '-inf'}
        return value
    if isinstance(value, dict):
        return {k: _enc(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enc(v) for v in value]
    return value


def _dec(value):
    if isinstance(value, dict):
        if set(value) == {'$f'}:
            return {'nan': NAN, 'inf': INF, '-inf': -INF}[value['$f']]
        return {k: _dec(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dec(v) for v in value]
    return value


# ------------------------------------------------------------ primitives --

def fdiv(a, b):
    """IEEE-754 division with numpy semantics (Python raises on x/0.0)."""
    if b == 0.0:
        if a != a or a == 0.0:
            return NAN
        return math.copysign(INF, a) * math.copysign(1.0, b)
    return a / b


class Ewm:
    """pandas ewm(adjust=False), replicated: the output is a weighted
    average whose old:new weight ratio is reset to (1-a)/a after each
    observation and decays by (1-a) per NaN step (a NaN input carries
    the previous output). Verified against pandas on price-scale
    series: agreement to ~1e-13 (op-arrangement rounding only), and
    fire-exact over ~925k foundation bars."""
    __slots__ = ('alpha', 'y', 'w')

    def __init__(self, alpha):
        self.alpha = alpha
        self.y = NAN
        self.w = 0.0

    def update(self, x):
        if x != x:
            self.w *= (1.0 - self.alpha)
            return self.y
        if self.w == 0.0:
            self.y = x
        else:
            self.y = (self.w * self.y + x) / (self.w + 1.0)
        self.w = (1.0 - self.alpha) / self.alpha
        return self.y

    def to_dict(self):
        return {'y': self.y, 'w': self.w}

    @classmethod
    def from_dict(cls, alpha, d):
        e = cls(alpha)
        e.y = d['y']
        e.w = d['w']
        return e


def _ema_alpha(n):
    return 2.0 / (n + 1.0)


class RollingSum:
    """pandas rolling(n).sum() with min_periods=n: NaN until the window
    is full of finite values. Kahan-Babuska compensated add/subtract,
    matching pandas' compensated roll_sum to ~1 ulp of the sum."""
    __slots__ = ('n', 'buf', 's', 'comp', 'nan')

    def __init__(self, n):
        self.n = n
        self.buf = deque()
        self.s = 0.0
        self.comp = 0.0
        self.nan = 0

    def _add(self, v):
        t = self.s + v
        if abs(self.s) >= abs(v):
            self.comp += (self.s - t) + v
        else:
            self.comp += (v - t) + self.s
        self.s = t

    def update(self, x):
        if len(self.buf) == self.n:
            old = self.buf.popleft()
            if old != old:
                self.nan -= 1
            else:
                self._add(-old)
        self.buf.append(x)
        if x != x:
            self.nan += 1
        else:
            self._add(x)
        if len(self.buf) < self.n or self.nan:
            return NAN
        return self.s + self.comp

    def to_dict(self):
        return {'buf': list(self.buf), 's': self.s, 'comp': self.comp,
                'nan': self.nan}

    @classmethod
    def from_dict(cls, n, d):
        r = cls(n)
        r.buf = deque(d['buf'])
        r.s = d['s']
        r.comp = d['comp']
        r.nan = d['nan']
        return r


class RollingMean:
    __slots__ = ('rs',)

    def __init__(self, n):
        self.rs = RollingSum(n)

    def update(self, x):
        s = self.rs.update(x)
        return s / self.rs.n if s == s else NAN

    def to_dict(self):
        return self.rs.to_dict()

    @classmethod
    def from_dict(cls, n, d):
        r = cls(n)
        r.rs = RollingSum.from_dict(n, d)
        return r


class Aroon:
    """Aroon(L): 100*(L - barsSinceExtreme)/L over the trailing L+1
    bars including the current one; ties go to the MOST RECENT extreme
    (the study's argmax over the reversed window)."""
    __slots__ = ('L', 'hi', 'lo', 'count')

    def __init__(self, L):
        self.L = L
        self.hi = deque()
        self.lo = deque()
        self.count = 0

    def update(self, h, l):
        self.hi.append(h)
        self.lo.append(l)
        if len(self.hi) > self.L + 1:
            self.hi.popleft()
            self.lo.popleft()
        self.count += 1
        if len(self.hi) < self.L + 1:
            return NAN, NAN
        L = self.L
        best_hi, since_hi = None, 0
        best_lo, since_lo = None, 0
        for pos, v in enumerate(self.hi):
            if best_hi is None or v >= best_hi:
                best_hi, since_hi = v, L - pos
        for pos, v in enumerate(self.lo):
            if best_lo is None or v <= best_lo:
                best_lo, since_lo = v, L - pos
        return 100.0 * (L - since_hi) / L, 100.0 * (L - since_lo) / L

    def to_dict(self):
        return {'hi': list(self.hi), 'lo': list(self.lo),
                'count': self.count}

    @classmethod
    def from_dict(cls, L, d):
        a = cls(L)
        a.hi = deque(d['hi'])
        a.lo = deque(d['lo'])
        a.count = d['count']
        return a


class Squeeze:
    """TTM Squeeze histogram (Carter/LazyBear standard construction):
    linear-regression ENDPOINT of delta over L bars, where
    delta = close - (DonchianMidpoint(L) + SMA(close, L))/2. The
    regression uses the series-global bar index exactly as the study's
    pandas construction does (the endpoint is offset-invariant; the
    arithmetic mirrors the study's rolling sums)."""
    __slots__ = ('L', 'hi', 'lo', 'sma', 's1', 'sgi', 'count')

    def __init__(self, L):
        self.L = L
        self.hi = deque()
        self.lo = deque()
        self.sma = RollingSum(L)
        self.s1 = RollingSum(L)
        self.sgi = RollingSum(L)
        self.count = 0

    def update(self, h, l, c, idx):
        self.hi.append(h)
        self.lo.append(l)
        if len(self.hi) > self.L:
            self.hi.popleft()
            self.lo.popleft()
        ssum = self.sma.update(c)
        if len(self.hi) < self.L:
            delta = NAN
        else:
            mid = (max(self.hi) + min(self.lo)) / 2.0
            delta = c - (mid + ssum / self.L) / 2.0
        s1 = self.s1.update(delta)
        sgi = self.sgi.update(delta * idx if delta == delta else NAN)
        self.count += 1
        if s1 != s1 or sgi != sgi:
            return NAN
        L = self.L
        start = idx - (L - 1)
        sxy = sgi - start * s1
        sx = L * (L - 1) / 2.0
        sxx = (L - 1) * L * (2 * L - 1) / 6.0
        slope = (L * sxy - sx * s1) / (L * sxx - sx * sx)
        return s1 / L + slope * (L - 1) / 2.0

    def to_dict(self):
        return {'hi': list(self.hi), 'lo': list(self.lo),
                'sma': self.sma.to_dict(), 's1': self.s1.to_dict(),
                'sgi': self.sgi.to_dict(), 'count': self.count}

    @classmethod
    def from_dict(cls, L, d):
        s = cls(L)
        s.hi = deque(d['hi'])
        s.lo = deque(d['lo'])
        s.sma = RollingSum.from_dict(L, d['sma'])
        s.s1 = RollingSum.from_dict(L, d['s1'])
        s.sgi = RollingSum.from_dict(L, d['sgi'])
        s.count = d['count']
        return s


class DmiOscillator:
    """DMI Oscillator(22, Wilder's) — the frozen oscillating ratio
    definition: +DI = 100*S(+DM)/S(TR), -DI = 100*S(-DM)/S(TR) with
    Wilder smoothing S, oscillator = 100*(+DI - -DI)/(+DI + -DI).

    ISOLATED BY DESIGN (desk Task 008 open dependency): if Josh's TOS
    check shows the real DMI_Oscillator(isCumulative=yes) drifts
    one-sided instead of oscillating, the definition swap is confined
    to this class."""
    __slots__ = ('n', 'spdm', 'sndm', 'str_', 'ph', 'pl', 'pc')

    def __init__(self, n=22):
        self.n = n
        a = 1.0 / n
        self.spdm = Ewm(a)
        self.sndm = Ewm(a)
        self.str_ = Ewm(a)
        self.ph = self.pl = self.pc = None

    def update(self, h, l, c):
        if self.pc is None:
            pdm = ndm = 0.0
            tr = h - l
        else:
            up = h - self.ph
            dn = self.pl - l
            pdm = up if (up > dn and up > 0) else 0.0
            ndm = dn if (dn > up and dn > 0) else 0.0
            tr = max(h - l, abs(h - self.pc), abs(l - self.pc))
        self.ph, self.pl, self.pc = h, l, c
        sp = self.spdm.update(pdm)
        sn = self.sndm.update(ndm)
        st = self.str_.update(tr)
        pdi = fdiv(100.0 * sp, st)
        ndi = fdiv(100.0 * sn, st)
        return fdiv(100.0 * (pdi - ndi), pdi + ndi)

    def to_dict(self):
        return {'spdm': self.spdm.to_dict(), 'sndm': self.sndm.to_dict(),
                'str': self.str_.to_dict(), 'ph': self.ph, 'pl': self.pl,
                'pc': self.pc}

    @classmethod
    def from_dict(cls, n, d):
        o = cls(n)
        o.spdm = Ewm.from_dict(1.0 / n, d['spdm'])
        o.sndm = Ewm.from_dict(1.0 / n, d['sndm'])
        o.str_ = Ewm.from_dict(1.0 / n, d['str'])
        o.ph, o.pl, o.pc = d['ph'], d['pl'], d['pc']
        return o


class Rsi:
    """RSI(18) on close, Wilder (ThinkScript built-in construction):
    Wilder-smoothed average gain/loss of close differences;
    RSI = 100 - 100/(1 + ag/al); ag=al=0 is undefined (NaN)."""
    __slots__ = ('ag', 'al', 'pc')

    def __init__(self, n=18):
        a = 1.0 / n
        self.ag = Ewm(a)
        self.al = Ewm(a)
        self.pc = None

    def update(self, c):
        if self.pc is None:
            gain = loss = NAN
        else:
            d = c - self.pc
            gain = d if d > 0 else 0.0
            loss = -d if d < 0 else 0.0
        self.pc = c
        ag = self.ag.update(gain)
        al = self.al.update(loss)
        return 100.0 - fdiv(100.0, 1.0 + fdiv(ag, al))

    def to_dict(self):
        return {'ag': self.ag.to_dict(), 'al': self.al.to_dict(),
                'pc': self.pc}

    @classmethod
    def from_dict(cls, n, d):
        r = cls(n)
        r.ag = Ewm.from_dict(1.0 / n, d['ag'])
        r.al = Ewm.from_dict(1.0 / n, d['al'])
        r.pc = d['pc']
        return r


# ------------------------------------------------------------- detector --

class Indicators:
    """All ICE component states for one series (one symbol x timeframe).
    Component values are computed once per bar and shared by both
    variants, exactly as the study computes them per frame."""
    __slots__ = ('pc', 'tsi_a1', 'tsi_a2', 'tsi_b1', 'tsi_b2', 'tsi_f',
                 'dmi', 'atr', 'sq20', 'sq32', 'ar25', 'ar48', 'qs50',
                 'qs24', 'macd_f', 'macd_s', 'rsi')

    def __init__(self):
        self.pc = None
        self.tsi_a1 = Ewm(_ema_alpha(22))
        self.tsi_a2 = Ewm(_ema_alpha(13))
        self.tsi_b1 = Ewm(_ema_alpha(22))
        self.tsi_b2 = Ewm(_ema_alpha(13))
        self.tsi_f = Ewm(_ema_alpha(8))
        self.dmi = DmiOscillator(22)
        self.atr = Ewm(1.0 / 14.0)
        self.sq20 = Squeeze(20)
        self.sq32 = Squeeze(32)
        self.ar25 = Aroon(25)
        self.ar48 = Aroon(48)
        self.qs50 = RollingMean(50)
        self.qs24 = RollingMean(24)
        self.macd_f = Ewm(1.0 / 6.0)
        self.macd_s = Ewm(1.0 / 24.0)
        self.rsi = Rsi(18)

    def update(self, o, h, l, c, idx):
        if self.pc is None:
            m = NAN
            tr = h - l
        else:
            m = c - self.pc
            tr = max(h - l, abs(h - self.pc), abs(l - self.pc))
        self.pc = c
        a = self.tsi_a2.update(self.tsi_a1.update(m))
        b = self.tsi_b2.update(self.tsi_b1.update(abs(m)))
        tsi = self.tsi_f.update(fdiv(100.0 * a, b))
        dmi = self.dmi.update(h, l, c)
        atr = self.atr.update(tr)
        sq20 = self.sq20.update(h, l, c, idx)
        sq32 = self.sq32.update(h, l, c, idx)
        a25u, a25d = self.ar25.update(h, l)
        a48u, a48d = self.ar48.update(h, l)
        qs50 = self.qs50.update(c - o)
        qs24 = self.qs24.update(c - o)
        macd = self.macd_f.update(c) - self.macd_s.update(c)
        rsi = self.rsi.update(c)
        return {'tsi': tsi, 'dmi': dmi, 'atr': atr, 'sq20': sq20,
                'sq32': sq32, 'a25u': a25u, 'a25d': a25d, 'a48u': a48u,
                'a48d': a48d, 'qs50': qs50, 'qs24': qs24, 'macd': macd,
                'rsi': rsi}

    def to_dict(self):
        return {'pc': self.pc,
                'tsi_a1': self.tsi_a1.to_dict(), 'tsi_a2': self.tsi_a2.to_dict(),
                'tsi_b1': self.tsi_b1.to_dict(), 'tsi_b2': self.tsi_b2.to_dict(),
                'tsi_f': self.tsi_f.to_dict(), 'dmi': self.dmi.to_dict(),
                'atr': self.atr.to_dict(), 'sq20': self.sq20.to_dict(),
                'sq32': self.sq32.to_dict(), 'ar25': self.ar25.to_dict(),
                'ar48': self.ar48.to_dict(), 'qs50': self.qs50.to_dict(),
                'qs24': self.qs24.to_dict(), 'macd_f': self.macd_f.to_dict(),
                'macd_s': self.macd_s.to_dict(), 'rsi': self.rsi.to_dict()}

    @classmethod
    def from_dict(cls, d):
        o = cls()
        o.pc = d['pc']
        o.tsi_a1 = Ewm.from_dict(_ema_alpha(22), d['tsi_a1'])
        o.tsi_a2 = Ewm.from_dict(_ema_alpha(13), d['tsi_a2'])
        o.tsi_b1 = Ewm.from_dict(_ema_alpha(22), d['tsi_b1'])
        o.tsi_b2 = Ewm.from_dict(_ema_alpha(13), d['tsi_b2'])
        o.tsi_f = Ewm.from_dict(_ema_alpha(8), d['tsi_f'])
        o.dmi = DmiOscillator.from_dict(22, d['dmi'])
        o.atr = Ewm.from_dict(1.0 / 14.0, d['atr'])
        o.sq20 = Squeeze.from_dict(20, d['sq20'])
        o.sq32 = Squeeze.from_dict(32, d['sq32'])
        o.ar25 = Aroon.from_dict(25, d['ar25'])
        o.ar48 = Aroon.from_dict(48, d['ar48'])
        o.qs50 = RollingMean.from_dict(50, d['qs50'])
        o.qs24 = RollingMean.from_dict(24, d['qs24'])
        o.macd_f = Ewm.from_dict(1.0 / 6.0, d['macd_f'])
        o.macd_s = Ewm.from_dict(1.0 / 24.0, d['macd_s'])
        o.rsi = Rsi.from_dict(18, d['rsi'])
        return o


def _xup(prev, x):
    """ThinkScript `crosses above 0`: prev <= 0 and now > 0 (NaN-safe:
    every comparison against NaN is False)."""
    return prev <= 0 and x > 0


def _xdn(prev, x):
    return prev >= 0 and x < 0


class SeriesState:
    """One symbol x timeframe series: streaming indicators + the frozen
    vote assembly. feed() returns (snapshot, fires) with fires a list of
    (variant, side). Bar values MUST be Python floats (numpy bool/float
    scalars corrupt the vote sums: np.bool_ + np.bool_ is not addition).
    """
    __slots__ = ('ind', 'prev', 'w3b', 'w3s', 'count', 'last_ts')

    KEYS = ('tsi', 'dmi', 'sq20', 'sq32', 'a25u', 'a25d', 'a48u', 'a48d',
            'qs50', 'qs24', 'macd', 'rsi')

    def __init__(self):
        self.ind = Indicators()
        self.prev = {k: NAN for k in self.KEYS}
        self.w3b = deque(maxlen=3)
        self.w3s = deque(maxlen=3)
        self.count = 0
        self.last_ts = None

    def feed(self, ts, o, h, l, c):
        idx = self.count
        s = self.ind.update(o, h, l, c, idx)
        p = self.prev
        tsi_b, tsi_s = _xup(p['tsi'], s['tsi']), _xdn(p['tsi'], s['tsi'])
        dmi_b, dmi_s = _xup(p['dmi'], s['dmi']), _xdn(p['dmi'], s['dmi'])
        sq20_b = p['sq20'] < 0 and s['sq20'] > 0
        sq20_s = p['sq20'] > 0 and s['sq20'] < 0
        sq32_b = p['sq32'] < 0 and s['sq32'] > 0
        sq32_s = p['sq32'] > 0 and s['sq32'] < 0
        qs50_b, qs50_s = _xup(p['qs50'], s['qs50']), _xdn(p['qs50'], s['qs50'])
        qs24_b, qs24_s = _xup(p['qs24'], s['qs24']), _xdn(p['qs24'], s['qs24'])
        macd_b, macd_s = _xup(p['macd'], s['macd']), _xdn(p['macd'], s['macd'])
        ev_b = (s['a25u'] > s['a25d']) and (p['a25u'] <= p['a25d'])
        ev_s = (s['a25d'] > s['a25u']) and (p['a25d'] <= p['a25u'])
        self.w3b.append(bool(ev_b))
        self.w3s.append(bool(ev_s))
        a25_b, a25_s = any(self.w3b), any(self.w3s)
        a48_b = s['a48u'] >= 70
        a48_s = s['a48d'] >= 70
        rsi_b = s['rsi'] >= 64.0
        rsi_s = s['rsi'] <= 46.0
        fin = math.isfinite
        base_fin = (fin(s['tsi']) and fin(s['dmi']) and fin(s['sq20'])
                    and fin(s['a25u']) and fin(s['a25d']) and fin(s['qs50']))
        rsi_fin = (fin(s['tsi']) and fin(s['dmi']) and fin(s['sq32'])
                   and fin(s['a48u']) and fin(s['a48d']) and fin(s['qs24'])
                   and fin(s['macd']) and fin(s['rsi']))
        fires = []
        if idx >= WARMUP:
            if base_fin:
                if tsi_b + dmi_b + sq20_b + a25_b + qs50_b >= 3:
                    fires.append(('base', 'long'))
                if tsi_s + dmi_s + sq20_s + a25_s + qs50_s >= 3:
                    fires.append(('base', 'short'))
            if rsi_fin:
                if tsi_b + dmi_b + sq32_b + a48_b + qs24_b + macd_b + rsi_b >= 3:
                    fires.append(('max6rsi', 'long'))
                if tsi_s + dmi_s + sq32_s + a48_s + qs24_s + macd_s + rsi_s >= 3:
                    fires.append(('max6rsi', 'short'))
        self.prev = {k: s[k] for k in self.KEYS}
        self.count += 1
        self.last_ts = ts
        return s, fires

    def to_dict(self):
        return {'ind': self.ind.to_dict(), 'prev': dict(self.prev),
                'w3b': list(self.w3b), 'w3s': list(self.w3s),
                'count': self.count, 'last_ts': self.last_ts}

    @classmethod
    def from_dict(cls, d):
        o = cls()
        o.ind = Indicators.from_dict(d['ind'])
        o.prev = dict(d['prev'])
        o.w3b = deque((bool(x) for x in d['w3b']), maxlen=3)
        o.w3s = deque((bool(x) for x in d['w3s']), maxlen=3)
        o.count = d['count']
        o.last_ts = d['last_ts']
        return o


# ------------------------------------------------------------ bars / ET --

def _et(ts):
    return datetime.fromtimestamp(ts, ET)


def _minute_of_day(ts):
    d = _et(ts)
    return d.hour * 60 + d.minute


def _is_rth(ts):
    return 570 <= _minute_of_day(ts) < 960


def _et_date(ts):
    return _et(ts).date()


def _session_close_ts(d):
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).timestamp()


class SymbolState:
    """Both timeframe series of one symbol + the in-progress 5M bucket
    + the store watermark (last bar timestamp examined, RTH or not)."""
    __slots__ = ('m1', 'm5', 'bucket', 'wm', 'seeded')

    def __init__(self):
        self.m1 = SeriesState()
        self.m5 = SeriesState()
        self.bucket = None
        self.wm = 0.0
        self.seeded = False

    def feed_1m(self, ts, o, h, l, c, emit=True):
        """Feed one RTH 1-minute bar. Returns fire records:
        (variant, timeframe, side, fire_ts, atr)."""
        out = []
        bts = ts - (ts % 300)
        b = self.bucket
        if b is not None and bts != b['ts']:
            out += self._flush_bucket(emit)
            b = None
        if b is None:
            self.bucket = {'ts': bts, 'o': o, 'h': h, 'l': l, 'c': c}
        else:
            b['h'] = max(b['h'], h)
            b['l'] = min(b['l'], l)
            b['c'] = c
        snap, fires = self.m1.feed(ts, o, h, l, c)
        if emit:
            for variant, side in fires:
                out.append((variant, '1M', side, ts, snap['atr']))
        return out

    def _flush_bucket(self, emit=True):
        b = self.bucket
        self.bucket = None
        if b is None:
            return []
        snap, fires = self.m5.feed(b['ts'], b['o'], b['h'], b['l'], b['c'])
        if not emit:
            return []
        return [(variant, '5M', side, b['ts'], snap['atr'])
                for variant, side in fires]

    def to_dict(self):
        return {'v': 1, 'm1': self.m1.to_dict(), 'm5': self.m5.to_dict(),
                'bucket': self.bucket, 'wm': self.wm, 'seeded': self.seeded}

    @classmethod
    def from_dict(cls, d):
        o = cls()
        o.m1 = SeriesState.from_dict(d['m1'])
        o.m5 = SeriesState.from_dict(d['m5'])
        o.bucket = d['bucket']
        o.wm = d['wm']
        o.seeded = d['seeded']
        return o


def resample_5m(bars):
    """Epoch-aligned 300s buckets over the minutes present (the study's
    construction). bars: list of (ts, o, h, l, c) ascending."""
    out = []
    for ts, o, h, l, c in bars:
        bts = ts - (ts % 300)
        if out and out[-1][0] == bts:
            p = out[-1]
            out[-1] = (bts, p[1], max(p[2], h), min(p[3], l), c)
        else:
            out.append((bts, o, h, l, c))
    return out


# --------------------------------------------------------------- scorer --

def _gap_bad(seg, entry_pos, exit_pos, bar_seconds):
    """Study gap rule: any bar-start step j in [entry_pos+1, exit_pos]
    whose ts difference exceeds 15 minutes of missing bars."""
    limit = 900 + bar_seconds
    for j in range(entry_pos + 1, exit_pos + 1):
        if seg[j][0] - seg[j - 1][0] > limit:
            return True
    return False


def score_fire(seg, fire_pos, sign, atr, bar_seconds, fire_date,
               horizons=HORIZONS):
    """Score one fire over a bar segment of its own timeframe.

    seg: ascending (ts, o, h, l, c) covering the fire bar and everything
    stored after it (RTH only). fire_pos: index of the fire bar in seg.
    Returns {horizon: ('scored', r, exit_ts, exit_px) |
                       ('dropped', reason) | ('pending',)} — 'pending'
    only where the segment cannot yet decide (mirrors the study's
    index arithmetic on a still-growing series)."""
    n = len(seg)
    entry_pos = fire_pos + 1
    out = {}
    if atr is None or not (atr > 0):
        return {h: ('dropped', 'zero_atr') for h in horizons}
    entry = seg[entry_pos] if entry_pos < n else None
    for hz in horizons:
        if hz == 'session':
            # exit = close of the last bar of the fire session present
            # in the segment; decidable only once the caller marks the
            # session over (handled by resolve()).
            exit_pos = None
            for i in range(n - 1, fire_pos, -1):
                if _et_date(seg[i][0]) == fire_date:
                    exit_pos = i
                    break
            if exit_pos is None or entry is None or exit_pos < entry_pos:
                out[hz] = ('dropped', 'session_order')
                continue
        else:
            exit_pos = fire_pos + hz * 60 // bar_seconds
            if exit_pos >= n:
                out[hz] = ('pending',)
                continue
            if entry is None:
                out[hz] = ('dropped', 'no_entry')
                continue
        if _gap_bad(seg, entry_pos, exit_pos, bar_seconds):
            out[hz] = ('dropped', 'gap')
            continue
        exit_px = seg[exit_pos][4]
        r = sign * (exit_px - entry[1]) / atr
        out[hz] = ('scored', r, seg[exit_pos][0], exit_px)
    return out


# ------------------------------------------------------------ bar store --

def _payload_dict(p):
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            return {}
    return p or {}


def _fetch_bars(c, symbol, lo, hi):
    """Stored 1-minute bars for a symbol in (lo, hi], ascending, deduped
    by ts (latest write wins). Returns [(ts, o, h, l, c)] as floats."""
    from .store import events as events_table
    rows = c.execute(select(events_table.c.ts, events_table.c.payload)
                     .where(events_table.c.kind == 'bar',
                            events_table.c.symbol == symbol,
                            events_table.c.ts > lo,
                            events_table.c.ts <= hi)
                     .order_by(events_table.c.ts, events_table.c.id))
    by_ts = {}
    for ts, payload in rows:
        p = _payload_dict(payload)
        try:
            by_ts[float(ts)] = (float(ts), float(p['o']), float(p['h']),
                                float(p['l']), float(p['c']))
        except (KeyError, TypeError, ValueError):
            continue
    return [by_ts[k] for k in sorted(by_ts)]


# ---------------------------------------------------------------- engine --

class IceShadow:
    """Engine-tick driver: seeds per-symbol streaming state from stored
    bars, detects fires on new bars, persists state + fire rows, and
    resolves open fires' horizons from stored bars. All DB work happens
    on the caller's connection inside the caller's transaction; the
    in-memory state is a cache of the persisted state and MUST be
    dropped (reset()) whenever the surrounding transaction fails, so a
    rollback can never desynchronize it."""

    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.states = {}
        self.loaded = False
        self.last_run = 0.0

    def reset(self):
        self.states = {}
        self.loaded = False
        self.last_run = 0.0

    # ------------------------------------------------------------ state --

    def _load_states(self, c):
        saved = self.db.prefix(c, 'ice_shadow:state:')
        for key, value in saved.items():
            symbol = key.split(':', 2)[2]
            try:
                self.states[symbol] = SymbolState.from_dict(_dec(value))
            except Exception:
                LOG.warning('ice_shadow: dropping unreadable state for %s '
                            '(will reseed)', symbol)
        self.loaded = True

    def _save_state(self, c, symbol):
        self.db.put(c, 'ice_shadow:state:' + symbol,
                    _enc(self.states[symbol].to_dict()))

    # ------------------------------------------------------------- seed --

    def _seed(self, c, symbol, now):
        st = SymbolState()
        bars = _fetch_bars(c, symbol, now - SEED_DAYS * 86400, now)
        last = None
        for ts, o, h, l, cl in bars:
            if ts + 60 > now:
                break
            last = ts
            if _is_rth(ts):
                st.feed_1m(ts, o, h, l, cl, emit=False)
        # The trailing bucket is left pending on purpose: it may still
        # be mid-bucket in the store, and live detection tops it up /
        # flushes it when a later bucket's bar arrives.
        st.wm = last if last is not None else now - 3600
        st.seeded = True
        self.states[symbol] = st
        self._save_state(c, symbol)

    # ----------------------------------------------------------- detect --

    def _detect(self, c, symbol, now):
        st = self.states[symbol]
        bars = _fetch_bars(c, symbol, st.wm, now)
        fires = []
        changed = False
        for ts, o, h, l, cl in bars:
            if ts + 60 > now:
                break
            st.wm = ts
            changed = True
            if _is_rth(ts):
                fires += st.feed_1m(ts, o, h, l, cl, emit=True)
        if fires:
            self._record_fires(c, symbol, fires, now)
        if changed:
            self._save_state(c, symbol)
        return len(fires)

    def _record_fires(self, c, symbol, fires, now):
        rows = []
        for variant, tf, side, fire_ts, atr in fires:
            fid = identity('ice_shadow', variant, symbol, tf, side, fire_ts)
            payload = {'version': VERSION, 'variant': variant,
                       'symbol': symbol, 'timeframe': tf, 'side': side,
                       'fire_ts': fire_ts, 'atr': atr, 'entry': None,
                       'horizons': {str(h): {'status': 'pending'}
                                    for h in HORIZONS},
                       'created': now, 'resolved_at': None}
            rows.append(dict(id=fid, variant=variant, symbol=symbol,
                             timeframe=tf, side=side, fire_ts=fire_ts,
                             status='open', created=now, r60=None,
                             payload=payload))
        for start in range(0, len(rows), 250):
            c.execute(self.db.insert(ice_shadow_fires)
                      .values(rows[start:start + 250])
                      .on_conflict_do_nothing(index_elements=['id']))

    # ---------------------------------------------------------- resolve --

    def _resolve(self, c, now):
        rows = c.execute(select(ice_shadow_fires)
                         .where(ice_shadow_fires.c.status == 'open')
                         .order_by(ice_shadow_fires.c.fire_ts)
                         .limit(RESOLVE_CAP)).mappings()
        by_symbol = {}
        for row in rows:
            by_symbol.setdefault(row['symbol'], []).append(row)
        resolved = 0
        for symbol, srows in by_symbol.items():
            lo = min(r['fire_ts'] for r in srows) - 900
            bars1 = [b for b in _fetch_bars(c, symbol, lo, now)
                     if _is_rth(b[0])]
            if not bars1:
                continue
            bars5 = resample_5m(bars1)
            pos1 = {b[0]: i for i, b in enumerate(bars1)}
            pos5 = {b[0]: i for i, b in enumerate(bars5)}
            for row in srows:
                if self._resolve_one(c, row, bars1, bars5, pos1, pos5, now):
                    resolved += 1
        return resolved

    def _resolve_one(self, c, row, bars1, bars5, pos1, pos5, now):
        p = row['payload']
        tf = row['timeframe']
        seg, pos_map = (bars1, pos1) if tf == '1M' else (bars5, pos5)
        bs = 60 if tf == '1M' else 300
        pos = pos_map.get(row['fire_ts'])
        if pos is None:
            return False
        fire_date = _et_date(row['fire_ts'])
        sign = 1 if row['side'] == 'long' else -1
        horizons = p['horizons']
        pending = [h for h, v in horizons.items() if v['status'] == 'pending']
        if not pending:
            return False
        changed = False
        if p.get('entry') is None and pos + 1 < len(seg):
            p['entry'] = {'ts': seg[pos + 1][0], 'price': seg[pos + 1][1]}
            changed = True
        stale = now - row['fire_ts'] > STALE_SECONDS
        later_date = any(_et_date(b[0]) > fire_date for b in seg)
        session_over = (later_date
                        or now > _session_close_ts(fire_date) + 1800)
        results = score_fire(seg, pos, sign, p.get('atr'), bs, fire_date,
                             horizons=[h if h == 'session' else int(h)
                                       for h in pending])
        for h in pending:
            key = h if h == 'session' else int(h)
            res = results[key]
            if h == 'session' and not session_over and not stale:
                continue
            if res[0] == 'pending' and not stale:
                continue
            if res[0] == 'pending':
                horizons[h] = {'status': 'dropped', 'reason': 'stale_no_data'}
            elif res[0] == 'scored':
                horizons[h] = {'status': 'scored', 'r': res[1],
                               'exit_ts': res[2], 'exit': res[3]}
                if not math.isfinite(res[1]):
                    horizons[h] = {'status': 'dropped', 'reason': 'overflow'}
            else:
                horizons[h] = {'status': 'dropped', 'reason': res[1]}
            changed = True
        if all(v['status'] != 'pending' for v in horizons.values()):
            p['resolved_at'] = now
            h60 = horizons.get('60') or {}
            r60 = h60.get('r') if h60.get('status') == 'scored' else None
            c.execute(ice_shadow_fires.update()
                      .where(ice_shadow_fires.c.id == row['id'])
                      .values(status='done', r60=r60, payload=p))
            return True
        if changed:
            c.execute(ice_shadow_fires.update()
                      .where(ice_shadow_fires.c.id == row['id'])
                      .values(payload=p))
        return False

    # -------------------------------------------------------------- tick --

    def tick(self, c, now):
        if not getattr(self.cfg, 'ice_shadow_enabled', False):
            return
        if now - self.last_run < TICK_SECONDS:
            return
        self.last_run = now
        if not self.loaded:
            self._load_states(c)
        universe = [s for s in self.cfg.watch_symbols]
        seeded_now = 0
        for symbol in universe:
            st = self.states.get(symbol)
            if st is None or not st.seeded:
                if seeded_now < SEED_BUDGET:
                    self._seed(c, symbol, now)
                    seeded_now += 1
        fires = 0
        for symbol in universe:
            st = self.states.get(symbol)
            if st is not None and st.seeded:
                fires += self._detect(c, symbol, now)
        resolved = self._resolve(c, now)
        self.db.put(c, 'ice_shadow:worker', {
            'at': now, 'version': VERSION, 'universe': len(universe),
            'seeded_symbols': sum(1 for s in universe
                                  if (self.states.get(s) is not None
                                      and self.states[s].seeded)),
            'processed_fires': fires, 'resolved': resolved})


# -------------------------------------------------------------- summary --

def _stats(values):
    vals = sorted(values)
    n = len(vals)
    if not n:
        return {'n': 0, 'mean_r': None, 'win_rate': None, 'median_r': None}
    mean = sum(vals) / n
    wins = sum(1 for v in vals if v > 0)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {'n': n, 'mean_r': mean, 'win_rate': wins / n, 'median_r': median}


def summarize(db, c, now, day_start):
    """Per-variant accumulation for the digest + status endpoint."""
    out = {'version': VERSION, 'variants': {}}
    f = ice_shadow_fires
    for variant in VARIANTS:
        today = c.execute(select(func.count()).select_from(f)
                          .where(f.c.variant == variant,
                                 f.c.fire_ts >= day_start,
                                 f.c.fire_ts < day_start + 86400)).scalar() or 0
        total = c.execute(select(func.count()).select_from(f)
                          .where(f.c.variant == variant)).scalar() or 0
        open_n = c.execute(select(func.count()).select_from(f)
                           .where(f.c.variant == variant,
                                  f.c.status == 'open')).scalar() or 0
        r60s = c.execute(select(f.c.r60).where(f.c.variant == variant,
                                               f.c.r60.isnot(None))
                         .order_by(f.c.r60)).scalars().all()
        last = c.execute(select(func.max(f.c.fire_ts))
                         .where(f.c.variant == variant)).scalar()
        out['variants'][variant] = {
            'fires_today': int(today), 'fires_total': int(total),
            'open': int(open_n), 'last_fire_ts': last,
            'h60': _stats([float(v) for v in r60s])}
    out['worker'] = db.get(c, 'ice_shadow:worker')
    return out


def status(db, c, cfg, now):
    """One-call production verification surface (mirrors
    /api/iceberg-status): enablement, worker heartbeat, per-variant
    fire counts and +60m shadow stats for the current ET day + to date."""
    today_et = datetime.fromtimestamp(now, ET)
    day_start = datetime(today_et.year, today_et.month, today_et.day,
                         tzinfo=ET).timestamp()
    out = summarize(db, c, now, day_start)
    out['enabled'] = bool(getattr(cfg, 'ice_shadow_enabled', False))
    out['at'] = now
    out['basis'] = ('Shadow evidence only: hypothetical underlying '
                    'outcomes of ICE base / Max6+RSI fires; no trades, '
                    'no alerts, no premium.')
    return out
