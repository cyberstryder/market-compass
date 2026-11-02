"""
hurricane_v3_mars_fills.py — MARS BUILD, honest-fill harness (2026-11-02)
==========================================================================
Same signal logic as hurricane_v3_mars.py. Execution model replaced:

  ENTRY  : _entry() returns a resting BUY LIMIT. It is NOT filled on the signal bar.
           It fills on a LATER 1s bar of that instrument only if a print occurred
           below the limit (strict: low < lim; relaxed: low <= lim and vol >= min_fill_volume).
           Unfilled after entry_order_life_secs → cancelled. fill_delay recorded.
  EXIT   : _monitor() decides a reason at bar t; the SELL executes on the NEXT bar
           with a print: market exits fill at that bar's `exit_market_px` ('low' = bid-side
           proxy, 'close'), rounded DOWN to the tick, minus extra_slippage_ticks.
           OSC_PROFIT is a resting sell limit at exit_target: fills only if a print went
           THROUGH the target (strict: high > target). No ceil-rounding anywhere on sells.
  PFMACD : uses the last COMPLETED 6s bucket (k < t6), not the in-progress one.
  WINDOWS: volume gate / stop reference / bail are wall-clock (t_sec), not print-count.
  SPX    : spx_5m.parquet is used only if its dates intersect the bar dates. No rescaling,
           no remapping. If absent, Mars falls back to atm_map open and stamps spx_source.
  PLACEBO: V3Config(placebo_rate=p) skips every signal gate (vol pulse, cone, pct, slope)
           and accepts a candidate at random with prob p. Same bands, same SPX dist gate,
           same fill model, same exits. Compare to the real run at matched entry rate.
  REPORT : trades carry `profit_gated` (CONE/FLOW/TRAIL exits can only fire on winners).

Original Mars header follows unchanged.
--------------------------------------------------------------------------
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
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from collections import deque
import math
import random
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
        # FIX: bucket t6 spans t_sec..t_sec+5 and is not complete yet → use k < t6
        k=next((k for k in reversed(keys) if k<t6), None)
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
    min_bid:float=0.05; max_bid:float=1.00  # MARS: $0.15-$1.00 primary, $0.05 restricted
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
    comm_per_contract:float=0.65
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
    mars_max_per_band:int=4       # max 4 concurrent positions per band
    # $0.05 re-entry rule: only after profitable $0.10 exit, past 15:00 ET
    mars_floor_reentry_min_exit:float=0.10  # must have exited at >= this
    mars_floor_reentry_after_h:int=15       # 15:00 ET

    # ── EARTH: Greeks recording ───────────────────────────────
    earth_record_greeks:bool=True
    earth_greeks_interval_secs:int=10   # snapshot every 10s per active position
    earth_vix_default:float=18.0        # fallback if VIX not available

    # ── FILLS (2026-11-02 harness) ────────────────────────────
    fill_strict:bool=True              # True: buy fills only on low < lim; sell limit only on high > target
    min_fill_volume:int=3              # relaxed mode: prints at the limit need this much volume
    entry_order_life_secs:int=300      # cancel resting buy after this
    exit_market_px:str='low'           # 'low' (bid-side proxy) or 'close' for market sells
    extra_slippage_ticks:int=0         # extra ticks taken off every sell fill
    # ── PLACEBO ───────────────────────────────────────────────
    placebo_rate:Optional[float]=None  # None = real strategy; float = random entry at this rate
    placebo_seed:int=7


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

    def update(self, price, bar_open=None, volume=0, t_sec=None):
        self.prices.append(price)
        self.opens.append(bar_open if bar_open is not None else price)
        self.volumes.append(volume)
        # wall-clock history: (t_sec, volume) — bars are sparse, so index != seconds
        if not hasattr(self,'vol_hist'): self.vol_hist=deque()
        if t_sec is not None:
            self.vol_hist.append((t_sec, volume))
            while self.vol_hist and self.vol_hist[0][0] < t_sec-600: self.vol_hist.popleft()
        if len(self.prices)>120:
            self.prices=self.prices[-120:]
            self.opens=self.opens[-120:]
            self.volumes=self.volumes[-120:]

    def update_layers(self, close, high, low):
        self.dw.update(close, high, low)
        self.cone.tick_1s(close, high, low)
        self.cone_fast.tick_1s(close, high, low)

    def vol_last_n_secs(self, n, now_t=None):
        """Volume traded in the last N wall-clock seconds (inclusive of the current bar)."""
        vh=getattr(self,'vol_hist',None)
        if vh and now_t is not None:
            return sum(v for t,v in vh if t > now_t-n)
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
        self.bars1s['date_str']=self.bars1s['timestamp'].dt.strftime('%Y-%m-%d')
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
                # date_str must come from the timestamp, never trusted from the file
                spx['date_str']=spx_et.dt.strftime('%Y-%m-%d')
                bar_days=set(self.bars1s['date_str'].unique())
                spx_days=set(spx['date_str'].unique())
                common=bar_days & spx_days
                if not common:
                    print(f"  SPX: spx_5m.parquet covers {sorted(spx_days)[:1]}..{sorted(spx_days)[-1:]} "
                          f"but bars are {sorted(bar_days)}. NOT USED — no remapping, no rescaling. "
                          f"Pull SPX (or ES) for the actual dates.")
                else:
                    spx=spx[spx['date_str'].isin(common)]
                    self.spx_prices={(row['date_str'],int(row['t_sec'])):float(row['open'])
                                     for _,row in spx.iterrows()}
                    missing=bar_days-spx_days
                    print(f"  SPX 5-min bars: {len(self.spx_prices):,} bars for {len(common)} day(s)"
                          + (f"; no SPX for {sorted(missing)}" if missing else ""))
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
            'mars_band_a','mars_band_b','mars_band_c',
            'mars_band_full','mars_floor_reentry','mars_floor_blocked',
            'conv_high','conv_med','conv_low',
            'spx_none',  # silent killer — no SPX price found
            'spx_static_fallback',
            # fill harness
            'orders_placed','orders_filled','orders_expired','orders_cancelled',
            'exit_limit_fills','exit_market_fills',
            'placebo_rejected','no_room',
        ]}
        self._rng=random.Random(config.placebo_seed)

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
        if limit_px * 100 > 100: return 0  # exceeds $100/head
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
        placebo = self.cfg.placebo_rate is not None
        lo, hi, pct = st.rolling_stats(self.cfg.osc_window, self.cfg.grid_tick)
        if lo is None: return None
        if not placebo and (hi-lo)/(lo+1e-7) < self.cfg.min_range_pct:
            self.stats['range_blocked'] += 1; return None
        swell = st.swell_until is not None and ts <= st.swell_until

        # ── EARTH GATE 1: VOLUME PULSE (wall-clock window) ────
        vol_30s = st.vol_last_n_secs(self.cfg.earth_vol_window_secs, now_t=t_sec)
        if not placebo and vol_30s < self.cfg.earth_min_vol_30s:
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
        elif curr > self.cfg.mars_band_c_hi:
            return None  # above $1.00 — not our game
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
        band_count += sum(1 for po in getattr(self,'_pending',{}).values()
                          if po.get('premium_band') == premium_band)
        if band_count >= self.cfg.mars_max_per_band:
            self.stats['mars_band_full'] += 1
            return None

        # Mercury tier compat (for cone pct gates downstream)
        if curr <= 0.10: premium_tier = 'FLOOR'
        elif curr >= 0.20: premium_tier = 'HIGH'
        else: premium_tier = 'MID'
        max_fill_ratio = self.cfg.max_fill_ratio

        # ── VENUS: SPX velocity gate (floor + far OTM only) ───
        spx_now = self.spx(ds, t_sec); spx_source='5m'
        if spx_now is None:
            # Fallback: use ATM map (static daily SPX open) if available
            atm_val = self.atm_map.get(ds)
            if atm_val is not None:
                spx_now = float(atm_val); spx_source='static_open'
                self.stats['spx_static_fallback'] += 1
            else:
                self.stats['spx_none'] += 1
                if self.stats['spx_none'] == 1:
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
        if not pf_agrees: self.stats['pf_blocked'] += 1
        slope_confirms = ((st.direction=='C' and pf_slope>0) or
                          (st.direction=='P' and pf_slope<0))

        # ── DUAL CONE ─────────────────────────────────────────
        cone_blended=0.5; cone_ticks=0; spread_ticks=0; fill_ratio=0.0
        exit_target=lo+(hi-lo)*self.cfg.exit_pct
        dual_agree=False; fast_blended=0.5
        dw_wave=st.dw.wave if st.dw.is_warmed() else 0.0

        if placebo:
            # PLACEBO: no signal gates. Random acceptance at the real run's entry rate.
            # Keeps: band, band-limit, SPX dist gate, sizing, fill model, exits.
            if self._rng.random() >= self.cfg.placebo_rate:
                self.stats['placebo_rejected'] += 1; return None
            if self.cfg.cone_enabled and st.cone.is_ready():
                pred_slow=st.cone.predict(curr, pf_hist, pf_rm, pf_slope, dw_wave,
                                          st.dw.histogram, self.cfg.cone_fwd_bars, self.cfg.grid_tick)
                if pred_slow and pred_slow['ceiling']>exit_target: exit_target=pred_slow['ceiling']
        elif self.cfg.cone_enabled and st.cone.is_ready():
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
        g=round(bar_open/tick, 6)                 # FIX: float noise made ceil(0.30/0.05)=7 → bid 0.35 for a 0.30 print
        rup=math.ceil(g)*tick
        on_grid=abs(g-round(g))<1e-6
        limit_px=rup if on_grid else rup-tick
        if cone_ticks>0: limit_px-=cone_ticks*tick; self.stats['cone_lowered']+=1
        limit_px=round(max(limit_px, self.cfg.min_bid), 2)
        exit_target=round(math.floor(exit_target/tick+1e-9)*tick, 2)   # sell limit must sit on the grid
        if exit_target < limit_px + tick - 1e-9:
            # no room: target at/below the bid means a guaranteed round-trip loss → don't place
            self.stats['no_room'] += 1; return None

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
            'T_mins_at_entry': (390 - t_sec/60) if t_sec else 300,
            'venus_would_block':venus_would_block,
            'spx_source':spx_source,
            'signal_price':curr,
            'signal_t_sec':t_sec,
            **bs_data,
        }

    # ── EXIT MONITOR ──────────────────────────────────────────
    def _monitor(self, st, curr, high, ts, ts_et, ds, trades,
                 pf_hist, pf_rm, pf_slope, t_sec, T_mins):
        pos=st.position; ep=pos['entry_price']; qty=pos['qty']
        gain=(curr-ep)/(ep+1e-7); secs=(ts-pos['entry_time']).total_seconds()
        # wall-clock price history: (t_sec, price)
        ph_t=pos.setdefault('ph_t',[])
        ph_t.append((t_sec,curr))
        while ph_t and ph_t[0][0] < t_sec-max(self.cfg.bail_secs,self.cfg.stop_window_secs)-5: ph_t.pop(0)
        def ref_secs_ago(n):
            """last print at or before (now - n). None if none in window."""
            cands=[p for t,p in ph_t if t<=t_sec-n]
            return cands[-1] if cands else None
        pos['price_history'].append(curr)
        if len(pos['price_history'])>self.cfg.stop_window_secs+5:
            pos['price_history']=pos['price_history'][-(self.cfg.stop_window_secs+5):]
        reason=None; exit_px=curr
        if pos.get('pending_exit') is not None:
            return  # sell already working; fills are handled in run_backtest
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

        elif reason is None and (high>pos['exit_target'] if self.cfg.fill_strict
                                  else high>=pos['exit_target']):
            # resting sell limit at exit_target, placed at entry: a print THROUGH it = filled
            exit_px=pos['exit_target']; reason='OSC_PROFIT'
        elif reason is None and zone==-2 and self.cfg.zone_neg2_cut:
            exit_px=curr; reason='ZONE2_CUT'
        elif reason is None and gain>=1.00:
            ref=ref_secs_ago(self.cfg.stop_window_secs)
            if ref is not None and ref>0 and curr<ref*(1-self.cfg.stop_fraction):
                reason='GUST_TRAIL'; exit_px=curr
        elif reason is None and (ref:=ref_secs_ago(self.cfg.stop_window_secs)) is not None:
            drop=ref-curr
            st_t=0.10 if ep<=0.50 else (0.15 if ep<=0.80 else 0.20)
            if drop>=st_t: reason='STOP'
        elif reason is None and secs>=self.cfg.bail_secs:
            bail=False
            rec=[p for t,p in ph_t if t>t_sec-self.cfg.bail_secs]
            if rec and all(abs(p-ep)<=tick for p in rec): bail=True
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

        # FORCE_EXIT at 150s — market sell, no favourable rounding
        if not reason and secs>=150:
            exit_px=curr; reason='FORCE_EXIT'

        if reason:
            # Snapshot exit-time state now; the fill happens later (see run_backtest).
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
            snap=dict(x_pf=x_pf,x_bl=x_bl,x_pu=x_pu,x_dw=x_dw,
                      x_spx=self.spx(ds,t_sec) or 0.0,T_mins=T_mins,
                      signal_ts=ts,signal_px=curr,secs_at_signal=secs)
            if reason=='OSC_PROFIT':
                # resting limit, print went through it on THIS bar → fill at target now
                self._book_exit(st, trades, ds, ts, pos['exit_target'], reason, snap, 'LIMIT')
            else:
                pos['pending_exit']={'reason':reason,'snap':snap}

    # ── EXIT BOOKING (single place where a sell is priced) ────
    def _book_exit(self, st, trades, ds, ts, raw_px, reason, snap, kind):
        pos=st.position; ep=pos['entry_price']; qty=pos['qty']; tick=self.cfg.grid_tick
        exit_px=math.floor(raw_px/tick+1e-9)*tick - self.cfg.extra_slippage_ticks*tick
        exit_px=max(exit_px, 0.0)  # a worthless print is a worthless fill
        comm=qty*self.cfg.comm_per_contract*2
        pnl=(exit_px-ep)*qty*100-comm
        secs=(ts-pos['entry_time']).total_seconds()
        self.stats['exit_limit_fills' if kind=='LIMIT' else 'exit_market_fills']+=1
        rec=self._trade(st,pos,ds,ts,exit_px,pnl,reason,secs,
                        snap['x_pf'],snap['x_bl'],snap['x_pu'],snap['x_dw'],
                        snap['x_spx'],snap['T_mins'])
        rec['exit_kind']=kind
        rec['exit_signal_time']=snap['signal_ts']
        rec['exit_signal_px']=snap['signal_px']
        rec['exit_fill_delay']=(ts-snap['signal_ts']).total_seconds()
        rec['exit_slip_ticks']=round((snap['signal_px']-exit_px)/tick,1)
        trades.append(rec)
        st.last_exit_ts=ts
        st.last_exit_won = pnl > 0
        st.last_exit_price = exit_px
        st.position=None
        return pnl

    # ── TRADE RECORD ──────────────────────────────────────────
    def _trade(self, st, pos, ds, ts, xp, pnl, reason, secs,
               x_pf='N/A', x_bl=0.5, x_pu=0.0, x_dw=0.0,
               x_spx=0.0, T_mins=0.0):
        ep=pos['entry_price']
        entry_spx = pos.get('entry_spx', 0.0)
        exit_spx  = x_spx if (x_spx and x_spx > 1000) else entry_spx

        # BS Greeks at ENTRY
        sigma = self.vix / 100.0
        T_entry_mins = pos.get('T_mins_at_entry', 300)
        T_e = max(T_entry_mins, 5) / (252 * 390)
        ge = _bs_greeks(entry_spx, st.strike, T_e, RISK_FREE_RATE, sigma, st.direction) if entry_spx > 1000 else {}

        # BS Greeks at EXIT
        T_x = max(T_mins, 5) / (252 * 390)
        gx = _bs_greeks(exit_spx, st.strike, T_x, RISK_FREE_RATE, sigma, st.direction) if exit_spx > 1000 else {}

        return {
            'date':ds, 'strike':st.strike, 'direction':st.direction,
            'entry_time':pos['entry_time'], 'exit_time':ts,
            'hold_secs':secs, 'entry_price':ep, 'exit_price':xp,
            'gain_pct':(xp-ep)/(ep+1e-7)*100, 'qty':pos['qty'], 'pnl_usd':pnl,
            'exit_reason':reason, 'premium_band':pos.get('premium_band','?'),
            'conviction':pos.get('conviction','LOW'),
            # Volume + flow
            'vol_30s':pos.get('vol_30s',0),
            'is_elite_flow':pos.get('is_elite_flow',False),
            'peak_price':pos.get('peak_price',ep),
            'fill_delay':pos.get('fill_delay_secs',0),
            # Cone at entry
            'cone_blended':pos.get('cone_blended',0.5),
            'spread_ticks':pos.get('spread_ticks',0),
            'dual_agree':pos.get('dual_agree',False),
            'slope_confirms':pos.get('slope_confirms',False),
            # PFMACD
            'pf_dir':pos.get('pf_dir','N/A'), 'exit_pf_dir':x_pf,
            # Greeks at ENTRY
            'entry_delta':ge.get('delta',0.0), 'entry_gamma':ge.get('gamma',0.0),
            'entry_theta':ge.get('theta_day',0.0), 'entry_vega':ge.get('vega_1pct',0.0),
            'entry_vanna':ge.get('vanna',0.0),
            # Greeks at EXIT
            'exit_delta':gx.get('delta',0.0), 'exit_gamma':gx.get('gamma',0.0),
            'exit_theta':gx.get('theta_day',0.0), 'exit_vega':gx.get('vega_1pct',0.0),
            'exit_vanna':gx.get('vanna',0.0),
            # SPX + time reference
            'entry_spx':round(entry_spx,2), 'exit_spx':round(exit_spx,2),
            'T_mins_entry':round(T_entry_mins,1), 'T_mins_exit':round(T_mins,1),
            # Observation
            'venus_would_block':pos.get('venus_would_block',False),
            'spx_source':pos.get('spx_source','?'),
            # Fill harness
            'signal_price':pos.get('signal_price',ep),
            'signal_time':pos.get('signal_time',pos['entry_time']),
            'entry_fill_delay':pos.get('fill_delay_secs',0),
            'profit_gated':reason in ('CONE_EXIT','FLOW_EXIT','TRAIL_EXIT'),
            'placebo':self.cfg.placebo_rate is not None,
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
        bars10['date_str']=bars10['timestamp'].dt.strftime('%Y-%m-%d')
        bankroll=self.cfg.starting_capital

        for ds, _ in bars10.groupby('date_str'):
            if ds not in self.atm_map: continue
            spx_open=float(self.atm_map[ds])
            strikes=self._build_strikes(ds, spx_open)
            if not strikes: continue
            self._current_strikes = strikes  # MARS: for band counting in _entry
            self._pending = {}               # set below; band count includes resting orders

            day1s=(self.bars1s[self.bars1s['date_str']==ds]
                   .sort_values('timestamp'))
            if len(day1s)==0: continue

            # Market expiry = 4:15 PM ET
            day_date=pd.Timestamp(ds).date()
            expiry_et=pd.Timestamp(f'{ds} 16:15:00').tz_localize('America/New_York')
            concurrent=0
            five_min_bar_tracker={}  # strike → last 5m bar seen
            pending={}               # iid → resting BUY order (dict from _entry)
            self._pending=pending
            tick=self.cfg.grid_tick

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

                    # ── 1. PENDING SELL: fills on this bar (first print after the signal) ──
                    if st.position is not None and st.position.get('pending_exit') is not None:
                        pe=st.position['pending_exit']
                        raw=low if self.cfg.exit_market_px=='low' else close
                        pnl=self._book_exit(st, trades, ds, ts, raw, pe['reason'], pe['snap'], 'MARKET')
                        bankroll+=pnl; concurrent=max(0,concurrent-1)

                    # ── 2. PENDING BUY: fills only if a later print went below the limit ──
                    if iid in pending:
                        po=pending[iid]
                        age=(ts-po['ts']).total_seconds()
                        if self.cfg.fill_strict: hit = low < po['lim']-1e-9
                        else: hit = low <= po['lim']+1e-9 and volume>=self.cfg.min_fill_volume
                        if hit and st.position is None and concurrent<self.cfg.max_concurrent:
                            st.position={
                                'entry_price':po['lim'],'qty':po['qty'],'entry_time':ts,
                                'fill_delay_secs':age,'price_history':[po['lim']],
                                'last_bar_ts':ts,'direction':st.direction,
                                'peak_price':po['lim'],'swell_entry':po['swell'],
                                'entry_volume':volume,'exit_target':po['exit_target'],
                                'signal_time':po['ts'],
                                **{k:po[k] for k in [
                                    'real_dist','entry_spx','pf_dir','cone_blended','cone_ticks',
                                    'spread_ticks','fill_ratio','dual_agree','slope_confirms',
                                    'fast_blended','vol_30s','conviction','is_elite_flow',
                                    'bs_theoretical','bs_iv','bs_edge','bs_delta','bs_gamma',
                                    'premium_band','venus_would_block','T_mins_at_entry',
                                    'spx_source','signal_price','signal_t_sec',
                                ] if k in po},
                            }
                            concurrent+=1; self.stats['orders_filled']+=1
                            pending.pop(iid)
                        elif age>=self.cfg.entry_order_life_secs or not in_entry_window:
                            self.stats['orders_expired']+=1; pending.pop(iid)

                    st.update(close, open_, volume, t_sec=t_sec)
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

                    # EXIT check (always, even outside entry window).
                    # Decides a reason only; the sell fills on the NEXT bar (step 1 above).
                    if st.position is not None:
                        # a position filled on this very bar: don't evaluate exits on its fill bar
                        if st.position['entry_time']!=ts:
                            st.position['last_bar_ts']=ts
                            self._monitor(st, close, high, ts, ts_et, ds, trades,
                                         pf_hist, pf_rm, pf_slope, t_sec, T_mins)
                            if st.position is None:          # OSC_PROFIT limit filled this bar
                                concurrent=max(0,concurrent-1)
                                bankroll+=trades[-1]['pnl_usd'] if trades else 0

                    # ENTRY check → places a RESTING order, never a position
                    elif (iid not in pending and in_entry_window and
                          concurrent+len(pending)<self.cfg.max_concurrent and
                          close>=self.cfg.min_bid and
                          close<=self.cfg.max_bid):
                        bar_dict={'close':close,'open':open_,'high':high,'low':low}
                        po=self._entry(st, bar_dict, ts, ds, t_sec,
                                      pf_dir, pf_hist, pf_rm, pf_slope,
                                      bankroll, T_mins)
                        if po is not None:
                            pending[iid]=po
                            self.stats['orders_placed']+=1

            # EOD close all positions
            eod_ts=day1s['timestamp'].max()
            eod_t_sec=int(((eod_ts.tz_convert('America/New_York').hour-9)*3600+
                           (eod_ts.tz_convert('America/New_York').minute-30)*60))
            lpx=(day1s[day1s['close']>0]
                 .groupby('instrument_id')['close'].last().to_dict())
            self.stats['orders_cancelled']+=len(pending); pending.clear()
            llow=(day1s[day1s['close']>0]
                 .groupby('instrument_id')['low'].last().to_dict()) if 'low' in day1s.columns else lpx
            eod_et=eod_ts.tz_convert('America/New_York')
            eod_T_mins=max((expiry_et-eod_et).total_seconds()/60.0,5.0)
            eod_spx=self.spx(ds,eod_t_sec) or 0.0
            for iid,st in strikes.items():
                if st.position is None: continue
                pos=st.position
                # last print of the day for this instrument, bid-side proxy; 0 if none (expired worthless)
                raw=(llow if self.cfg.exit_market_px=='low' else lpx).get(iid, 0.0)
                pe=pos.get('pending_exit')
                reason=pe['reason'] if pe else 'EOD_CLOSE'
                snap=pe['snap'] if pe else dict(x_pf='EOD',x_bl=0.5,x_pu=0.0,x_dw=0.0,
                                               x_spx=eod_spx,T_mins=eod_T_mins,
                                               signal_ts=eod_ts,signal_px=lpx.get(iid,0.0),
                                               secs_at_signal=0)
                pnl=self._book_exit(st, trades, ds, eod_ts, raw, reason, snap, 'MARKET')
                bankroll+=pnl

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
  Orders placed          : {s['orders_placed']:,}   (signal passed all gates)
  Orders FILLED          : {s['orders_filled']:,}   ({s['orders_filled']/max(s['orders_placed'],1)*100:.1f}% of placed)
  Orders expired/cancel  : {s['orders_expired']:,} / {s['orders_cancelled']:,}
  Exit fills limit/market: {s['exit_limit_fills']:,} / {s['exit_market_fills']:,}
  SPX static fallback    : {s['spx_static_fallback']:,}   (no 5m SPX → day open used; Venus flag meaningless there)
  Placebo rejected       : {s['placebo_rejected']:,}
  No room (target<=bid)  : {s['no_room']:,}
  Mode                   : {'PLACEBO p=%.4f'%self.cfg.placebo_rate if self.cfg.placebo_rate is not None else 'REAL'}  fill_strict={self.cfg.fill_strict}  exit_px={self.cfg.exit_market_px}  slip={self.cfg.extra_slippage_ticks}tk

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
