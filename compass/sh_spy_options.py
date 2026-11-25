"""Structure Hunter SPY — forward premium scorer (2026-11-04, Josh).

Paper / measurement scope ONLY: no orders, no broker contact, no
TradersPost or Alpaca wiring. The one outward surface is the
registration's §6 display-only shadow alerts to the #compass
Discord channel (research route; own flag, default ON; see the
push section below). They posted to the 0DTE channel until
2026-11-25, when Josh moved every shadow book to #compass so
#0dte carries the 0DTE scanner funnel alone.

Frozen registration:
files/structure-hunter-spy-options-registration-2026-11-04.md
(sha256 a567aac05e713fc05ef843ec6065f63cce9f5b4805d293e37f3aa0890ffc34ba).
The underlying configuration is the frozen SPY gauntlet PASS
(registration dd37a57e...): preset S3 with the 3R target, stop =
tighter of pattern invalidation vs 1.5xATR, 40-bar time stop,
reverse-on-opposite OFF, all classes on except Broadening, on SPY
1-minute RTH bars.

What this module does, per the frozen registration:

- Runs the parity-proven detection port (vendored below in pure
  Python from hidden_files/structure-hunter-gauntlet-2026-11-03/
  sh_port.py — the same engine the MNQ and SPY gauntlets scored)
  over live SPY 1-minute bars from the production bar pipeline
  (events kind='bar'), with a multi-session warm-up replay on every
  pass so the signal stream is deterministic.
- Forms episodes from the strategy's own entries (the pick/geometry
  logic of sh_spy.run_strategy_spy, shared verbatim with the
  share-book simulator used by the parity test): signal at a bar
  close, entry at the next bar, one position at a time.
- Prices the premium leg from live OPRA quotes: contract selected
  from the live chain state (chain:SPY) under the frozen rule —
  first listed expiry with >2h of life at entry, |delta| nearest
  0.50 — entry stamped at the first observed quote at/after the
  entry bar's open (stream quote row first, chain quote fallback);
  exits evaluated on 1-minute underlying closes (frozen stop, 3R
  target, 40-bar time stop, precedence in that order, then the
  15:45 ET hard flatten) and stamped at the first bid at/after the
  trigger instant. Fees $0.65/leg/side.
- The G2-style scoreability gate: an entry spread above
  max(15% of ask, $0.05) makes the episode gate_excluded — counted,
  underlying tracked identically, never premium-scored.
- data_gap is a first-class terminal state (entry or exit quotes
  missing past the frozen 180s pricing window; no admissible
  contract on the live chain). Neither win nor loss, never dropped.
- The underlying ruler is scored on the same episodes (100 shares,
  gauntlet base costs, WITH the 15:45 ET flatten), including for
  gate_excluded and premium-data_gap episodes.

Wiring standards, as in the C2 / Connors scorers: episodes live in
the sh_spy_options_episodes table (declared in compass/store.py)
and are registered in active_observations.inventory(), so a chosen
contract is pinned in the OPRA selection for its whole open life;
every lifecycle transition is appended as a sh_spy_options event
(readable through the cron-token events export); counts are
recomputed from the table on every pass into the
sh_spy_options:state summary; health rows go through Store.health()
with the full shape. Forward-only: the sample floor is signal
dates >= 2026-11-04 ET (the registration's §1); nothing backfills.

Alerts (§6 of the registration, Josh 2026-11-04 13:05 CT): episode
OPEN (premium priced) and CLOSE (premium scored) alerts, labeled
SH-SPY SHADOW, queued into this module's own outbox
(sh_spy_push_outbox_v1) AFTER the episode transaction commits and
drained by this module's own loop to the research route's
configured destination. They never enter the general alert
pipeline, are never classified under the silenced scanner class,
and the scanner/ICT silence gates are untouched. Scoring is
byte-identical with the alert flag on or off (pinned by test); a
queue or delivery failure lands on the health:sh_spy_push marker
and on the outbox row — episodes book regardless.
"""
import asyncio
import logging
import math
import time
import uuid
from collections import deque
from datetime import datetime

import httpx
from sqlalchemy import select, update

from .market import NY, day, number
from .store import identity, sh_spy_options_episodes as episodes, \
    sh_spy_push_outbox

VERSION = 'sh-spy-options-v1'
KIND = 'sh_spy_options'
STATE_KEY = 'sh_spy_options:state'
SYMBOL = 'SPY'
FEE = 0.65            # per leg per side, house convention
QTY = 1
MULT = 100
SHARES = 100
SLIP_RT = 0.02        # gauntlet base slippage, points/share round trip
FREEZE_DATE = '2026-11-04'       # registration §1 sample floor (ET)
SAMPLE_START = '2026-11-04'      # first verdict window opens here
QUOTE_WAIT_S = 180               # registration §3 pricing window pin
CHAIN_STALE_S = 120              # chain asof may lag the entry open by this
REPLAY_DAYS = 21                 # stored-bar window loaded per pass
FLATTEN_HHMM = (15, 45)          # ET hard flatten
ENTRY_CUTOFF_HHMM = (15, 45)     # entry bars opening at/after flatten: no episode
ET = NY
NAN = float('nan')

# ════════════════════════════════════════════════════════════════
# Vendored detection engine — pure-Python transcription of the
# parity-proven port (sh_port.py, Structure Hunter S0 settings as
# resolved for Josh's SPY configuration). Transcribed verbatim in
# logic; numpy arrays become lists. The parity test pins this
# engine + the strategy geometry below against the gauntlet's
# scored trade list.
# ════════════════════════════════════════════════════════════════

PIV = 5
FM_TOL = 0.15; FM_HEAD = 0.10; FM_MINATR = 1.5; FM_SYM = 2.5; FM_TREND = True
FM_TOUCH5 = True; FM_FLAT = 0.25; FM_POLE = 2.0; FM_WAIT = 60
FM_RT_BARS = 20; FM_RT_TOL = 0.25; FM_VOL_LEN = 20; FM_VOL_MULT = 1.2
FM_CUPN = 0.8
FM_TOGGLES = dict(hs=True, tpl=True, dbl=True, cup=True, tri=True, wdg=True,
                  rect=True, chan=True, flag=True, broad=False)
E_ATRK = 1.5; E_FRAC = 0.6; E_TIME = 40; MIN_R = 1.0
R_MULT = 3.0   # S3R3 preset: target = ref + dir * 3.0 * risk
WY = dict(trendlen=50, clxvol=2.0, clxrng=1.5, minh=2.0, maxh=8.0, minb=10,
          armax=30, maxbars=150, sprdep=0.50, sprbars=3, needspr=True)


def wilder_atr(h, l, c, length=14):
    n = len(h)
    tr = [0.0] * n
    if n == 0:
        return []
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = [NAN] * n
    if n >= length:
        atr[length - 1] = sum(tr[:length]) / length
        a = 1.0 / length
        for i in range(length, n):
            atr[i] = atr[i - 1] + a * (tr[i] - atr[i - 1])
    return atr


def sma(x, length):
    n = len(x)
    out = [NAN] * n
    if n >= length:
        cs = [0.0]
        for v in x:
            cs.append(cs[-1] + v)
        for i in range(length - 1, n):
            out[i] = (cs[i + 1] - cs[i + 1 - length]) / length
    return out


def rolling_min(x, length):
    n = len(x); out = [NAN] * n
    dq = deque()
    for i in range(n):
        while dq and x[dq[-1]] >= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - length:
            dq.popleft()
        if i >= length - 1:
            out[i] = x[dq[0]]
    return out


def rolling_max(x, length):
    n = len(x); out = [NAN] * n
    dq = deque()
    for i in range(n):
        while dq and x[dq[-1]] <= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - length:
            dq.popleft()
        if i >= length - 1:
            out[i] = x[dq[0]]
    return out


def pivot_at(arr, j, left, right, want_high):
    """Pine ta.pivothigh/pivotlow tie semantics: the center must be strictly
    more extreme than every NEWER bar in the window and at least as extreme
    as every OLDER bar. Window = arr[j-left .. j+right]; evaluated at confirm
    bar j+right."""
    v = arr[j]
    for k in range(j - left, j):
        if want_high:
            if arr[k] > v:
                return False
        else:
            if arr[k] < v:
                return False
    for k in range(j + 1, j + right + 1):
        if want_high:
            if arr[k] >= v:
                return False
        else:
            if arr[k] <= v:
                return False
    return True


