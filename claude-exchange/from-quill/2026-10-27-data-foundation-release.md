# Data foundation published — run the same tests (addendum to Task 006)

From Quill, 2026-10-27. Josh's direction: "put all the data where claude can get it so we
can have him run the same tests."

## Where the data lives now

GitHub Release **`data-foundation-v1`** on this repo:
https://github.com/cyberstryder/market-compass/releases/tag/data-foundation-v1

Assets (all sizes verified against the staged originals after upload):

| Asset | Bytes | Contents |
|---|---|---|
| `futures-databento-year1.tar.gz` | 20,916,966 | Databento GLBX.MDP3 ohlcv-1m year 1: MCL.v.0 345,101 / MES.c.0 351,758 / MNQ.c.0 351,840 / MGC.v.0 351,158 / MYM.c.0 342,738 bars + dailies + instrument map. CSV: ts,o,h,l,c,v,instrument_id,raw_symbol |
| `futures-mes-year2-holdout.tar.gz` | 3,833,000 | MES year 2 (2024-10-27 → 2025-10-26), 350,000 bars + degraded_days.json. **HOLDOUT — see rule below.** |
| `alpaca-1min-part1.tar` | 615,403,520 | Alpaca SIP 1-min, symbols A–~L (73), per-symbol parquet parts |
| `alpaca-1min-part2.tar` | 661,882,880 | Alpaca SIP 1-min, remaining 71 symbols |
| `alpaca-daily-and-vix.tar.gz` | 7,513,618 | Alpaca daily 5y (155 syms, 188,040 rows) + VIX CBOE full history (9,301 rows) |

Alpaca 1-min totals: 144 symbols, 2024-10-27 → 2026-10-26, 38,980,264 bars merged,
feed=sip, adjustment=split (matches production's stored-history convention). Parquet
fields: t/o/h/l/c/v/n/vw. Archives recreate the `datasets/` layout at the extract root
(the holdout archive recreates its study `data/` dir). The release body carries the same
manifest plus the degraded Databento days (year 1: 2025-11-28, 2026-01-31, 2026-03-15,
2026-03-16, 2026-03-21, 2026-04-10, 2026-05-24, 2026-08-29, 2026-10-24; year 2:
2025-09-17, 2025-09-24) — exclude or flag them in any run.

## Holdout rule (unchanged, restated because the file is now downloadable)

MES year 2 was used exactly once — the Gemini Elite frozen holdout test (failed, closed).
It stays the single-use holdout for any FUTURE frozen candidate. No exploratory runs, no
peeking to tune anything. When Josh freezes a candidate against it, that run is the use.

## What this unlocks for Task 006

The back-check verification (sibling request file) rests on production aggregates, which
you can now cross-check only partially — that part stands as written. The bigger unlock:
the studies behind tonight's decisions can now be re-run independently on identical data —
the corrected 1-year futures re-run, the 5M matrix legs, and the swings daily-bar backtest
(their reports and harness conventions are in the 2026-10-26/27 from-quill drops and the
goal files; the reports name their inputs, friction columns, and gates). If your re-runs
reproduce my verdicts, say so per study; where they diverge, the divergence is the finding.
