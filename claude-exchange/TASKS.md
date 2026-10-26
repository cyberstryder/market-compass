# Joint task queue

## Task 001 — Reconcile the AOI Fade | MES replay disagreement

**Status:** OPEN — waiting on Claude's trade list.
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

### Quill's deliverables on receipt

1. A trade-by-trade diff classifying every discrepancy (boundary /
   session-date, gate set, fill model, roll window, detector input, other),
   each with the responsible rule named.
2. A joint verdict memo in `from-quill/`: which numbers survive, what (if
   anything) must be fixed in either harness, and the corrected MES verdict
   for Josh's live/paper decision.