class Indicators:
    def __init__(self, bars):
        h, l, c, v = bars['h'], bars['l'], bars['c'], bars['v']
        self.atr = wilder_atr(h, l, c, 14)
        self.volma = sma(v, FM_VOL_LEN)
        self.wy_lo = rolling_min(l, WY['trendlen'])
        self.wy_hi = rolling_max(h, WY['trendlen'])
        n = bars['n']
        self.ph = [False] * n; self.pl = [False] * n
        for i in range(2 * PIV, n):
            j = i - PIV
            if pivot_at(h, j, PIV, PIV, True):
                self.ph[i] = True
            if pivot_at(l, j, PIV, PIV, False):
                self.pl[i] = True


def yline(b1, p1, b2, p2, x):
    return p1 + (p2 - p1) * (x - b1) / (b2 - b1)


def fm_name(kind, t):
    if kind == 4:
        return 'Inv. Cup & Handle' if t == 1 else 'Cup & Handle'
    if kind == 2:
        return 'H&S' if t == 1 else 'Inv. H&S'
    if kind == 3:
        return 'Triple Top' if t == 1 else 'Triple Bottom'
    return 'Double Top' if t == 1 else 'Double Bottom'


class Slot:
    __slots__ = ('act', 'b1', 'p1', 'b2', 'p2', 'st', 'hgt', 'inv', 'nm', 'id',
                 'uB1', 'uP1', 'uB2', 'uP2', 'dB1', 'dP1', 'dB2', 'dP2',
                 'ptEnd', 'apex', 'dir', 'pole')

    def __init__(self):
        self.act = False


class Trk:
    __slots__ = ('dir', 'b1', 'p1', 'b2', 'p2', 'tgt', 'fail', 'bar', 'vol',
                 'nm', 'id', 'cat', 'rtOpen')

    def __init__(self, d, b1, p1, b2, p2, tgt, fail, bar, vol, nm, id_, cat):
        self.dir, self.b1, self.p1, self.b2, self.p2 = d, b1, p1, b2, p2
        self.tgt, self.fail, self.bar, self.vol = tgt, fail, bar, vol
        self.nm, self.id, self.cat, self.rtOpen = nm, id_, cat, True


class WyRng:
    __slots__ = ('s', 'st', 'scM', 'scB', 'hiM', 'hiB', 'arM', 'arB', 'stDone',
                 'penB', 'penM', 'penLB', 'sosB', 'rngB', 'spr', 'sig', 'sosEv')

    def __init__(self, s):
        self.s = s; self.st = 0; self.scM = NAN; self.scB = 0; self.hiM = NAN
        self.hiB = 0; self.arM = NAN; self.arB = 0; self.stDone = False
        self.penB = None; self.penM = NAN; self.penLB = 0; self.sosB = 0
        self.rngB = 0; self.spr = False; self.sig = False; self.sosEv = False


