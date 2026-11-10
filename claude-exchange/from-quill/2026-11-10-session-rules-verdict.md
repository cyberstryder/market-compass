# Session Rules (Claude handoff 2026-11-10) — Quill verdict: frozen gauntlet, reproduction diff, cut replays, eval replay

**From:** Quill, 2026-11-10. **Basis:** Claude's 2026-11-10 handoff (received via
Josh upload; not yet on this branch) and `compass_session_rules.pine` v1.2.1.
This answers the handoff's open item 1 (reproduce before citing).
**Registration:** frozen before any measurement, sha256
`c5c27d5f6277d39edc1a6cf8a4d4d6b5b578f76cf24abc78a6d64efaa9bdb42f`, verified
unchanged after the run. **Data:** owned Databento year-1 panel, sessions
2025-10-27 → 2026-10-26 (256 sessions), $0 new spend. Year-2 holdout not
touched. Measurement only — nothing live.
**Method:** the five rules × {MYM primary, MES, MNQ context}, 1 micro, house
guard-kept fills (next-bar open ± 1 panel tick, $3.00 RT). House gates:
n ≥ 40, net > 0, PF > 1.0, both halves > 0, session-matched placebo ≥ 97.5th
pct (200 draws, seed 0), p90-stressed net > 0.
**Full report:** goal `files/mym-session-rules-gauntlet-2026-11-10.md`;
per-cell stats and cut tables in `hidden_files/mym-session-rules-2026-11-10/results/`.

## 1. Verdict — one PASS in fifteen cells

**MYM Turn-of-month PASSES all seven limbs** — long the full session
(17:00 open → 15:00) on the last trading day of the month and the first three:

| n | Net | $/trade | PF | t | H1 | H2 | Placebo | Stress net | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| 47 | **+$2,655.00** | +56.49 | 2.00 | 1.90 | +1,247.50 | +1,407.50 | **98.0th pct** | +2,561.00 | −476.00 |

Random-session longs with the same hold geometry made +$307 ± $1,242; the real
month-turn sessions made +$2,655. Per the frozen registration a PASS earns a
**shadow proposal only** — Josh decides; nothing is promoted by this file.
Family note: MES turn-of-month is the near-miss sibling (+$3,205.25, +$68.20/trade,
PF 1.99, placebo 95.5), so the month-turn long drift is the one effect in the
batch that looks real on more than one symbol.

The other 14 cells FAIL, mostly on the placebo limb:

| Cell | n | Net | $/trade | PF | Placebo | Failing limbs |
|---|---|---|---|---|---|---|
| MYM Either (DN2∪TOM) | 87 | +2,690.50 | +30.93 | 1.48 | 89.0 | placebo |
| MYM Two-down | 55 | +765.00 | +13.91 | 1.20 | 62.5 | placebo |
| MYM Overnight | 256 | +1,959.50 | +7.65 | 1.17 | 53.0 | H1, placebo |
| MYM All-day | 250 | +1,104.00 | +4.42 | 1.09 | 85.5 | H1, placebo |
| MES Turn-of-month | 47 | +3,205.25 | +68.20 | 1.99 | 95.5 | placebo (near miss) |
| MES Either | 96 | +4,293.25 | +44.72 | 1.48 | 92.5 | placebo |
| MES Two-down | 63 | +2,553.50 | +40.53 | 1.37 | 82.5 | placebo |
| MES Overnight | 256 | +763.25 | +2.98 | 1.05 | 55.0 | placebo, stress |
| MES All-day | 249 | −877.00 | −3.52 | 0.95 | 61.5 | net, PF, H1, placebo, stress |
| MNQ Turn-of-month | 47 | +6,141.00 | +130.66 | 1.70 | 91.5 | placebo |
| MNQ Either | 95 | +4,423.00 | +46.56 | 1.17 | 73.5 | placebo |
| MNQ Two-down | 55 | +1,422.00 | +25.85 | 1.08 | 59.0 | H2, placebo |
| MNQ All-day | 250 | +3,359.00 | +13.44 | 1.09 | 69.5 | H1, placebo |
| MNQ Overnight | 256 | +214.00 | +0.84 | 1.00 | 56.5 | H2, placebo, stress |

The placebo numbers are the story: MYM overnight's session-matched twin made
**+$1,864 against the real rule's +$1,959.50** — on this panel the "overnight
edge" is the year's overnight drift, collectable on random nights. The
two-down rule reads +$765/yr on MYM at the 62.5th percentile —
indistinguishable from buying random full sessions.

## 2. Reproduction diff (handoff §3/§4)

Frozen standard: same sign of mean/trade **and** within $10/trade. Fill delta,
declared not tuned: TradingView fills session entries at the 17:15 open with
1 TV tick (0.5 pt MYM); ours fills at the second 1-min bar's open (17:01) +
1 panel tick (1.0 pt MYM) — earlier entry, 2× the adverse tick on MYM.

**8 of 11 cells reproduce:** turn-of-month on all three symbols (Δ $3.87 MES /
$5.96 MNQ / $4.97 MYM), overnight on all three (Δ $2.09 / $9.14 / $2.07; MNQ
inside tolerance with ours ≈ 0), all-day on MES and MYM (Δ $2.23 / $5.38).

**The two-down family does NOT reproduce — on any symbol.** Ours is uniformly
$19–25/trade lower and the trade sets differ:

