# Medusa / Chimera New Uploads — Triage (medusa-gate-runner.zip, Chimera-Medusa-v1.zip)

**Date:** 2026-11-17. **Sources:** both zips as uploaded 2026-11-17 09:40 CT, extracted to `hidden_files/medusa-new-uploads-2026-11-17/` (one subdir per zip). Every extracted file's size verified against the zip listing — no zero-byte surprises (the only 0-byte files are the three normal empty `__init__.py`). All code and docs read. **Triage only: nothing was run, backtested, traded, or sent anywhere.** The frozen Chimera forward test (base + B1+C + A, registration 2026-11-15, week 1 = Nov 16–20 running) is untouched by anything here.

## Bottom line

- **Most of both zips is verbatim repackaging** of material already triaged: the oracle, the gate, and the entire Chimera package are byte-identical to the Nov 15 / Chimera.rar copies (verified by direct file comparison, and the v1 tree's SHA-256 hashes all match its own FROZEN.md).
- **Two things are genuinely new:** (1) a completed gated-vs-ungated test of Medusa's own signal with a **negative verdict supplied by the author's side itself** — the ungated classifier's 15:15 sign accuracy is a coin flip (49.2%) and the gate makes it worse (45.7%); and (2) **flipper mod (a) finally built** — the Medusa 11-state classifier wired into Chimera as its intraday regime source (`medusa_adapter.py`, the "Chimera-Medusa v1" merge), with an in-sample, Black-Scholes-priced A/B whose own Phase 2 follow-up was tested and rejected by the author's side.
- **No evidence files ship in either zip** — no trade logs, no results JSON/CSV, no screenshots. Every number rests on the summary docs. Both tests are, however, reproducible from what's included plus data already on our disk.

## Zip 1 — medusa-gate-runner.zip (6 files, 153,018 bytes)

A runnable terminal package for Medusa's EOD signal: classifier + regime gate + a live "today" runner + the historical gate test. Signal only.

| File | Size | Lines | Purpose |
|---|---|---|---|
| `medusa_oracle.py` | 79,248 | 1,644 | The Medusa oracle v2.5-fix3 (11-state classifier `classify_regime` / `classify_periods`, brief generator). **Byte-identical to the 2026-11-15 upload** (md5-verified against `~/workspace/user/files/medusa_oracle.py`). |
| `gate_as_uploaded.py` | 24,365 | 569 | The regime gate exactly as uploaded. **Byte-identical to `medusa_regime_gate.py` from 2026-11-15.** |
| `gate_calibrated.py` | 25,778 | 586 | The same gate with ONE change: the spacelike bar filter recalibrated (see below). Everything else — osmosis thresholds 0.35/0.55/0.72, WAIT at 0.28, BEAR_3/BULL_3 immunity — is the author's, unchanged (51-line diff, confined to the header + `compute_bar_weights`). |
| `decide_today.py` | 6,811 | 164 | New. Fetches today's SPX 5-min bars from yfinance, classifies, applies the calibrated gate, prints the 15:15 ET decision (PRELIMINARY before 15:15). Signal only. Its own docstring carries the negative test verdict. |
| `run_gate_test.py` | 14,506 | 320 | New. The gated-vs-ungated historical test harness (spec below). |
| `README.md` | 2,310 | — | Package readme; leads with the desk's own test verdict (quoted below). |

### Is the "gate-runner" flipper mod (a)?

**No — not the Chimera gate.** Mod (a) was an 11-regime gate *for Chimera's intraday entries*. This package is about **Medusa's own once-a-day 15:15 decision**: it takes the regime gate triaged on Nov 15 (spacelike filter + contrarian-pressure osmosis + WAIT) and wires it to the oracle's `classify_periods()` composite at the real decision time, 15:15 ET. Inputs: a day's 5-min bars (SPX via yfinance live, or the pinned SPY dataset historically), daily ATR(17), VIX, prior-day score/MACD context. Outputs: the (possibly osmosed) state, direction, and WAIT/stand-down. It does not plug into Chimera at all — the Chimera integration is zip 2.

### What's new in it

1. **The gate test itself (the headline).** `run_gate_test.py` scores the decision→close residual only (15:15→16:00), direction from the state, WAIT days stood down and reported, no threshold tuning anywhere. Datasets: 499 SPY sessions (the pinned Alpaca 1-min bundle, aggregated to 5-min — SPY points, ~SPX/10 scale) + ~59 SPX days from yfinance. The README's verdict, quoted exactly:

   > - The ungated classifier's 3:15 PM ET sign accuracy was **49.2%** — a
   >   coin flip. There is no measured edge in the signal.
   > - The gate made it **worse**: 45.7% sign, net −24 pts vs ungated. It
   >   stood down 69 correct bear calls while fixing 52 wrong ones.
   > - The 5× spacelike calibration changed zero decisions in 558 days.

   And: "Run the machinery to see how it works; don't mistake it for a proven strategy." This is the author's side reporting a negative result on its own system, unprompted.
2. **The uploaded gate's spacelike filter was dead code.** As shipped, it compared a log-return fraction against ATR/78 *in points* — the units never meet, so it could never fire (the harness's sanity check asserts the uploaded weights are >0.999 on synthetic data, i.e. dead). The calibrated copy fixes the units (points velocity, limit 5 × ATR/√78) — and per the verdict above, the fixed filter still changed zero decisions in 558 days.
3. **Two wiring defects fixed** in the harness: the gate is called with keyword arguments (the docstring's positional call misbinds `trend_clarity` into the unused `s0` slot), and `trend_clarity` is passed raw (the oracle stores the weighted contribution, ÷0.10 to recover it). The gate is also applied at 15:15 with its late window = 14:15→15:15, not the docstring's 13:30→14:30 framing.
4. One convention difference to note: the test anchors S0 at the day's **open**; the author's own brief anchors S0 at the prior close. States can differ between anchors (disclosed in both README and `decide_today.py`).

### Evidence

None inside the zip: no day-level CSVs (the harness writes them when run), and the "full write-up: gate-test-summary" is explicitly "(in the desk copy)" — not included. **But the claim is cheaply checkable**: the harness is here, and the 499-day SPY dataset it needs is the bundle already on our disk. Sign accuracy on the residual is also a weaker test than a full option replay — it says nothing about strike selection or premiums — but a coin-flip sign on the underlying's last-45-minutes move leaves essentially nothing for the option layer to rescue after spreads.

## Zip 2 — Chimera-Medusa-v1.zip (38 files, 210,931 bytes; `share-v1/`)

The merged system: **Chimera v1 with its regime module swapped for the Medusa classifier** — i.e. flipper mod (a), built. Frozen by the author's side on 2026-11-16 for their own 20-trading-day forward test (see below).

| File | Size | Purpose |
|---|---|---|
| `FROZEN.md` | 4,009 | Frozen config, the author's pre-registered forward-test protocol, SHA-256 of every file. **All hashes verified against the extracted tree — exact match.** |
| `README.md` | 3,402 | What v1 is, how to run, reference results, caveats. |
| `merge-phase1-summary-2026-11-16.md` | 3,619 | Phase 1 build + A/B results (below). |
| `merge-phase2-summary-2026-11-16.md` | 2,578 | Phase 2 rotation-rule variants — tested, **rejected** (below). |
| `medusa_adapter.py` | 8,695 | **The only genuinely new engine code.** Aggregates 1-min bars into completed 5-min bars, classifies day-to-date with Medusa (`classify_periods()` composite, `classify_regime()` fallback), maps the 11 states onto Chimera's 5 (score = `effective_score`; NEUTRAL → UNKNOWN, which blocks new entries), refreshes on 5-min boundaries, and feeds `ChimeraEngine.update_regime()`. Also carries the Phase 2 `rotate_only_when_trending` flag, default OFF. |
| `medusa_oracle.py` | 79,248 | Same oracle, byte-identical (also identical across both zips). Only its classifier functions are used. |
| `chimera/` (9 .py + .pyc) | — | The Chimera v1 package. **Every .py byte-identical to the Chimera.rar extraction triaged 2026-11-15** (cmp-verified: `__init__`, `smoke_test`, `core/config`, `core/types`, `engine/chimera`, `engine/inventory`, `signals/cones`, `signals/regime`). |
| `replay_ab.py` | 8,834 | New A/B harness: baseline vs merged on the last 5 trading days, same data/config, cost grid applied after. yfinance data. |
| `replay_recent.py` | 10,365 | The original baseline replay, byte-identical to the RAR copy. |

### Engine diff vs the previously triaged Chimera

There is nothing to diff — the engine is the same code. Entry rules (cone/regime agreement, high-confidence gate, sweet-delta band 0.22–0.32), exit rules (`cone_target_hit` at spot ±8 fwd target, `regime_reversal` at |score| ≥ 0.30 against the position, `disagreement_rotation` after 2 disagreeing reads, `session_flatten` 15:55) are all unchanged, because the files are unchanged. The **only** behavioral change in v1 is the regime source: Medusa's 11-state, 5-min, day-to-date composite instead of Chimera's 4/5-state, trailing-41×1-min classifier. Their mapping choices (11→5 state map, `effective_score` as the score, NEUTRAL blocks entries, daily ATR(17)) were the choices the Nov 15 triage said would have to be pre-registered as ours if we built it — the author's side has now made and frozen its own version of them.

**Pricing: Black-Scholes model prices throughout** — entries at mid, IV = VIX/100 flat, exits at BS price, zero commission in-engine (a $3.10/RT + spread grid is applied to the trade list afterwards). FROZEN.md and the README both state this plainly: "This tests the REGIME LAYER, not fills." It is **not** real bid/ask — and real bid/ask is exactly where our honest replay showed Chimera's apparent edge dying.

### Claims and their evidence

In-sample reference (Nov 10/11/12/13/16, 2026 — the days v1 was built on; FROZEN.md labels them "the tuning set, NOT evidence"):

> Baseline: cone targets 188 @ +$58,194; regime reversals 205 @ -$32,530.
> Merged:   cone targets 134 @ +$41,597; regime reversals 8 @ -$2,467;
>           disagreement rotations 141 @ -$21,631.
> With $3.10/RT commission + spread: baseline breakeven $0.173/side,
> merged breakeven $0.249/side (net @ $0.20/side: baseline -$2,547,
> merged +$2,828).

Totals: baseline +$17,968 / 476 RT → merged +$15,241 / 288 RT (per-RT $37.7 → $52.9). The regime swap did what it was aimed at — regime-reversal exits collapsed 205 → 8 — **and the bleed moved**: disagreement rotations went 70 @ −$6,403 → 141 @ −$21,631. Gross fell slightly on 40% fewer trades.

Phase 2 (their own follow-up, reported as a negative result): delaying rotation to 5 reads, gating rotation to trending states, or both — none fixed the rotation bucket; Nov 10 got worse under the delay variants. Their standing conclusion, quoted: "Rotation exits are a symptom, not the disease: rotated heads were genuinely wrong-side, cut at ~−$153 avg — the same economics as the baseline's regime-reversal exits (~−$159 avg). The loss is baked in at ENTRY." Phase 2 is OFF in the frozen v1.

Feed-drift caveat (theirs, and creditable): yfinance revises recent 1-min bars — identical frozen code on two pulls 15 minutes apart moved the merged total +$15,241 → +$17,536 (288 → 282 RT). Their forward protocol pins each day's bars to disk at capture.

**Evidence in the zip: none.** The referenced `ab-results-2026-11-16.json` and `phase2-results-2026-11-16.json` live in the author's workspace and are not included. The numbers rest on the two summary documents. (One consistency note in their favor: the Phase 1 baseline reproduced their standalone run exactly, +$17,968 / 476 RT, and the buckets match the shape of our own replay — cone targets strongly positive, the reversal bucket deeply negative — at BS prices.)

### The author's own forward test — separate from ours

FROZEN.md pre-registers a 20-trading-day forward test starting **Tue Nov 17, 2026** (expected verdict Tue Dec 15), both engines run after each close on that day's fresh yfinance bars, days journaled once, never re-run. PASS (all three): merged net@$0.20 > 0; merged net@$0.20 > baseline net@$0.20; merged breakeven ≥ $0.20/side. It judges the regime layer at model prices only, by its own statement.

**This does not change, overlap, or contaminate our frozen forward test** (base + B1+C + A at real OPRA prices, 8 scored weeks, Saturday cron, week 1 already running). v1 is not one of our configs and must not be folded in mid-test. Flag only the naming collision: two different "forward tests" on this system start the same week — ours (real prices, 8 weeks) and theirs (model prices, 20 days).

## Safety scan (both zips)

Clean. No credentials, API keys, tokens, passwords, or webhook URLs anywhere; no environment-variable reads; no hardcoded key-like strings. No broker or order-placement code — the Chimera engine "emits Decision objects only," the gate package is explicitly signal-only, and both READMEs say so. The only network use is yfinance market-data downloads (oracle, `decide_today.py`, `replay_recent.py`, and the test harness). One portability wart, not a safety issue: `medusa_adapter.py` hardcodes a `sys.path` insert for the author's local `~/workspace/medusa/incoming-2026-11-16/` directory.

## Against the prior record — what changes, what doesn't

- **Supersedes one open item from the 2026-11-15 Medusa triage:** "Medusa's own tradable claim remains entirely unmeasured." It has now been measured — by the author's side, on sign accuracy — and there is no edge in it (49.2% ungated; the gate worse). Pending our reproduction of their harness, the standalone-Medusa question looks answered without us spending anything on it.
- **Confirms the Nov 15 gate flags:** the BEAR_3/BULL_3 "100% accuracy" immunity is still asserted and still unevidenced; the MC-tuned thresholds now have an out-of-sample-style test and fail it. New since Nov 15: the spacelike mechanism the triage described as designed **never actually fired as shipped** (units bug).
- **Corroborates our variant-A readout from a second direction:** swapping the regime layer relocates Chimera's bleed (there: regime_reversal → disagreement_rotation; ours: regime_reversal → disagreement_rotation as well) rather than removing it, and the author's Phase 2 conclusion — loss baked in at entry — matches.
- **Mod (a) is no longer hypothetical:** it exists as frozen code with the author's own mapping choices. That removes the "taxonomy not supplied / choices would be ours" blocker noted on Nov 15 — if it is ever tested here, the adapter *as frozen* is the testable artifact.
- **Nothing here touches any delivered verdict:** Chimera base loses at real prices; C not supported; B1 damage-reducer only; B1+C approximately breakeven; A supported with caveats; A-strict not supported. Nothing here is priced at real bid/ask, so nothing here can overturn a real-price verdict.

## Honest assessment — what, if anything, is worth testing (separate proposals only)

1. **Reproduce the gate test (cheap, high value, no new approval of substance — it's a verification, not a new strategy test).** Run `run_gate_test.py` against the SPY bundle already on disk: CPU only, no data spend. If 49.2% / 45.7% reproduces, Medusa standalone is closed on the author's own evidence and the never-approved standalone test never needs deciding. If it doesn't reproduce, that discrepancy is itself the finding to relay.
2. **Chimera-Medusa v1 through the honest-replay machinery — the only version of a v1 test that would mean anything, and only as a NEW frozen proposal for Josh.** Their A/B is BS-priced on 5 in-sample days; our whole Chimera record says BS pricing is where the phantom edge lives. The archived DBIX + OPRA weeks (Nov 2–6, Nov 9–13) could price v1 at real bid/ask the same way base/B1+C/A were priced. Prior expectation should be low: their own bucket analysis shows the bleed relocating, not shrinking, and Phase 2 already failed to fix it. It must not be added to the running forward test.
3. **Nothing else.** `decide_today.py` is a viewer for a signal with a measured coin-flip record; the gate calibration changed zero decisions in 558 days; Phase 2 is the author's own rejected work. And per standing instructions, nothing from this triage gets sent to the author — the relay-draft decision already pending with Josh is a separate item.
