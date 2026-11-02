# Task 012 verification — Hurricane v3 fill-model audit (Quill, 2026-11-02)

Checked against Josh's originals on file (`hurricane_v3_{moonraker,mercury,venus,earth,mars}.py`,
`07_backtest_mars-richesse.py` in Quill's user-files copy — the same six files Claude audited).
Independently consistent with Quill's own 2026-11-02 Hurricane verdict, reached before this drop.

## Confirmed (line-checked against the originals)

- **#1 Instant fill (Earth, Mars).** Mars `run_backtest` creates the position on the signal bar at
  `po['lim']` with `'fill_delay_secs': 1` hard-coded (mars.py:1139–1166); `fill_ts` is computed and
  never used. Earth is the same pattern (earth.py:1057/1063). Entry limit at mars.py:804–807
  (`rup = ceil(open/tick)·tick`, on-grid or one tick under, minus `cone_ticks`).
  Moonraker's `pending` model confirmed at moonraker.py:433–467: fill only on a later bar with
  `low <= lim <= high and volume >= 3` (`min_fill_volume = 3`, :260), 300-bar life.
- **#2 No bid/ask anywhere.** Trade prints serve as both sides; only commission is modeled. Confirmed by inspection.
- **#3 Sells rounded up.** Universal `exit_px = ceil(exit_px/tick)·tick` at mars.py:941 and EOD
  `xp = ceil(xp/tick)·tick` at :1179. `OSC_PROFIT` at :886–887 fills at `exit_target` on
  `curr >= target or high >= target`, no volume requirement.
- **#4 Profit-gated exits.** `CONE_EXIT` gated on `gain >= cone_exit_min_gain` (0.05, :332/:912);
  `FLOW_EXIT` and `TRAIL_EXIT` gated on `is_profitable` (`curr > ep`, :856/:868/:922). 100% WR on
  those rows is by construction, as Claude states.
- **#5 SPX fabricated in the runner.** `07_backtest_mars-richesse.py:62–83` exactly as described:
  Jan-2025 parquet pattern rescaled to the May-2026 open and force-mapped onto
  `sorted(dates)[0]` only. The gravity thesis was not tested against real SPX in that runner.
- **#6 PFMACD look-ahead.** `get()` at mars.py:200–204 selects the bucket `k <= t6` — the 6-s
  bucket still in progress.
- **#7 Print-count windows.** `vol_last_n_secs` (mars.py:409–411) sums `self.volumes[-n:]` —
  the last n *prints*, despite the docstring "True volume traded in last N seconds."
- **#8 DeltaWave change.** Earth/Mars assign the local `prev_close = close` before computing
  reach, so reach is `high − close` / `close − low` of the current bar; Moonraker uses the prior
  close. Confirmed in all three files.
- **#9(b) No room check.** `exit_target = lo + 0.8·(hi − lo)` (mars.py:736) can sit at/below the
  entry limit; the originals book it as an instant `OSC_PROFIT`. The patch's `no_room` counter
  addresses a real hole.
- **#10 Cosmetic.** Runner prints "$1000" start (:99) against `1_000_000 + tp` end (:111) and
  saves `*_earth_*` filenames (:213) — all confirmed.

## One precision correction — #9(a), the float example as written does not reproduce

`math.ceil(0.30/0.05)` in CPython float64 is **6**, not 7: `0.30/0.05 = 5.999999999999999`,
so `rup` for a float64 0.30 print is 0.30, not a 0.35 bid. The mechanism is real only when
`bar_open` arrives slightly *above* 0.30 — e.g. a float32-sourced 0.3000000119 from parquet
(ratio 6.0000002 → ceil 7). So: defect class stands, the literal example needs the
float32-source qualifier, and the patch's `round(bar_open/tick, 6)` guard (patched file :872)
is the right fix either way. This does not change the ranking — #1–#3 carry the verdict.

## The patched harness — what could and could not be checked

- Both dropped files pass `py_compile`. The claimed fixes are present on inspection: resting
  buy limit filling only on a later bar with `low < lim` (strict; `--relaxed` = `low <= lim`
  and volume ≥ 3), 300 s order life, next-bar floor-rounded market exits, `OSC_PROFIT` as a
  resting sell limit on `high > target`, PFMACD lagged to `k < t6` (:227–228), `no_room`
  counter (:878–881), `profit_gated` / `entry_fill_delay` trade fields, placebo mode.
- **Smoke-test numbers are UNVERIFIABLE from the desk:** 771 placed / 204 filled (26%),
  median fill delay 127 s, CONE_EXIT WR 100%→58%, ~4.5 min/pass — no synthetic dataset,
  generator script, or output CSV was included in the drop. They are plausible and consistent
  with the fill model, but they stand on Claude's say-so until an artifact lands.
- `mars_fills.diff` (26 hunks) carries Claude's local paths in its headers
  (`/mnt/user-data/uploads/hurricane_v3_mars.py`), so it does not apply verbatim against the
  desk/user-file layout without renaming. Reference diff only; the patched module itself is complete.

## Overlap with work already in motion (for Josh, not a blocker)

Quill's frozen Hurricane settling test (registration 2026-11-02, Josh-approved $143.49) is
already pulling the definitive data Claude lists under "Not done": Databento `cbbo-1s` BBO
quotes (buy at ask / sell at bid) plus ohlcv-1s bars over 20 frozen sessions (2026-10-05→10-30),
run for Venus **and** Earth **and** Mars. Claude's harness is the complementary piece for
Josh's May catalog: a trade-print proxy, weaker than BBO fills, but the only route that
re-tests the original May sample. No catalog is on the desk, so Quill cannot run it here;
nothing in this drop needs a production change, a merge, or live money.

## Disposition

**Concurred.** The 64–72% WR series is a harness property; Earth-vs-Mars is undecidable until
fills are honest — same conclusion Quill reached independently this morning. Awaiting Josh:
whether he wants the May catalog published to the desk (Claude runs the harness) or runs
`07_backtest_mars_fills.py` locally — and the settling test's BBO results will supersede the
proxy either way on the October sample.
