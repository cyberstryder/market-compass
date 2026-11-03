"""
mid_info_test.py — does the Hurricane signal carry ANY information about the option mid?
=========================================================================================
Claude, 2026-11-02. For Quill to run against the settling-test artifacts (not on the desk).

Question (one that the settling test did not need to answer, because nothing survived):
  after each Hurricane signal, does the quoted MID of that strike move in the trade's
  favour over the next 10 / 30 / 60 s, by more than a random second on the same strike
  and day would? Mid-to-mid removes spread, fees, fill existence and exit logic — it is
  the signal alone.

Answer shape, per build:
  mean / median signed Δmid at +10, +30, +60 s, in $0.05 ticks and in % of entry mid;
  the same for a matched random-second null (same instrument, same day, same count);
  real − null, with a day-clustered t.
  Also split by: elite flow (vol_30s >= 100) and by band / premium tier where present.

Reading:
  real − null  ≈ 0 ticks                → no information; close the series.
  real − null  > 0 but < 1 tick (+ fees) → information exists, not monetisable as a taker here;
                                           next test is the same signal against ES 1-s mid.
  real − null  ≥ 1 tick at +30 s         → re-open; a passive/maker expression might exist.

INPUTS (adapt the two loader functions; everything else is generic):
  1. Signal set: one row per authored OR executable signal. Use the AUTHORED signal set
     (every signal the strategy produced), not just executable fills — the fill filter is
     itself adversely selected and would understate the signal. Required columns:
        date (YYYY-MM-DD), instrument_id (as in cbbo), t_sec (seconds from 09:30 ET of
        the SIGNAL bar, not the fill), direction ('C'/'P'), and optionally vol_30s,
        premium_band / premium_tier, build.
     If only the executable trade CSVs exist, pass those with --signal-col; results are
     then "fill-conditional" and the report says so.
  2. cbbo-1s per day: parquet with instrument_id, ts_event (UTC ns or datetime),
     bid_px_00, ask_px_00 (Databento OPRA cbbo-1s, levels[0]). Carry-forward NBBO,
     px > 0 = valid — same semantics as the settling test (D7–D12).

USAGE
  python3 mid_info_test.py --signals signals_*.parquet --cbbo-dir ./cbbo/ --out mid_info_results
  Options: --horizons 10,30,60   --null-draws 1   --seed 7   --min-mid 0.05   --fill-conditional
  Output: <out>.csv (per-row Δmid), <out>_summary.csv, printed table.
"""
import argparse, glob, sys
import numpy as np, pandas as pd

TICK = 0.05

def load_signals(paths, fill_conditional):
    df = pd.concat([pd.read_parquet(p) if p.endswith('.parquet') else pd.read_csv(p) for p in paths],
                   ignore_index=True)
    # tolerate the trade-CSV column names from the harness / settling test
    ren = {}
    if 't_sec' not in df.columns:
        for c in ['signal_t_sec', 'entry_t_sec']:
            if c in df.columns: ren[c] = 't_sec'; break
    if 'date' not in df.columns and 'date_str' in df.columns: ren['date_str'] = 'date'
    df = df.rename(columns=ren)
    if 't_sec' not in df.columns:
        tcol = 'signal_time' if 'signal_time' in df.columns else 'entry_time'
        ts = pd.to_datetime(df[tcol], utc=True).dt.tz_convert('America/New_York')
        df['t_sec'] = (ts.dt.hour - 9) * 3600 + (ts.dt.minute - 30) * 60 + ts.dt.second
        if 'date' not in df.columns: df['date'] = ts.dt.strftime('%Y-%m-%d')
    need = {'date', 'instrument_id', 't_sec', 'direction'}
    miss = need - set(df.columns)
    if miss: sys.exit(f'signals missing columns: {miss}; have {list(df.columns)}')
    df['date'] = df['date'].astype(str).str[:10]
    df['instrument_id'] = df['instrument_id'].astype(np.int64)
    df['t_sec'] = df['t_sec'].astype(int)
    if 'build' not in df.columns: df['build'] = 'ALL'
    df['fill_conditional'] = fill_conditional
    return df

