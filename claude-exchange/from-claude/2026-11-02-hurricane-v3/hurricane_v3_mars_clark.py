"""
hurricane_v3_mars_clark.py — Mars-richesse-Clark
=================================================
Richesse, unchanged, plus exactly three things:

  1. cbbo-1s feed  — the calendar clock beside Mars's transaction clock
  2. OI-VWMACD     — resting-book direction, the twin of PFMACD
  3. Band D        — $1.05-$3.00, a sandbox above richesse's $1.00 ceiling

Plus two corrections that are measurements, not opinions:
  - commission $1.55/fill ($3.10 round trip), from IBKR's own reports
  - band D per-head cap $300, since a $3.00 contract costs $300

Everything else is richesse. No entry window, no chaff filter, no
geiger changes, no per-band sizing table, no resting exit. Those were
plaster over structural issues and have been removed.

Strategy section below is byte-identical to hurricane_v3_mars_richesse.py.
"""
from __future__ import annotations
"""
hurricane_v3_mars.py — MARS BUILD
======================================
One MACD to rule them all: 6-24-15 everywhere.

EARTH thesis: only trade strikes where the market is already alive.
The 94% WR sub-10s cohort enters at median 1.2% participation into
394-contract flow. The dead strikes (86 pure flat exits) had 7 contracts
total across the entire fill window. The signal is volume — not price,
not Greeks, not theory. Real market volume behind the fill.

MARS — the god of war. 12 heads, 1 contract each, organized by premium band.

  Heads 1-4:   $0.15–$0.30   (the foot soldiers — cheap gamma, fast oscillation)
  Heads 5-8:   $0.35–$0.50   (the cavalry — mid gamma, reliable oscillation)
  Heads 9-12:  $0.55–$1.00   (the heavy infantry — high gamma, strong conviction)

  1 contract per head. $100 max per head. 12 concurrent max.
  $0.05 forbidden — except as re-entry after profitable $0.10 exit, post 15:00 ET.
  Venus velocity gate demoted to observation flag.
  Volume pulse gate remains the primary quality filter.

  Architecture: same engine, premium band assignment at entry time.
  Per-band limit of 4 concurrent positions. No architectural change to StrikeState.

MARS additions on top of EARTH:
─────────────────────────────────
1. VOLUME PULSE GATE
   Before any entry: does this strike have real flow right now?
   Count contracts traded in the last earth_vol_window_secs (30s).
   Require >= earth_min_vol_30s (15 contracts). Dead = skip.
   This eliminates the "we ARE the market" problem where we're
   placing 24 contracts into 7 available.

2. CONVICTION SIZING
   Not a flat max_contracts. Size proportional to signal quality.
   Three tiers:
     HIGH conviction  → 100% of capital limit per trade
     MED  conviction  → 60%
     LOW  conviction  → 30%
   Conviction = f(dual_agree, slope_confirms, true_fill_vol, swell)
   $1M bankroll, no artificial ceiling on contracts — only OI cap.

3. OI-AWARE POSITION CAP
   Never exceed earth_max_oi_pct (5%) of strike's available OI
   on any single entry. Real OI from oi_map.parquet.
   Dynamic OI: if strike has been active for N 5-min bars,
   assume OI growing at earth_oi_growth_rate (50 contracts/bar).

4. GREEKS RECORDING LAYER
   Every 10s per active position: snapshot delta, gamma, theta,
   vega, vanna, charm. Saved to hurricane_v3_greeks.parquet.
   Pure observation — zero influence on trading logic.
   The dataset that reveals what Greeks actually mean for THIS system.

5. BS THEORETICAL PRICE CHECK
   At entry: compute BS theoretical price for the strike.
   If market price < BS_lower (option underpriced vs theory) → flag.
   If market price > BS_upper (option overpriced vs theory) → flag.
   Recorded in trade log as bs_theoretical, bs_iv, bs_edge.
   Observation only. Trading logic unchanged.

Planet sequence:
  Moonraker  1031t  64.0% WR  $24,766  baseline
  Mercury     922t  68.5% WR  $22,683  premium tiers
  Venus       830t  72.0% WR  $21,375  SPX velocity gate
  Earth       ???t  ???% WR   $???     volume pulse + conviction sizing

$1M bankroll. No limit on contracts except OI cap.
"""
from dataclasses import dataclass, field
from typing import Optional
from collections import deque
import math
import pandas as pd
import numpy as np
from pathlib import Path
from scipy.stats import norm
from scipy.optimize import brentq


# ══════════════════════════════════════════════════════════════
# WILDERS EMA
# ══════════════════════════════════════════════════════════════
class WildersEMA:
    __slots__ = ('alpha','value')
    def __init__(self, period):
        self.alpha = 1.0/period; self.value = None
    def update(self, x):
        if self.value is None: self.value = x
        else: self.value += self.alpha*(x-self.value)
        return self.value


# ══════════════════════════════════════════════════════════════
# BLACK-SCHOLES ENGINE (observation layer — does not trade)
# ══════════════════════════════════════════════════════════════
RISK_FREE_RATE = 0.053

