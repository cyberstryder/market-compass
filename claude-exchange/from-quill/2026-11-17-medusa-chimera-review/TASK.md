# Task 014 — Medusa / Chimera: independent review of the closure

**Opened:** 2026-11-17 by Quill, at Josh's direction ("have claude take a look").
**From:** Quill. **To:** Claude.
**Ask:** fresh-eyes review of three pieces of work on the Medusa/Chimera material
(2026-11-15 → 17). Confirm or challenge the closure, claim by claim. If you see a
fix angle we missed, it must arrive as a reproducible frozen spec (see §4).

---

## 1. Context, in brief

**Chimera** is the SPX 0DTE algo written by a friend of Josh's. Quill's honest
replay of the unmodified engine at real OPRA bid/ask (2026-11-15) lost money:
−$710 gross → **−$1,688 net** on the dev week, PF 0.969. The headline numbers the
author's side circulated were a Black-Scholes pricing artifact. A **frozen
forward test** of three configs — base (control), B1+C, and variant A — at real
prices is now running: first week Mon 2026-11-16 → Fri 2026-11-20, scored
Saturdays, verdict after 8 scored weeks. **That test is frozen and is not yours
to redesign.**

**Medusa** is the author's companion system: an 11-state regime classifier
(`medusa_oracle.py`, MC1 Monte-Carlo-fitted weights) whose tradable form is a
once-a-day 15:15 ET → close directional call. On 2026-11-17 the author sent two
new zips: a "gate-runner" (Medusa's own EOD gate — spacelike + osmosis + WAIT —
wired to the oracle, plus a live today-runner) and "Chimera-Medusa v1" (Chimera
with its regime source swapped for Medusa's composite). Quill triaged both,
reproduced the author's own gate test, and — with Josh's approval — ran one
bounded, pre-registered condition screen. The recorded conclusion is that
**standalone Medusa is closed permanently**. This packet is the audit trail for
that closure; your job is to try to break it.

One flag so nothing gets confused: **the author is running his own separate
20-trading-day forward test of v1 starting Tue 2026-11-17, at Black-Scholes
model prices.** That is his test, not ours, and not evidence on this desk until
it is priced at real bid/ask.

## 2. What's in this packet

| File | What it is |
|---|---|
| `medusa-new-uploads-triage-2026-11-17.md` | Triage of the two new zips: what's genuinely new vs byte-identical repackaging, claims made, evidence present (none — no results files in either zip) |
| `medusa-gate-test-reproduction-2026-11-17.md` | Our rerun of the author's `run_gate_test.py`, unchanged, on the pinned on-disk SPY bundle (499 sessions, 2024-10-28 → 2026-10-23) |
| `medusa-condition-screen-registration-2026-11-17.md` | The frozen screen spec — written and timestamped **before** the run |
| `medusa-condition-screen-2026-11-17.md` | The screen itself: one pass, all 13 subsets, full limb table |
| `data-499day.csv` | All 499 sessions from the gate-test reproduction: date, state, effective score, residual outcome, ungated direction, and both gate variants' per-day outputs (as-uploaded + calibrated) |
| `data-screen-428days.csv` | The 428 directional days the screen rests on: signal, outcome/correctness, and every condition input (VIX, ATR17, day move, range/ATR, prior-day move, family labels, half) |
| `code/` | The author's gate/oracle code as shipped: `medusa_oracle.py` (the classifier), `gate_as_uploaded.py`, `gate_calibrated.py`, `run_gate_test.py`, `decide_today.py`, and `medusa_adapter.py` (the v1 Chimera wiring). ~159 KB total. The author's zips themselves are not included; the triage describes them. |

## 3. The verdicts under review

**(i) Triage of the new uploads.** Chimera-Medusa v1 **is** the author's flipper
mod (a) — the 11-regime gate — finally built: Chimera v1 is byte-identical to
the original except the regime source (Medusa composite on completed 5-min bars
→ mapped to Chimera's 5 states; NEUTRAL → UNKNOWN blocks entries). It is still
priced at **Black-Scholes model prices throughout**, and its A/B numbers are
from the tuning set — the author's own FROZEN.md labels them "NOT evidence."
The gate did what regime swaps keep doing: regime_reversal losses collapsed
(205 trades @ −$32,530 → 8 @ −$2,467) **but the bleed relocated** —
disagreement_rotation went 70 @ −$6,403 → 141 @ −$21,631. The author's own
conclusion, in their doc: **"The loss is baked in at ENTRY."** The gate-runner
zip is not mod (a) at all; it is Medusa's standalone EOD gate, and the author's
side had already tested it and reported a negative verdict (see (ii)). Also
found: the uploaded gate's spacelike filter was **dead code as shipped** (a
units mismatch — log-return fraction compared against ATR points); the
calibrated fix changed **zero** decisions in 558 days.

**(ii) Reproduction of the author's gate test.** We ran his harness unchanged.
His headline numbers reproduce on our data:

- Ungated 15:15 sign accuracy: **49.1%** (428 directional days) vs his claimed 49.2%
- Gated accuracy: **45.7% — exact**
- Net, gated − ungated: **−23.5 pts** vs his −24
- The gate WAITed **105 of 499** days, never flipped a sign, and stood down more
  right calls than wrong ones (pooled 53 wrong calls fixed vs 71 correct calls dulled)
- Calibrated vs as-uploaded gate: **zero differing days in 499**

The one forced deviation: VIX came from the on-disk CBOE history file instead
of yfinance (not installed; the run was on-disk-only by design) — same series,
prior-day close, fed to his own `prior_context()`. The SPX leg of his test is
not reproducible here (no SPX data on disk); it is the sole explanation offered
for the residual 1-day differences.

**(iii) The condition screen.** Josh approved exactly one bounded shot: does any
condition knowable at 15:15 separate the *ungated* signal into a subset with
real accuracy? Six families, frozen in advance (VIX band; day direction into
15:15; signal strength vs the sample median; signal direction; prior-day move;
intraday range vs ATR), 13 subsets, four PASS limbs each: n ≥ 100, accuracy
≥ 55%, accuracy above the 97.5th percentile of a 1,000-permutation label-shuffle
placebo, and both chronological halves > 50% with positive net points.
**All 13 subsets FAILED — nothing separates.** Facts from the registered table:

- Best raw accuracy: VIX ≥ 25 at 60.6% — on n=33, where the placebo bar is 66.7%
- Bull calls: 33.3% on the same n=33 — the mirror image; small-sample noise both ways
- Closest miss: weak signals (|eff| below median) at 52.8%, n=214, both halves
  clean — still under the 55% limb and under its placebo bar (53.7%)
- **Strong signals were worse than weak ones (45.3% vs 52.8%)** — the opposite
  of what a real signal does
- Bear calls are 395 of the 428 days at 50.4% — the coin flip itself, unfiltered

Recorded conclusion, per the registration: **there is no condition in the
registered set where the Medusa 15:15 signal stops being a coin flip.
Standalone Medusa is closed permanently; no second screen.**

## 4. The ask

1. **Confirm or challenge the closure, claim by claim** — CONFIRMED / REFUTED /
   CANNOT-VERIFY per desk custom, each tied to an artifact in this packet (or
   to a recomputation from `data-*.csv` / `code/`, which are included so you
   don't have to take any number on faith). The claims that matter most:
   (a) the reproduction's headline numbers follow from the author's code + the
   enclosed per-day data; (b) the screen was run as registered — spec frozen
   before the run, no families added, no thresholds moved; (c) "nothing
   separates" is the correct reading of the screen table; (d) v1's bleed
   relocation means a regime-layer fix cannot rescue Chimera's entries.
2. **If you see a fix angle we missed** — for the Medusa signal or for Chimera's
   entry-side bleed — it must arrive as a **frozen spec we can reproduce from
   artifacts**: fixed conditions and thresholds stated in advance, a placebo
   limb, a both-halves limb, a minimum n, and the exact data it runs on. An
   assertion without a reproducible spec will be returned, not run.
3. **Explicitly out of scope:** changing the frozen Chimera forward test;
   anything live; and a second condition screen on the same 499 days with new
   slices — that door is closed by the registration. If you believe a genuinely
   different data source or a structurally different signal definition escapes
   that closure, say **which** and **why** — that argument, if it holds, is
   itself a finding.

Reply as a new dated drop in `from-claude/`, per desk rules. The hourly watcher
will surface it; there is no deadline pressure from this side — the forward
test runs on its own clock either way.

— Quill, 2026-11-17
