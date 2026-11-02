"""
hurricane_v2_oscillation.py
============================
Hurricane v2 — Pure Oscillation Scalper

ORIGIN: Watching SPX 0DTE option prices tick live in ToS.
A strike in the delta activation zone oscillates: 0.25 → 0.40 → 0.30 → 0.50.
Get in at the low, get out at the high, spam it until the strike dies,
move to the next one in the swell.

NO MACD. NO VWAP GRAVITY. NO COMPOSITE SCORES.

ARCHITECTURE:
  1. OSCILLATION DETECTOR
     Rolling 30s min/max per strike.
     Price at bottom 20% of range → BUY signal.
     Price at top 80% of range   → SELL signal.

  2. SWELL DETECTOR
     When lead strike (nearest OTM) starts moving up,
     flag the next 1-2 OTM strikes as "swell candidates".
     Enter those on their next dip within 10s.

  3. ENTRY
     Price ≤ 20th percentile of 30s rolling range → enter.
     Min price: $0.10. Max price: $2.00.
     Up to 20 contracts per entry.

  4. EXIT
     A) Price ≥ 80th percentile of entry-time range → PROFIT
     B) Price drops >1/3 in 5 consecutive seconds   → STOP
     C) 30s with no movement toward target           → BAIL

  5. BEST_BEFORE
     On stop: park contract. Watch for +35% recovery.
     Once armed: trailing stop (>1/3 drop in 5s).
     16:00 EOD: expire worthless.

Inputs:
  bars1s_df  : 1-second OHLCV with instrument_id, strike_price, instrument_class
  bars10s_df : 10-second bars (used only for ATM price anchor each 10s)
  atm_map    : {date_str: atm_price}
  oi_map_df  : OI weights per instrument per day

Usage:
  python 07_backtest.py   (no changes needed to backtest runner)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import math
import pandas as pd
import numpy as np


# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════
@dataclass
class OscConfig:

    # ── Sweet zone distance bands (OTM points from ATM) ───────────
    sz_dist_min   : float = 10.0    # closest OTM to watch
    sz_dist_max   : float = 75.0    # furthest OTM to watch

    # ── Premium filter ─────────────────────────────────────────────
    min_bid       : float = 0.10    # min $0.10 per contract
    max_bid       : float = 0.80    # max $0.80 -- oscillation only, no expensive contracts

    # ── Oscillation detector ───────────────────────────────────────
    osc_window    : int   = 30      # rolling window in seconds
    entry_pct     : float = 0.20    # buy at bottom 20% of range
    exit_pct      : float = 0.80    # sell at top 80% of range
    min_range_pct : float = 0.10    # range must be ≥10% of price to trade
                                    # avoids dead/illiquid strikes

    # ── Geiger Zone system ────────────────────────────────────────────
    # Like a metal detector: measures how much a strike is tilting
    # beyond its normal oscillation range.
    # Zone +2: blew past top → overheating, sell
    # Zone -2: broke below range → going cold, cut
    # Zone +1: top of range → normal profit exit
    # Zone -1: bottom of range → entry signal
    zone2_extension : float = 0.00  # zone+2 fires exactly at range hi
    zone_neg2_cut   : bool  = False  # velocity stop handles losses, not zone-2

    # ── Swell detector ─────────────────────────────────────────────
    swell_enabled       : bool  = True
    swell_lead_move_pct : float = 0.15   # lead strike up ≥15% → swell active
    swell_window_secs   : int   = 10     # enter swell strikes within 10s
    swell_otm_count     : int   = 2      # how many OTM strikes to flag

    # ── Position sizing ────────────────────────────────────────────
    max_contracts       : int   = 20     # max 20 contracts per entry
    max_premium_deploy  : float = 400.0  # max $ deployed per entry
    max_concurrent      : int   = 5      # max simultaneous open positions

    # ── Exit rules ────────────────────────────────────────────────
    bail_secs           : int   = 20     # no progress in 20s → bail (was 30)
    stop_window_secs    : int   = 5      # trailing stop window
    stop_fraction       : float = 0.333  # >1/3 drop in stop_window → stop

    # ── Parking filter ────────────────────────────────────────
    # Only park contracts ≤ this price. Above → immediate stop, no park.
    # Expensive stalled contracts don't recover -- cut them immediately.
    bb_max_park_price   : float = 0.80   # only park if entry_price ≤ $0.80

    # ── BEST_BEFORE ────────────────────────────────────────────────
    bb_arm_pct          : float = 0.35   # +35% to arm trailing stop
    bb_trail_window     : int   = 5      # trailing stop window (seconds)
    bb_trail_fraction   : float = 0.333  # >1/3 drop = sell

    # ── Commission (IBKR) ─────────────────────────────────────────
    comm_per_contract   : float = 0.65   # $0.65 per side per contract

    # ── Session gate ───────────────────────────────────────────────
    entry_start_et_h    : int   = 9
    entry_start_et_m    : int   = 30
    entry_stop_et_h     : int   = 15   # full day -- stop entries at 3:30 PM
    entry_stop_et_m     : int   = 30   # last entry 3:30, exits by 4:00

    # ── Capital ────────────────────────────────────────────────────
    starting_capital    : float = 10_000.0

    # ── Grid rounding ($0.05 tick) ────────────────────────────────
    grid_tick           : float = 0.05

    # ── Gap exit: strike went dark while holding ──────────────────
    gap_exit_secs       : float = 15.0    # no bar for 15s → exit


# ══════════════════════════════════════════════════════════════════════
# STRIKE STATE — tracks oscillation state per strike per day
# ══════════════════════════════════════════════════════════════════════
@dataclass
class StrikeState:
    instrument_id   : int
    strike          : float
    direction       : str        # 'C' or 'P'
    dist            : float      # OTM distance from ATM

    # Rolling price history (1s closes)
    prices          : list = field(default_factory=list)
    opens           : list = field(default_factory=list)

    # Active position
    position        : Optional[dict] = None

    # BEST_BEFORE bucket
    best_before     : list = field(default_factory=list)

    # Swell flag
    swell_until     : Optional[pd.Timestamp] = None

    def update(self, price: float, bar_open: float = None):
        self.prices.append(price)
        self.opens.append(bar_open if bar_open is not None else price)
        if len(self.prices) > 120:
            self.prices = self.prices[-120:]
        if len(self.opens) > 120:
            self.opens = self.opens[-120:]

    def rolling_stats(self, window: int, grid_tick: float = 0.05):
        """Returns (lo, hi, pct_in_range).
        Range from prior bars (no look-forward).
        Current bar's OPEN, rounded UP to nearest tick, used for pct."""
        if len(self.prices) < 4:
            return None, None, None
        prior = self.prices[-(window+1):-1] if len(self.prices) > window else self.prices[:-1]
        if len(prior) < 3:
            return None, None, None
        lo = min(prior)
        hi = max(prior)
        # Use current bar's open, rounded UP to nearest tick
        raw_open = self.opens[-1] if self.opens else self.prices[-1]
        curr_eval = math.ceil(raw_open / grid_tick) * grid_tick
        rng = hi - lo
        pct = (curr_eval - lo) / (rng + 1e-7)
        return lo, hi, pct

    def geiger_zone(self, window: int, zone2_ext: float = 0.50):
        """Returns zone (-2,-1,0,+1,+2), lo, hi, rng.
        Range from prior bars only (no look-forward)."""
        if len(self.prices) < 6:
            return 0, None, None, None
        prior = self.prices[-(window+1):-1] if len(self.prices) > window else self.prices[:-1]
        w_pos = [p for p in prior if p > 0]
        if len(w_pos) < 5:
            return 0, None, None, None
        lo   = min(w_pos)
        hi   = max(w_pos)
        rng  = hi - lo
        curr = self.prices[-1]
        if curr <= 0 or rng < lo * 0.05:
            return 0, lo, hi, rng
        if   curr >= hi + rng * zone2_ext: zone =  2
        elif curr >= hi:                   zone =  1
        elif curr <= lo - rng * zone2_ext: zone = -2
        elif curr <= lo:                   zone = -1
        else:                              zone =  0
        return zone, lo, hi, rng