def _bs_greeks(S, K, T, r, sigma, opt_type='C'):
    """Full analytical Greek suite. Returns dict or None if degenerate."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(0.0, S-K) if opt_type=='C' else max(0.0, K-S)
        return dict(price=round(intrinsic,4), delta=1.0 if intrinsic>0 else 0.0,
                    gamma=0.0, theta_day=0.0, vega_1pct=0.0,
                    vanna=0.0, charm_day=0.0, iv=sigma)
    try:
        d1  = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
        d2  = d1 - sigma*np.sqrt(T)
        nd1 = norm.pdf(d1); Nd1 = norm.cdf(d1); Nd2 = norm.cdf(d2)
        if opt_type == 'C':
            price = S*Nd1 - K*np.exp(-r*T)*Nd2
            delta = Nd1
        else:
            price = K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)
            delta = Nd1 - 1.0
        gamma  = nd1 / (S * sigma * np.sqrt(T))
        term1  = -(S * nd1 * sigma) / (2 * np.sqrt(T))
        theta  = ((term1 - r*K*np.exp(-r*T)*Nd2) if opt_type=='C'
                  else (term1 + r*K*np.exp(-r*T)*norm.cdf(-d2))) / 365
        vega   = S * nd1 * np.sqrt(T) / 100
        vanna  = -nd1 * d2 / sigma
        charm  = (-nd1 * (2*r*T - d2*sigma*np.sqrt(T)) /
                  (2*T*sigma*np.sqrt(T))) / 365
        return dict(price=round(price,4), delta=round(delta,4),
                    gamma=round(gamma,6), theta_day=round(theta,4),
                    vega_1pct=round(vega,4), vanna=round(vanna,6),
                    charm_day=round(charm,6), iv=round(sigma,4))
    except Exception:
        return None

def _implied_vol(market_price, S, K, T, r, opt_type='C'):
    """Solve BS for sigma given market price. Returns None if unsolvable."""
    if T <= 0 or market_price <= 0: return None
    intrinsic = max(0.0, S-K) if opt_type=='C' else max(0.0, K-S)
    if market_price < intrinsic: return None
    try:
        def obj(s): return _bs_greeks(S,K,T,r,s,opt_type)['price'] - market_price
        if obj(0.01)*obj(5.0) > 0: return None
        return brentq(obj, 0.01, 5.0, xtol=1e-6)
    except Exception:
        return None

def _t_from_mins(mins, floor_mins=5.0):
    return max(mins, floor_mins) / (252 * 390)


# ══════════════════════════════════════════════════════════════
# PFMACD — Volume-Weighted Flow, 6-24-15 on 6s bars
# ══════════════════════════════════════════════════════════════
class PFMACDEngine:
    def __init__(self, bars1s, fast=6, slow=24, signal=15):
        self.fast=fast; self.slow=slow; self.sig=signal
        self.daily={}
        self._build(bars1s)

    def _build(self, bars1s):
        bars = bars1s.copy()
        bars['ts_et'] = bars['timestamp'].dt.tz_convert('America/New_York')
        bars['t_sec'] = ((bars['ts_et'].dt.hour-9)*3600 +
                         (bars['ts_et'].dt.minute-30)*60 +
                         bars['ts_et'].dt.second)
        bars = bars[bars['t_sec'] >= 0]
        bars['t_6s'] = bars['t_sec'] // 6
        if 'volume' not in bars.columns: bars['volume'] = 1
        for ds, day in bars.groupby('date_str'):
            agg = day.groupby(['t_6s','instrument_id']).agg(
                h=('high','max'), l=('low','min'),
                vol=('volume','sum'),
                cls=('instrument_class','first')
            ).reset_index()
            agg['mid'] = (agg['h'] + agg['l']) / 2.0
            agg['dollar_flow'] = agg['mid'] * agg['vol']
            pf = {}
            for t6, g in agg.groupby('t_6s'):
                call_flow = g[g['cls']=='C']['dollar_flow'].sum()
                put_flow  = g[g['cls']=='P']['dollar_flow'].sum()
                pf[int(t6)] = call_flow - put_flow
            if pf: self.daily[ds] = self._run_macd(pf)
        print(f"    PFMACD (VW): {len(self.daily)} days, "
              f"{sum(len(v) for v in self.daily.values())} 6s bars, "
              f"Wilders {self.fast}-{self.slow}-{self.sig}")

    def _run_macd(self, pf):
        af=1.0/self.fast; asl=1.0/self.slow; ag=1.0/self.sig
        st=sorted(pf.keys())
        ef=pf[st[0]]; es=pf[st[0]]; eg=0.0
        result={}; hbuf=deque(maxlen=self.slow); prev_h=0.0
        for t in st:
            v=pf[t]
            ef=v*af+ef*(1-af); es=v*asl+es*(1-asl)
            ml=ef-es; eg=ml*ag+eg*(1-ag)
            h=ml-eg; hbuf.append(h)
            slope=h-prev_h
            result[t] = (h, float(np.mean(hbuf)), slope)
            prev_h=h
        return result

    def get(self, ds, t_sec):
        if ds not in self.daily: return 0.0, 0.0, 0.0, 'NEUTRAL'
        t6=t_sec//6; day=self.daily[ds]
        keys=sorted(day.keys())
        k=next((k for k in reversed(keys) if k<=t6), None)
        if k is None: return 0.0, 0.0, 0.0, 'NEUTRAL'
        h,rm,slope=day[k]
        direction='BULL' if h>rm else ('BEAR' if h<rm else 'NEUTRAL')
        return h, rm, slope, direction


# ══════════════════════════════════════════════════════════════
# DELTAWAVE — 6-24-15 on 1s bars per strike
# ══════════════════════════════════════════════════════════════
class StrikeDeltaWave:
    __slots__ = ('h_fast','h_slow','l_fast','l_slow',
                 'sig_ema','wave','signal','histogram','n')
    def __init__(self, fast=6, slow=24, signal=15):
        self.h_fast=WildersEMA(fast); self.h_slow=WildersEMA(slow)
        self.l_fast=WildersEMA(fast); self.l_slow=WildersEMA(slow)
        self.sig_ema=WildersEMA(signal)
        self.wave=0.0; self.signal=0.0; self.histogram=0.0; self.n=0
    def update(self, close, high, low):
        prev_close=close
        if self.n>0:
            up=max(0.0, high-prev_close); dn=max(0.0, prev_close-low)
        else: up=0.0; dn=0.0
        hf=self.h_fast.update(up); hs=self.h_slow.update(up)
        lf=self.l_fast.update(dn); ls=self.l_slow.update(dn)
        self.wave=(hf-hs)-(lf-ls)
        self.signal=self.sig_ema.update(self.wave)
        self.histogram=self.wave-self.signal; self.n+=1
    def is_warmed(self): return self.n>=24


# ══════════════════════════════════════════════════════════════
# CONE — 6s bars, PFMACD+DW nudge
# ══════════════════════════════════════════════════════════════
class StrikeCone:
    def __init__(self, window=25, tide_k=0.10, wave_k=0.10):
        self.window=window; self.tide_k=tide_k; self.wave_k=wave_k
        self.up_reaches=deque(maxlen=window)
        self.dn_reaches=deque(maxlen=window)
        self.prev_6s_close=None
        self.bar_buf=[]; self.n_6s=0

    def tick_1s(self, close, high, low):
        self.bar_buf.append((close, high, low))
        if len(self.bar_buf) >= 6:
            bc=self.bar_buf[-1][0]
            bh=max(b[1] for b in self.bar_buf)
            bl=min(b[2] for b in self.bar_buf)
            self.bar_buf=[]
            self._update_6s(bc, bh, bl)

    def _update_6s(self, close, high, low):
        if self.prev_6s_close is not None and self.prev_6s_close>0:
            self.up_reaches.append(max(0.0, high-self.prev_6s_close))
            self.dn_reaches.append(max(0.0, self.prev_6s_close-low))
            self.n_6s+=1
        self.prev_6s_close=close

    def predict(self, curr, pf_hist, pf_rm, pf_slope, dw_wave, dw_hist, fwd_bars, tick=0.05):
        if not self.is_ready(): return None
        a_up=float(np.mean(self.up_reaches)) if self.up_reaches else 0.01
        a_dn=float(np.mean(self.dn_reaches)) if self.dn_reaches else 0.01
        tot=a_up+a_dn
        raw_frac=a_up/tot if tot>0.001 else 0.5
        tide=pf_hist-pf_rm
        blended=raw_frac+self.tide_k*np.tanh(tide)+self.wave_k*np.tanh(dw_wave)
        blended=float(np.clip(blended,0.05,0.95))
        pred_up=a_up*np.sqrt(fwd_bars)*(2*blended)
        pred_dn=a_dn*np.sqrt(fwd_bars)*(2*(1-blended))
        ceiling=curr+pred_up; floor_p=curr-pred_dn
        spread_ticks=pred_up/tick if tick>0 else 0
        floor_ticks=max(0,int((curr-floor_p)/tick)) if floor_p<curr else 0
        return dict(blended=round(blended,4),
                    pred_up=round(pred_up,4), pred_dn=round(pred_dn,4),
                    ceiling=round(ceiling,3), floor=round(floor_p,3),
                    spread_ticks=round(spread_ticks,2), floor_ticks=floor_ticks)

    def is_ready(self): return self.n_6s>=5


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
@dataclass
class V3Config:
    # Universe
    sz_dist_min:float=5.0
    sz_dist_max:float=125.0
    min_bid:float=0.05; max_bid:float=3.00  # CLARK: band D sandbox to $3.00
    osc_window:int=30
    max_entry_pct:float=0.50
    exit_pct:float=0.80
    min_range_pct:float=0.05
    zone2_extension:float=0.00; zone_neg2_cut:bool=False
    swell_enabled:bool=True; swell_lead_move_pct:float=0.15
    swell_window_secs:int=10; swell_otm_count:int=2

    # ── EARTH: sizing ─────────────────────────────────────────
    # $1M bankroll. No flat max_contracts. Size = conviction × OI cap.
    max_concurrent:int=12               # MARS: 12 heads total, 4 per band — we filter better
    # CLARK: measured from IBKR reports over three live sessions.
    # $1.00 order minimum + SPXW index surcharge = $1.55/fill.
    comm_per_contract:float=1.55
    entry_start_et_h:int=9; entry_start_et_m:int=30
    entry_stop_et_h:int=15; entry_stop_et_m:int=55
    starting_capital:float=1_000.0  # realistic starting capital
    grid_tick:float=0.05; gap_exit_secs:float=15.0

    # Conviction sizing — fraction of max capital per trade
    conv_high_frac:float=1.00   # 100% — all signals agree + real volume + SPX moving
    conv_med_frac:float=0.60    # 60%  — most signals agree
    conv_low_frac:float=0.30    # 30%  — minimal confirmation, volume present
    max_deploy_per_trade:float=50_000.0  # hard ceiling per single entry ($50k)
    # OI cap: never exceed this % of strike's available OI
    max_oi_pct:float=0.05       # 5% of OI max per entry
    oi_growth_rate:int=50       # contracts added per 5-min bar strike is "active"

    # Timing
    bail_secs:int=20; stop_window_secs:int=5; stop_fraction:float=0.333

    # PFMACD
    pfmacd_enabled:bool=True

    # Cone
    cone_enabled:bool=True
    cone_window:int=25; cone_fast_window:int=12
    cone_fwd_bars:int=12
    tide_k:float=0.10; wave_k:float=0.10
    min_spread_ticks:float=2.0
    max_fill_ratio:float=0.80
    cone_exit_min_gain:float=0.05
    cone_exit_min_ticks:float=4.0

    # Premium tiers (Mercury)
    tier_floor_price:float=0.05
    tier_floor_max_pct:float=0.65
    tier_high_price:float=0.20
    tier_high_max_pct:float=0.85
    tier_high_max_fill_ratio:float=0.90

    # Venus SPX velocity gate
    venus_dist_threshold:float=75.0
    venus_toward_vel_min:float=1.0
    venus_lookback_secs:int=300

    # ── EARTH: volume pulse gate ──────────────────────────────
    earth_vol_window_secs:int=30   # lookback window for volume check
    earth_min_vol_30s:int=15       # minimum contracts in last 30s to enter
    # Sub-10s elite mode: if volume is extreme AND sub-10s likely → max size
    earth_elite_vol_threshold:int=100   # >100 contracts in 30s = high-flow event

    # ── MARS: premium band system ─────────────────────────────
    # Band A: $0.15-$0.30  (heads 1-4)
    # Band B: $0.35-$0.50  (heads 5-8)
    # Band C: $0.55-$1.00  (heads 9-12)
    mars_band_a_lo:float=0.15; mars_band_a_hi:float=0.30
    mars_band_b_lo:float=0.35; mars_band_b_hi:float=0.50
    mars_band_c_lo:float=0.55; mars_band_c_hi:float=1.00
    # ── CLARK: band D, the sandbox above richesse's $1.00 ceiling ──
    mars_band_d_lo:float=1.05; mars_band_d_hi:float=3.00
    # ── CLARK: OI-VWMACD (resting book direction, from cbbo-1s) ──
    oivw_enabled:bool=True
    oivw_bucket_secs:int=6
    mars_max_per_band:int=4       # max 4 concurrent positions per band
    # $0.05 re-entry rule: only after profitable $0.10 exit, past 15:00 ET
    mars_floor_reentry_min_exit:float=0.10  # must have exited at >= this
    mars_floor_reentry_after_h:int=15       # 15:00 ET

    # ── EARTH: Greeks recording ───────────────────────────────
    earth_record_greeks:bool=True
    earth_greeks_interval_secs:int=10   # snapshot every 10s per active position
    earth_vix_default:float=18.0        # fallback if VIX not available


# ══════════════════════════════════════════════════════════════
# STRIKE STATE
# ══════════════════════════════════════════════════════════════
@dataclass
class StrikeStateV3:
    instrument_id:int; strike:float; direction:str; dist:float
    prices:list=field(default_factory=list)
    opens:list=field(default_factory=list)
    volumes:list=field(default_factory=list)         # EARTH: rolling 1s volumes
    vol_5m_bars_active:int=0                         # EARTH: how many 5m bars active
    position:Optional[dict]=None
    swell_until:Optional[pd.Timestamp]=None
    last_exit_ts:Optional[pd.Timestamp]=None
    cone:StrikeCone=field(default=None)
    cone_fast:StrikeCone=field(default=None)
    dw:StrikeDeltaWave=field(default=None)

    def __post_init__(self):
        if self.cone is None: self.cone = StrikeCone()
        if self.cone_fast is None: self.cone_fast = StrikeCone(window=12)
        if self.dw is None: self.dw = StrikeDeltaWave()
        self.last_exit_won = True
        self.last_exit_price = 0.0

    def update(self, price, bar_open=None, volume=0):
        self.prices.append(price)
        self.opens.append(bar_open if bar_open is not None else price)
        self.volumes.append(volume)
        if len(self.prices)>120:
            self.prices=self.prices[-120:]
            self.opens=self.opens[-120:]
            self.volumes=self.volumes[-120:]

    def update_layers(self, close, high, low, ts=None):
        self.dw.update(close, high, low)
        self.cone.tick_1s(close, high, low)
        self.cone_fast.tick_1s(close, high, low)

    def vol_last_n_secs(self, n):
        """True volume traded in last N seconds."""
        return sum(self.volumes[-n:]) if len(self.volumes)>=n else sum(self.volumes)

    def rolling_stats(self, window, grid_tick=0.05):
        if len(self.prices)<4: return None, None, None
        prior=self.prices[-(window+1):-1] if len(self.prices)>window else self.prices[:-1]
        if len(prior)<2: return None, None, None
        lo=min(prior); hi=max(prior)
        if hi<=lo: return None, None, None
        curr=self.prices[-1]
        pct=(curr-lo)/(hi-lo)
        return lo, hi, pct

    def geiger_zone(self, window, extension=0.0):
        lo,hi,pct=self.rolling_stats(window)
        if lo is None: return 0,lo,hi,pct
        lower2=lo+(hi-lo)*0.20*(1-extension)
        upper2=lo+(hi-lo)*0.80*(1+extension)
        if pct>=0.80+extension: return 2, lo, hi, pct
        elif pct<=0.20-extension: return -2, lo, hi, pct
        return 0, lo, hi, pct



# ══════════════════════════════════════════════════════════════
# EARTH STRATEGY ENGINE
# ══════════════════════════════════════════════════════════════
class HurricaneV3:

    def __init__(self, config, bars1s_df, bars10s_df, atm_map,
                 oi_map_df, catalog_dir=None, vix=None):
        self.cfg=config
        self.bars10s=bars10s_df
        self.atm_map=atm_map
        self.vix = vix if vix is not None else config.earth_vix_default

        print("  Indexing 1s bars...")
        tc='ts_event' if 'ts_event' in bars1s_df.columns else 'timestamp'
        self.bars1s=bars1s_df.rename(columns={tc:'timestamp'})
        self.bars1s['timestamp']=pd.to_datetime(self.bars1s['timestamp'],utc=True)
        self.bars1s['date_str']=self.bars1s['timestamp'].dt.date.astype(str)
        print(f"    {len(self.bars1s):,} bars, {self.bars1s['date_str'].nunique()} days")

        # OI map: instrument_id → open_interest
        self.oi_map={}
        if oi_map_df is not None and len(oi_map_df)>0:
            id_col = 'instrument_id' if 'instrument_id' in oi_map_df.columns else oi_map_df.columns[0]
            oi_col = next((c for c in ['open_interest','oi','OI'] if c in oi_map_df.columns), None)
            if oi_col:
                self.oi_map = dict(zip(oi_map_df[id_col], oi_map_df[oi_col]))

        if config.pfmacd_enabled:
            print("\n  Building PFMACD engine (6s bars, 6-24-15)...")
            self.pfmacd=PFMACDEngine(self.bars1s)
        else:
            self.pfmacd=None

        # SPX 5-min prices
        self.spx_prices={}
        spx_path=(Path(catalog_dir)/'spx_5m.parquet'
                  if catalog_dir else None)
        if spx_path and spx_path.exists():
            try:
                spx=pd.read_parquet(spx_path, columns=['timestamp','date_str','open'])
                spx['timestamp']=pd.to_datetime(spx['timestamp'],utc=True)
                spx_et=spx['timestamp'].dt.tz_convert('America/New_York')
                spx['t_sec']=((spx_et.dt.hour-9)*3600+
                              (spx_et.dt.minute-30)*60)
                spx=spx[spx['t_sec']>=0]
                self.spx_prices={(row['date_str'],int(row['t_sec'])):float(row['open'])
                                 for _,row in spx.iterrows()}
                print(f"  SPX 5-min bars: {len(self.spx_prices):,} bars loaded (open price)")
                if self.spx_prices:
                    sample = list(self.spx_prices.items())[:2]
                    print(f"  SPX sample keys: {sample}")
            except Exception as e:
                print(f"  SPX: error loading — {e}")
        else:
            print("  SPX: spx_5m.parquet not found — run 14_fetch_spx_5m.py first")

        self.greeks_log=[]   # observation layer — no trading influence
        self.stats={k:0 for k in [
            'checked','pf_blocked','spread_blocked','fill_blocked',
            'range_blocked','pct_blocked','dual_confirmed','slope_confirmed',
            'cone_lowered','cone_target_set','cone_exit','base_entries',
            'volume_blocked','earth_vol_blocked','earth_elite',
            'tier_floor','tier_mid','tier_high',
            'venus_blocked','venus_would_block',
            # Mars
            'mars_band_a','mars_band_b','mars_band_c','mars_band_d',
            'oivw_blocked','oivw_agreed',
            'mars_band_full','mars_floor_reentry','mars_floor_blocked',
            'conv_high','conv_med','conv_low',
            'spx_none',  # silent killer — no SPX price found
        ]}

    # ── SPX LOOKUP ────────────────────────────────────────────
    def spx(self, ds, t_sec):
        """Open of last completed 5-min bar. Returns None if unavailable."""
        if not self.spx_prices: return None
        bar_t=(t_sec//300)*300
        key=(ds, bar_t)
        if key in self.spx_prices: return self.spx_prices[key]
        for n in range(1,5):
            k=(ds, bar_t-n*300)
            if k in self.spx_prices: return self.spx_prices[k]
        return None

    # ── CONVICTION SCORING ────────────────────────────────────
    def _conviction(self, dual_agree, slope_confirms, swell,
                    vol_30s, pct, pf_agrees, toward_vel=None):
        """
        Score = count of green lights.
        HIGH = 4+ signals aligned
        MED  = 2-3 signals aligned
        LOW  = 1 signal (volume present, everything else borderline)

        Volume must always be present (earth_min_vol_30s) — this is
        the foundation. Without it we don't enter at all.
        """
        score = 0
        if dual_agree:       score += 2   # both cones agree = strong
        if slope_confirms:   score += 1   # PFMACD momentum
        if swell:            score += 1   # swell = setup building
        if pf_agrees:        score += 1   # dollar flow agrees
        if pct < 0.35:       score += 1   # deep in range, maximum room
        if vol_30s >= self.cfg.earth_elite_vol_threshold:
            score += 2                    # elite flow event
        elif vol_30s >= self.cfg.earth_min_vol_30s * 3:
            score += 1                    # strong flow
        if toward_vel is not None and toward_vel > 2.0:
            score += 1                    # SPX committed your way

        if score >= 6:   return 'HIGH'
        elif score >= 3: return 'MED'
        else:            return 'LOW'

    # ── OI-AWARE SIZING ───────────────────────────────────────
    def _size_mars(self, limit_px):
        """Mars sizing: 1 contract per head. Always. No exceptions.
        At $0.15: $15 risk. At $1.00: $100 risk. All invisible to the book.
        """
        if limit_px <= 0: return 0
        cap = 300 if limit_px > self.cfg.mars_band_c_hi else 100
        if limit_px * 100 > cap: return 0  # $100/head, $300 in band D
        return 1

    # ── BS OBSERVATION ────────────────────────────────────────
    def _bs_snapshot(self, market_price, S, strike, direction, T_mins):
        """
        Observation-only. Compute theoretical price and IV.
        Returns dict of BS metrics to record. Does NOT affect trading.
        """
        if S is None or S <= 0: return {}
        T = _t_from_mins(T_mins)
        # Infer sigma from VIX as baseline; then solve for actual IV
        sigma_baseline = self.vix / 100.0
        iv = _implied_vol(market_price, S, strike, T, RISK_FREE_RATE, direction)
        # Use IV if solvable, else VIX baseline
        sigma = iv if iv else sigma_baseline
        g = _bs_greeks(S, strike, T, RISK_FREE_RATE, sigma, direction)
        if g is None: return {}
        edge = market_price - g['price']  # positive = market overprices vs BS
        return {
            'bs_theoretical': g['price'],
            'bs_iv':          round(sigma, 4) if sigma else None,
            'bs_edge':        round(edge, 4),
            'bs_delta':       g['delta'],
            'bs_gamma':       g['gamma'],
            'bs_theta_day':   g['theta_day'],
            'bs_vega_1pct':   g['vega_1pct'],
            'bs_vanna':       g['vanna'],
            'bs_charm_day':   g['charm_day'],
        }

    # ── GREEKS RECORDING ─────────────────────────────────────
    def _record_greeks(self, st, ts, ds, t_sec, spx_now, T_mins):
        """Record Greek snapshot for active positions. Pure observation."""
        if not self.cfg.earth_record_greeks: return
        pos = st.position
        if pos is None: return
        curr = st.prices[-1] if st.prices else pos['entry_price']
        if spx_now is None or spx_now <= 0: return
        T = _t_from_mins(T_mins)
        sigma_baseline = self.vix / 100.0
        iv = _implied_vol(curr, spx_now, st.strike, T,
                         RISK_FREE_RATE, st.direction)
        sigma = iv if iv else sigma_baseline
        g = _bs_greeks(spx_now, st.strike, T, RISK_FREE_RATE,
                       sigma, st.direction)
        if g is None: return
        self.greeks_log.append({
            'timestamp':  ts,
            'date':       ds,
            'strike':     st.strike,
            'direction':  st.direction,
            'in_position':True,
            'hold_secs':  (ts-pos['entry_time']).total_seconds(),
            'market_price': curr,
            'spx':        round(spx_now, 2),
            'real_dist':  round(abs(st.strike-spx_now), 1),
            'T_mins':     round(T_mins, 1),
            'iv':         round(sigma, 4),
            'delta':      g['delta'],
            'gamma':      g['gamma'],
            'theta_day':  g['theta_day'],
            'vega_1pct':  g['vega_1pct'],
            'vanna':      g['vanna'],
            'charm_day':  g['charm_day'],
            'entry_price':pos['entry_price'],
            'unrealized_pnl': round((curr-pos['entry_price'])*pos['qty']*100, 2),
        })

    # ── ENTRY LOGIC ───────────────────────────────────────────
    def _entry(self, st, bar, ts, ds, t_sec, pf_dir, pf_hist, pf_rm,
               pf_slope, available_capital, T_mins):
        """
        EARTH entry: volume pulse gate + conviction sizing.
        All Venus logic preserved. Volume added as hard prerequisite.
        BS snapshot recorded at entry for observation.
        """
        self.stats['checked'] += 1
        curr = bar['close']
        lo, hi, pct = st.rolling_stats(self.cfg.osc_window, self.cfg.grid_tick)
        if lo is None: return None
        if (hi-lo)/(lo+1e-7) < self.cfg.min_range_pct:
            self.stats['range_blocked'] += 1; return None
        swell = st.swell_until is not None and ts <= st.swell_until

        # ── EARTH GATE 1: VOLUME PULSE ────────────────────────
        # The 94% WR cohort: median 1.2% participation into 394-contract flow.
        # The dead trades: 7 contracts total across the entire fill window.
        # If this strike is dead, skip it. Market silence is not opportunity.
        vol_30s = st.vol_last_n_secs(self.cfg.earth_vol_window_secs)
        if vol_30s < self.cfg.earth_min_vol_30s:
            self.stats['earth_vol_blocked'] += 1
            return None
        is_elite_flow = vol_30s >= self.cfg.earth_elite_vol_threshold
        if is_elite_flow: self.stats['earth_elite'] += 1

        # ── MARS: Premium band assignment ─────────────────────
        if self.cfg.mars_band_a_lo <= curr <= self.cfg.mars_band_a_hi:
            premium_band = 'A'; self.stats['mars_band_a'] += 1
        elif self.cfg.mars_band_b_lo <= curr <= self.cfg.mars_band_b_hi:
            premium_band = 'B'; self.stats['mars_band_b'] += 1
        elif self.cfg.mars_band_c_lo <= curr <= self.cfg.mars_band_c_hi:
            premium_band = 'C'; self.stats['mars_band_c'] += 1
        elif curr <= 0.05:
            # $0.05 FLOOR — only as re-entry after $0.10 win, post 15:00 ET
            ts_et = ts.tz_convert('America/New_York')
            is_afternoon = ts_et.hour >= self.cfg.mars_floor_reentry_after_h
            had_good_exit = (st.last_exit_won and
                            st.last_exit_price >= self.cfg.mars_floor_reentry_min_exit and
                            st.last_exit_ts is not None and
                            (ts - st.last_exit_ts).total_seconds() < 60)
            if is_afternoon and had_good_exit:
                premium_band = 'FLOOR'; self.stats['mars_floor_reentry'] += 1
            else:
                self.stats['mars_floor_blocked'] += 1
                return None
        elif self.cfg.mars_band_d_lo <= curr <= self.cfg.mars_band_d_hi:
            premium_band = 'D'; self.stats['mars_band_d'] += 1
        elif curr > self.cfg.mars_band_d_hi:
            return None  # above $3.00 — tick becomes $0.10, out of scope
        else:
            # Gap between bands ($0.06-$0.14, $0.31-$0.34, $0.51-$0.54)
            # Assign to nearest band
            if curr < self.cfg.mars_band_a_lo:
                self.stats['mars_floor_blocked'] += 1; return None
            elif curr < self.cfg.mars_band_b_lo:
                premium_band = 'A'; self.stats['mars_band_a'] += 1  # $0.31-$0.34 → A
            elif curr < self.cfg.mars_band_c_lo:
                premium_band = 'B'; self.stats['mars_band_b'] += 1  # $0.51-$0.54 → B
            else:
                premium_band = 'C'; self.stats['mars_band_c'] += 1

        # Check per-band concurrent limit
        band_count = sum(1 for sv in self._current_strikes.values()
                        if sv.position is not None and
                        sv.position.get('premium_band') == premium_band)
        if band_count >= self.cfg.mars_max_per_band:
            self.stats['mars_band_full'] += 1
            return None

        # Mercury tier compat (for cone pct gates downstream)
        if curr <= 0.10: premium_tier = 'FLOOR'
        elif curr >= 0.20: premium_tier = 'HIGH'
        else: premium_tier = 'MID'
        max_fill_ratio = self.cfg.max_fill_ratio

        # ── VENUS: SPX velocity gate (floor + far OTM only) ───
        spx_now = self.spx(ds, t_sec)
        if spx_now is None:
            self.stats['spx_none'] += 1
            if self.stats['spx_none'] == 1:
                # First miss — print diagnostic
                bar_t = (t_sec // 300) * 300
                sample_keys = list(self.spx_prices.keys())[:3]
                print(f"  [SPX DEBUG] First miss: ds='{ds}' t_sec={t_sec} bar_t={bar_t}")
                print(f"  [SPX DEBUG] Sample keys in spx_prices: {sample_keys}")
            return None
        real_dist = abs(st.strike - spx_now)
        if real_dist < self.cfg.sz_dist_min or real_dist > self.cfg.sz_dist_max:
            self.stats['range_blocked'] += 1; return None

        # ── MARS: Venus velocity demoted to observation flag ──
        toward_vel = None; venus_would_block = False
        if real_dist > self.cfg.venus_dist_threshold:
            spx_past = self.spx(ds, t_sec - self.cfg.venus_lookback_secs)
            if spx_past is not None:
                spx_vel = spx_now - spx_past
                toward_vel = spx_vel if st.direction=='C' else -spx_vel
                if toward_vel <= self.cfg.venus_toward_vel_min:
                    venus_would_block = True
                    self.stats['venus_would_block'] += 1
                    # NOT returning None — observation only

        # ── PFMACD ────────────────────────────────────────────
        pf_agrees = not ((st.direction=='C' and pf_dir=='BEAR') or
                         (st.direction=='P' and pf_dir=='BULL'))

        # ── CLARK: does the resting book favour this side too? ──
        # PFMACD says money moved that way (executions).
        # OI-VWMACD says depth is stacked that way (quotes).
        oivw = getattr(self, 'oivw', None)
        if self.cfg.oivw_enabled and oivw is not None and oivw.is_ready():
            if not oivw.agrees(st.direction):
                self.stats['oivw_blocked'] += 1; return None
            self.stats['oivw_agreed'] += 1
        if not pf_agrees: self.stats['pf_blocked'] += 1
        slope_confirms = ((st.direction=='C' and pf_slope>0) or
                          (st.direction=='P' and pf_slope<0))

        # ── DUAL CONE ─────────────────────────────────────────
        cone_blended=0.5; cone_ticks=0; spread_ticks=0; fill_ratio=0.0
        exit_target=lo+(hi-lo)*self.cfg.exit_pct
        dual_agree=False; fast_blended=0.5
        dw_wave=st.dw.wave if st.dw.is_warmed() else 0.0

        if self.cfg.cone_enabled and st.cone.is_ready():
            pred_slow=st.cone.predict(curr, pf_hist, pf_rm, pf_slope,
                                      dw_wave, st.dw.histogram,
                                      self.cfg.cone_fwd_bars, self.cfg.grid_tick)
            pred_fast=None
            if st.cone_fast.is_ready():
                pred_fast=st.cone_fast.predict(curr, pf_hist, pf_rm, pf_slope,
                                               dw_wave, st.dw.histogram,
                                               self.cfg.cone_fwd_bars, self.cfg.grid_tick)
            if pred_slow:
                cone_blended=pred_slow['blended']
                spread_ticks=pred_slow['spread_ticks']
                if pred_fast:
                    fast_blended=pred_fast['blended']
                    if st.direction=='C': dual_agree=cone_blended>0.50 and fast_blended>0.50
                    else: dual_agree=cone_blended<0.50 and fast_blended<0.50

                # Tier-aware pct ceiling (Mercury)
                if premium_tier=='FLOOR':
                    if dual_agree and slope_confirms: max_pct=0.80 if not swell else 0.85
                    elif dual_agree or slope_confirms: max_pct=0.70 if not swell else 0.75
                    else: max_pct=self.cfg.tier_floor_max_pct
                elif premium_tier=='HIGH':
                    if dual_agree and slope_confirms: max_pct=0.95 if not swell else 0.98
                    elif dual_agree or slope_confirms: max_pct=self.cfg.tier_high_max_pct
                    else: max_pct=0.80 if not swell else 0.85
                else:
                    if dual_agree and slope_confirms: max_pct=0.90 if not swell else 0.95
                    elif dual_agree or slope_confirms: max_pct=0.80 if not swell else 0.85
                    else: max_pct=0.70 if not swell else 0.75

                if pct>max_pct:
                    self.stats['pct_blocked']+=1; return None
                if dual_agree: self.stats['dual_confirmed']+=1
                if slope_confirms: self.stats['slope_confirmed']+=1
                if spread_ticks<self.cfg.min_spread_ticks:
                    self.stats['spread_blocked']+=1; return None
                rolling_mid=(lo+hi)/2
                cone_range=pred_slow['pred_up']+pred_slow['pred_dn']
                if cone_range>0:
                    fill_ratio=abs(curr-rolling_mid)/cone_range
                if fill_ratio>max_fill_ratio:
                    self.stats['fill_blocked']+=1; return None
                if pred_slow['floor_ticks']>0: cone_ticks=pred_slow['floor_ticks']
                slow_ceil=pred_slow['ceiling']
                fast_ceil=pred_fast['ceiling'] if pred_fast else slow_ceil
                best_ceil=min(slow_ceil,fast_ceil)
                if best_ceil>exit_target:
                    exit_target=best_ceil; self.stats['cone_target_set']+=1
        else:
            if premium_tier=='FLOOR': max_pct=self.cfg.tier_floor_max_pct if not swell else 0.75
            elif premium_tier=='HIGH': max_pct=self.cfg.tier_high_max_pct if not swell else 0.90
            else: max_pct=0.70 if not swell else 0.80
            if pct>max_pct: self.stats['pct_blocked']+=1; return None
            if pct>0.50: return None

        # ── EARTH: CONVICTION SCORING ─────────────────────────
        conviction = self._conviction(
            dual_agree, slope_confirms, swell, vol_30s, pct,
            pf_agrees, toward_vel)

        # ── EARTH: OI-AWARE SIZING ────────────────────────────
        tick=self.cfg.grid_tick
        bar_open=bar.get('open',curr)
        rup=math.ceil(bar_open/tick)*tick
        on_grid=abs(bar_open-round(bar_open/tick)*tick)<0.001
        limit_px=rup if on_grid else rup-tick
        if cone_ticks>0: limit_px-=cone_ticks*tick; self.stats['cone_lowered']+=1
        limit_px=max(limit_px, self.cfg.min_bid)

        qty=self._size_mars(limit_px)
        if qty<=0: return None

        # Track conviction stats
        self.stats[f'conv_{conviction.lower()}']+=1
        self.stats['base_entries']+=1

        # ── BS SNAPSHOT (observation only) ────────────────────
        bs_data = self._bs_snapshot(limit_px, spx_now, st.strike,
                                    st.direction, T_mins)

        return {
            'lim':limit_px,'qty':qty,'ts':ts,'bw':0,
            'exit_target':exit_target,'swell':swell,
            'real_dist':round(real_dist,1),
            'entry_spx':round(spx_now,2),
            'pf_dir':pf_dir,
            'cone_blended':round(cone_blended,3),
            'cone_ticks':cone_ticks,
            'spread_ticks':round(spread_ticks,1),
            'fill_ratio':round(fill_ratio,3),
            'dual_agree':dual_agree,
            'slope_confirms':slope_confirms,
            'fast_blended':round(fast_blended,3),
            'vol_30s':vol_30s,
            'conviction':conviction,
            'is_elite_flow':is_elite_flow,
            'premium_band':premium_band,
            'venus_would_block':venus_would_block,
            **bs_data,
        }

    # ── EXIT MONITOR ──────────────────────────────────────────
    def _monitor(self, st, curr, high, ts, ts_et, ds, trades,
                 pf_hist, pf_rm, pf_slope, t_sec, T_mins):
        pos=st.position; ep=pos['entry_price']; qty=pos['qty']
        gain=(curr-ep)/(ep+1e-7); secs=(ts-pos['entry_time']).total_seconds()
        pos['price_history'].append(curr)
        if len(pos['price_history'])>self.cfg.stop_window_secs+5:
            pos['price_history']=pos['price_history'][-(self.cfg.stop_window_secs+5):]
        reason=None; exit_px=curr
        zone,_,_,_=st.geiger_zone(self.cfg.osc_window, self.cfg.zone2_extension)
        pos['peak_price']=max(pos.get('peak_price',ep),curr,high)
        peak=pos['peak_price']; peak_gain=peak-ep
        tick=self.cfg.grid_tick
        is_profitable=curr>ep

        # Greek snapshot (every earth_greeks_interval_secs)
        if self.cfg.earth_record_greeks:
            secs_int=int(secs)
            if secs_int>0 and secs_int%self.cfg.earth_greeks_interval_secs==0:
                spx_now=self.spx(ds,t_sec)
                self._record_greeks(st,ts,ds,t_sec,spx_now,T_mins)

        if secs<3: return

        # TRAIL from peak
        if peak_gain>=tick and is_profitable:
            current_gain=curr-ep
            if current_gain<peak_gain*0.50:
                exit_px=curr; reason='TRAIL_EXIT'

        # ZONE2 checkpoint
        if reason is None and zone==2:
            dw_wave=st.dw.wave if st.dw.is_warmed() else 0.0
            cone_says_hold=False
            if self.cfg.cone_enabled and st.cone.is_ready():
                pred=st.cone.predict(curr,pf_hist,pf_rm,pf_slope,
                                     dw_wave,st.dw.histogram,
                                     self.cfg.cone_fwd_bars,tick)
                if pred and pred['spread_ticks']>self.cfg.cone_exit_min_ticks:
                    cone_says_hold=True
            if cone_says_hold and pf_slope>0 and is_profitable: pass
            else: exit_px=curr; reason='ZONE2_EXIT'

        elif reason is None and (curr>=pos['exit_target'] or high>=pos['exit_target']):
            exit_px=pos['exit_target']; reason='OSC_PROFIT'
        elif reason is None and zone==-2 and self.cfg.zone_neg2_cut:
            exit_px=curr; reason='ZONE2_CUT'
        elif reason is None and gain>=1.00:
            if len(pos['price_history'])>=self.cfg.stop_window_secs:
                ref=pos['price_history'][-self.cfg.stop_window_secs]
                if ref>0 and curr<ref*(1-self.cfg.stop_fraction):
                    reason='GUST_TRAIL'; exit_px=curr
        elif reason is None and len(pos['price_history'])>=self.cfg.stop_window_secs:
            ref=pos['price_history'][-self.cfg.stop_window_secs]
            drop=ref-curr
            st_t=0.10 if ep<=0.50 else (0.15 if ep<=0.80 else 0.20)
            if drop>=st_t: reason='STOP'
        elif reason is None and secs>=self.cfg.bail_secs:
            bail=False
            rec=(pos['price_history'][-self.cfg.bail_secs:]
                 if len(pos['price_history'])>=self.cfg.bail_secs
                 else pos['price_history'])
            if all(abs(p-ep)<=tick for p in rec): bail=True
            lt=pos.get('last_bar_ts')
            if lt and (ts-lt).total_seconds()>=self.cfg.gap_exit_secs: bail=True
            if bail: reason='TIME_BAIL'; exit_px=curr

        # CONE_EXIT (100% WR in all builds)
        if reason is None and self.cfg.cone_enabled and st.cone.is_ready():
            if gain>=self.cfg.cone_exit_min_gain:
                dw_wave=st.dw.wave if st.dw.is_warmed() else 0.0
                pred=st.cone.predict(curr,pf_hist,pf_rm,pf_slope,
                                     dw_wave,st.dw.histogram,
                                     self.cfg.cone_fwd_bars,tick)
                if pred and pred['spread_ticks']<=self.cfg.cone_exit_min_ticks:
                    reason='CONE_EXIT'; exit_px=curr
                    self.stats['cone_exit']+=1

        # FLOW_EXIT: money leaving your side of the chain
        if reason is None and secs>=10 and is_profitable:
            flow_against=False
            if pos['direction']=='C' and pf_hist<pf_rm and pf_slope<0:
                flow_against=True
            elif pos['direction']=='P' and pf_hist>pf_rm and pf_slope>0:
                flow_against=True
            if flow_against:
                exit_px=curr; reason='FLOW_EXIT'

        # FORCE_EXIT at 150s
        if not reason and secs>=150:
            ph=pos['price_history']
            if len(ph)>=3 and ph[-1]<=ph[-2]<=ph[-3]:
                exit_px=(curr//tick)*tick
            else:
                exit_px=math.ceil(curr/tick)*tick
            exit_px=max(exit_px,tick); reason='FORCE_EXIT'

        if reason:
            exit_px=math.ceil(exit_px/tick)*tick
            comm=qty*self.cfg.comm_per_contract*2
            pnl=(exit_px-ep)*qty*100-comm
            x_pf='N/A'; x_bl=0.5; x_pu=0.0; x_dw=0.0
            if self.pfmacd:
                x_pf='BULL' if pf_hist>pf_rm else ('BEAR' if pf_hist<pf_rm else 'NEUTRAL')
            if st.dw.is_warmed(): x_dw=round(st.dw.wave,5)
            if self.cfg.cone_enabled and st.cone.is_ready():
                dw_w=st.dw.wave if st.dw.is_warmed() else 0.0
                ep_pred=st.cone.predict(curr,pf_hist,pf_rm,pf_slope,
                                        dw_w,st.dw.histogram,
                                        self.cfg.cone_fwd_bars,tick)
                if ep_pred:
                    x_bl=round(ep_pred['blended'],3)
                    x_pu=round(ep_pred['pred_up'],4)
            x_spx=self.spx(ds,t_sec) or 0.0
            trades.append(self._trade(st,pos,ds,ts,exit_px,pnl,reason,secs,
                                      x_pf,x_bl,x_pu,x_dw,x_spx,T_mins))
            st.last_exit_ts=ts
            st.last_exit_won = pnl > 0
            st.last_exit_price = exit_px
            st.position=None

    # ── TRADE RECORD ──────────────────────────────────────────
    def _trade(self, st, pos, ds, ts, xp, pnl, reason, secs,
               x_pf='N/A', x_bl=0.5, x_pu=0.0, x_dw=0.0,
               x_spx=0.0, T_mins=0.0):
        ep=pos['entry_price']
        entry_spx=pos.get('entry_spx',0.0)
        exit_spx=x_spx
        entry_dist=round(abs(st.strike-entry_spx),1) if entry_spx else pos.get('real_dist',0)
        exit_dist=round(abs(st.strike-exit_spx),1) if exit_spx else 0.0
        spx_travel=round(exit_spx-entry_spx,2) if (entry_spx and exit_spx) else 0.0
        toward=round(spx_travel if st.direction=='C' else -spx_travel,2)
        return {
            'date':ds,'strike':st.strike,'direction':st.direction,
            'entry_time':pos['entry_time'],'exit_time':ts,
            'hold_secs':secs,'entry_price':ep,'exit_price':xp,
            'gain_pct':(xp-ep)/(ep+1e-7)*100,'qty':pos['qty'],'pnl_usd':pnl,
            'exit_reason':reason,'swell_entry':pos.get('swell_entry',False),
            'entry_spx':entry_spx,'entry_dist':entry_dist,
            'exit_spx':exit_spx,'exit_dist':exit_dist,
            'spx_travel':spx_travel,'toward_strike':toward,
            'peak_price':pos.get('peak_price',ep),
            'entry_volume':pos.get('entry_volume',0),
            'vol_30s_at_entry':pos.get('vol_30s',0),
            'fill_delay':pos.get('fill_delay_secs',0),
            'pf_dir':pos.get('pf_dir','N/A'),
            'cone_blended':pos.get('cone_blended',0.5),
            'cone_ticks':pos.get('cone_ticks',0),
            'spread_ticks':pos.get('spread_ticks',0),
            'fill_ratio':pos.get('fill_ratio',0.0),
            'dual_agree':pos.get('dual_agree',False),
            'slope_confirms':pos.get('slope_confirms',False),
            'fast_blended':pos.get('fast_blended',0.5),
            'conviction':pos.get('conviction','LOW'),
            'is_elite_flow':pos.get('is_elite_flow',False),
            # BS observation
            'bs_theoretical':pos.get('bs_theoretical',None),
            'bs_iv':pos.get('bs_iv',None),
            'bs_edge':pos.get('bs_edge',None),
            'bs_delta':pos.get('bs_delta',None),
            'bs_gamma':pos.get('bs_gamma',None),
            # Exit state
            'exit_pf_dir':x_pf,'exit_cone_blended':x_bl,
            'exit_pred_up':x_pu,'exit_dw_wave':x_dw,
            'T_mins_at_exit':round(T_mins,1),
        }

    # ── BUILD STRIKES ─────────────────────────────────────────
    def _build_strikes(self, ds, spx_open):
        day=self.bars1s[self.bars1s['date_str']==ds]
        if len(day)==0 or 'strike_price' not in day.columns: return {}
        seen={}
        for iid, grp in day.groupby('instrument_id'):
            iid=int(iid); row=grp.iloc[0]
            strike=float(row.get('strike_price',0))
            cls=str(row.get('instrument_class',''))
            if not strike or not cls: continue
            prices=grp['close'].values; prices=prices[prices>0]
            if len(prices)==0 or np.median(prices)>self.cfg.max_bid*3: continue
            seen[iid]=StrikeStateV3(
                instrument_id=iid, strike=strike,
                direction=cls.strip().upper()[0] if cls else 'C',
                dist=0.0)
        return seen

    # ── SWELL DETECTION ───────────────────────────────────────
    def _swell(self, st, curr, ts, strikes, spx_open):
        if not self.cfg.swell_enabled: return
        move_thresh=curr*self.cfg.swell_lead_move_pct
        otm=[s for iid,s in strikes.items()
             if s.dist>st.dist and s.direction==st.direction]
        otm.sort(key=lambda x:x.dist)
        activated=0
        for s in otm[:self.cfg.swell_otm_count]:
            if len(s.prices)>=self.cfg.swell_window_secs:
                window=s.prices[-self.cfg.swell_window_secs:]
                if max(window)-min(window)>=move_thresh: activated+=1
        if activated>=self.cfg.swell_otm_count:
            st.swell_until=ts+pd.Timedelta(seconds=self.cfg.swell_window_secs)

    # ── MAIN BACKTEST LOOP ────────────────────────────────────
    def run_backtest(self):
        trades=[]
        bars10=self.bars10s.sort_values('timestamp')
        bars10['date_str']=bars10['timestamp'].dt.date.astype(str)
        bankroll=self.cfg.starting_capital

        for ds, _ in bars10.groupby('date_str'):
            if ds not in self.atm_map: continue
            spx_open=float(self.atm_map[ds])
            strikes=self._build_strikes(ds, spx_open)
            if not strikes: continue
            self._current_strikes = strikes  # MARS: for band counting in _entry

            day1s=(self.bars1s[self.bars1s['date_str']==ds]
                   .sort_values('timestamp'))
            if len(day1s)==0: continue

            # Market expiry = 4:15 PM ET
            day_date=pd.Timestamp(ds).date()
            expiry_et=pd.Timestamp(f'{ds} 16:15:00').tz_localize('America/New_York')
            concurrent=0
            five_min_bar_tracker={}  # strike → last 5m bar seen

            for ts, sec_bars in day1s.groupby('timestamp'):
                ts_et=ts.tz_convert('America/New_York')
                t_sec=int(((ts_et.hour-9)*3600+(ts_et.minute-30)*60+ts_et.second))
                if t_sec<0: continue

                # Time to expiry in minutes
                T_mins=max((expiry_et-ts_et).total_seconds()/60.0, 5.0)

                in_entry_window=(
                    (ts_et.hour>self.cfg.entry_start_et_h or
                     (ts_et.hour==self.cfg.entry_start_et_h and
                      ts_et.minute>=self.cfg.entry_start_et_m)) and
                    (ts_et.hour<self.cfg.entry_stop_et_h or
                     (ts_et.hour==self.cfg.entry_stop_et_h and
                      ts_et.minute<self.cfg.entry_stop_et_m)))

                pf_hist,pf_rm,pf_slope,pf_dir=self.pfmacd.get(ds,t_sec) if self.pfmacd else (0.0,0.0,0.0,'NEUTRAL')

                # Track concurrent positions
                concurrent=sum(1 for s in strikes.values() if s.position is not None)

                for iid, row in sec_bars.groupby('instrument_id').first().iterrows():
                    st=strikes.get(int(iid))
                    if st is None: continue
                    close=float(row.get('close',0))
                    if close<=0: continue
                    high=float(row.get('high',close))
                    low=float(row.get('low',close))
                    volume=int(row.get('volume',0))
                    open_=float(row.get('open',close))

                    st.update(close, open_, volume)
                    st.update_layers(close, high, low)

                    # Track 5-min bar activity for dynamic OI
                    cur_5m=t_sec//300
                    if iid not in five_min_bar_tracker:
                        five_min_bar_tracker[iid]=cur_5m
                    elif cur_5m!=five_min_bar_tracker[iid]:
                        five_min_bar_tracker[iid]=cur_5m
                        if st.position is not None or st.vol_last_n_secs(60)>5:
                            st.vol_5m_bars_active+=1

                    # SWELL check
                    if self.cfg.swell_enabled and len(st.prices)>10:
                        self._swell(st, close, ts, strikes, spx_open)

                    # EXIT check (always, even outside entry window)
                    if st.position is not None:
                        self._monitor(st, close, high, ts, ts_et, ds, trades,
                                     pf_hist, pf_rm, pf_slope, t_sec, T_mins)
                        if st.position is None:
                            concurrent=max(0,concurrent-1)
                            bankroll+=trades[-1]['pnl_usd'] if trades else 0

                    # ENTRY check
                    elif (in_entry_window and
                          concurrent<self.cfg.max_concurrent and
                          close>=self.cfg.min_bid and
                          close<=self.cfg.max_bid):
                        bar_dict={'close':close,'open':open_,'high':high,'low':low}
                        po=self._entry(st, bar_dict, ts, ds, t_sec,
                                      pf_dir, pf_hist, pf_rm, pf_slope,
                                      bankroll, T_mins)
                        if po is not None:
                            fill_ts=ts+pd.Timedelta(seconds=max(1,
                                    min(int(po['lim']/self.cfg.grid_tick),300)))
                            st.position={
                                'entry_price':po['lim'],
                                'qty':po['qty'],
                                'entry_time':ts,
                                'fill_delay_secs':1,
                                'price_history':[close],
                                'last_bar_ts':ts,
                                'direction':st.direction,
                                'peak_price':po['lim'],
                                'swell_entry':po['swell'],
                                'entry_volume':volume,
                                'exit_target':po['exit_target'],
                                **{k:po[k] for k in [
                                    'real_dist','entry_spx','pf_dir',
                                    'cone_blended','cone_ticks','spread_ticks',
                                    'fill_ratio','dual_agree','slope_confirms',
                                    'fast_blended','vol_30s','conviction',
                                    'is_elite_flow',
                                ] if k in po},
                                **{k:po[k] for k in [
                                    'bs_theoretical','bs_iv','bs_edge',
                                    'bs_delta','bs_gamma',
                                    'premium_band','venus_would_block',
                                ] if k in po},
                            }
                            concurrent+=1

            # EOD close all positions
            eod_ts=day1s['timestamp'].max()
            eod_t_sec=int(((eod_ts.tz_convert('America/New_York').hour-9)*3600+
                           (eod_ts.tz_convert('America/New_York').minute-30)*60))
            lpx=(day1s[day1s['close']>0]
                 .groupby('instrument_id')['close'].last().to_dict())
            for iid,st in strikes.items():
                if st.position is None: continue
                xp=lpx.get(iid, st.position['entry_price'])
                pos=st.position; ep=pos['entry_price']; qty=pos['qty']
                tick=self.cfg.grid_tick
                xp=math.ceil(xp/tick)*tick
                comm=qty*self.cfg.comm_per_contract*2
                pnl=(xp-ep)*qty*100-comm
                sec=int((eod_ts-pos['entry_time']).total_seconds())
                eod_et=eod_ts.tz_convert('America/New_York')
                eod_T_mins=max((expiry_et-eod_et).total_seconds()/60.0,5.0)
                eod_spx=self.spx(ds,eod_t_sec) or 0.0
                trades.append(self._trade(st,pos,ds,eod_ts,xp,pnl,
                                          'EOD_CLOSE',sec,'EOD',0.5,0.0,0.0,
                                          eod_spx,eod_T_mins))
                bankroll+=pnl
                st.position=None

        self._print_stats()
        result=pd.DataFrame(trades)
        if result.empty: return result
        result=result.sort_values('exit_time').reset_index(drop=True)
        result['bankroll']=self.cfg.starting_capital+result['pnl_usd'].cumsum()
        return result

    # ── DIAGNOSTICS ───────────────────────────────────────────
    def _print_stats(self):
        s=self.stats; c=s['checked']
        if c==0: print("\n  No entries checked."); return
        print(f"""
{'='*65}
HURRICANE v3 — MARS BUILD DIAGNOSTICS
{'='*65}
  Entry checks           : {c:,}
  Entries placed          : {s['base_entries']:,}

  Blocked by:
    Dead range             : {s['range_blocked']:,}
    Too high in range      : {s['pct_blocked']:,}
    PFMACD (wrong flow)    : {s['pf_blocked']:,}  ({s['pf_blocked']/max(c,1)*100:.1f}%)
    Spread too narrow      : {s['spread_blocked']:,}
    Fill ratio too high    : {s['fill_blocked']:,}
    Volume too thin (fill) : {s['volume_blocked']:,}
    EARTH: vol pulse gate  : {s['earth_vol_blocked']:,}  ← dead strikes eliminated
    Venus: SPX velocity    : {s['venus_blocked']:,}
    SPX lookup miss (!)    : {s['spx_none']:,}  ← if >0, check date_str format

  Premium bands:
    Band A ($0.15-$0.30)   : {s['mars_band_a']:,}
    Band B ($0.35-$0.50)   : {s['mars_band_b']:,}
    Band C ($0.55-$1.00)   : {s['mars_band_c']:,}
    $0.05 re-entry allowed : {s['mars_floor_reentry']:,}
    $0.05 blocked          : {s['mars_floor_blocked']:,}
    Band full (4 max)      : {s['mars_band_full']:,}
    Venus would-block (obs): {s['venus_would_block']:,}
    Elite flow events      : {s['earth_elite']:,}  (>100 contracts/30s)

  Conviction breakdown:
    HIGH conviction        : {s['conv_high']:,}
    MED  conviction        : {s['conv_med']:,}
    LOW  conviction        : {s['conv_low']:,}

  Cone:
    Dual cone confirmed    : {s['dual_confirmed']:,}
    Slope confirmed        : {s['slope_confirmed']:,}
    Limit price lowered    : {s['cone_lowered']:,}
    Exit target set        : {s['cone_target_set']:,}
    CONE_EXIT fired        : {s['cone_exit']:,}
{'='*65}""")

    def save_greeks_log(self, path='hurricane_v3_greeks.parquet'):
        """Save Greeks observation log to parquet."""
        if not self.greeks_log:
            print("  Greeks log empty — no active positions recorded.")
            return
        df=pd.DataFrame(self.greeks_log)
        df.to_parquet(path, index=False)
        print(f"  Greeks log: {len(df):,} snapshots → {path}")


# ═══════════════════════════════════════════════════════════════
# CLARK — cbbo-1s: the calendar clock, and the resting book
# ═══════════════════════════════════════════════════════════════
import math as _math
from collections import deque as _deque


class ClarkState:
    """Calendar-clock state for one strike. Mirror of StrikeStateV3."""
    def __init__(self, dbid, strike, direction):
        self.dbid=dbid; self.strike=strike; self.direction=direction
        self.quotes=_deque(maxlen=400); self.bar_times=_deque(maxlen=200)
        self.last_quote_sec=-1

    def on_quote(self, ts, bid, ask, bsz, asz):
        sec=int(ts)
        if sec==self.last_quote_sec: self.quotes[-1]=(sec,bid,ask,bsz,asz); return
        self.last_quote_sec=sec; self.quotes.append((sec,bid,ask,bsz,asz))

    def on_bar(self, ts): self.bar_times.append(ts)

    def mid_stats(self, window_secs=30):
        """Mars's rolling_stats formula, on the calendar clock, on the mid."""
        if len(self.quotes)<4: return None,None,None
        now=self.quotes[-1][0]
        prior=[q for q in self.quotes if now-q[0]<=window_secs]
        if len(prior)<3: return None,None,None
        mids=[(q[1]+q[2])/2.0 for q in prior[:-1]]
        lo,hi=min(mids),max(mids)
        if hi<=lo: return lo,hi,None
        curr=(prior[-1][1]+prior[-1][2])/2.0
        return lo,hi,(curr-lo)/(hi-lo)

    def clock_rate(self, n=30):
        """Bars per second — the bridge between the two clocks."""
        if len(self.bar_times)<2: return None
        w=list(self.bar_times)[-n:]; sp=w[-1]-w[0]
        return None if sp<=0 else (len(w)-1)/sp

    def bars_span_secs(self, n=30):
        if len(self.bar_times)<2: return None
        w=list(self.bar_times)[-n:]; return w[-1]-w[0]

    def _last(self): return self.quotes[-1] if self.quotes else None
    def spread(self):
        q=self._last(); return None if q is None else q[2]-q[1]
    def spread_pct(self):
        q=self._last()
        if q is None: return None
        mid=(q[1]+q[2])/2.0
        return None if mid<=0 else (q[2]-q[1])/mid
    def micro_price(self):
        q=self._last()
        if q is None: return None
        _,b,a,bs,as_=q; t=bs+as_
        return None if t<=0 else (a*bs+b*as_)/t
    def imbalance(self):
        q=self._last()
        if q is None: return None
        t=q[3]+q[4]; return None if t<=0 else (q[3]-q[4])/t
    def notional(self):
        q=self._last()
        if q is None: return None,None
        return q[3]*q[1]*100.0, q[4]*q[2]*100.0
    def range_per_sqrt_bar(self, lo, hi, n=30):
        if lo is None or hi is None or n<=0: return None
        return (hi-lo)/_math.sqrt(n)


