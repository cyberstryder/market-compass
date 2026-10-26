# Live Stop-Slippage Study — Task 002 (2026-10-25)

**Question.** When a live (TradersPost-sent) futures position stops out, how far does the
*actual broker exit* land from the *planned stop*, in ticks? That distribution is the shared
stop-fill rule for both replay harnesses: Quill's fills stops at the stop exactly (verified
47/47 on the AOI Fade|MES replay — optimistic by construction), Claude's fills at the
minute close after the breach (pessimistic bound). This study measures production truth.
It is the agreed closure for the AOI Fade|MES harness disagreement; per Josh's 2026-10-25
decision, MES stays live while this lands.

**Per-trade data:** `2026-10-25-stop-slippage-per-trade.csv` (beside this note; also in the goal files as `futures-stop-slippage-per-trade-2026-10-25.csv`) —
all 45 matched sent trades with planned stop, both exits, and per-tier slippage where defined.

## Data and coverage

- **Broker side:** Tradovate orders export, account TDFYSL100276089058, Oct 14–24, 2026
  (363 orders, 234 filled) — the Phase 0 artifact. Round trips rebuilt by FIFO position
  accounting per contract, aggregated per entry lot (one FP-010 exit was split 1+1 across a
  bracket-stop fill and a market fill at the same price). **45/45 paper sent trades matched**
  (root + side + qty = 2, closest entry within ±20 min — the Phase 0 method), zero lots left
  open. No fresher Tradovate pull (Oct 25) is available on disk or via API, so the broker
  tier is bounded to this matched set — stated, not papered over.
- **Paper side:** `sent-trades-paper.csv` (Phase 0) — every `traderspost_sent=true` paper
  trade in the production DB. The paper epoch starts at the Oct 20 book reset; live sent
  trades from Oct 14–19 have no surviving paper records (Phase 0 §5c) and cannot enter
  either tier.
- **Production check (Oct 25):** `/api/simple` and `/api/simple/detail?type=futures` show
  the newest sent trades are Oct 23 — no live sent trades exist after the export window
  (Oct 24 produced none in the visible set; Oct 25 is pre-session). Nothing is missing
  from the tiers on that account.
- **Planned stop** = the protective Stop order transmitted to the broker: its `Stop Price`
  when the bracket filled (12 trades), or the canceled bracket's `Stop Price` when the
  paper engine's own close signal beat the bracket (recovered for 3 of 8 such trades;
  the other 5 are Oct 22–23 incident-window or same-minute-flatten trades with no bracket
  stop in the export — FP-023, FP-033, FP-034, FP-037, FP-041 — and are excluded from
  Tier 2 rather than estimated).
- **Ticks verified from the records:** MES/MNQ 0.25, MCL 0.01, MYM 0.5 (Tradovate
  `_tickSize`; matches the brief). $/point per contract: MNQ 2, MES 5, MYM 0.50, MCL 100.
  Slippage is signed adverse: positive = filled worse than the stop. Dollars are per
  2-contract trade, fees excluded.

## Tier 1 — broker truth: filled bracket stop vs its own stop price

Every live sent trade whose broker exit was (at least partly) the filled bracket stop,
data-gap trades excluded.

| Set | n | Mean | Median | p75 | p90 | Worst |
|---|---:|---:|---:|---:|---:|---:|
| All | 12 | 1.25 tk | 0.5 tk | 2.0 tk | 3.8 tk | 5 tk |
| **Equity index (MNQ + MYM)** | **11** | **1.36 tk** | **1.0 tk** | **2.0 tk** | **4.0 tk** | **5 tk** |
| MNQ | 3 | 3.0 tk | 4.0 tk | 4.5 tk | 4.8 tk | 5 tk |
| MYM | 8 | 0.75 tk | 0.5 tk | 1.25 tk | 2.0 tk | 2 tk |
| MCL | 1 | 0 tk | — | — | — | 0 tk |
| BOS+FVG | 11 | 1.36 tk | 1.0 tk | 2.0 tk | 4.0 tk | 5 tk |
| Turtle Soup | 1 | 0 tk | — | — | — | 0 tk |

In dollars this is small change (median $0.25, worst $5.00 per 2-lot) because most of the
sample is MYM at $0.25 per tick per contract; the tick numbers are the portable ones.
**No fill was ever better than the stop and none was catastrophically through it** — the
worst live stop fill all week was 5 ticks past the level (FP-007, MNQ BOS+FVG, Oct 20).

Honest limits: n = 12 is small. Detector coverage is BOS+FVG (11) + one Turtle Soup;
**AOI Fade has zero Tier 1 observations** (its one live stop trade, FP-023, was closed by
the paper engine's market signal, not the bracket) and **MES has zero** (its only sent
trade in the epoch, FP-031, is a data-gap exclusion below). The MES rule therefore comes
from the equity-index micros — same tick (0.25) and same bracket mechanics — not from MES
itself. That is a calibration gap to close as live AOI Fade|MES stops accumulate.

**Excluded (house rule): 4 data-gap trades** — FP-009, FP-031, FP-042, FP-044. All four
were in fact closed by their broker brackets at 0–1 ticks of slippage (1, 0, 0, 1), which
reinforces the Tier 1 picture, but they stay out of the stats.

## Tier 2 — paper book: paper exit vs planned stop

