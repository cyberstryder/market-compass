# Task 012 verification — Hurricane v2 origin note (Quill, 2026-11-02)

Checked against the two desk copies AND Josh's three originals on Quill's side
(`hurricane_v2_oscillation-787deployed-161kprofit-May9th21h50.py`,
`hurricane_v2_oscillation-May13th.py`, `hurricane_v2_final_150s-May13th.py`).

## Confirmed

- **Provenance / byte identity.** Desk copies are byte-identical to Josh's originals:
  May-9 md5 `7b72ad21d4b3e98b7e45c197e27babac`, May-13 md5 `980b0e68371900d01fef941601c5276b`.
  The two May-13 originals (oscillation and final_150s) are byte-identical to each other
  (same md5). Both desk files pass `py_compile`.
- **May-9 = May-13 minus FORCE_EXIT, exactly.** 713 vs 724 lines; the entire diff is the
  11-line FORCE_EXIT block at May-13 `:582-593`. No other difference.
- **Config, verbatim in both files:** 10–75 pts OTM (`:64-65`), $0.10–$0.80 (`:68-69`),
  bottom-20% entry (`entry_pct 0.20`, `:73`), target `lo + 0.8·range` (`exit_pct 0.80`,
  `:74`, `:419`), up to 20 contracts / $400 per entry (`:95-96`), 5 concurrent (`:97`),
  $10k start (`:124`), $0.65/side (`:115`). Entry evaluation uses the bar open rounded
  UP to the tick against the prior range (`rolling_stats`, `:164-180`).
- **Entry fill line, exact:** pending fill at `:318` in both files —
  `low <= limit_px <= high` on a later bar, `bars_waited` recorded as
  `fill_delay_secs`, 7-bar life (`:340`). No volume condition anywhere in either
  file (zero `volume` hits). Moonraker's `volume >= 3` confirmed at
  moonraker.py:437 (`min_fill_volume = 3`, :260).
- **Exit rounding lines, exact:** shared monitor snap `exit_px = ceil(exit_px/tick)·tick`
  at May-9 `:587` / May-13 `:598`. `OSC_PROFIT` at `:519-521` fills at `exit_target`
  on `curr >= target or high >= target`, no volume.
- **No PFMACD (audit defect 6 does not exist here), no MACD/cone/flow pricing:**
  the only such hit is the header's "NO MACD. NO VWAP GRAVITY. NO COMPOSITE SCORES."
- **Window semantics (defect 7 applies):** `osc_window = 30` indexes the last 30
  entries of a per-strike price list appended once per 1-second bar that EXISTS —
  not wall-clock 30 s and not exchange prints. Claude's "30-print" is the right
  direction; strictly it is 30 bars-present.
- **Tick math:** one $0.05 tick at 20 contracts = $0.05 × 100 × 20 = $100. Correct.

## Corrections — three claims do not survive the code

1. **The entry limit is NOT "ceil(open/tick)·tick − 1 tick".** At `:405-408`:
   `limit_px = rounded_up if on_grid else rounded_up - tick`. On-grid opens fill at
   the open itself; off-grid opens get the tick below the rounded-up value, i.e.
   0 to <1 tick under the open — never a guaranteed full tick. This is the grid
   snap Moonraker kept, not Earth/Mars's 1–4-tick-under limit.
2. **"Defects 1–4 apply verbatim" is wrong for #1 and #4, and overbroad for #3.**
   - #1 (instant fill): does NOT apply. v2 entries ARE the honest resting-limit
     model — later-bar range containment with the wait recorded — that
     Moonraker–Venus kept and Earth/Mars deleted. What v2 lacks is only the
     volume ≥ 3 condition.
   - #2 (no spread): applies verbatim.
   - #3 (sells rounded up): applies to monitor exits (lines above), but NOT to
     every sell — v2 `EOD_CLOSE` uses the last real close raw, with no ceil snap
     (unlike v3's EOD).
   - #4 (profit-gated exits): does NOT apply. There is no CONE/FLOW exit and no
     `is_profitable` gate in v2; ZONE2 / OSC_PROFIT / STOP / TIME_BAIL are not
     profit-gated. (The only profit-conditioned path is GUST_TRAIL, which arms
     only at gain ≥ 100% before trailing.)
3. **The $161k attribution does not sum.** Claude's own $50–100/trade × 787 =
   $39,350–$78,700 — a quarter to a half of $161k, not "the headline."
   $161k / 787 ≈ $205/trade ≈ 2 ticks at the maximum 20 contracts on every
   trade, while the combined entry+exit rounding is bounded at <2 ticks and
   quantity is `min(20, int(400/(price·100)))` — 5 contracts at a $0.80 entry,
   so a tick is worth $25–$100, not always $100. Rounding × size is a major
   inflation source; it is not a proven full accounting of the headline.

## Unverifiable from the drop

- **787 trades / $161k themselves:** filename only. No scoreboard, trade list,
  or output artifact in either file (zero hits for 787/161 in the code).
- **"FORCE_EXIT was added because positions were sitting":** the code comment
  says only "HARD 150s FORCE EXIT — no exceptions, no context." The motive is
  plausible inference, not in the artifact. Precision on the mechanism itself:
  the down-snap condition is the last three price-history closes
  non-increasing (`hist[-1] <= hist[-2] <= hist[-3]`, per-bar closes, not prints),
  and the subsequent shared ceil at `:598` does not undo it because the floor
  value is already on-grid. Claude's mechanism description otherwise stands.
- **"~1 h port":** an estimate. The honest-fill Mars harness does carry the
  same-named `rolling_stats` / `geiger_zone` (`mars_fills.py:455/465`), so a
  port is plausible, but v2's ladder differs (scaled tick-threshold stop,
  flat/gap TIME_BAIL, FORCE_EXIT, and a swell overlay Claude's note omits:
  `swell_enabled`, entry threshold relaxing to 0.35 for swell-flagged strikes
  at `:395-398`) — it is an adaptation, not a drop-in.

## Disposition

**Concurred on the core, with the corrections above.** v2 is the origin, its
monitor exits carry the same ceil-rounding + touch-fill + no-spread defects as
v3, and it is the cleanest statement of the oscillation thesis — but its ENTRY
model is the honest one the later builds removed, so "same fill model as
everything after it" (the commit subject) is not accurate for entries.
Sequencing concurred: no v2 run is started; the frozen settling test stays on
Venus/Earth/Mars, and any v2 honest-fill + placebo run is a new registration
for Josh to call, queued behind the Mars catalog decision that is already
awaiting him. Nothing here touches Compass production or live money.