class ClarkBook:
    """All strikes. Observation only — nothing here gates a trade."""
    def __init__(self, window_secs=30, osc_bars=30):
        self.states={}; self.window_secs=window_secs; self.osc_bars=osc_bars
        self.quotes_seen=0; self.bars_seen=0; self.samples=[]

    def get(self,dbid,strike=0.0,direction='C'):
        st=self.states.get(dbid)
        if st is None: st=ClarkState(dbid,strike,direction); self.states[dbid]=st
        return st
    def on_quote(self,dbid,strike,direction,ts,b,a,bs,as_):
        self.quotes_seen+=1; self.get(dbid,strike,direction).on_quote(ts,b,a,bs,as_)
    def on_bar(self,dbid,strike,direction,ts):
        self.bars_seen+=1; self.get(dbid,strike,direction).on_bar(ts)

    def observe(self,dbid,pct_trade,lo,hi,px,ts):
        st=self.states.get(dbid)
        if st is None: return None
        lo_m,hi_m,pct_mid=st.mid_stats(self.window_secs)
        bn,an=st.notional()
        row={'ts':ts,'dbid':dbid,'trade_px':px,'pct_trade':pct_trade,
             'pct_mid':pct_mid,
             'div':(pct_trade-pct_mid) if (pct_trade is not None and pct_mid is not None) else None,
             'trade_lo':lo,'trade_hi':hi,'mid_lo':lo_m,'mid_hi':hi_m,
             'trade_range':(hi-lo) if (hi is not None and lo is not None) else None,
             'mid_range':(hi_m-lo_m) if (hi_m is not None and lo_m is not None) else None,
             'clock_rate':st.clock_rate(self.osc_bars),
             'bars_span_s':st.bars_span_secs(self.osc_bars),
             'spread':st.spread(),'spread_pct':st.spread_pct(),
             'micro':st.micro_price(),'imbalance':st.imbalance(),
             'rng_per_sqrt_bar':st.range_per_sqrt_bar(lo,hi,self.osc_bars),
             'bid_notional':bn,'ask_notional':an}
        self.samples.append(row); return row

    def status(self, top=6):
        print(f'  [CLARK] {len(self.states)} strikes | quotes:{self.quotes_seen:,} '
              f'bars:{self.bars_seen:,} | samples:{len(self.samples):,}')
        rows=[]
        for st in self.states.values():
            _,_,pct=st.mid_stats(self.window_secs)
            if pct is None: continue
            rows.append((st.dbid,pct,st.clock_rate(self.osc_bars),
                         st.spread_pct(),st.imbalance()))
        rows.sort(key=lambda r:-(r[2] or 0))
        for dbid,pct,cr,sp,imb in rows[:top]:
            print(f'          {dbid[-9:]}  pct_mid:{pct:5.2f}  clock:{cr if cr else 0:4.2f}/s'
                  f'  spr:{(sp or 0)*100:4.1f}%  imb:{imb if imb else 0:+5.2f}')

    def dump_csv(self, path='clark_samples.csv'):
        if not self.samples: print('  [CLARK] no samples'); return
        import csv
        keys=list(self.samples[0].keys())
        with open(path,'w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows(self.samples)
        print(f'  [CLARK] {len(self.samples):,} paired samples -> {path}')

    def summary(self):
        s=[r for r in self.samples if r['div'] is not None]
        print(f'\n  CLARK — TWO CLOCKS')
        print(f'  paired samples : {len(s):,} of {len(self.samples):,}')
        if not s: return
        import statistics as _S
        div=[r['div'] for r in s]
        cr=[r['clock_rate'] for r in s if r['clock_rate']]
        tr=[r['trade_range'] for r in s if r['trade_range'] is not None]
        mr=[r['mid_range'] for r in s if r['mid_range'] is not None]
        sp=[r['spread_pct'] for r in s if r['spread_pct']]
        print(f'  divergence     : mean {_S.mean(div):+.3f}  median {_S.median(div):+.3f}  '
              f'|div|>0.3 in {sum(1 for d in div if abs(d)>0.3)/len(div)*100:.1f}%')
        if cr: print(f'  clock_rate     : median {_S.median(cr):.2f} bars/s '
                     f'({self.osc_bars/_S.median(cr):.0f}s for a {self.osc_bars}-bar window)')
        if tr: print(f'  range (trade)  : median ${_S.median(tr):.3f}')
        if mr: print(f'  range (mid)    : median ${_S.median(mr):.3f}')
        if sp: print(f'  spread/mid     : median {_S.median(sp)*100:.1f}%')
        flat=sum(1 for r in s if r['trade_range'] is not None and r['trade_range']<0.051)
        print(f'  degenerate     : {flat}/{len(s)} with trade range <= 1 tick '
              f'({flat/len(s)*100:.1f}%)')


class OIVWMACD:
    """
    OI-VWMACD — the resting-order twin of PFMACD, from cbbo-1s.

        PFMACD    : SUM(trade_px * volume)      calls - puts   (executions)
        OI-VWMACD : SUM(bid_sz*bid - ask_sz*ask) calls - puts   (quotes)

    Trades say what happened. The book says what is ready to happen.

    Two readings, because they answer different questions:
        bias     — which side is favoured NOW (level). Use this to gate.
        momentum — is pressure ACCELERATING (MACD). Decays on a constant
                   imbalance and will eventually read the wrong way.

    Flushes on real N-SECOND clock boundaries, not on a count of
    records — StreamingPFMACD counts 6 arrivals, which on a busy chain
    is a fraction of a second and drifts with activity.
    """
    def __init__(self, fast=6, slow=24, signal=15, bucket_secs=6):
        self.fa=1.0/fast; self.sa=1.0/slow; self.ga=1.0/signal; self.la=1.0/slow
        self.ef=None; self.es=None; self.eg=0.0; self.level=None
        self.hist=0.0; self.rm=0.0; self.slope=0.0; self.prev_hist=0.0
        self.hist_buf=_deque(maxlen=slow)
        self.bucket=bucket_secs; self._cur=None
        self._cp=0.0; self._pp=0.0; self._cd=0.0; self._pd=0.0
        self.flushes=0; self.quotes=0
        self.last_net=0.0; self.call_depth=0.0; self.put_depth=0.0

    def tick_quote(self, ts, direction, bid, ask, bsz, asz):
        b=bsz*bid*100.0; a=asz*ask*100.0
        if direction=='C': self._cp+=(b-a); self._cd+=(b+a)
        else:              self._pp+=(b-a); self._pd+=(b+a)
        self.quotes+=1
        blk=int(ts)//self.bucket
        if self._cur is None: self._cur=blk
        elif blk!=self._cur: self._cur=blk; self._flush()

    def _flush(self):
        net=self._cp-self._pp; self.last_net=net
        self.call_depth=self._cd; self.put_depth=self._pd
        self._cp=self._pp=self._cd=self._pd=0.0; self.flushes+=1
        self.level = net if self.level is None else net*self.la+self.level*(1-self.la)
        if self.ef is None: self.ef=net; self.es=net
        else:
            self.ef=net*self.fa+self.ef*(1-self.fa)
            self.es=net*self.sa+self.es*(1-self.sa)
        ml=self.ef-self.es; self.eg=ml*self.ga+self.eg*(1-self.ga)
        self.prev_hist=self.hist; self.hist=ml-self.eg
        self.hist_buf.append(self.hist)
        self.rm=sum(self.hist_buf)/len(self.hist_buf) if self.hist_buf else 0.0
        self.slope=self.hist-self.prev_hist

    def is_ready(self): return self.flushes>=5

    @property
    def momentum(self):
        if not self.is_ready(): return 'NEUTRAL'
        if self.hist>self.rm: return 'BULL'
        if self.hist<self.rm: return 'BEAR'
        return 'NEUTRAL'

    @property
    def bias(self):
        if not self.is_ready() or self.level is None: return 'NEUTRAL'
        scale=max(abs(self.call_depth)+abs(self.put_depth),1.0)
        r=self.level/scale
        if r> 0.02: return 'BULL'
        if r<-0.02: return 'BEAR'
        return 'NEUTRAL'

    @property
    def direction(self): return self.bias

    def agrees(self, opt_dir, mode='bias'):
        d=self.bias if mode=='bias' else self.momentum
        if d=='NEUTRAL': return True
        return not ((opt_dir=='C' and d=='BEAR') or (opt_dir=='P' and d=='BULL'))

    def describe(self):
        return (f"bias:{self.bias:<7s} mom:{self.momentum:<7s} "
                f"level:{(self.level or 0):>+12,.0f} "
                f"Cdepth:${self.call_depth:>11,.0f} Pdepth:${self.put_depth:>11,.0f} "
                f"flushes:{self.flushes}")


# ═══════════════════════════════════════════════════════════════
# LIVE DATABENTO RUNNER — data pipe only, zero strategy changes
# Subclasses HurricaneV3, feeds live bars through _entry/_monitor
# ═══════════════════════════════════════════════════════════════
import time as _time
import os as _os
import sys as _sys
import json as _json
import argparse as _ap
import signal as _sig
import asyncio as _aio
import warnings as _warn
_warn.filterwarnings("ignore", category=DeprecationWarning)
try: _aio.get_event_loop()
except RuntimeError: _aio.set_event_loop(_aio.new_event_loop())
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

from hurricane_cmdlog import CmdLog
try:
    from hurricane_hotkeys import HotkeyHandler
except ImportError:
    HotkeyHandler = None


class StreamingPFMACD:
    """Same math as PFMACDEngine, streaming input."""
    def __init__(self, fast=6, slow=24, signal=15):
        self.fa=1.0/fast;self.sa=1.0/slow;self.ga=1.0/signal
        self.ef=None;self.es=None;self.eg=0.0
        self.hist=0.0;self.rm=0.0;self.slope=0.0;self.prev_hist=0.0
        self.hist_buf=deque(maxlen=slow)
        self._buf=[];self._cf=0.0;self._pf=0.0
    def tick_1s(self,price,volume,direction):
        df=price*volume
        if direction=='C': self._cf+=df
        else: self._pf+=df
        self._buf.append(1)
        if len(self._buf)>=6: self._flush()
    def _flush(self):
        net=self._cf-self._pf;self._cf=0;self._pf=0;self._buf=[]
        if self.ef is None: self.ef=net;self.es=net
        else:
            self.ef=net*self.fa+self.ef*(1-self.fa)
            self.es=net*self.sa+self.es*(1-self.sa)
        ml=self.ef-self.es;self.eg=ml*self.ga+self.eg*(1-self.ga)
        self.prev_hist=self.hist;self.hist=ml-self.eg
        self.hist_buf.append(self.hist)
        self.rm=sum(self.hist_buf)/len(self.hist_buf) if self.hist_buf else 0.0
        self.slope=self.hist-self.prev_hist
    @property
    def direction(self):
        if self.hist>self.rm: return 'BULL'
        elif self.hist<self.rm: return 'BEAR'
        return 'NEUTRAL'


class LiveMarsEngine(HurricaneV3):
    """Subclass: skip batch init, feed live bars through exact _entry/_monitor."""

    def __init__(self, config, heads, bankroll, conviction, start_et, spx_open):
        # Skip batch __init__, set attributes directly
        self.cfg = config
        self.vix = config.earth_vix_default
        self.oi_map = {}
        self.greeks_log = []
        self.atm_map = {}
        self.spx_prices = {}
        self.pfmacd = None  # not used — streaming PFMACD passed as args
        self._pfmacd_stream = StreamingPFMACD()
        self.oivw = OIVWMACD(bucket_secs=getattr(config,'oivw_bucket_secs',6))
        self._live_strikes = {}
        self._current_strikes = self._live_strikes  # Mars band counting
        self._dbid_to_iid = {}
        self._iid_counter = 1
        self.positions = {}
        self.band_counts = {'A':0, 'B':0, 'C':0}
        self._spx_current = spx_open
        self._today_ds = _dt.now(_tz.utc).strftime('%Y-%m-%d')
        for t in range(0, 23400, 300):
            self.spx_prices[(self._today_ds, t)] = spx_open
        self.stats = {k:0 for k in [
            'checked','pf_blocked','spread_blocked','fill_blocked',
            'range_blocked','pct_blocked','dual_confirmed','slope_confirmed',
            'cone_lowered','cone_target_set','cone_exit','base_entries',
            'volume_blocked','earth_vol_blocked','earth_elite',
            'tier_floor','tier_mid','tier_high',
            'venus_blocked','venus_would_block',
            'mars_band_a','mars_band_b','mars_band_c','mars_band_d',
            'oivw_blocked','oivw_agreed',
            'mars_band_full','mars_floor_reentry','mars_floor_blocked',
            'conv_high','conv_med','conv_low','spx_none',
        ]}
        # Live state
        self.heads = heads
        self.bankroll = bankroll
        self.conviction_filter = conviction
        self.log = CmdLog(mode='SHADOW', heads=heads, bankroll=bankroll,
                          min_conviction=conviction)
        h, m = start_et.split(':')
        self.start_secs = (int(h)-9)*3600 + (int(m)-30)*60
        self.bars_processed = 0
        self.hotkeys = None
        if HotkeyHandler:
            self.hotkeys = HotkeyHandler()
            self.hotkeys.on_kill = lambda: self._close_all()
            self.hotkeys.on_status = lambda: self.log.status()
            self.hotkeys.on_quit = lambda: _sys.exit(0)
            self.hotkeys.start()
        _sig.signal(_sig.SIGINT, lambda s,f: _sys.exit(0))

    def _get_strike(self, dbid, strike, direction):
        if dbid not in self._live_strikes:
            iid = self._iid_counter; self._iid_counter += 1
            self._dbid_to_iid[dbid] = iid
            dist = abs(strike - self._spx_current)
            st = StrikeStateV3(instrument_id=iid, strike=strike,
                               direction=direction, dist=dist)
            self._live_strikes[dbid] = st
        return self._live_strikes[dbid]

    def update_spx(self, new_spx, t_sec):
        self._spx_current = new_spx
        bar_t = (t_sec // 300) * 300
        self.spx_prices[(self._today_ds, bar_t)] = new_spx

    def on_bar(self, dbid, strike, direction, o, h, l, c, v, t_sec, ts_utc):
        self.bars_processed += 1
        if t_sec > 23160 or c <= 0 or v <= 0: return
        if c < 0.05 or c > 1.00: return  # Mars premium range

        st = self._get_strike(dbid, strike, direction)
        st.update(c, o, v)
        st.update_layers(c, h, l)
        self._pfmacd_stream.tick_1s(c, v, direction)

        pf = self._pfmacd_stream
        pf_hist = pf.hist; pf_rm = pf.rm; pf_slope = pf.slope; pf_dir = pf.direction

        # Time to expiry
        utc_h = (int(ts_utc) % 86400) // 3600
        utc_m = (int(ts_utc) % 3600) // 60
        T_mins = max(5.0, (16*60 - ((utc_h-4)*60 + utc_m)))  # mins to 4PM ET

        # EXIT check — every bar
        if st.position is not None:
            ts_pd = pd.Timestamp(ts_utc, unit='s', tz='UTC')
            ts_et = ts_pd.tz_convert('America/New_York')
            self._monitor(st, c, h, ts_pd, ts_et, self._today_ds,
                         self._trades_buf, pf_hist, pf_rm, pf_slope, t_sec, T_mins)
            if st.position is None:
                # Trade completed
                if self._trades_buf:
                    tr = self._trades_buf[-1]
                    pnl = tr['pnl_usd']
                    self.bankroll += pnl
                    reason = tr['exit_reason']
                    band = tr.get('premium_band', '?')
                    self.log.exit_filled(dbid, tr['exit_price'], reason)
                    # Release band slot
                    if band in self.band_counts:
                        self.band_counts[band] = max(0, self.band_counts[band] - 1)
            return

        # ENTRY check
        if t_sec < self.start_secs: return
        concurrent = sum(1 for s in self._live_strikes.values() if s.position is not None)
        if concurrent >= self.cfg.max_concurrent: return
        if c < self.cfg.min_bid or c > self.cfg.max_bid: return

        ts_pd = pd.Timestamp(ts_utc, unit='s', tz='UTC')
        ts_et = ts_pd.tz_convert('America/New_York')
        in_entry_window = (
            (ts_et.hour > self.cfg.entry_start_et_h or
             (ts_et.hour == self.cfg.entry_start_et_h and
              ts_et.minute >= self.cfg.entry_start_et_m)) and
            (ts_et.hour < self.cfg.entry_stop_et_h or
             (ts_et.hour == self.cfg.entry_stop_et_h and
              ts_et.minute < self.cfg.entry_stop_et_m)))
        if not in_entry_window: return

        bar_dict = {'close':c, 'open':o, 'high':h, 'low':l}
        po = self._entry(st, bar_dict, ts_pd, self._today_ds, t_sec,
                         pf_dir, pf_hist, pf_rm, pf_slope,
                         self.bankroll, T_mins)
        if po is not None:
            band = po.get('premium_band', '?')
            # Check conviction gate BEFORE creating position
            accepted = self.log.entry_sent(dbid, po['lim'], band, st.direction,
                                po.get('conviction','MED'), po.get('vol_30s',0))
            if not accepted:
                return
            st.position = {
                'entry_price': po['lim'], 'qty': po['qty'],
                'entry_time': ts_pd, 'fill_delay_secs': 1,
                'price_history': [c], 'last_bar_ts': ts_pd,
                'direction': st.direction, 'peak_price': po['lim'],
                'swell_entry': po.get('swell', False),
                'entry_volume': v, 'exit_target': po['exit_target'],
                **{k: po[k] for k in [
                    'real_dist','entry_spx','pf_dir','cone_blended',
                    'cone_ticks','spread_ticks','fill_ratio','dual_agree',
                    'slope_confirms','fast_blended','vol_30s','conviction',
                    'is_elite_flow','premium_band','venus_would_block',
                ] if k in po},
            }
            if band in self.band_counts:
                self.band_counts[band] += 1
            self.log.entry_filled(dbid, po['lim'])

        if self.bars_processed % 1000 == 0:
            self.print_status()

    def print_status(self):
        pos = sum(1 for s in self._live_strikes.values() if s.position is not None)
        bands = ' '.join(f'{k}:{v}' for k,v in self.band_counts.items())
        print(f'  Bars:{self.bars_processed:,} Pos:{pos}/{self.cfg.max_concurrent} '
              f'Bands:[{bands}] SPX:{self._spx_current:.1f} '
              f'PFMACD:{self._pfmacd_stream.direction} Bank:${self.bankroll:,.2f}')

    def _close_all(self):
        for dbid, st in self._live_strikes.items():
            if st.position:
                c = st.prices[-1] if st.prices else st.position['entry_price']
                pnl = (c - st.position['entry_price']) * st.position['qty'] * 100
                self.log.exit_filled(dbid, c, 'FORCE_EXIT')
                st.position = None

    _trades_buf = []


def _parse_spxw(sym):
    try:
        s = sym.replace(' ', '')
        if not s.upper().startswith('SPXW'): return None
        rest = s[4:]
        for i, ch in enumerate(rest):
            if ch in ('C','P') and i >= 4:
                ss = rest[i+1:]
                strike = int(ss[:8])/1000.0 if len(ss)>=8 else float(ss)
                return (strike, ch)
        return None
    except: return None


def run_live(engine, db_key):
    try: import databento as db
    except ImportError: print('  pip install databento'); return

    today = _dt.now(_tz.utc).strftime('%y%m%d')
    if _dt.now(_tz.utc).hour < 4:
        today = (_dt.now(_tz.utc) - _td(days=1)).strftime('%y%m%d')

    print(f'  Connecting Databento | 0DTE: {today}')
    client = db.Live(key=db_key)
    client.subscribe(dataset='OPRA.PILLAR', schema='ohlcv-1s',
                     stype_in='parent', symbols=['SPXW.OPT'])
    client.subscribe(dataset='OPRA.PILLAR', schema='cbbo-1s',
                     stype_in='parent', symbols=['SPXW.OPT'])
    print('  Subscribed: ohlcv-1s (trade clock) + cbbo-1s (calendar clock)\n')

    id_to_sym = {}; sym_cache = {}; dte0 = set(); count = 0
    last_spx_update = 0
    clark = getattr(engine,'clark',None); quotes = 0

    import atexit
    atexit.register(lambda: engine.log.summary())

    try:
        for record in client:
            rt = type(record).__name__
            if 'SymbolMapping' in rt or 'InstrumentDef' in rt:
                iid = record.hd.instrument_id if hasattr(record,'hd') else getattr(record,'instrument_id',0)
                sym = getattr(record,'stype_out_symbol','') or getattr(record,'raw_symbol','') or ''
                if iid and sym: id_to_sym[iid] = sym.strip()
                if len(id_to_sym) % 500 == 0 and len(id_to_sym) <= 5000:
                    print(f'  [MAP] {len(id_to_sym)}')
                continue
            if 'System' in rt or 'Error' in rt:
                msg = getattr(record,'msg','') or getattr(record,'err','') or ''
                if 'End of interval' not in str(msg): print(f'  [SYS] {msg}')
                continue
            iid_num = record.hd.instrument_id if hasattr(record,'hd') else getattr(record,'instrument_id',0)
            sym = id_to_sym.get(iid_num)
            if not sym: continue
            sc = sym.replace(' ','')
            if len(sc) < 10 or sc[4:10] != today: continue
            if sym not in sym_cache:
                p = _parse_spxw(sym)
                if p is None: continue
                sym_cache[sym] = p
            if iid_num not in dte0:
                dte0.add(iid_num)
                if len(dte0) <= 10 or len(dte0) % 50 == 0:
                    print(f'  [0DTE] {len(dte0)} strikes')
            strike, direction = sym_cache[sym]
            ts_ns = int(record.ts_event) if hasattr(record,'ts_event') else 0
            ts_sec = ts_ns / 1_000_000_000

            # ── CALENDAR CLOCK — cbbo-1s carries .levels, not .open ──
            if hasattr(record,'levels'):
                if clark is None: continue
                try:
                    _lv=record.levels[0]
                    _b=float(_lv.bid_px)/1e9; _a=float(_lv.ask_px)/1e9
                    _bs=int(_lv.bid_sz);      _as=int(_lv.ask_sz)
                except Exception: continue
                if _b<=0 or _a<=0 or _a>50 or _b>50 or _a<_b: continue
                quotes+=1
                clark.on_quote(sym,strike,direction,ts_sec,_b,_a,_bs,_as)
                engine.oivw.tick_quote(ts_sec,direction,_b,_a,_bs,_as)
                if quotes%20000==0:
                    print(f'  [QUOTES] {quotes:,} | {len(clark.states)} strikes')
                    print(f'  [OIVW]   {engine.oivw.describe()}')
                continue

            # ── TRANSACTION CLOCK — ohlcv-1s ──
            if not hasattr(record,'open'): continue
            o = float(record.open)/1e9; h = float(record.high)/1e9
            l = float(record.low)/1e9; c = float(record.close)/1e9
            v = int(record.volume)
            if c <= 0 or v <= 0: continue
            count += 1

            utc_h = (int(ts_sec)%86400)//3600; utc_m = (int(ts_sec)%3600)//60
            t_sec = (utc_h*60+utc_m-240-570)*60

            # SPX update every 5 min via yfinance
            now = _time.time()
            if now - last_spx_update > 300:
                try:
                    import yfinance as yf
                    spx = yf.download('^GSPC', period='1d', interval='1m', progress=False)
                    if not spx.empty:
                        if hasattr(spx.columns, 'levels'):
                            spx.columns = spx.columns.get_level_values(0)
                        engine.update_spx(float(spx['Close'].iloc[-1]), t_sec)
                except: pass
                last_spx_update = now

            if clark is not None: clark.on_bar(sym,strike,direction,ts_sec)

            engine.on_bar(sym, strike, direction, o, h, l, c, v, t_sec, ts_sec)

            if clark is not None:
                _st=engine._live_strikes.get(sym)
                if _st is not None and len(getattr(_st,'prices',[]))>4:
                    _lo,_hi,_pct=_st.rolling_stats(engine.cfg.osc_window,
                                                   engine.cfg.grid_tick)
                    clark.observe(sym,_pct,_lo,_hi,c,ts_sec)

            if count % 5000 == 0 and count <= 50000:
                print(f'  [FEED] {count:,} bars | {quotes:,} quotes | '
                      f'{len(dte0)} 0DTE strikes')
            if clark is not None and count % 10000 == 0:
                clark.status()

    except KeyboardInterrupt: print('\n  Shutting down...')
    finally:
        try: client.stop()
        except: pass
        engine.log.summary()


def main_live():
    p = _ap.ArgumentParser(description='Mars-Richesse LIVE shadow')
    p.add_argument('--start', type=str, default='10:00')
    p.add_argument('--heads', type=int, default=12)
    p.add_argument('--bankroll', type=float, default=1000.0)
    p.add_argument('--conv', type=str, default='MED_HIGH')
    # ── CLARK ──
    p.add_argument('--oivw', action='store_true',
                   help='gate entries on OI-VWMACD: the resting book must '
                        'favour the same side as the trade flow')
    p.add_argument('--oivw-bucket', type=int, default=6,
                   help='OI-VWMACD flush interval in SECONDS (real clock)')
    p.add_argument('--clark-window', type=int, default=30,
                   help='calendar-clock window in SECONDS')
    p.add_argument('--max-premium', type=float, default=3.00,
                   help='band D ceiling. 1.00 = richesse, no band D')
    p.add_argument('--csv', type=str, default='clark_samples.csv')
    # ── broker ──
    p.add_argument('--live', action='store_true',
                   help='route orders to IBKR via broker_hub + hurricane_heads')
    p.add_argument('--port', type=int, default=7497,
                   help='7497 TWS paper | 4002 Gateway paper')
    p.add_argument('--flatten-start', action='store_true',
                   help='market-sell any option position found at startup')
    p.add_argument('--sim-broker', action='store_true')
    p.add_argument('--limit-exits', action='store_true',
                   help="send exits as LMT at Mars's own price. Default is "
                        "MKT: Mars clears st.position the moment it decides "
                        "to exit, so FORCE_EXIT at 150s can no longer fire "
                        "and a resting limit has nothing behind it.")
    p.add_argument('--entry-ttl', type=float, default=24.0,
                   help='cancel an unfilled BUY after N seconds and let Mars '
                        're-scan. 24s matches the Wilders slow period (6/24/15)')
    p.add_argument('--sell-ttl',  type=float, default=60.0,
                   help='backstop: escalate a stuck sell to MKT. Should never '
                        'fire when exits are already MKT.')
    a = p.parse_args()

    db_key = _os.environ.get('DATABENTO_API_KEY', '')
    if not db_key: print('  Set DATABENTO_API_KEY'); return

    # SPX
    spx_open = 7500.0
    try:
        import yfinance as yf
        s = yf.download('^GSPC', period='1d', interval='1m', progress=False)
        if not s.empty:
            if hasattr(s.columns, 'levels'): s.columns = s.columns.get_level_values(0)
            spx_open = float(s['Close'].iloc[-1])
            print(f'  SPX: {spx_open:.2f}')
    except Exception as e: print(f'  SPX fallback: {spx_open} ({e})')

    # VIX
    cfg = V3Config()
    try:
        import yfinance as yf
        v = yf.download('^VIX', period='1d', interval='1d', progress=False)
        if not v.empty:
            if hasattr(v.columns, 'levels'): v.columns = v.columns.get_level_values(0)
            cfg.earth_vix_default = float(v['Close'].iloc[-1])
            print(f'  VIX: {cfg.earth_vix_default:.1f}')
    except: pass

    cfg.starting_capital = a.bankroll
    cfg.max_concurrent = a.heads

    print(f'\n  MARS-RICHESSE SHADOW | {a.heads} heads | ${a.bankroll:,.0f}')
    print(f'  Premium: ${cfg.min_bid}-${cfg.max_bid} | Start: {a.start} ET')
    print(f'  Strategy: hurricane_v3_mars-richesse (EXACT, zero changes)\n')

    cfg.max_bid          = a.max_premium
    cfg.oivw_enabled     = a.oivw
    cfg.oivw_bucket_secs = a.oivw_bucket

    engine = LiveMarsEngine(config=cfg, heads=a.heads, bankroll=a.bankroll,
                            conviction=a.conv, start_et=a.start, spx_open=spx_open)
    engine.clark = ClarkBook(window_secs=a.clark_window, osc_bars=cfg.osc_window)

    _b = 'A-B-C-D' if cfg.max_bid > 1.00 else 'A-B-C'
    print(f'  BANDS  {_b}  up to ${cfg.max_bid:.2f}  '
          f'{cfg.mars_max_per_band}/band   '
          f'cap $100/head (${300 if cfg.max_bid>1 else 100} in D)')
    print(f'  COMM   ${cfg.comm_per_contract:.2f}/fill = '
          f'${cfg.comm_per_contract*2:.2f} round trip (IBKR measured)')
    print(f'  OIVW   {"GATING" if cfg.oivw_enabled else "observing only"}  '
          f'{cfg.oivw_bucket_secs}s buckets (real clock)')
    print(f'  CLARK  cbbo-1s recording only — gates nothing')

    hub = runner = None
    if a.live:
        from broker_hub import BrokerHub, IBKRPort, ShadowPort
        from hurricane_heads import HeadRunner
        port = (ShadowPort() if a.sim_broker
                else IBKRPort(port=a.port, market_exits=not a.limit_exits,
                              flatten_on_start=a.flatten_start))
        hub = BrokerHub(port, n_slots=a.heads)
        if not hub.start():
            print('  broker link failed'); return
        runner = HeadRunner(engine, hub, n_heads=a.heads, resting_exit=False,
                            entry_ttl=a.entry_ttl, sell_ttl=a.sell_ttl)
        engine.on_bar = runner.on_bar
        print(f'  BROKER {port.name}  {a.heads} heads  '
              f'exits {"LMT at Mars price" if a.limit_exits else "MKT"}  '
              f'entryTTL {a.entry_ttl:.0f}s  sellTTL {a.sell_ttl:.0f}s')
    else:
        print('  BROKER none — CmdLog shadow')
    print()

    try:
        run_live(engine, db_key)
    except KeyboardInterrupt:
        pass
    finally:
        if runner is not None:
            import time as _t
            print('\n  flattening...')
            runner.flatten()
            for _ in range(60): runner.pump(); _t.sleep(0.1)
            hub.print_status(); runner.summary()
            _rc=getattr(hub.port,'total_commission',0.0)
            if _rc: print(f'  real commission (IBKR): ${_rc:,.2f}')
            hub.stop()
        engine.clark.summary()
        engine.clark.dump_csv(a.csv)


if __name__ == '__main__':
    main_live()
