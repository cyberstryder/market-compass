# Joint task queue

## Task 001 — Reconcile the AOI Fade | MES replay disagreement

**Status:** CLAUDE DELIVERED 2026-10-25 — waiting on Quill's classification diff + verdict memo. Claimed by Claude.
**Opened:** 2026-10-25 by Quill. **Decision it feeds:** whether AOI Fade on
MES stays live to TradersPost or returns to paper-only (Josh decides;
Quill's recommendation on the desk is paper-only while this is open).

### The disagreement

- Quill's harness (production engine code, day-chunked replay over archived
  Databento 1-minute bars): AOI Fade | MES = **90 counted trades, +$2,569** —
  Leg A (2026-09-23 → 10-23 CT) 32 trades / +$1,154.83 / PF 1.59; Leg B
  (2026-08-24 → 09-22 CT) 58 trades / +$1,414.44 / PF 1.40.
- Claude's harness (its 2026-10-25 snapshot review): **73 trades, +$1,014** —
  September PF 1.29 decaying to October PF 1.03, final two weeks −$364 and
  −$393.

The trade sets differ by ~17 trades and the October stories conflict, so at
least one harness is mishandling something (gates, session dates, fills, or
rolls). We find out which by diffing, not debating.

### Claude's deliverables (drop in `from-claude/`)

1. The 73-trade list as CSV — same columns as Quill's export in
   `from-quill/2026-10-25-aoi-fade-mes-quill-trades.csv`, timezone stated.
2. A short harness spec: window and session-date convention (how a Sunday
   18:00 CT open is dated), which production gates were applied (news
   pause, regime gate, R:R ≥ 1.5, stop ≥ 0.5×ATR, cooldown, allowlists),
   fill model, and roll handling.

### Claude's drops (2026-10-25)
- `from-claude/2026-10-25-aoi-fade-mes-claude-trades.csv` — 73 rows, America/Chicago, Quill's columns.
- `from-claude/2026-10-25-claude-harness-spec.md` — window, clock, gates, fill model, roll handling.
- `from-claude/2026-10-25-mes-preliminary-diff.md` — Claude's own pairing of the two lists: 17 pre-window, 66/73 matched (60 s keying offset), gap fully attributed: stop-fill convention −$1,210, entry-shift flips −$646, entry-shift unmatched −$553.
- `from-claude/2026-10-25-replay_matrix.py` — the harness.

### Quill's deliverables on receipt

1. A trade-by-trade diff classifying every discrepancy (boundary /
   session-date, gate set, fill model, roll window, detector input, other),
   each with the responsible rule named.
2. A joint verdict memo in `from-quill/`: which numbers survive, what (if
   anything) must be fixed in either harness, and the corrected MES verdict
   for Josh's live/paper decision.

## Task 002 — Live stop-slippage study (the shared stop-fill rule)

**Status:** DONE — 2026-10-25 by Quill.
**Opened:** 2026-10-25 by Josh's decision (MES stays live; this study is the
closure for Task 001's fill-convention disagreement).
**Drop:** `from-quill/2026-10-25-stop-slippage-study.md` +
`from-quill/2026-10-25-stop-slippage-per-trade.csv` (all 45 matched trades).

### Result

Measured on the live TradersPost-sent book, Oct 14–24 epoch (45/45 paper
trades matched to Tradovate round trips):

- **Tier 1 (broker truth):** filled bracket stops landed a **median +1 tick**
  past the stop (equity micros, n = 11; p75 +2, p90 +4, worst +5, never
  better than the level). Quill's at-stop fill is optimistic; the
  minute-close fill is far outside anything the broker produced.
- **Tier 2 (paper book):** paper exits recorded a median **+6 ticks** past
  the stop (n = 15) — the paper engine's quote lag, and paper exits were a
  median 6 ticks *worse* than the broker's actual fills on the same trades.
- **Shared rule adopted: fill stop exits at stop − 2 ticks adverse**
  (Tier 1 equity p75) in both harnesses; targets stay at the level.
- **AOI Fade|MES re-priced under the rule:** Leg A +$1,074.84 / PF 1.53,
  Leg B +$1,264.45 / PF 1.34 (combined PF 1.41; PF 1.29–1.47 across the
  median→p90 range). The promotion verdict survives; the disagreement was a
  ~$230 fill-convention gap on 90 trades.

Caveats in the drop: Tier 1 n = 12 total (BOS+FVG 11, Turtle Soup 1; AOI
Fade and MES have no live bracket-stop observations yet — the rule is
calibrated on equity-index micros and should be re-fit as live AOI Fade
stops accumulate). 4 data-gap trades excluded per house rule. Tier 2 covers
the matched epoch only — closed-trade stops are not exposed by any public
endpoint for the paper-only book.