def load_cbbo_day(cbbo_dir, date):
    cands = sorted(glob.glob(f'{cbbo_dir}/*{date}*.parquet')) or sorted(glob.glob(f'{cbbo_dir}/*{date.replace("-","")}*.parquet'))
    if not cands: return None
    q = pd.concat([pd.read_parquet(p) for p in cands], ignore_index=True)
    bid = next(c for c in ['bid_px_00', 'bid_px', 'bid'] if c in q.columns)
    ask = next(c for c in ['ask_px_00', 'ask_px', 'ask'] if c in q.columns)
    tcol = 'ts_event' if 'ts_event' in q.columns else 'timestamp'
    ts = pd.to_datetime(q[tcol], utc=True) if not np.issubdtype(q[tcol].dtype, np.integer) \
         else pd.to_datetime(q[tcol], unit='ns', utc=True)
    et = ts.dt.tz_convert('America/New_York')
    q = pd.DataFrame({'instrument_id': q['instrument_id'].astype(np.int64),
                      't_sec': ((et.dt.hour - 9) * 3600 + (et.dt.minute - 30) * 60 + et.dt.second).astype(int),
                      'bid': q[bid].astype(float), 'ask': q[ask].astype(float)})
    q = q[(q.t_sec >= 0) & (q.t_sec < 23400)]
    # Databento fixed-price scaling if present
    if q['ask'].abs().max() > 1e6: q['bid'] /= 1e9; q['ask'] /= 1e9
    q.loc[q.bid <= 0, 'bid'] = np.nan; q.loc[q.ask <= 0, 'ask'] = np.nan
    q = q.sort_values(['instrument_id', 't_sec']).drop_duplicates(['instrument_id', 't_sec'], keep='last')
    return q