def run_engine(bars, ind, debug=None):
    o, h, l, c, v = bars['o'], bars['h'], bars['l'], bars['c'], bars['v']
    n = bars['n']
    atr, volma = ind.atr, ind.volma
    phv = [None] * n; plv = [None] * n
    for i in range(n):
        if ind.ph[i]:
            phv[i] = h[i - PIV]
        if ind.pl[i]:
            plv[i] = l[i - PIV]

    fmZP, fmZB, fmZT = [], [], []
    fmRv = [Slot(), Slot()]
    fmLn = Slot()
    fmHist = deque()          # draw ids, cap fmMax=10; eviction deactivates slots
    fmBlock = 0; fmRevEnd = 0
    fmTr = []
    wyA, wyD = WyRng(1), WyRng(-1)
    sig_stream = [None] * n
    diag = {'brk': {}, 'late': {}, 'stored': {}, 'wy_sos': 0, 'wy_sig': 0,
            'rev_inv': 0, 'lin_inv': 0}

    def store_draw(did):
        fmHist.append(did)
        while len(fmHist) > 10:
            old = fmHist.popleft()
            for r in fmRv:
                if r.act and r.id == old:
                    r.act = False
            if fmLn.act and fmLn.id == old:
                fmLn.act = False

    def drop_draw(did):
        try:
            fmHist.remove(did)
        except ValueError:
            pass

    def track(sigs, d, b1, p1, b2, p2, lvl, tgt, fail, vol, nm, did, cat, late, i):
        fmTr.append(Trk(d, b1, p1, b2, p2, tgt, fail, i, vol, nm, did, cat))
        while len(fmTr) > 30:
            fmTr.pop(0)
        sigs.append(dict(dir=d, lvl=lvl, tgt=tgt, fail=fail, cat=cat, vol=vol,
                         retest=False, late=late, nm=nm))
        if debug is not None:
            debug.append(('sig', i, cat, d, nm, round(lvl, 2), round(tgt, 2), round(fail, 2)))
        diag['brk'][cat] = diag['brk'].get(cat, 0) + 1
        if late:
            diag['late'][cat] = diag['late'].get(cat, 0) + 1

    for i in range(n):
        sigs = []
        a = atr[i]
        ci, hi_, lo_, vi = c[i], h[i], l[i], v[i]
        vol_ok = (vi > 0 and not math.isnan(volma[i]) and vi >= volma[i] * FM_VOL_MULT)
        minH = FM_MINATR * a if not math.isnan(a) else NAN

        # ── pattern pivots + detection (fresh pivots only) ──
        for t in (1, -1):
            pv = phv[i] if t == 1 else plv[i]
            if pv is None:
                continue
            pb = i - PIV
            fresh = False
            if not fmZT or fmZT[-1] != t:
                fmZP.append(pv); fmZB.append(pb); fmZT.append(t); fresh = True
            elif (t == 1 and pv > fmZP[-1]) or (t == -1 and pv < fmZP[-1]):
                fmZP[-1] = pv; fmZB[-1] = pb
            while len(fmZP) > 60:
                fmZP.pop(0); fmZB.pop(0); fmZT.pop(0)
            if not fresh:
                continue
            zn = len(fmZP)
            P = lambda k: fmZP[-1 - k]
            B = lambda k: fmZB[-1 - k]
            kind = 0; km = 0; nb1 = 0; np1 = 0.0; nb2 = 1; np2 = 0.0
            cHg = NAN; cInv = NAN
            if zn >= 5 and (FM_TOGGLES['hs'] or FM_TOGGLES['tpl']):
                H3 = t * P(0); N2 = t * P(1); H2 = t * P(2); N1 = t * P(3); H1 = t * P(4)
                trOk = (not FM_TREND) or (zn >= 6 and t * P(5) < min(N1, N2))
                d1 = B(2) - B(4); d2 = B(0) - B(2)
                symOk = max(d1, d2) <= FM_SYM * max(min(d1, d2), 1)
                nAvg = 0.5 * (N1 + N2)
                aboveOk = min(H1, H2, H3) > max(N1, N2)
                hT = (H1 + H2 + H3) / 3.0 - nAvg
                if (FM_TOGGLES['tpl'] and trOk and symOk and aboveOk and hT >= minH
                        and max(H1, H2, H3) - min(H1, H2, H3) <= FM_TOL * hT
                        and abs(N1 - N2) <= 0.5 * hT):
                    kind = 3
                    km = 4 if (H1 >= H2 and H1 >= H3) else (2 if H2 >= H3 else 0)
                hH = H2 - nAvg
                if (kind == 0 and FM_TOGGLES['hs'] and trOk and symOk and aboveOk
                        and hH >= minH and H2 - max(H1, H3) >= FM_HEAD * hH
                        and abs(H1 - H3) <= FM_TOL * hH and abs(N1 - N2) <= 0.5 * hH):
                    kind = 2; km = 2
                if kind > 0:
                    nb1, np1, nb2, np2 = B(3), P(3), B(1), P(1)
            if kind == 0 and FM_TOGGLES['cup'] and zn >= 4:
                cc = -t; hnd = P(0); rR = P(1); bot = P(2); rL = P(3)
                dep = cc * (0.5 * (rL + rR) - bot)
                hDep = cc * (rR - hnd)
                cupW = B(1) - B(3); hW = B(0) - B(1)
                if (dep >= minH and abs(rL - rR) <= FM_TOL * dep and 0 < hDep <= 0.5 * dep
                        and cupW >= 4 * PIV and hW <= 0.5 * cupW):
                    kind = 4; km = 2; nb1 = B(1); np1 = rR; nb2 = B(1) + 1; np2 = rR
                    cHg = dep; cInv = bot + cc * dep * 0.5
            if kind == 0 and FM_TOGGLES['dbl'] and zn >= 3:
                B2 = t * P(0); NN = t * P(1); B1 = t * P(2)
                trOk2 = (not FM_TREND) or (zn >= 4 and t * P(3) < NN)
                hD = max(B1, B2) - NN
                if trOk2 and hD >= minH and abs(B1 - B2) <= FM_TOL * hD:
                    kind = 1; km = 2 if B1 >= B2 else 0
                    nb1 = B(1); np1 = P(1); nb2 = B(1) + 1; np2 = P(1)
            if kind > 0:
                ix = 0 if t == 1 else 1
                dr_id = i * 2 + (0 if t == 1 else 1)
                hg = cHg if kind == 4 else abs(P(km) - yline(nb1, np1, nb2, np2, B(km)))
                inv = cInv if kind == 4 else P(km)
                r = Slot()
                r.act = True; r.b1, r.p1, r.b2, r.p2 = nb1, np1, nb2, np2
                r.st = i; r.hgt = hg; r.inv = inv; r.nm = fm_name(kind, t); r.id = dr_id
                fmRv[ix] = r
                fmRevEnd = max(fmRevEnd, B(0))
                if debug is not None:
                    debug.append(('store_rev', i, r.nm, ix))
                diag['stored'][r.nm] = diag['stored'].get(r.nm, 0) + 1
                store_draw(dr_id)

            # ── line patterns ──
            m = 5; poleSkip = False
            if kind == 0 and zn >= m:
                leg0 = abs(P(m - 1) - P(m - 2)); legMax = 0.0
                for k in range(0, m - 2):
                    legMax = max(legMax, abs(P(k) - P(k + 1)))
                if legMax > 0 and leg0 >= FM_POLE * legMax:
                    if m == 5:
                        m = 4
                    else:
                        poleSkip = True
            if (kind == 0 and not poleSkip and zn >= m
                    and B(m - 1) >= max(fmBlock, fmRevEnd)):
                kHn = 0 if t == 1 else 1
                kHo = (4 if m == 5 else 2) if t == 1 else 3
                kLn = 1 if t == 1 else 0
                kLo = 3 if t == 1 else (4 if m == 5 else 2)
                uB1, uP1, uB2, uP2 = B(kHo), P(kHo), B(kHn), P(kHn)
                dB1, dP1, dB2, dP2 = B(kLo), P(kLo), B(kLn), P(kLn)
                xs, xe = B(m - 1), B(0); W = xe - xs
                Hs = yline(uB1, uP1, uB2, uP2, xs) - yline(dB1, dP1, dB2, dP2, xs)
                He = yline(uB1, uP1, uB2, uP2, xe) - yline(dB1, dP1, dB2, dP2, xe)
                if W > 0 and Hs >= minH and He > 0:
                    slU = (uP2 - uP1) / (uB2 - uB1); slL = (dP2 - dP1) / (dB2 - dB1)
                    su = slU * W / Hs; sd = slL * W / Hs; rat = He / Hs
                    midOk = True
                    if m == 5:
                        midL = yline(uB1, uP1, uB2, uP2, B(2)) if t == 1 else yline(dB1, dP1, dB2, dP2, B(2))
                        midOk = abs(P(2) - midL) <= FM_TOL * Hs
                    if midOk:
                        uF = abs(su) < FM_FLAT; lF = abs(sd) < FM_FLAT
                        uUp = su >= FM_FLAT; uDn = su <= -FM_FLAT
                        lUp = sd >= FM_FLAT; lDn = sd <= -FM_FLAT
                        lnm = ''; cat = 0; ldr = 0
                        if uF and lF:
                            lnm, cat = 'Rectangle', 3
                        elif uF and lUp:
                            lnm, cat, ldr = 'Ascending Triangle', 1, 1
                        elif uDn and lF:
                            lnm, cat, ldr = 'Descending Triangle', 1, -1
                        elif uDn and lUp:
                            lnm, cat = 'Symmetrical Triangle', 1
                        elif uUp and lUp:
                            if rat < 0.75:
                                lnm, cat, ldr = 'Rising Wedge', 2, -1
                            elif rat <= 1.33:
                                lnm, cat = 'Rising Channel', 4
                            else:
                                lnm, cat = 'Broadening Pattern', 5
                        elif uDn and lDn:
                            if rat < 0.75:
                                lnm, cat, ldr = 'Falling Wedge', 2, 1
                            elif rat <= 1.33:
                                lnm, cat = 'Falling Channel', 4
                            else:
                                lnm, cat = 'Broadening Pattern', 5
                        else:
                            lnm, cat = 'Broadening Pattern', 5
                        pole = 0.0
                        if FM_TOGGLES['flag'] and zn >= m + 1 and cat in (1, 3, 4):
                            poleLen = abs(P(m - 1) - P(m)); poleBars = B(m - 1) - B(m)
                            poleUp = P(m - 1) > P(m)
                            isTriS = cat == 1 and ldr == 0; isRect = cat == 3
                            isChDn = cat == 4 and su < 0; isChUp = cat == 4 and su > 0
                            if poleLen >= FM_POLE * Hs and poleBars <= W:
                                if poleUp and (isChDn or isRect):
                                    lnm, cat, ldr, pole = 'Bull Flag', 6, 1, poleLen
                                elif poleUp and isTriS:
                                    lnm, cat, ldr, pole = 'Bull Pennant', 6, 1, poleLen
                                elif not poleUp and (isChUp or isRect):
                                    lnm, cat, ldr, pole = 'Bear Flag', 6, -1, poleLen
                                elif not poleUp and isTriS:
                                    lnm, cat, ldr, pole = 'Bear Pennant', 6, -1, poleLen
                        catOn = {1: FM_TOGGLES['tri'], 2: FM_TOGGLES['wdg'],
                                 3: FM_TOGGLES['rect'], 4: FM_TOGGLES['chan'],
                                 5: FM_TOGGLES['broad'], 6: FM_TOGGLES['flag']}.get(cat, False)
                        if catOn:
                            if fmLn.act and xs < fmLn.ptEnd:
                                drop_draw(fmLn.id)
                            dr_id = i * 2 + (0 if t == 1 else 1)
                            apx = xs + math.floor(Hs / (slL - slU) + 0.5) if slL > slU else None
                            nl = Slot()
                            nl.act = True
                            nl.uB1, nl.uP1, nl.uB2, nl.uP2 = uB1, uP1, uB2, uP2
                            nl.dB1, nl.dP1, nl.dB2, nl.dP2 = dB1, dP1, dB2, dP2
                            nl.st = i; nl.ptEnd = xe; nl.apex = apx; nl.dir = ldr
                            nl.hgt = Hs; nl.pole = pole; nl.nm = lnm; nl.id = dr_id
                            fmLn = nl
                            if debug is not None:
                                debug.append(('store_line', i, lnm, cat, pole))
                            diag['stored'][lnm] = diag['stored'].get(lnm, 0) + 1
                            store_draw(dr_id)

        # ── reversal neckline break ──
        for ix in (0, 1):
            r = fmRv[ix]
            if not r.act:
                continue
            s = 1 if ix == 0 else -1
            nk = yline(r.b1, r.p1, r.b2, r.p2, i)
            if (s == 1 and ci < nk) or (s == -1 and ci > nk):
                r.act = False
                tg = nk - s * r.hgt
                track(sigs, -s, r.b1, r.p1, r.b2, r.p2, nk, tg, r.inv, vol_ok,
                      r.nm, r.id, 1, i == r.st, i)
            elif (s == 1 and ci > r.inv) or (s == -1 and ci < r.inv) or i - r.st > FM_WAIT:
                r.act = False
                diag['rev_inv'] += 1

        # ── line pattern break ──
        if fmLn.act:
            uN = yline(fmLn.uB1, fmLn.uP1, fmLn.uB2, fmLn.uP2, i)
            dN = yline(fmLn.dB1, fmLn.dP1, fmLn.dB2, fmLn.dP2, i)
            bd = 1 if ci > uN else (-1 if ci < dN else 0)
            if bd != 0:
                fmLn.act = False
                fmBlock = i
                lv = uN if bd == 1 else dN
                tg = lv + bd * (fmLn.pole if fmLn.pole > 0 else fmLn.hgt)
                ct = 3 if fmLn.pole > 0 else 2
                if bd == 1:
                    track(sigs, 1, fmLn.uB1, fmLn.uP1, fmLn.uB2, fmLn.uP2, lv, tg,
                          dN, vol_ok, fmLn.nm, fmLn.id, ct, i == fmLn.st, i)
                else:
                    track(sigs, -1, fmLn.dB1, fmLn.dP1, fmLn.dB2, fmLn.dP2, lv, tg,
                          uN, vol_ok, fmLn.nm, fmLn.id, ct, i == fmLn.st, i)
            elif i - fmLn.st > FM_WAIT or (fmLn.apex is not None and i >= fmLn.apex):
                fmLn.act = False
                diag['lin_inv'] += 1

        # ── retest + outcome tracker ──
        for j in range(len(fmTr) - 1, -1, -1):
            q = fmTr[j]
            if i <= q.bar:
                continue
            lv = yline(q.b1, q.p1, q.b2, q.p2, i)
            rTol = FM_RT_TOL * a
            done = False
            if q.rtOpen:
                lost = ci < lv - rTol if q.dir == 1 else ci > lv + rTol
                touched = (lo_ <= lv + rTol and ci > lv) if q.dir == 1 else (hi_ >= lv - rTol and ci < lv)
                if i - q.bar > FM_RT_BARS or lost:
                    q.rtOpen = False
                elif i - q.bar >= 2 and touched:
                    q.rtOpen = False
                    sigs.append(dict(dir=q.dir, lvl=lv, tgt=q.tgt, fail=q.fail,
                                     cat=q.cat, vol=q.vol, retest=True, late=False,
                                     nm=q.nm))
            if (q.dir == 1 and ci < q.fail) or (q.dir == -1 and ci > q.fail):
                done = True
            elif (q.dir == 1 and hi_ >= q.tgt) or (q.dir == -1 and lo_ <= q.tgt):
                done = True
            elif i - q.bar > FM_WAIT * 3:
                done = True
            if done:
                del fmTr[j]

        # ── Wyckoff ──
        wyA.sig = wyD.sig = wyA.sosEv = wyD.sosEv = False
        wy_clx = False
        if not math.isnan(a):
            barR = hi_ - lo_
            has_vol = vi > 0 and not math.isnan(volma[i])
            wy_clx = barR >= WY['clxrng'] * a and (
                vi >= WY['clxvol'] * volma[i] if has_vol else barR >= (WY['clxrng'] + 0.5) * a)
        ctx = dict(i=i, hi=hi_, lo=lo_, ci=ci, a=a, wy_clx=wy_clx,
                   wy_lo=ind.wy_lo[i], wy_hi=ind.wy_hi[i])
        _wy_step(wyA, wyD, plv[i], phv[i], ctx)
        _wy_step(wyD, wyA, (-phv[i] if phv[i] is not None else None),
                 (-plv[i] if plv[i] is not None else None), ctx)
        for w in (wyA, wyD):
            if w.sosEv:
                diag['wy_sos'] += 1
            if w.sig:
                diag['wy_sig'] += 1
                hh = w.arM - w.scM
                if w.s == 1:
                    track(sigs, 1, w.arB, w.arM, w.arB + 1, w.arM, w.arM,
                          w.arM + hh, w.scM, vol_ok, 'Wyckoff Accumulation',
                          -1, 4, False, i)
                else:
                    track(sigs, -1, w.arB, -w.arM, w.arB + 1, -w.arM, -w.arM,
                          -(w.arM + hh), -w.scM, vol_ok, 'Wyckoff Distribution',
                          -1, 4, False, i)
        sig_stream[i] = sigs
    return sig_stream, diag


