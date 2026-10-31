# Task 010 verification — blueprint audit + build (Quill, 2026-10-31)

Basis: Claude drop `defa8f7` (from-claude tree `f31dc2fc`). Production code
checked at `origin/main` `c289c81`; `compass/gap_fade.py` and
`compass/ict_paper.py` are byte-identical at the live production revision
`ef016f1` (live `/health` revision confirmed 2026-10-31), so every code claim
below holds against production, not just the branch tip.

## 1a. The 10-27 gap scoring audit

- **Reproduction: CONFIRMED for the headline rows; audit method sound.**
  `audit_gap.py` merges the re-run against `sweep_stop2.csv` on (sym, name)
  and requires n equal + net `isclose`. I verified the five `gap_fade_0.5`
  rows directly against the release CSV — all exact:
  MES 45 / +$1,450 / t 1.12 (H1 +1,636, H2 −186);
  MNQ 55 / +$4,623 / t 1.46 (H1 +3,674, H2 +949);
  MYM 51 / −$572 / t −0.54 (H1 +202, H2 −773);
  MCL 65 / −$1,352 / t −1.01 (H1 −649, H2 −703);
  MGC 75 / +$5,554 / t 1.47 (H1 +5,114, H2 +440).
  The full 30-row re-run was not executed on this side (it needs Claude's VM
  data + harness); the script, the CSV, and the spot check support it.
- **Placebo critique: CONFIRMED — the 63 / 76.5 / 96 percentiles are
  withdrawn, concurred.** `sim.py:placebo()` draws, per real trade, a random
  bar on the same session date within the same hour, same direction, same
  stop/target geometry. Gap fade fires once a day at a fixed clock (08:31),
  so the null trade is nearly the same trade; the percentile discriminates
  nothing for this family. Note the withdrawal is **immaterial to the
  standing verdict**: no `gap_fade_0.5` cell cleared the non-placebo gate
  limbs in Task 006 anyway (MES n=45 < 50; MNQ t=1.46 < 1.5; MGC t=1.47 <
  1.5; MYM/MCL negative). Gap fade was never a survivor, with or without the
  placebo.
- **H2 correction: CONFIRMED** from the same CSV rows above — "flips in H2"
  was MES-only; MNQ (+949) and MGC (+440) were small-positive in H2. Accepted.
- **`audit_gap2.py` outputs: UNVERIFIED (not refuted).** The direction-shuffle
  percentiles (93.4 / 90.2 / 38.0 / 26.8 / 72.6), the MES top-3 = 93%
  concentration, and the small-gap control averages (−15.4 / +11.6 / +9.1 /
  −0.5 / −57.2) rest on the memo alone: the script was dropped, its output
  was not, and no per-trade gap list is on the desk to recompute from.
  Claude: drop the output text if these are to be cited. The conclusion they
  support (nothing clears 97.5 on a biting null) is consistent with the
  verified t-statistics (≤ 1.47 on the fade cells) but is not independently
  confirmed here.

## 1b. Production `compass/gap_fade.py` — four defects CONFIRMED, two precision notes

1. **Unsupported docstring: CONFIRMED.** `gap_fade.py:14` reads verbatim
   "Research: 67.8% win rate on MES/MNQ across 2,967 trades with proper
   filters." A grep of the desk and the repo finds no other occurrence and
   no supporting artifact. At the module's own 0.5–2.0% filter, qualifying
   days run to tens per year per symbol (the sweep's ≥0.5-ATR fade: 45–75/yr);
   2,967 MES/MNQ trades cannot come from the ≤ 1 year of micro bars the desk
   holds. Treat as unsupported — concurred.
2. **It can never book: CONFIRMED.** `ict_paper.STRATEGY_TAGS`
   (`ict_paper.py:27–49`) has no `gap_fade` entry, so `submit()` returns
   `unknown_detector` at `ict_paper.py:271–272` on every signal.
   `gap_fade` is also absent from `DETECTOR_FLAGS`, so `paper_enabled()`
   would return False as the next gate. Precision note: the module header
   declares "Evidence only… never trades," yet `scan()` calls `submit()` +
   `record_decline()` on every signal — the never-book outcome is guaranteed
   by the missing registration either way. `GAP_FADE_ENABLED` also defaults
   to false (`config.py:127`).
3. **Stop buffer: CONFIRMED in mechanism, OVERSTATED in consequence.**
   `buf = 0.25 × _atr(bars)`, and `_atr` averages the last 14 true ranges of
   the 1-minute bar window (`ict_bars`) — the buffer itself is 0.25 × a
   1-minute ATR14, as claimed. But total risk is not the buffer:
   `risk = |entry − stop|` = OR-extreme-to-gap-extreme distance + buffer,
   and the `ict_paper` guard compares total `risk_pts` against 0.5 × ATR14
   (`ict_paper.py:335–346`, ATR likewise from 1-minute bars). "The guardrail
   would decline most of them as `stop_too_tight`" is therefore not
   established — it declines only tight-geometry days — and the "~1–3 ticks
   on MES" buffer magnitude is unmeasured (no ATR distribution dropped).
   The core defect (a sub-ATR buffer built on a 1-minute ATR) stands; the
   decline-rate claim should be withdrawn or proven.
   One blocker Claude did not list: `detect()` admits R:R ≥ 1.0 (target =
   50% gap fill) while `submit()`'s global guard requires R:R ≥ 1.5 — even
   with a tag, a share of signals would decline `rr_too_low`.
4. **Re-fire + prior-close: CONFIRMED with one correction.** `detect()` has
   no upper time bound (only `now ≥ 08:45`), pins `entry` to the OR extreme
   regardless of current price, and sets `signal_ts = now`; `scan()` runs on
   a 120 s throttle and calls detect + submit every scan. Submit attempts
   therefore repeat every ~2 minutes all day — not "after every 15-min
   cooldown" (submit's 15-min cooldown only applies after a closed position,
   which can never exist here). If the tag were added, `existing_position`
   and `past_flatten` in `submit()` would bound actual bookings, but
   detect/scan themselves never stop firing. `_prior_rth_close_ts`
   subtracts a calendar day and sets 15:15, so Monday's "prior close"
   resolves to the last bar ≤ Sunday 15:15 — Friday's final bar, not Friday
   15:15; holidays have the same shape. Confirmed.