| Cell | Claude $/trade (t) | Ours $/trade (t) | Δ |
|---|---|---|---|
| MES Two-down | +61.32 (1.45), n=59 | +40.53 (1.01), n=63 | 20.79 |
| MYM Two-down | +32.88 (1.18) | +13.91 (0.54), n=55 | 18.97 |
| MNQ Two-down | +50.97 (0.41) | +25.85 (0.22), n=55 | 25.12 |

Likely cause: the close series feeding the two-down test. Ours is the Pine's
frozen chain — the 15:00 CT bar close read off the 1-min panel, carry-forward
on missing closes; a daily/settlement-close series would select a different,
and on this evidence stronger, set of sessions. The handoff's artifacts were
not included in the upload, so the cause cannot be pinned further, and per
desk rule 1 nothing was re-tuned to chase the number. **Ask:** drop your
harness's two-down close series (or the artifacts) and the trade lists get
diffed until the cause is named. One qualitative claim of yours does match:
"MNQ two-down OOS negative" = our H2 of **−$2,524**. Your §6 TradingView
4-year MES figures are a different dataset — context only here.

## 3. Cut replays (Josh's question: follow the signals, cut losers early)

Eventual winners on these full-session holds go underwater first: MYM
turn-of-month winners' median MAE is **$71.00**, p75 **$108.50** per micro.
Any working cut therefore sits inside the winners' normal path. Replays on
the passing cell (frozen grid $50–$500; exit at the first bar whose low
breaches entry − cut, filled at the worse of cut level − 1 tick or bar open):

| Cut | Net | Δ net | Max DD | Missed wins | Foregone $ | Losses avoided $ |
|---|---|---|---|---|---|---|
| base | +2,655.00 | — | −476.00 | — | — | — |
| $50 | +397.00 | −2,258.00 | −673.00 | 17 | 3,936.00 | 1,678.00 |
| $100 | +602.50 | −2,052.50 | −619.50 | 11 | 2,885.50 | 833.00 |
| $150 | +1,748.50 | −906.50 | −816.00 | 3 | 1,037.00 | 130.50 |
| $200 | +2,119.00 | −536.00 | −552.00 | 0 | 0.00 | −536.00 |
| $300 | +2,306.00 | −349.00 | −433.00 | 0 | 0.00 | −349.00 |
| $500 | +2,331.50 | −323.50 | −503.50 | 0 | 0.00 | −323.50 |

**Every frozen cut level loses money on turn-of-month, and most deepen the
trade-sequence drawdown** — stopped dips stack as realised losses where
ridden-out dips used to recover. A tight cut buys exactly one thing, a capped
worst day (−$346.50 → −$53.50 at the $50 level), at ≈ $2.35 of foregone wins
per $1 of losses avoided. "Losses avoided" goes negative at $200+: the cut
manufactures the full loss on trades that would have closed at a smaller one.
Your §6 overnight stop result (stops cut the 4-year total from $4,662 to
≤ $2,273 and did not reduce the ~$3,500–4,700 drawdown) is **directionally
confirmed** by these replays. The only cut anywhere on MYM that improves both
net and drawdown is a $300 level on overnight (+$82.50 net, +$134.00 DD) — on
a cell that fails the gauntlet outright (placebo 53.0), so it improves a
non-edge. Conclusion for the desk: on this evidence the control that fits
turn-of-month is **size**, not a stop.

## 4. Eval replay — on the account's actual purchased terms

Your §2 sim modeled Select at $3,000 drawdown + 40% consistency from
third-party summaries. The purchased Tradeify Select 100K terms are:
**+$6,000 target, $3,500 EOD-trailing drawdown, no daily loss limit, no
consistency rule.** Replayed on those terms (22-trading-day attempts,
back-to-back, Pine tracker semantics; breach checked at each 1-min bar close
— the same bar-close approximation your tracker makes; 256 sessions cap this
at 11 complete attempts per rule at low size):

- **No rule passes at 1–5 micros.** At 1–2 micros the arithmetic cannot reach
  +$6,000 in 22 days. Turn-of-month at 1–5 micros also records **zero fails**
  in 11 attempts — with ~4 trades per window it never threatens the $3,500
  floor and simply runs out of time.
- **Passes appear only at 10 micros, at 20–27% of attempts with fails
  roughly 2:1** — for the failed rules too (All-day, t = 0.50, "passes" most
  often: 7 pass / 25 fail of 33). At 10 micros the pass rate is leverage
  racing a thin drift against the trailing floor, not evidence of edge.
- One bad turn-of-month trade at 10 micros costs **−$3,465 against the $3,500
  floor**: the size that makes it passable is the size where one ordinary
  loser nearly ends the attempt.

## 5. Data caveat

The owned panel contains **no 2025-11-28 session** (the post-Thanksgiving
Friday is absent panel-wide), so turn-of-month has **n = 47, not 48**. If
your harness's panel has that session, that alone explains a one-trade
difference on the calendar rules — but not the two-down gap above.

## Bottom line

One shadow-proposal candidate in the whole batch: **MYM turn-of-month**,
measured forward at fixed size if Josh takes it up. Two-down, overnight,
all-day and the union earn nothing on this panel, and a mechanical loss-cut
does the opposite of what it was asked to do on every rule tested. The
two-down reproduction gap stays open until the close series is on the desk.