def _wy_step(w, o, pLm, pHm, ctx):
    i, hi_, lo_, ci, a = ctx['i'], ctx['hi'], ctx['lo'], ctx['ci'], ctx['a']
    s = w.s
    mL = lo_ if s == 1 else -hi_
    mH = hi_ if s == 1 else -lo_
    mC = s * ci
    nExt = (lo_ <= ctx['wy_lo']) if s == 1 else (hi_ >= ctx['wy_hi'])
    pb = i - PIV
    oRng = o.st in (2, 3)
    if (w.st == 0 and ctx['wy_clx'] and nExt and not oRng) or (w.st == 1 and mL < w.scM):
        w.st = 1; w.scM = mL; w.scB = i; w.hiM = mH; w.hiB = i
    elif w.st == 1:
        if mH > w.hiM:
            w.hiM = mH; w.hiB = i
        if pHm is not None and pb > w.scB and w.hiM - w.scM > WY['maxh'] * a:
            w.st = 0
        elif pHm is not None and pb > w.scB and w.hiM - w.scM >= WY['minh'] * a and not oRng:
            if o.st == 1:
                o.st = 0
            w.arM = w.hiM; w.arB = w.hiB; w.rngB = i; w.st = 2
            w.stDone = False; w.spr = False; w.penB = None
        elif i - w.scB > WY['armax']:
            w.st = 0
    elif w.st in (2, 3):
        hh = w.arM - w.scM
        if mC < w.scM - WY['sprdep'] * hh:
            w.st = 0
        elif mC > w.arM:
            ok = w.spr or not WY['needspr']
            w.sosEv = True
            w.sosB = i; w.st = 4
            if ok:
                w.sig = True
        else:
            if (w.st == 2 and not w.stDone and pLm is not None and pb > w.arB
                    and w.scM - 0.1 * hh <= pLm <= w.scM + 0.35 * hh):
                w.stDone = True
            if w.st == 2 and i - w.rngB >= WY['minb']:
                if mL < w.scM:
                    if w.penB is None:
                        w.penB = i; w.penM = mL; w.penLB = i
                    elif mL < w.penM:
                        w.penM = mL; w.penLB = i
                if w.penB is not None:
                    if mC > w.scM and w.penM >= w.scM - WY['sprdep'] * hh:
                        w.spr = True; w.st = 3; w.penB = None
                    elif i - w.penB >= WY['sprbars']:
                        w.penB = None
            if w.st != 0 and i - w.scB > WY['maxbars']:
                w.st = 0
    elif w.st == 4:
        hh = w.arM - w.scM
        if pLm is not None and pb > w.sosB and pLm > w.scM + 0.5 * hh:
            w.st = 0
        elif mC < w.scM or i - w.sosB > WY['armax']:
            w.st = 0


# ════════════════════════════════════════════════════════════════
# Strategy layer — the pick/geometry logic of sh_spy.run_strategy_spy
# (S3R3, revOpp OFF), shared by the live scorer and the share-book
# simulator below. The simulator transcribes run_strategy_spy
# verbatim (mode='real', in_flat all False) and exists so the parity
# test can pin this module against the gauntlet's scored trade list.
# ════════════════════════════════════════════════════════════════

def pick_entry(sigs, atr_i, close_i):
    """The strategy's entry decision at one bar, given the bar's signal
    list: the LAST non-retest signal is the pick (the port's loop
    overwrites), then the frozen geometry gates apply (risk > 0,
    measured move > 0, R multiple >= MIN_R at the 3R target). Returns
    a pend_entry dict or None."""
    pick = None
    for s in sigs or []:
        if s['retest']:
            continue
        pick = s
    if pick is None:
        return None
    if atr_i is None or math.isnan(atr_i):
        return None
    d = pick['dir']; ref = close_i; a = atr_i
    sPat = pick['fail']
    sAtr = ref - d * E_ATRK * a
    stp = max(sPat, sAtr) if d == 1 else min(sPat, sAtr)
    risk = d * (ref - stp)
    if risk <= 0:
        return None
    mm = d * (pick['tgt'] - ref)
    tgt = ref + d * R_MULT * risk
    rr = d * (tgt - ref) / risk
    if rr < MIN_R or mm <= 0:
        return None
    return dict(dir=d, stop=stp, tgt=tgt, risk=risk,
                cat=pick['cat'], nm=pick['nm'])


def run_strategy_shares(bars, ind, sig_stream):
    """run_strategy_spy transcribed (real mode, S3R3 cfg): next-bar-open
    fills, bracket frozen at the signal bar, stop-first intrabar,
    close_all at next open, 40-bar time stop from the signal bar,
    revOpp OFF (no entry while a position is open)."""
    o, h, l, c = bars['o'], bars['h'], bars['l'], bars['c']
    atr = ind.atr
    n = bars['n']
    pos = None; pend_entry = None; pend_close = None
    posDir = 0; prev_inpos = False
    trades = []

    def close_pos(px, bar, reason):
        nonlocal pos
        t = dict(pos)
        t['exit'] = px; t['exit_bar'] = bar; t['reason'] = reason
        t['gross_pts'] = pos['dir'] * (px - pos['entry'])
        trades.append(t)
        pos = None

    for i in range(n):
        if pend_close is not None and pos is not None:
            close_pos(o[i], i, pend_close)
            pend_close = None
        if pend_entry is not None:
            pe = pend_entry; pend_entry = None
            if pos is not None and pe['dir'] == pos['dir']:
                pos['stop'] = pe['stop']; pos['tgt'] = pe['tgt']
                pos['risk'] = pe['risk']; pos['sig_bar'] = pe['sig_bar']
            else:
                if pos is not None:
                    close_pos(o[i], i, 'reverse')
                pos = dict(dir=pe['dir'], entry=o[i], entry_bar=i,
                           stop=pe['stop'], tgt=pe['tgt'], risk=pe['risk'],
                           cat=pe['cat'], nm=pe['nm'], sig_bar=pe['sig_bar'])
        if pos is not None:
            d = pos['dir']
            if d == 1:
                if l[i] <= pos['stop']:
                    close_pos(o[i] if o[i] < pos['stop'] else pos['stop'], i, 'stop')
                elif h[i] >= pos['tgt']:
                    close_pos(o[i] if o[i] > pos['tgt'] else pos['tgt'], i, 'target')
            else:
                if h[i] >= pos['stop']:
                    close_pos(o[i] if o[i] > pos['stop'] else pos['stop'], i, 'stop')
                elif l[i] <= pos['tgt']:
                    close_pos(o[i] if o[i] < pos['tgt'] else pos['tgt'], i, 'target')
        inpos_now = pos is not None
        if pos is not None:
            if i - pos['sig_bar'] >= E_TIME:
                pend_close = 'time'
        sigs = sig_stream[i] or []
        canEnter = pend_close is None and pos is None
        if sigs and canEnter:
            pe = pick_entry(sigs, atr[i], c[i])
            if pe is not None:
                pe['sig_bar'] = i
                pend_entry = pe
                posDir = pe['dir']
        if pos is None and prev_inpos:
            posDir = 0
        prev_inpos = inpos_now
    return trades