Disposition: gap fade has no live or paper seat and its flag defaults off —
no live decision changes. The delete-or-rewrite recommendation is noted;
that is a production change and is **Josh's call** (below). Nothing executed.

## 2. The transcript blueprint, built and scored

- **Full-blueprint table: RECOMPUTED EXACT** from `blueprint_trades.csv`
  (319 trades). All five rows match the memo and `blueprint_results.txt` on
  n / net / avg / WR / PF / DD / t / H1 / H2 / gap–ORB splits:
  MES 55 / +$459 / t 0.56; MNQ 77 / +$789 / t 0.39; MYM 61 / −$114 / t −0.19;
  MCL 78 / −$120 / t −0.19; MGC 48 / −$246 / t −0.16. The direction-shuffle
  percentiles also reproduce exactly by replicating `report()`'s seed-0
  sign-flip null on the CSV pnls (70.9 / 63.8 / 40.9 / 44.6 / 44.6), and the
  reason shares match the results file. Invariants in the CSV: hold ≤ 120
  bars on every trade (time stop binds), ≤ 1 trade/day/symbol under the
  regime switch, fills 08:45–10:52 CT.
- **`blueprint.py`: read line-by-line against the memo spec — faithful.**
  CFG matches the memo (limit fills only on trade-through by 1 tick at the
  limit price; stops −2 ticks; $3 RT; ORB stop = OR mid, target 2R, deadline
  11:00, 60-min validity; gap ≥ 0.5% + open beyond prior H/L, 25% retrace
  limit into the 08:30–08:45 bar, stop 1 tick past its extreme, target =
  prior RTH close, R:R ≥ 2, valid to 10:30; regime ATR5 > ATR20 and the chop
  floor both computed from prior sessions only — no look-ahead found in
  `session_table`). Two conservative orderings noted: the time-stop/flatten
  checks run before stop/target on their bar, and a position can stop out on
  its own fill bar.
- **Ablations + friction test: CODE-REVIEWED, NOT RECOMPUTED.** No
  per-ablation trade lists were dropped, so these rest on Claude's harness;
  every ablation figure in the memo matches `blueprint_results.txt` exactly
  and `run_blueprint.py`'s configs match their labels (regime-off MES
  +$1,482 / t 1.23, MNQ −$959, MGC −$3,297; transcript-gate gap fade stop
  rates 86–90%, WR 10–14%, only MES positive at +$198; ORB limit-retest MES
  +$302, MNQ −$5,922, MGC −$5,160; optimistic fill-at-level bound MES
  +$2,555 / t 1.77; friction +1.5 tk leaves MES +$272, MNQ +$691).
- **Pine mirror: read in full — faithful,** and its header honestly
  discloses the two fill-model differences (TradingView fills limits on
  touch vs the harness's trade-through; stops at the level vs −2 ticks).
  Not executed (no TradingView run on this side).

## Verdict

- **Concur with Claude's recommendation: no blueprint component earns a
  shadow slot.** On verified numbers the full blueprint is break-even noise
  on the equity-index micros (t ≤ 0.56) and negative on MYM/MCL/MGC; nothing
  clears a biting null; the regime switch does not sort days (regime-off
  improves MES). Same corridor result as Task 006's ORB/gap sweeps, now
  under the transcript's exact gates.
- The untested intraday spread leg (MES–MNQ / MGC–MCL) is noted; neither
  side has built it and no decision is needed now.

## Awaiting Josh

1. `compass/gap_fade.py`: delete it, or rewrite it (fix the docstring, the
   registration, the stop geometry, the re-fire, and the prior-close
   calendar bug) — a production change; not executed.
2. Nothing else in this drop needs a decision: no shadow slot is proposed
   by either agent, and no live or paper state changes.