Paper exits are evaluated on ~4 Hz quotes, so this measures the *paper engine's*
quote-granularity lag, not broker execution. Population: the 20 non-gap paper stop exits
among the 45 sent trades; planned stops recoverable for 15 (12 filled brackets + 3
canceled brackets).

| Set | n | Mean | Median | p75 | p90 | Worst |
|---|---:|---:|---:|---:|---:|---:|
| All (ticks) | 15 | 9.5 tk | 6 tk | 11 tk | 20.8 tk | 39 tk |
| Equity index (ticks) | 14 | 10.1 tk | 6 tk | 12.5 tk | 21.6 tk | 39 tk |
| MNQ | 3 | 18.3 tk | 14 tk | — | — | 39 tk |
| MYM | 11 | 7.8 tk | 6 tk | 8 tk | 16 tk | 24 tk |
| MCL | 1 | 2 tk | — | — | — | 2 tk |

Scope note, plainly: the brief asked for *every* paper futures stop exit since Oct 14.
That population is not reachable — no public endpoint exposes a closed trade's stop
(`compass/simple.py` renders Stop only for open trades, and the futures detail shows
TradersPost-sent trades only), and the live DB no longer holds the Oct epoch. Tier 2 is
therefore the stop exits inside the durable matched epoch, with the paper-only book
explicitly out of reach rather than reconstructed. Treat Tier 2 as a *lower bound sample*
of paper stop behaviour, drawn from the live-sent subset.

**Paper exit vs broker fill (same 15 trades):** the paper book's recorded exit was worse
than what the broker actually got by a median of **6 ticks** (mean 13.3, worst 76 —
FP-028, whose broker twin exited at target while paper recorded a stop). This replicates
Phase 0's exit-side finding with stop trades specifically: **paper stop exits are
pessimistic relative to live fills**, the opposite direction from Quill's replay harness.

## Conclusion — the shared stop-fill rule

Three fill conventions, measured against production:

| Convention | Stop fill | Verdict vs production |
|---|---|---|
| Quill's harness (current) | exactly at stop (0 tk) | Optimistic — below even the Tier 1 median |
| **Production (Tier 1, equity)** | **median +1 tk, p75 +2 tk, p90 +4 tk, worst +5 tk** | — |
| Claude's harness | minute close after breach | Pessimistic bound — resembles Tier 2 paper lag (median 6 tk), not broker fills |

**Recommended rule for both harnesses: fill stop exits at stop − 2 ticks adverse**
(the Tier 1 equity p75: it covers three quarters of observed live stop fills exactly, the
median outcome with a tick of margin, and sits far inside Claude's bound). Keep target
fills at the level (broker target fills in this sample landed on their limit prices;
neither harness's target convention was contradicted).

### AOI Fade|MES re-priced under the rule

Quill's 90 counted trades (the published cells; MES tick value $1.25 × qty 2 = $2.50 per
tick per stop trade; 16 stop trades in Leg A, 30 in Leg B). X = 0 reproduces the published
cells to the cent, which validates the repricing arithmetic.

| Rule | Leg A (32 tr) | Leg B (58 tr) | Combined (90 tr) |
|---|---:|---:|---:|
| X = 0 (as published) | +$1,154.84 · PF 1.592 | +$1,414.45 · PF 1.402 | +$2,569.29 · PF 1.470 |
| X = 1 (Tier 1 median) | +$1,114.84 · PF 1.560 | +$1,339.45 · PF 1.372 | +$2,454.29 · PF 1.439 |
| **X = 2 (recommended)** | **+$1,074.84 · PF 1.530** | **+$1,264.45 · PF 1.344** | **+$2,339.29 · PF 1.410** |
| X = 4 (≈ Tier 1 p90) | +$994.84 · PF 1.472 | +$1,114.45 · PF 1.292 | +$2,109.29 · PF 1.356 |

**The MES verdict survives the correction.** Under the production-calibrated fill rule,
AOI Fade|MES remains clearly positive on both legs (PF 1.53 / 1.34); even at p90 slippage
it holds PF 1.47 / 1.29. The harness disagreement is closed: it was a fill-convention gap
worth ~$230 (at X = 2) on the 90 trades — real, worth fixing in both harnesses, and not
large enough to change the promotion decision. The kill-watch backstop (scorecard PF < 1)
stands as the guard while live MES stop fills accumulate their own Tier 1 sample.

## Method notes & limits

- FIFO round trips: 117 entry lots in the export window; the 45 matched consume 45; the
  rest are the pre-reset cohort (63), the one manual MES anomaly, splits, and Oct 18–19
  trades — same census as Phase 0 (109 in-epoch round trips + post-epoch fragments).
- Paper timestamps are minute-precision displays; broker fills are second-precision
  (CDT as displayed; no DST boundary in range). No Tier 1/2 figure depends on timestamps
  beyond matching.
- MCL caveat (Phase 0 §2): paper MCL prices carry the contract-basis wedge, so FP-015's
  Tier 2 row (2 tk) compares across contract months and is shown for completeness only.
  Its Tier 1 row (0 tk) is internal to the traded contract and valid.
- Small-n warning applies throughout: Tier 1 n = 12 (11 equity). The rule should be
  re-fit once live AOI Fade stops (any symbol) add observations — the live-combo
  scorecard era that began Oct 25 will generate them.