# ══════════════════════════════════════════════════════════════════════
# MAIN STRATEGY
# ══════════════════════════════════════════════════════════════════════
class HurricaneV2:

    def __init__(self, config: OscConfig,
                 bars1s_df: pd.DataFrame,
                 bars10s_df: pd.DataFrame,
                 atm_map: dict,
                 oi_map_df: pd.DataFrame,
                 atm_10s_df: pd.DataFrame = None):

        self.cfg      = config
        self.bars1s   = bars1s_df
        self.bars10s  = bars10s_df
        self.atm_map  = atm_map
        self.oi_map   = oi_map_df

        # Index 1s bars by instrument_id for O(1) lookup
        print("  Indexing 1s bars by instrument_id...")
        ts_col = 'ts_event' if 'ts_event' in bars1s_df.columns else 'timestamp'
        self.bars1s = bars1s_df.rename(columns={ts_col: 'timestamp'})
        self.bars1s['timestamp'] = pd.to_datetime(self.bars1s['timestamp'], utc=True)
        self.bars1s['date_str']  = self.bars1s['timestamp'].dt.date.astype(str)

        self._by_iid = {}
        for iid, grp in self.bars1s.groupby('instrument_id'):
            self._by_iid[int(iid)] = grp.sort_values('timestamp').reset_index(drop=True)
        print(f"  {len(self._by_iid):,} instruments indexed")

        # OI map lookup
        self._oi_lookup = {}
        if oi_map_df is not None and len(oi_map_df) > 0:
            for _, r in oi_map_df.iterrows():
                self._oi_lookup[(str(r['date_str']), int(r['instrument_id']))] = float(r['oi_weight'])

        # Dynamic ATM from put-call parity
        self._atm_dynamic = {}
        if atm_10s_df is not None and len(atm_10s_df) > 0:
            atm_10s_df = atm_10s_df.copy()
            atm_10s_df['timestamp'] = pd.to_datetime(atm_10s_df['timestamp'], utc=True)
            for date_str, day in atm_10s_df.groupby('date_str'):
                day_sorted = day.sort_values('timestamp')
                self._atm_dynamic[date_str] = {
                    'timestamps': day_sorted['timestamp'].values,
                    'atm_values': day_sorted['atm_grid'].values,
                }
            print(f"  Dynamic ATM loaded: {len(self._atm_dynamic)} days")
        else:
            print(f"  Dynamic ATM not available — using static ATM map")

    def _get_atm(self, date_str: str, ts: pd.Timestamp = None) -> float:
        if ts is not None and date_str in self._atm_dynamic:
            dyn = self._atm_dynamic[date_str]
            ts_np = ts.to_datetime64()
            idx = np.searchsorted(dyn['timestamps'], ts_np, side='right') - 1
            if idx >= 0:
                return float(dyn['atm_values'][idx])
        return float(self.atm_map.get(date_str, 0))

    # ── Run backtest ───────────────────────────────────────────────
    def run_backtest(self) -> pd.DataFrame:
        trades = []
        bars10 = self.bars10s.sort_values('timestamp')
        bars10['date_str'] = bars10['timestamp'].dt.date.astype(str)

        for date_str, day10 in bars10.groupby('date_str'):
            if date_str not in self.atm_map:
                continue
            atm = self._get_atm(date_str)

            # Build per-strike state for this day
            strikes = self._build_strike_states(date_str, atm)
            if not strikes:
                continue

            # Get all 1s bars for this day, sorted
            day1s = self.bars1s[self.bars1s['date_str'] == date_str].sort_values('timestamp')
            if len(day1s) == 0:
                continue

            open_count = 0  # concurrent positions
            open_keys  = set()  # tracks (strike, direction) of open positions
            pending_entries = {}  # iid → pending order

            # Iterate second by second
            for ts, sec_bars in day1s.groupby('timestamp'):
                ts_et = ts.tz_convert('America/New_York')
                h, m  = ts_et.hour, ts_et.minute
                in_entry_window = (
                    (h == self.cfg.entry_start_et_h and m >= self.cfg.entry_start_et_m) or
                    (h > self.cfg.entry_start_et_h and
                     h < self.cfg.entry_stop_et_h) or
                    (h == self.cfg.entry_stop_et_h and m < self.cfg.entry_stop_et_m)
                )

                # Build price map for this second
                px_map = {}
                for _, row in sec_bars.iterrows():
                    iid = int(row['instrument_id'])
                    px_map[iid] = {
                        'close' : float(row['close']),
                        'open'  : float(row.get('open', row['close'])),
                        'high'  : float(row.get('high', row['close'])),
                        'low'   : float(row.get('low',  row['close'])),
                    }

                # ── Check pending ENTRY fills ─────────────────
                filled = []
                for p_iid, pord in list(pending_entries.items()):
                    pord['bars_waited'] += 1
                    if p_iid in px_map:
                        b = px_map[p_iid]
                        if b['low'] <= pord['limit_px'] <= b['high']:
                            st_fill = strikes.get(p_iid)
                            if st_fill and st_fill.position is None:
                                st_fill.position = {
                                    'instrument_id'  : p_iid,
                                    'strike'         : st_fill.strike,
                                    'direction'      : st_fill.direction,
                                    'entry_price'    : pord['limit_px'],
                                    'entry_time'     : ts,
                                    'signal_time'    : pord['signal_ts'],
                                    'fill_delay_secs': pord['bars_waited'],
                                    'qty'            : pord['qty'],
                                    'entry_lo'       : pord['entry_lo'],
                                    'entry_hi'       : pord['entry_hi'],
                                    'exit_target'    : pord['exit_target'],
                                    'last_progress_ts': ts,
                                    'price_history'  : [pord['limit_px']],
                                    'swell_entry'    : pord['swell_entry'],
                                }
                                open_count += 1
                                open_keys.add((st_fill.strike, st_fill.direction))
                            filled.append(p_iid)
                    if pord['bars_waited'] >= 7:
                        filled.append(p_iid)
                for p_iid in set(filled):
                    pending_entries.pop(p_iid, None)

                # Count open positions
                open_count = 0
                open_keys  = set()
                for s in strikes.values():
                    if s.position is not None:
                        open_count += 1
                        open_keys.add((s.strike, s.direction))

                # Process each strike
                for iid, st in strikes.items():
                    if iid not in px_map:
                        continue
                    bar = px_map[iid]
                    curr = bar['close']
                    if curr <= 0:
                        continue

                    # Update rolling history
                    st.update(curr, bar.get('open', curr))

                    # ── Monitor open position ──────────────────────
                    if st.position is not None:
                        st.position['last_bar_ts'] = ts  # track for gap exit
                        self._monitor_position(st, curr, bar['high'], ts, ts_et,
                                               date_str, trades)
                        if st.position is None:
                            open_count -= 1

                    # BEST_BEFORE disabled (Option C -- cut immediately)
                    # if st.best_before:
                    #     self._monitor_bb(st, curr, bar['high'], ts,
                    #                      date_str, trades)

                    # ── Entry check ────────────────────────────────
                    if (st.position is None and
                            iid not in pending_entries and
                            in_entry_window and
                            open_count < self.cfg.max_concurrent and
                            (st.strike, st.direction) not in open_keys and
                            self.cfg.min_bid <= curr <= self.cfg.max_bid):

                        lo, hi, pct = st.rolling_stats(
                            self.cfg.osc_window, self.cfg.grid_tick)
                        if lo is None:
                            continue

                        rng_pct = (hi - lo) / (lo + 1e-7)
                        if rng_pct < self.cfg.min_range_pct:
                            continue

                        swell_active = (st.swell_until is not None and
                                        ts <= st.swell_until)
                        entry_threshold = (0.35 if swell_active
                                           else self.cfg.entry_pct)

                        if pct <= entry_threshold:
                            # Grid snap: round open UP to nearest tick for eval,
                            # then aim at that tick (if on grid) or one below (if between)
                            tick = self.cfg.grid_tick
                            bar_open = bar.get('open', curr)
                            rounded_up = math.ceil(bar_open / tick) * tick
                            on_grid = abs(bar_open - round(bar_open / tick) * tick) < 0.001
                            limit_px = rounded_up if on_grid else rounded_up - tick
                            limit_px = max(limit_px, self.cfg.min_bid)

                            qty = self._size_qty(limit_px)
                            if qty > 0:
                                pending_entries[iid] = {
                                    'limit_px'   : limit_px,
                                    'qty'        : qty,
                                    'signal_ts'  : ts,
                                    'bars_waited': 0,
                                    'entry_lo'   : lo,
                                    'entry_hi'   : hi,
                                    'exit_target': lo + (hi-lo)*self.cfg.exit_pct,
                                    'swell_entry': swell_active,
                                }

                    # ── Swell detection ────────────────────────────
                    if self.cfg.swell_enabled and st.position is not None:
                        self._check_swell(st, curr, ts, strikes, atm)

            # EOD: force-close all open positions at last known price
            eod_ts = day1s['timestamp'].max()
            # Build last known price per instrument from actual bar data
            last_px_map = (
                day1s[day1s['close'] > 0]
                .groupby('instrument_id')['close']
                .last()
                .to_dict()
            )
            for iid, st in strikes.items():
                if st.position is not None:
                    ep      = st.position['entry_price']
                    # Use last real bar price, fall back to entry_price if none
                    ep_exit = last_px_map.get(iid, ep)
                    qty     = st.position['qty']
                    comm    = qty * self.cfg.comm_per_contract * 2
                    pnl     = (ep_exit - ep) * qty * 100 - comm
                    secs    = (eod_ts - st.position['entry_time']).total_seconds()
                    trades.append(self._make_trade(
                        date_str, st, st.position, eod_ts,
                        ep_exit, pnl, 'EOD_CLOSE', secs
                    ))
                    st.position = None

        return pd.DataFrame(trades)

    # ── Build strike states for one day ───────────────────────────
    def _build_strike_states(self, date_str: str, atm: float) -> dict:
        # Find all instruments active on this day with bars
        day_bars = self.bars1s[self.bars1s['date_str'] == date_str]
        if len(day_bars) == 0:
            return {}

        # Get definitions from bars (strike_price, instrument_class already merged)
        if 'strike_price' not in day_bars.columns:
            return {}

        seen = {}
        for iid, grp in day_bars.groupby('instrument_id'):
            iid = int(iid)
            row = grp.iloc[0]
            strike = float(row.get('strike_price', 0))
            cls    = str(row.get('instrument_class', ''))
            if not strike or not cls:
                continue
            dist = abs(strike - atm)
            if dist < self.cfg.sz_dist_min or dist > self.cfg.sz_dist_max:
                continue
            # Price filter: check if this strike ever traded in our range
            prices = grp['close'].values
            prices = prices[prices > 0]
            if len(prices) == 0:
                continue
            median_px = np.median(prices)
            if median_px > self.cfg.max_bid * 3:
                continue  # way too expensive, skip
            seen[iid] = StrikeState(
                instrument_id=iid,
                strike=strike,
                direction=cls,
                dist=dist,
            )
        return seen

    # ── Monitor active position ────────────────────────────────────
    def _monitor_position(self, st: StrikeState, curr: float,
                          high: float, ts: pd.Timestamp,
                          ts_et, date_str: str, trades: list):
        pos = st.position
        ep  = pos['entry_price']
        qty = pos['qty']
        gain = (curr - ep) / (ep + 1e-7)
        secs = (ts - pos['entry_time']).total_seconds()

        # Update price history for trailing stop
        pos['price_history'].append(curr)
        if len(pos['price_history']) > self.cfg.stop_window_secs + 5:
            pos['price_history'] = pos['price_history'][-(self.cfg.stop_window_secs+5):]

        reason = None
        exit_px = curr

        # Geiger zone for current price
        zone, z_lo, z_hi, z_rng = st.geiger_zone(
            self.cfg.osc_window, self.cfg.zone2_extension)

        # A) Zone +2: tilting hard past top → overheating, sell
        if zone == 2:
            exit_px = curr
            reason  = 'ZONE2_EXIT'

        # B) Normal profit: hit exit target (zone +1 territory)
        elif curr >= pos['exit_target'] or high >= pos['exit_target']:
            exit_px = pos['exit_target']
            reason  = 'OSC_PROFIT'

        # C) Zone -2: dead volcano → cut immediately, no park
        elif zone == -2 and self.cfg.zone_neg2_cut:
            exit_px = curr
            reason  = 'ZONE2_CUT'

        # D) Big gust: >100% → trailing stop
        elif gain >= 1.00:
            if len(pos['price_history']) >= self.cfg.stop_window_secs:
                ref = pos['price_history'][-self.cfg.stop_window_secs]
                if ref > 0 and curr < ref * (1 - self.cfg.stop_fraction):
                    reason  = 'GUST_TRAIL'
                    exit_px = curr

        # E) Scaled velocity stop: drop must exceed tick threshold
        #    $0.10-0.20 entry → need $0.10 drop (2 ticks)
        #    $0.20-0.35 → $0.10 drop
        #    $0.35-0.50 → $0.10 drop
        #    $0.50-0.80 → $0.15 drop (3 ticks)
        #    $0.80-1.25 → $0.20 drop (4 ticks)
        #    >$1.25     → $0.25 drop (5 ticks)
        elif len(pos['price_history']) >= self.cfg.stop_window_secs:
            ref = pos['price_history'][-self.cfg.stop_window_secs]
            drop = ref - curr
            if ep <= 0.20:
                stop_thresh = 0.10
            elif ep <= 0.35:
                stop_thresh = 0.10
            elif ep <= 0.50:
                stop_thresh = 0.10
            elif ep <= 0.80:
                stop_thresh = 0.15
            elif ep <= 1.25:
                stop_thresh = 0.20
            else:
                stop_thresh = 0.25
            if drop >= stop_thresh:
                reason = 'STOP'

        # F) Time bail: strike is dead. Two ways to be dead:
        #    1) Price stuck within ±1 tick of entry for bail_secs
        #    2) No bar arrived for gap_exit_secs (strike went dark)
        elif secs >= self.cfg.bail_secs:
            bail = False
            # Check flat price
            tick = self.cfg.grid_tick
            recent = pos['price_history'][-self.cfg.bail_secs:] if len(pos['price_history']) >= self.cfg.bail_secs else pos['price_history']
            all_flat = all(abs(p - ep) <= tick for p in recent)
            if all_flat:
                bail = True
            # Check gap (no bar for too long)
            last_ts = pos.get('last_bar_ts')
            if last_ts is not None:
                gap = (ts - last_ts).total_seconds()
                if gap >= self.cfg.gap_exit_secs:
                    bail = True
            if bail:
                reason  = 'TIME_BAIL'
                exit_px = curr

        # H) HARD 150s FORCE EXIT — no exceptions, no context.
        #    Sell at nearest tick down (pressure-aware).
        if not reason and secs >= 150:
            tick = self.cfg.grid_tick
            # If price is dropping (last 3 bars falling), sell one tick below
            hist = pos['price_history']
            if len(hist) >= 3 and hist[-1] <= hist[-2] <= hist[-3]:
                exit_px = (curr // tick) * tick  # snap DOWN — pressure
            else:
                exit_px = math.ceil(curr / tick) * tick  # snap UP — normal
            exit_px = max(exit_px, tick)  # never below one tick
            reason = 'FORCE_EXIT'

        if reason:
            # Grid snap exit price UP to nearest tick
            tick = self.cfg.grid_tick
            exit_px = math.ceil(exit_px / tick) * tick

            # Option C: cut immediately on all exits, no parking
            comm       = qty * self.cfg.comm_per_contract
            entry_comm = qty * self.cfg.comm_per_contract

            pnl  = (exit_px - ep) * qty * 100
            pnl -= (entry_comm + comm)
            trades.append({
                'date'        : date_str,
                'strike'      : st.strike,
                'direction'   : st.direction,
                'dist_otm'    : st.dist,
                'entry_time'  : pos['entry_time'],
                'exit_time'   : ts,
                'hold_secs'   : secs,
                'entry_price' : ep,
                'exit_price'  : exit_px,
                'gain_pct'    : (exit_px - ep) / (ep + 1e-7) * 100,
                'qty'         : qty,
                'pnl_usd'     : pnl,
                'exit_reason' : reason,
                'swell_entry' : pos.get('swell_entry', False),
                'fill_delay'  : pos.get('fill_delay_secs', 0),
            })
            st.position = None

    # ── Monitor BEST_BEFORE bucket ─────────────────────────────────
    def _monitor_bb(self, st: StrikeState, curr: float,
                    high: float, ts: pd.Timestamp,
                    date_str: str, trades: list):
        still_bb = []
        for pk in st.best_before:
            ep   = pk['entry_price']
            gain = (curr - ep) / (ep + 1e-7)
            g_hi = (high  - ep) / (ep + 1e-7)

            # Arm on high
            if not pk['armed'] and g_hi >= self.cfg.bb_arm_pct:
                pk['armed']       = True
                pk['peak_price']  = high
                pk['price_history'] = [curr]

            if pk['armed']:
                if high > pk['peak_price']:
                    pk['peak_price'] = high
                pk['price_history'].append(curr)
                win = self.cfg.bb_trail_window
                if len(pk['price_history']) > win + 2:
                    pk['price_history'] = pk['price_history'][-(win+2):]

                if len(pk['price_history']) >= win:
                    ref = pk['price_history'][-win]
                    if ref > 0 and curr < ref * (1 - self.cfg.bb_trail_fraction):
                        # Trailing stop fired
                        ep_entry = pk['entry_price']
                        qty = pk['qty']
                        pnl = (curr - ep_entry) * qty * 100
                        pnl -= qty * self.cfg.comm_per_contract  # exit comm only
                        secs = (ts - pk['entry_time']).total_seconds()
                        trades.append({
                            'date'        : date_str,
                            'strike'      : st.strike,
                            'direction'   : st.direction,
                            'dist_otm'    : st.dist,
                            'entry_time'  : pk['entry_time'],
                            'exit_time'   : ts,
                            'hold_secs'   : secs,
                            'entry_price' : ep_entry,
                            'exit_price'  : curr,
                            'gain_pct'    : (curr - ep_entry)/(ep_entry+1e-7)*100,
                            'qty'         : qty,
                            'pnl_usd'     : pnl,
                            'exit_reason' : 'BEST_BEFORE_SOLD',
                            'swell_entry' : pk.get('swell_entry', False),
                        })
                        continue  # don't re-add to still_bb

            still_bb.append(pk)
        st.best_before = still_bb

    # ── Swell detection ────────────────────────────────────────────
    def _check_swell(self, lead_st: StrikeState, curr: float,
                     ts: pd.Timestamp, strikes: dict, atm: float):
        if len(lead_st.prices) < 2:
            return
        prev = lead_st.prices[-2]
        move = (curr - prev) / (prev + 1e-7)
        if move >= self.cfg.swell_lead_move_pct:
            # Flag next N OTM strikes as swell candidates
            swell_end = ts + pd.Timedelta(seconds=self.cfg.swell_window_secs)
            otm_sorted = sorted(
                [(s.dist, iid) for iid, s in strikes.items()
                 if s.dist > lead_st.dist and
                 s.direction == lead_st.direction],
                key=lambda x: x[0]
            )
            for _, iid in otm_sorted[:self.cfg.swell_otm_count]:
                strikes[iid].swell_until = swell_end

    # ── Size quantity ──────────────────────────────────────────────
    def _size_qty(self, price: float) -> int:
        if price <= 0:
            return 0
        qty_by_capital = int(self.cfg.max_premium_deploy / (price * 100))
        qty = min(qty_by_capital, self.cfg.max_contracts)
        return max(qty, 1)

    # ── Trade record helper ────────────────────────────────────────
    def _make_trade(self, date_str, st, pos, ts, exit_px, pnl, reason, secs):
        ep = pos['entry_price']
        return {
            'date'        : date_str,
            'strike'      : st.strike,
            'direction'   : st.direction,
            'dist_otm'    : st.dist,
            'entry_time'  : pos['entry_time'],
            'exit_time'   : ts,
            'hold_secs'   : secs,
            'entry_price' : ep,
            'exit_price'  : exit_px,
            'gain_pct'    : (exit_px - ep) / (ep + 1e-7) * 100,
            'qty'         : pos['qty'],
            'pnl_usd'     : pnl,
            'exit_reason' : reason,
            'swell_entry' : pos.get('swell_entry', False),
        }