# ════════════════════════════════════════════════════════════════
# Contract selection + quote resolution (registration §2/§3)
# ════════════════════════════════════════════════════════════════

def spread_gate(bid, ask):
    """The frozen G2-style bound. Returns (spread, gate, passes)."""
    spread = ask - bid
    gate = max(0.15 * ask, 0.05)
    return spread, gate, spread <= gate + 1e-9


def expiry_instant(expiry_date):
    """An expiry's life ends at 16:00 ET on its expiry date."""
    return datetime.fromisoformat(expiry_date).replace(
        hour=16, minute=0, second=0, tzinfo=ET).timestamp()


def select_contract(contracts, dir, entry_ts, spot):
    """Frozen selection: type by direction; the first listed expiry
    with >2h of life remaining at entry; within it, the strike whose
    |delta| is nearest 0.50 over the admissible set (listed strikes
    carrying a quoted delta) with deterministic tie-breaks (smaller
    |delta-0.50|, strike closest to spot, lower strike). Returns the
    chain contract dict or None."""
    want = 'call' if dir == 1 else 'put'
    typed = [o for o in contracts or []
             if str(o.get('type') or '').lower() == want and o.get('expiry')]
    expiries = sorted({str(o['expiry']) for o in typed})
    chosen_expiry = None
    for e in expiries:
        if expiry_instant(e) - entry_ts > 2 * 3600:
            chosen_expiry = e
            break
    if chosen_expiry is None:
        return None
    cands = []
    for o in typed:
        if str(o['expiry']) != chosen_expiry:
            continue
        delta = number(o.get('delta'))
        strike = number(o.get('strike'))
        if delta is None or strike is None or not o.get('symbol'):
            continue
        cands.append(o)
    if not cands:
        return None

    def key(o):
        return (abs(abs(number(o['delta'])) - 0.50),
                abs(number(o['strike']) - spot) if spot else 0.0,
                number(o['strike']))
    return min(cands, key=key)


def _quote_candidate(q, source):
    """Normalize a quote dict to (ts, bid, ask, source) or None."""
    if not isinstance(q, dict):
        return None
    ts, bid, ask = number(q.get('ts')), number(q.get('bid')), number(q.get('ask'))
    if ts is None or bid is None or ask is None or ask < bid or ask <= 0:
        return None
    return ts, bid, ask, source


def first_quote_at_or_after(db, c, chain, contract_symbol, since_ts):
    """The first OPRA quote for the contract at/after since_ts that the
    scorer can observe: the stream quote row (quote:<OCC>) and the
    live chain state's contract quote are both candidates; the one
    with the smallest quote timestamp at/after since_ts wins, the
    stream row breaking ties. Returns (ts, bid, ask, source) or
    None."""
    cands = []
    q = db.get(c, 'quote:' + contract_symbol)
    cand = _quote_candidate(q, 'stream')
    if cand and cand[0] >= since_ts:
        cands.append(cand)
    for o in (chain or {}).get('contracts', []):
        if o.get('symbol') == contract_symbol:
            cand = _quote_candidate(o.get('quote') or {}, 'chain')
            if cand and cand[0] >= since_ts:
                cands.append(cand)
            break
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], 0 if x[3] == 'stream' else 1))
    return cands[0]


# ════════════════════════════════════════════════════════════════
# Display-only shadow alerts (registration §6). Own outbox + own
# drainer, following the house per-channel outbox precedent
# (ict_push). Destination = the research route's configured
# webhook, resolved through alert_routes.destination exactly as the
# routing layer resolves it (dedicated webhook or shared fallback).
# These alerts never enter the general alert pipeline.
# ════════════════════════════════════════════════════════════════

PUSH_KIND = 'sh_spy_push'
MAX_PUSH_ATTEMPTS = 10
REASON_LABEL = {'stop': 'STOP', 'target': 'TARGET', 'time': 'TIME STOP',
                'flatten': 'FLATTEN'}


def _contract_label(contract):
    if not contract:
        return 'SPY contract'
    typ = 'C' if str(contract.get('type') or '').lower() == 'call' else 'P'
    strike = number(contract.get('strike'))
    strike_s = ('%g' % strike) if strike is not None else '?'
    return 'SPY %s %s%s' % (contract.get('expiry') or '?', strike_s, typ)


def _money(v):
    if v is None:
        return '—'
    return '+$%.2f' % v if v >= 0 else '-$%.2f' % abs(v)


def format_open_message(payload):
    """SH-SPY SHADOW open alert (registration §6 labeling): identity
    line first, pattern + direction, contract, entry ask, and the
    shadow disclaimer. No live-trading language of any kind."""
    side = 'LONG' if payload.get('dir') == 1 else 'SHORT'
    emoji = '📈' if payload.get('dir') == 1 else '📉'
    lines = [
        '🧪 **SH-SPY SHADOW** — shadow forward test · display only',
        '%s **OPEN %s** · %s · SPY' % (emoji, side, payload.get('pattern', '?')),
        'Contract `%s` · entry ask `$%.2f`' % (
            _contract_label(payload.get('contract')), payload.get('entry_ask', 0)),
        'Underlying stop `%s` · target `%s` (frozen levels, scored on closes)' % (
            _fmt_level(payload.get('stop')), _fmt_level(payload.get('target'))),
        '_Shadow measurement only — not a 0DTE-book trade; nothing was booked or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'SH-SPY Shadow',
            'allowed_mentions': {'parse': []}}


def format_close_message(payload):
    """SH-SPY SHADOW close alert: identity line, pattern + direction,
    contract, exit bid + scored P&L + exit reason, shadow disclaimer."""
    side = 'LONG' if payload.get('dir') == 1 else 'SHORT'
    emoji = '📈' if payload.get('dir') == 1 else '📉'
    reason = REASON_LABEL.get(payload.get('reason'), str(payload.get('reason', '?')).upper())
    lines = [
        '🧪 **SH-SPY SHADOW** — shadow forward test · display only',
        '%s **CLOSE %s** · %s · SPY — %s' % (
            emoji, side, payload.get('pattern', '?'), reason),
        'Contract `%s` · entry ask `$%.2f` → exit bid `$%.2f`' % (
            _contract_label(payload.get('contract')), payload.get('entry_ask', 0),
            payload.get('exit_bid', 0)),
        'Scored P&L **%s** · underlying ruler **%s** (100 sh)' % (
            _money(payload.get('pnl')), _money(payload.get('underlying_pnl'))),
        '_Shadow measurement only — not a 0DTE-book trade; nothing was booked or sent anywhere._',
    ]
    return {'content': '\n'.join(lines)[:1900],
            'username': 'SH-SPY Shadow',
            'allowed_mentions': {'parse': []}}


def _fmt_level(v):
    try:
        return '%.2f' % float(v)
    except (TypeError, ValueError):
        return '—'


def queue_alert(db, cfg, payload):
    """Queue one shadow alert in the module's own outbox. Called only
    AFTER the episode transaction has committed, in its own
    transaction. Returns True when queued (or when alerts are off —
    nothing to do). On failure the error lands on the
    health:sh_spy_push marker and False is returned; the caller has
    already booked the episode, which never depends on this."""
    if not bool(getattr(cfg, 'sh_spy_alerts', True)):
        return True
    try:
        row_id = identity('sh-spy-push-v1', payload.get('episode_id', ''),
                          payload.get('type', ''), payload.get('at', 0))
        with db.tx() as c:
            c.execute(db.insert(sh_spy_push_outbox).values(
                id=row_id, status='pending', created=time.time(),
                payload=payload, delivery={'attempts': 0})
                .on_conflict_do_nothing(index_elements=['id']))
        return True
    except Exception as e:
        try:
            db.health(PUSH_KIND, 'error',
                      'shadow alert queue failed: %s' % type(e).__name__)
        except Exception:
            pass
        logging.getLogger('uvicorn.error').exception(
            'sh_spy_options: alert queue failed; episode already booked')
        return False