def mid_series(q_inst):
    """Dense 0..23399 carry-forward mid for one instrument. NaN until first two-sided quote."""
    s = pd.Series(np.nan, index=np.arange(23400))
    two = q_inst.dropna(subset=['bid', 'ask'])
    if len(two) == 0: return s
    s.loc[two.t_sec.values] = ((two.bid + two.ask) / 2).values
    return s.ffill()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', nargs='+', required=True)
    ap.add_argument('--cbbo-dir', required=True)
    ap.add_argument('--out', default='mid_info_results')
    ap.add_argument('--horizons', default='10,30,60')
    ap.add_argument('--null-draws', type=int, default=1)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--min-mid', type=float, default=0.05)
    ap.add_argument('--fill-conditional', action='store_true',
                    help='signals file is the executable TRADE set, not the authored signal set')
    a = ap.parse_args()
    H = [int(h) for h in a.horizons.split(',')]
    rng = np.random.default_rng(a.seed)
    sig = load_signals(a.signals, a.fill_conditional)
    print(f'signals: {len(sig):,} rows, {sig.date.nunique()} days, builds {sorted(sig.build.unique())}')

    rows = []
    for date, sd in sig.groupby('date'):
        q = load_cbbo_day(a.cbbo_dir, date)
        if q is None: print(f'  {date}: no cbbo file, skipped ({len(sd)} signals)'); continue
        by_inst = {i: g for i, g in q.groupby('instrument_id')}
        mids = {}
        for iid, g in sd.groupby('instrument_id'):
            if iid not in by_inst: continue
            m = mid_series(by_inst[iid]); mids[iid] = m
            valid_t = np.flatnonzero(~np.isnan(m.values))
            valid_t = valid_t[(valid_t >= 60) & (valid_t < 23400 - max(H) - 1)]
            for _, r in g.iterrows():
                t0 = r.t_sec
                m0 = m.iloc[t0] if 0 <= t0 < 23400 else np.nan
                if np.isnan(m0) or m0 < a.min_mid: continue
                base = dict(date=date, instrument_id=iid, build=r.build, direction=r.direction,
                            t_sec=t0, mid0=m0, kind='REAL',
                            vol_30s=r.get('vol_30s', np.nan), band=r.get('premium_band', r.get('premium_tier', '')))
                for h in H:
                    mh = m.iloc[t0 + h] if t0 + h < 23400 else np.nan
                    base[f'd{h}'] = (mh - m0) if not np.isnan(mh) else np.nan   # long premium: + is favourable
                rows.append(base)
                # matched null: same instrument, same day, random valid second(s)
                for k in range(a.null_draws):
                    if len(valid_t) == 0: break
                    tn = int(rng.choice(valid_t)); mn0 = m.iloc[tn]
                    if np.isnan(mn0) or mn0 < a.min_mid: continue
                    nb = dict(base, t_sec=tn, mid0=mn0, kind='NULL')
                    for h in H:
                        mh = m.iloc[tn + h] if tn + h < 23400 else np.nan
                        nb[f'd{h}'] = (mh - mn0) if not np.isnan(mh) else np.nan
                    rows.append(nb)
        print(f'  {date}: {len(sd):,} signals, {len(mids)} instruments with quotes')

    df = pd.DataFrame(rows)
    if df.empty: sys.exit('no rows — check instrument_id alignment between signals and cbbo')
    for h in H:
        df[f'd{h}_ticks'] = df[f'd{h}'] / TICK
        df[f'd{h}_pct'] = df[f'd{h}'] / df['mid0'] * 100
    df['elite'] = df['vol_30s'] >= 100
    df.to_csv(f'{a.out}.csv', index=False)

    def summarise(g, label):
        out = {'cohort': label, 'n_real': int((g.kind == 'REAL').sum()), 'n_null': int((g.kind == 'NULL').sum())}
        for h in H:
            r = g[g.kind == 'REAL']; n = g[g.kind == 'NULL']
            out[f'real_mean_ticks_{h}'] = r[f'd{h}_ticks'].mean()
            out[f'real_median_ticks_{h}'] = r[f'd{h}_ticks'].median()
            out[f'null_mean_ticks_{h}'] = n[f'd{h}_ticks'].mean()
            out[f'diff_ticks_{h}'] = out[f'real_mean_ticks_{h}'] - out[f'null_mean_ticks_{h}']
            out[f'real_pct_{h}'] = r[f'd{h}_pct'].mean()
            out[f'null_pct_{h}'] = n[f'd{h}_pct'].mean()
            out[f'real_up_frac_{h}'] = (r[f'd{h}'] > 0).mean()
            out[f'null_up_frac_{h}'] = (n[f'd{h}'] > 0).mean()
            # day-clustered t on the real−null difference of daily means
            dm = (r.groupby('date')[f'd{h}_ticks'].mean() - n.groupby('date')[f'd{h}_ticks'].mean()).dropna()
            out[f't_day_{h}'] = dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm))) if len(dm) > 2 and dm.std() > 0 else np.nan
            out[f'days_{h}'] = len(dm)
        return out

    S = []
    for b, g in df.groupby('build'):
        S.append(summarise(g, f'{b}:ALL'))
        for d, gg in g.groupby('direction'): S.append(summarise(gg, f'{b}:{d}'))
        if g['elite'].notna().any() and g['vol_30s'].notna().any():
            S.append(summarise(g[g.elite], f'{b}:ELITE'))
            S.append(summarise(g[~g.elite], f'{b}:NON_ELITE'))
        if (g['band'] != '').any():
            for bd, gg in g[g['band'] != ''].groupby('band'): S.append(summarise(gg, f'{b}:band_{bd}'))
    summ = pd.DataFrame(S); summ.to_csv(f'{a.out}_summary.csv', index=False)

    fc = ' (FILL-CONDITIONAL: executable trade set, understates the raw signal)' if a.fill_conditional else ''
    print(f'\nMID-TO-MID INFORMATION TEST{fc}\n' + '-' * 100)
    print(f"{'cohort':<22}{'n_real':>8}{'n_null':>8}" + ''.join(f"{'Δ'+str(h)+'s real':>12}{'null':>8}{'diff tk':>9}{'t_day':>7}" for h in H))
    for _, r in summ.iterrows():
        line = f"{r.cohort:<22}{r.n_real:>8}{r.n_null:>8}"
        for h in H:
            line += f"{r[f'real_mean_ticks_{h}']:>12.3f}{r[f'null_mean_ticks_{h}']:>8.3f}{r[f'diff_ticks_{h}']:>9.3f}{r[f't_day_{h}']:>7.2f}"
        print(line)
    print('\nunits: $0.05 ticks of option MID, signed in the trade\'s favour (long premium). 1 tick spread + $1.30 fees ≈ 1.26 ticks/contract to beat as a taker.')
    print(f'[Saved] {a.out}.csv  {a.out}_summary.csv')

if __name__ == '__main__':
    main()
