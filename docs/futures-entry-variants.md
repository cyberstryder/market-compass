# Forward futures entry comparisons

`futures-entry-variants-v1` freezes three entry selections on each new futures
setup trial. Existing alerts, portfolio simulation, unlimited research trials,
quote replay, fills, fees, stops and targets continue under their original rules.
No broker orders or extra vendor requests are introduced.

| Variant | Selection |
| --- | --- |
| Every entry | Every admitted independent futures trial, including alert-cooldown candidates. |
| First entry per trend | First admitted setup for that contract, strategy and direction in the observed trend. |
| Entry after pullback reset | The first entry, then an entry after a later completed minute bar spans EMA21 and a separate later bar closes beyond EMA9 in the trend direction. |

Trend is the scanner's completed 15-minute EMA21 direction: close above a rising
EMA for long, below a falling EMA for short. At least 21 complete higher-timeframe
bars are required. Neutral is a separate regime and selects no directional entry.
An observed bias change or a new futures risk day begins a regime. First means
first eligible **observed** after activation or regime change, never the first
trade that might have occurred earlier in the market. EMA lengths and reset rules
are versioned choices to test, not claims of optimality.

State advances on newly completed bars even without a qualifying signal, in both
the original engine and scanner. The original engine observes context before
admitting its signals; a scanner pass on the same bar is idempotent. State is
durable and updated in the engine transaction. Missing/stale context, a skipped
minute, or a signal whose completed-bar clock differs from state produces unknown
trend eligibility. After an intraday gap, eligibility resumes only after a
contiguously observed bias change (or a new risk day); a restart cannot silently
manufacture a new first entry. A touch on the entry bar cannot reset that entry.
Same-bar touch/reclaim is insufficient. Each rule consumes its own reset.

All variants reference the **same original one-unit trial outcome**. Overlapping
results are experiments, not an achievable account equity curve. An excluded
entry consumes no trend/reset slot. Source-ID deduplication preserves the frozen
selection on retry. Existing pre-activation records are counted separately and
never retrospectively classified.

Setup results and `/api/setup-study` expose counts grouped by dated contract,
strategy, direction, outcome model, fill model, and alert cohort. Selected,
skipped, unknown, excluded, open, unresolved, and closed outcomes remain separate.
Win rate and mean R use closed selected observations only. Every stored trial in the 30-day
window contributes; only individual detail pages are limited to 100 records. Full
trial records include entry decisions, their reason, regime clock, and reset
clocks. A bounded 30-second log reports selection and missing-context counts.

## Stale-feed observations on September 14, 2026

Collector source/poll clocks at 19:46–19:48 UTC show successful requests returning
unchanged, explicitly cached SPY and QQQ matrix snapshots roughly 14–15 minutes
old. QQQ then advanced to source time 19:48:27 UTC, close to receipt time. IWM's
same snapshot crossed the 180-second confirmation limit while polls continued.
Core matrix scheduling targets 60 seconds during cash-market hours, so the
multi-minute source delay cannot be explained by that interval alone.

This confirms intermittent vendor snapshot staleness, but does not establish the
vendor's cache TTL or whether its underlying publisher is delayed. Remaining work:
verify the endpoint's documented refresh behavior and account entitlements using
these source-versus-receipt examples; inspect per-item clocks wherever aggregate
source time is unknown; keep existing stale-data confirmation gates. Polling more
frequently or raising age limits would not make those observations newer.

Unusual-flow polling returned the same latest event across several 20–25-second
polls. That alone cannot distinguish a quiet filtered feed from delayed delivery.
Index gamma rows have individual `lastUpdated` clocks, so an unknown aggregate
snapshot clock must not be mistaken for proof that every constituent is stale.
Futures entry comparisons use completed price bars and retained quotes and do not
require a fresh equity options matrix to collect a research trial.