def push_destination(cfg):
    """The research route destination (url, mode) via the routing
    layer's own resolver; url None when the route is unconfigured.
    (2026-11-25, Josh: the shadow books post to #compass; the
    options_0dte route belongs to the 0DTE scanner funnel alone.
    The destination is resolved here at drain time and outbox rows
    persist no route, so rows still queued when this shipped
    follow the move automatically — none are stranded and none
    keep posting to #0dte.)"""
    from .alert_routes import destination
    url, mode = destination(cfg, 'research')
    return (url if url else None), mode


def deliver_one_push(db, client, url, now=None, max_attempts=MAX_PUSH_ATTEMPTS):
    """Attempt one pending shadow alert. Mirrors the fixed ict_push
    drainer semantics: any local failure (including a formatter
    error) fails the row with the cause recorded in its delivery —
    never strands a row in 'sending', never raises into the loop.
    Returns True if a row was attempted."""
    import re
    if not url or not re.fullmatch(
            r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+', url):
        return False
    now = time.time() if now is None else now
    with db.tx() as c:
        row = c.execute(select(sh_spy_push_outbox)
                        .where(sh_spy_push_outbox.c.status == 'pending')
                        .order_by(sh_spy_push_outbox.c.created).limit(1)
                        ).mappings().first()
        if not row:
            return False
        d = dict(row['delivery'])
        if d.get('next_attempt') and now < d['next_attempt']:
            return False
        d['attempts'] = d.get('attempts', 0) + 1
        c.execute(update(sh_spy_push_outbox)
                  .where(sh_spy_push_outbox.c.id == row['id'])
                  .values(status='sending', delivery=d))
        job = dict(row, delivery=d)

    status = 'failed'
    try:
        formatter = (format_close_message if job['payload'].get('type') == 'close'
                     else format_open_message)
        resp = client.post(url, params={'wait': 'true'},
                           json=formatter(job['payload']))
        if 200 <= resp.status_code < 300:
            status = 'delivered'
        elif resp.status_code == 429:
            status = 'pending'
            try:
                delay = max(1, min(3600, float(resp.json().get('retry_after', 30))))
            except Exception:
                delay = 30
            job['delivery']['next_attempt'] = now + delay
        elif resp.status_code >= 500:
            status = 'pending'
            job['delivery']['next_attempt'] = now + 30
    except httpx.HTTPError:
        status = 'pending'
        job['delivery']['next_attempt'] = now + 30
    except Exception as e:
        job['delivery']['error'] = '%s: %s' % (type(e).__name__, str(e)[:200])

    if status == 'pending' and job['delivery'].get('attempts', 0) >= max_attempts:
        status = 'failed'

    with db.tx() as c:
        c.execute(update(sh_spy_push_outbox)
                  .where(sh_spy_push_outbox.c.id == job['id'])
                  .values(status=status, delivery=job['delivery']))
    if status == 'delivered':
        db.health(PUSH_KIND, 'delivered',
                  'SH-SPY shadow alert confirmed by Discord', now)
    elif status == 'failed':
        db.health(PUSH_KIND, 'error',
                  'shadow alert delivery failed: %s'
                  % job['delivery'].get('error', 'attempts exhausted'))
    return True


async def run_push(db, cfg):
    """Drainer loop for the §6 shadow alerts (own outbox, research
    route destination). Never raises out of the loop."""
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if bool(getattr(cfg, 'sh_spy_options', True)) and \
                        bool(getattr(cfg, 'sh_spy_alerts', True)):
                    url, mode = push_destination(cfg)
                    if url:
                        await asyncio.to_thread(deliver_one_push, db, client, url)
                    else:
                        db.health(PUSH_KIND, 'not_configured',
                                  'research route has no webhook; '
                                  'shadow alerts stay queued')
            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    db.health(PUSH_KIND, 'error', type(e).__name__)
                except Exception:
                    pass
            await asyncio.sleep(2)


# ════════════════════════════════════════════════════════════════
# Scorer
# ════════════════════════════════════════════════════════════════

def _hhmm_ts(date_str, hhmm):
    """Epoch of a wall-clock time on an ET date."""
    return datetime.fromisoformat(date_str).replace(
        hour=hhmm[0], minute=hhmm[1], second=0, tzinfo=ET).timestamp()


def window_index(date_str):
    """Verdict windows: consecutive 4-week blocks from the sample
    start (registration §5), by signal date."""
    d0 = datetime.fromisoformat(SAMPLE_START).date()
    d = datetime.fromisoformat(date_str).date()
    return (d - d0).days // 28


class SHSpyOptions:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg, self.clock = db, cfg, clock
        self.owner = uuid.uuid4().hex

    def now(self, fallback):
        return self.clock() if self.clock else fallback

    # -- persistence ----------------------------------------------------------

    def _save(self, c, p, now, status=None):
        p['updated_at'] = now
        c.execute(update(episodes).where(episodes.c.id == p['id']).values(
            status=status or p['status'], updated=now, payload=p))

    def _event(self, c, p, kind, now, extra=None):
        body = {'episode_id': p['id'], 'symbol': SYMBOL, 'event': kind,
                'at': now, **(extra or {})}
        self.db.append(c, KIND, VERSION, SYMBOL, now, body,
                       key=identity(KIND, p['id'], kind, body.get('date')))

    def _active(self, c):
        """Episodes not yet final (any premium state)."""
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status.in_(('open', 'gate_excluded', 'data_gap')))
            .order_by(episodes.c.created)).scalars().all()
        return [p for p in rows if p.get('stage') != 'done']

    def _counts(self, c):
        rows = c.execute(select(episodes.c.status, episodes.c.payload)).all()
        counts = {'created': 0, 'opened': 0, 'closed': 0, 'open': 0,
                  'gate_excluded': 0, 'data_gap': 0, 'underlying_closed': 0}
        for status, p in rows:
            counts['created'] += 1
            if p.get('entry'):
                counts['opened'] += 1
            if p.get('underlying_exit'):
                counts['underlying_closed'] += 1
            if status in counts:
                counts[status] += 1
        return counts

    def _windows(self, c):
        """Per-window accrual summary (registration §5 frame): scored
        premium trades with pooled/window nets, PF, and the pooled
        underlying ruler over the same scored episodes."""
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        wins = {}
        pooled_u = 0.0
        for p in rows:
            if p.get('status') != 'closed' or p.get('pnl') is None:
                continue
            w = wins.setdefault(window_index(p['signal']['date']), {
                'window': window_index(p['signal']['date']), 'scored': 0,
                'premium_net': 0.0, 'gp': 0.0, 'gl': 0.0,
                'underlying_net': 0.0})
            w['scored'] += 1
            w['premium_net'] += p['pnl']
            if p['pnl'] > 0:
                w['gp'] += p['pnl']
            else:
                w['gl'] -= p['pnl']
            u = (p.get('underlying_exit') or {}).get('net_dollars')
            if u is not None:
                w['underlying_net'] += u
                pooled_u += u
        out = []
        for w in sorted(wins.values(), key=lambda x: x['window']):
            w['premium_pf'] = (round(w['gp'] / w['gl'], 3) if w['gl'] > 0
                               else (None if w['gp'] > 0 else 0.0))
            w['premium_net'] = round(w['premium_net'], 2)
            w['underlying_net'] = round(w['underlying_net'], 2)
            w.pop('gp'); w.pop('gl')
            out.append(w)
        return out

    def _write_state(self, c, now, extra=None):
        state = {'version': VERSION, 'at': now, 'counts': self._counts(c),
                 'windows': self._windows(c), **(extra or {})}
        old = self.db.get(c, STATE_KEY, {})
        merged = {**old, **state}
        self.db.put(c, STATE_KEY, merged)
        return merged

    # -- bars -----------------------------------------------------------------

    def _load_bars(self, c, now):
        """Completed SPY 1-minute RTH bars from the production bar
        pipeline over the replay window, ascending by ts."""
        since = now - REPLAY_DAYS * 86400
        rows = self.db.recent_bars_after(c, SYMBOL, since, 0, 50000)
        bars = []
        seen = set()
        for r in rows:
            ts = r['ts']
            if ts in seen or ts + 60 > now:
                continue
            p = r.get('payload') or {}
            vals = [number(p.get(k)) for k in ('o', 'h', 'l', 'c', 'v')]
            if any(v is None for v in vals):
                continue
            dt = datetime.fromtimestamp(ts, ET)
            mins = dt.hour * 60 + dt.minute
            if not (570 <= mins < 960):
                continue
            seen.add(ts)
            bars.append({'ts': ts, 'o': vals[0], 'h': vals[1], 'l': vals[2],
                         'c': vals[3], 'v': vals[4],
                         'session': dt.date().isoformat()})
        bars.sort(key=lambda b: b['ts'])
        return bars

    # -- episode lifecycle ------------------------------------------------------

    def _create_episode(self, c, pick, sig_bar, entry_bar, now):
        p = {
            'id': identity('sh-spy-options-episode-v1', sig_bar['session'],
                           sig_bar['ts'], pick['dir'], pick['cat'], pick['nm']),
            'version': VERSION, 'strategy': 'sh-spy-s3r3-options',
            'symbol': SYMBOL, 'underlying': SYMBOL,
            'side': 'long' if pick['dir'] == 1 else 'short',
            'dir': pick['dir'],
            'status': 'open', 'stage': 'awaiting_entry',
            'premium_status': 'awaiting_entry', 'created_at': now,
            'signal': {'date': sig_bar['session'], 'bar_ts': sig_bar['ts'],
                       'ref': sig_bar['c'], 'stop': pick['stop'],
                       'target': pick['tgt'], 'risk': pick['risk'],
                       'cat': pick['cat'], 'pattern': pick['nm'],
                       'entry_bar_ts': entry_bar['ts'],
                       'entry_date': entry_bar['session']},
            'contract': None, 'entry': None, 'exit_trigger': None,
            'exit': None, 'underlying_exit': None,
            'underlying_entry': {'price': entry_bar['o'],
                                 'bar_ts': entry_bar['ts']},
            'pnl': None, 'outcome': None, 'gate_exclusion': None,
            'entry_data_gap': None, 'exit_data_gap': None,
        }
        stmt = self.db.insert(episodes).values(
            id=p['id'], underlying=SYMBOL, status='open', created=now,
            updated=now, payload=p)
        res = c.execute(stmt.on_conflict_do_nothing(index_elements=['id']))
        if res.rowcount == 0:
            row = c.execute(select(episodes.c.payload).where(
                episodes.c.id == p['id'])).scalars().first()
            return row
        self._event(c, p, 'episode_created', now,
                    {'date': sig_bar['session'], 'pattern': pick['nm'],
                     'dir': pick['dir']})
        return p

    def _trigger_exit(self, c, p, bar, reason, now, instant=None):
        """Record the underlying exit at the trigger bar's close (or the
        flatten bar) and arm premium exit pricing. Premium-terminal
        episodes (gate/data_gap) finalize here."""
        entry_px = p['underlying_entry']['price']
        exit_px = bar['c']
        gross_pts = p['dir'] * (exit_px - entry_px)
        net_pts = gross_pts - SLIP_RT
        p['underlying_exit'] = {
            'bar_ts': bar['ts'], 'price': exit_px, 'reason': reason,
            'gross_pts': gross_pts, 'net_dollars': net_pts * SHARES,
            'bps': 10000.0 * net_pts / entry_px if entry_px else None}
        p['exit_trigger'] = {'reason': reason, 'bar_ts': bar['ts'],
                             'instant': instant if instant is not None
                             else bar['ts'] + 60,
                             'underlying_price': exit_px}
        if p['premium_status'] in ('gate_excluded', 'data_gap'):
            p['stage'] = 'done'
            self._event(c, p, 'underlying_closed', now,
                        {'date': p['signal']['date'], 'reason': reason,
                         'premium_status': p['premium_status']})
        else:
            if p.get('entry'):
                p['stage'] = 'awaiting_exit'
            self._event(c, p, 'exit_triggered', now,
                        {'date': p['signal']['date'], 'reason': reason})
        self._save(c, p, now)

    # -- main pass ---------------------------------------------------------------

    def process(self, now):
        alerts_out = []
        with self.db.tx() as c:
            state = self.db.get(c, STATE_KEY, {})
        bootstrap = 'bootstrap_at' not in state
        with self.db.tx() as c:
            bars = self._load_bars(c, now)
        if bootstrap:
            # Bootstrap watermark: episodes form only from bars that
            # complete AFTER the scorer is live. Pricing is from live
            # OPRA quotes observed at the pricing pass, so replaying a
            # completed session's bars at first boot would stamp
            # entries/exits off stale after-close quote rows — not the
            # live forward measurement the registration freezes. The
            # sample therefore starts with the first bar that completes
            # under the deployed scorer (the proposal's "first full
            # session after the build is production-verified").
            boot_mark = bars[-1]['ts'] if bars else None
            with self.db.tx() as c:
                self.db.append(c, KIND, VERSION, SYMBOL, now,
                               {'event': 'bootstrap', 'at': now,
                                'bars_loaded': len(bars),
                                'sample_floor': FREEZE_DATE,
                                'watermark_bar_ts': boot_mark},
                               key=identity(KIND, 'bootstrap'))
                extra = {'bootstrap_at': now}
                if boot_mark is not None:
                    extra['last_bar_ts'] = boot_mark
                self._write_state(c, now, extra)
            state = {**state, 'bootstrap_at': now}
            if boot_mark is not None:
                state['last_bar_ts'] = boot_mark
        if not bars:
            self.db.health('sh_spy_options', 'waiting',
                           'SH-SPY options scorer (paper/measurement): '
                           'no SPY bars in the replay window yet')
            return

        arrays = {'o': [b['o'] for b in bars], 'h': [b['h'] for b in bars],
                  'l': [b['l'] for b in bars], 'c': [b['c'] for b in bars],
                  'v': [b['v'] for b in bars], 'n': len(bars)}
        ind = Indicators(arrays)
        sig_stream, _diag = run_engine(arrays, ind)
        idx_of = {b['ts']: i for i, b in enumerate(bars)}
        counters = {'signals_taken': state.get('signals_taken', 0),
                    'flatten_skipped': state.get('flatten_skipped', 0)}

        with self.db.tx() as c:
            active = self._active(c)
            act = next((p for p in active if not p.get('exit_trigger')), None)
            watermark = state.get('last_bar_ts')
            start = 0
            if watermark is not None and watermark in idx_of:
                start = idx_of[watermark] + 1
            elif watermark is not None:
                start = next((i for i, b in enumerate(bars)
                              if b['ts'] > watermark), len(bars))
            pending_pick = None   # (pick, sig_bar index) awaiting its entry bar
            new_watermark = watermark
            for k in range(start, len(bars)):
                b = bars[k]
                if b['session'] < FREEZE_DATE:
                    new_watermark = b['ts']
                    continue
                # (a) resolve a pending pick: this bar is its entry bar.
                if pending_pick is not None:
                    pick, sig_i = pending_pick
                    pending_pick = None
                    et_dt = datetime.fromtimestamp(b['ts'], ET)
                    if (b['session'] == bars[sig_i]['session']
                            and (et_dt.hour, et_dt.minute) >= ENTRY_CUTOFF_HHMM):
                        counters['flatten_skipped'] += 1
                    else:
                        counters['signals_taken'] += 1
                        act = self._create_episode(c, pick, bars[sig_i], b, now)
                # (b) exits for the open episode, on this bar's close.
                if act is not None and not act.get('exit_trigger') \
                        and act.get('underlying_entry'):
                    ei = idx_of.get(act['signal']['entry_bar_ts'])
                    si = idx_of.get(act['signal']['bar_ts'])
                    if ei is not None and si is not None and k >= ei:
                        d, cl = act['dir'], b['c']
                        reason = None
                        if (d == 1 and cl <= act['signal']['stop']) or \
                                (d == -1 and cl >= act['signal']['stop']):
                            reason = 'stop'
                        elif (d == 1 and cl >= act['signal']['target']) or \
                                (d == -1 and cl <= act['signal']['target']):
                            reason = 'target'
                        elif k - si >= E_TIME:
                            reason = 'time'
                        if reason:
                            self._trigger_exit(c, act, b, reason, now)
                            act = None
                # (c) pick at this bar when flat (the share book enters
                # on the same bar a position closed).
                if act is not None and act.get('exit_trigger'):
                    act = None
                if act is None and pending_pick is None and sig_stream[k]:
                    pe = pick_entry(sig_stream[k], ind.atr[k], b['c'])
                    if pe is not None:
                        pending_pick = (pe, k)
                if pending_pick is None:
                    new_watermark = b['ts']
            if pending_pick is not None:
                # The signal bar stays unprocessed; the next pass
                # re-evaluates it once its entry bar exists.
                sig_ts = bars[pending_pick[1]]['ts']
                prior = [b['ts'] for b in bars if b['ts'] < sig_ts]
                new_watermark = prior[-1] if prior else watermark
            # Hard flatten (clock-driven; the bar loop's own triggers on
            # the 15:44 close already won precedence inside the loop).
            today = day(now)
            flat_ts = _hhmm_ts(today, FLATTEN_HHMM)
            if now >= flat_ts:
                for p in self._active(c):
                    if p.get('exit_trigger') or not p.get('underlying_entry'):
                        continue
                    entry_date = p['signal']['entry_date']
                    inst = _hhmm_ts(entry_date, FLATTEN_HHMM)
                    if now < inst:
                        continue
                    cands = [b for b in bars if b['ts'] + 60 <= inst]
                    bar = cands[-1] if cands else bars[0]
                    self._trigger_exit(c, p, bar, 'flatten', now, instant=inst)
            self._write_state(c, now, {
                'last_bar_ts': new_watermark,
                'last_session': bars[-1]['session'],
                'signals_taken': counters['signals_taken'],
                'flatten_skipped': counters['flatten_skipped']})

        for step in (self._price_entries, self._price_exits):
            step_alerts = []
            try:
                with self.db.tx() as c:
                    step(c, now, step_alerts)
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'sh_spy_options pricing step %s failed', step.__name__)
            else:
                # Only alerts whose episode transaction committed are
                # queued (queueing itself happens after, in queue_alert).
                alerts_out.extend(step_alerts)
        for payload in alerts_out:
            queue_alert(self.db, self.cfg, payload)
        with self.db.tx() as c:
            state = self._write_state(c, now)
        counts = state['counts']
        self.db.health('sh_spy_options', 'running',
                       'SH-SPY options forward premium scorer '
                       '(paper/measurement): '
                       f"created {counts['created']}, opened {counts['opened']}, "
                       f"closed {counts['closed']}, open {counts['open']}, "
                       f"gate_excluded {counts['gate_excluded']}, "
                       f"data_gap {counts['data_gap']}",
                       last_session=state.get('last_session'),
                       **counts)

    # -- premium pricing ---------------------------------------------------------

    def _price_entries(self, c, now, alerts_out):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)
            ).scalars().all()
        for p in rows:
            if p['premium_status'] != 'awaiting_entry' or p.get('entry'):
                continue
            entry_ts = p['signal']['entry_bar_ts']
            if now < entry_ts:
                continue
            deadline = entry_ts + QUOTE_WAIT_S
            chain = self.db.get(c, 'chain:' + SYMBOL) or {}
            chain_asof = number(chain.get('asof'))
            chosen = None
            chain_reason = 'chain_unavailable'
            if chain and chain_asof is not None \
                    and chain_asof >= entry_ts - CHAIN_STALE_S:
                spot = number(chain.get('selection_reference')) \
                    or p['signal']['ref']
                chosen = select_contract(chain.get('contracts'), p['dir'],
                                         entry_ts, spot)
                chain_reason = 'no_admissible_contract'
            if chosen is None:
                if now > deadline:
                    p['status'] = 'data_gap'
                    p['premium_status'] = 'data_gap'
                    p['stage'] = 'done' if p.get('underlying_exit') \
                        else 'underlying_only'
                    p['entry_data_gap'] = {'reason': chain_reason,
                                           'chain_asof': chain_asof}
                    self._event(c, p, 'entry_data_gap', now,
                                {'date': p['signal']['date'],
                                 'reason': chain_reason})
                    self._save(c, p, now, status='data_gap')
                continue
            q = first_quote_at_or_after(self.db, c, chain,
                                        chosen['symbol'], entry_ts)
            if q is None:
                if now > deadline:
                    p['status'] = 'data_gap'
                    p['premium_status'] = 'data_gap'
                    p['stage'] = 'done' if p.get('underlying_exit') \
                        else 'underlying_only'
                    p['entry_data_gap'] = {
                        'reason': 'entry_quote_missing',
                        'contract_symbol': chosen['symbol']}
                    self._event(c, p, 'entry_data_gap', now,
                                {'date': p['signal']['date'],
                                 'reason': 'entry_quote_missing',
                                 'contract': chosen['symbol']})
                    self._save(c, p, now, status='data_gap')
                continue
            qts, bid, ask, src = q
            contract = {'symbol': chosen['symbol'],
                        'expiry': chosen['expiry'],
                        'strike': number(chosen['strike']),
                        'type': str(chosen['type']).lower(),
                        'delta': number(chosen.get('delta'))}
            spread, gate, passes = spread_gate(bid, ask)
            if not passes:
                p['status'] = 'gate_excluded'
                p['premium_status'] = 'gate_excluded'
                p['stage'] = 'done' if p.get('underlying_exit') \
                    else 'underlying_only'
                p['gate_exclusion'] = {
                    'contract': contract, 'bid': bid, 'ask': ask,
                    'quote_ts': qts, 'quote_source': src,
                    'spread': spread, 'gate': gate}
                self._event(c, p, 'gate_excluded', now,
                            {'date': p['signal']['date'],
                             'contract': chosen['symbol'],
                             'spread': spread, 'gate': gate})
                self._save(c, p, now, status='gate_excluded')
                continue
            p['contract'] = contract
            p['entry'] = {'contract_symbol': chosen['symbol'],
                          'expiry': chosen['expiry'],
                          'strike': contract['strike'],
                          'delta': contract['delta'],
                          'bid': bid, 'ask': ask, 'quote_ts': qts,
                          'quote_source': src, 'qty': QTY,
                          'commission': FEE, 'priced_at': now}
            p['planned_loss'] = ask * MULT * QTY + 2 * FEE
            p['premium_status'] = 'open'
            p['stage'] = 'awaiting_exit' if p.get('exit_trigger') else 'open'
            self._event(c, p, 'entry_priced', now,
                        {'date': p['signal']['date'],
                         'contract': chosen['symbol'], 'ask': ask})
            self._save(c, p, now)
            alerts_out.append({
                'type': 'open', 'episode_id': p['id'],
                'pattern': p['signal']['pattern'], 'dir': p['dir'],
                'contract': contract, 'entry_ask': ask,
                'stop': p['signal']['stop'], 'target': p['signal']['target'],
                'at': now})

    def _price_exits(self, c, now, alerts_out):
        rows = c.execute(select(episodes.c.payload).where(
            episodes.c.status == 'open').order_by(episodes.c.created)
            ).scalars().all()
        for p in rows:
            if p['premium_status'] != 'open' or not p.get('exit_trigger') \
                    or p.get('exit'):
                continue
            trig = p['exit_trigger']
            instant = trig['instant']
            chain = self.db.get(c, 'chain:' + SYMBOL) or {}
            q = first_quote_at_or_after(self.db, c, chain,
                                        p['contract']['symbol'], instant)
            if q is None:
                if now > instant + QUOTE_WAIT_S:
                    p['status'] = 'data_gap'
                    p['premium_status'] = 'data_gap'
                    p['stage'] = 'done'
                    p['exit_data_gap'] = {
                        'reason': 'exit_quote_missing',
                        'trigger_reason': trig['reason'],
                        'instant': instant}
                    self._event(c, p, 'exit_data_gap', now,
                                {'date': p['signal']['date'],
                                 'reason': 'exit_quote_missing',
                                 'trigger_reason': trig['reason']})
                    self._save(c, p, now, status='data_gap')
                continue
            qts, bid, ask, src = q
            entry_ask = p['entry']['ask']
            pnl = (bid - entry_ask) * MULT * QTY - 2 * FEE
            p['exit'] = {'contract_symbol': p['contract']['symbol'],
                         'bid': bid, 'ask': ask, 'quote_ts': qts,
                         'quote_source': src, 'qty': QTY,
                         'commission': FEE, 'priced_at': now,
                         'reason': trig['reason']}
            p['pnl'] = pnl
            p['outcome'] = 'win' if pnl > 0 else 'loss' if pnl < 0 \
                else 'breakeven'
            p['premium_status'] = 'closed'
            p['stage'] = 'done'
            p['status'] = 'closed'
            self._event(c, p, 'exit_priced', now,
                        {'date': p['signal']['date'],
                         'reason': trig['reason'], 'pnl': pnl})
            self._save(c, p, now, status='closed')
            alerts_out.append({
                'type': 'close', 'episode_id': p['id'],
                'pattern': p['signal']['pattern'], 'dir': p['dir'],
                'contract': p['contract'], 'entry_ask': entry_ask,
                'exit_bid': bid, 'pnl': pnl, 'reason': trig['reason'],
                'underlying_pnl': (p.get('underlying_exit') or {})
                .get('net_dollars'),
                'at': now})

    # -- driver ---------------------------------------------------------------------

    def cycle(self):
        with self.db.tx() as c:
            if not self.db.lease(c, 'sh-spy-options', self.owner, 120):
                return
        self.process(self.now(time.time()))

    async def run(self):
        self.clock = time.time
        while True:
            try:
                await asyncio.to_thread(self.cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('uvicorn.error').exception(
                    'sh_spy_options worker failed; retrying')
            await asyncio.sleep(20)
