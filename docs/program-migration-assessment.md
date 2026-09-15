# Program migration assessment — September 15, 2026

Scope: Morning Algo, New Smoothers and the existing automated MNQ/MGC strategies.
Compass is the primary development platform; each original remains authoritative
for its current alerts/orders. These are code-level findings, not proof that every
source setting is currently enabled or that matching trading performance exists.

## Morning Algo — move first

Reviewed source `cyberstryder/morning-algo-tracker` at
`f459740a452a3cdf17fbb62af3d38edc1fc63c4d`: `app/main.py`, `app/options.py`,
`app/candidate_models.py`, and the existing Compass feed projection.

Already in Compass: authenticated rolling source imports, saved signal settings,
source option/checkpoint snapshots, secondary reviews, immutable revision ledger,
and independent retained-quote endpoint measurements at 5/15/30/60 minutes.

Not transferred:

- Direct signal/checkpoint ingestion and validation of cumulative native candles.
  Source schema 2 requires one-minute entries and validates ordered bars against
  checkpoint prices/ranges. Maintain source event identity and replay semantics.
- Research batches: BASELINE, BLOCKED_ORB, BLOCKED_FAST, DELAYED_CONFIRM and
  PULLBACK_RECLAIM; candidate window is 180 minutes. Their history and coverage
  diagnostics are not included in the existing signal-only Compass feed.
- Exact option policy `nearest_expiry_atm_call_v1`: actual nearest expiration
  1–14 calendar days after signal day, nearest strike within the policy band,
  lower-strike tie break, validated OCC metadata, OPRA quotes. An option failure
  must not suppress the stock alert. Preserve enrichment/retry and original
  Discord-message edit behavior before transferring notification ownership.
- Source stock-bar research, sampled option histories, exports and coverage reports.
  The PR36 midpoint monitor does not duplicate candle closes or continuous peaks.

Next build: a read-only versioned export for research batches and native candles,
then direct Compass intake with source schema tests reused. Compare input IDs,
late/duplicate events, initial receipt times and all measurement windows. Require
complete paired forward cohorts and a documented rollback before switching sends.
Historical records older than the current 14-day polling window require explicit
bounded export/reconciliation; current feed scope is at most 45 days.

## New Smoothers — move second

Reviewed source `cyberstryder/smoothers-new` at
`dbcce5fd9e4d2daa79e52b51bb61599d5202aab3`: `main.py`, `jobs/entry_monday.py`,
`shared/monitoring.py`, `shared/market_calendar.py`, `web/compass_bridge.py`.

Source schedules run in its configured timezone (calendar semantics use New York).
Entry candidate runs weekdays at 10:35, with the job admitting only the first
actual exchange session of the week. The calendar handles Monday/Friday holidays
and early closes; do not substitute a fixed Monday/Friday UTC cron.

Official target checks use completed native one-minute bars at :15s, with a 16:00
final check. Premium advisory checks are separated on a five-minute cadence at
:35s so they cannot delay target alerts. Weekly closure waits until the final
actual session closes plus five minutes. Preserve per-ticker configs, direction,
weekly cohort identity, quality ranking, original option choice, model entry,
target touches, modeled exit fields and notification delivery ownership.

Current Compass feed exports signals, all OPEN records and recently resolved
records, with full original row and selected option fields. It does not transfer
scheduler/job history, timing shadow datasets, timeframe research or all premium
sample history. Some source rows report strategy version as `unrecorded`; assign
provenance honestly rather than guessing a version during migration.

Next build: mirror job/quality/research histories and source configuration
versions; port pure signal/quality/monitoring functions behind shadow scheduling.
Verify a full weekly cycle and holiday/early-close fixtures before cutover.
Underlying WIN is a target event, not a profitable option execution.

## Automated futures — execution last

Assessed Compass's current parser and `docs/project-connections.md`, not a fresh
export of the user's active TradingView charts. Current recognized sources are
MNQ 0.10.3 and MGC 0.7.3 on three-minute charts. Obtain the exact active Pine files
and settings before claiming code parity. MNQ supports long/short; current MGC
source parser supports short entries. Do not expand behavior silently.

Already observed: cloned TradingView custom entry/exit/cutoff messages, source
versions, proposed brackets and one-contract quantity. TradingView continuous
prices and explicit Databento contracts stay distinct.

Critical gap: custom messages omit bracket-fill exits; cutoff messages can occur
while the emulator is already flat. These observations cannot establish actual
broker positions, complete trades or realized P&L. Broker fill/position ingestion
and reconciliation must precede an execution cutover.

Next build: reconcile original alert log with Compass observations; import actual
fills/positions read-only; model order identities and recovery. Then verify
paper order lifecycle, OCO/brackets, partial fills/rejections, stale-feed handling,
disconnect/restart recovery, daily loss locks, sizing and prop cutoff flattening.
Risk settings must be the active user's settings, not historical examples.
TradingView/TradersPost remains the execution route until a separately authorized
live switch. Exactly one order sender per strategy/account at cutover.

## Shared cutover gate

1. Full source inventory and versioned data/config imports, including research.
2. Shadow rules and measurements; discrepancies/missing data shown explicitly.
3. Source-equivalent reports/exports and verified notification ownership.
4. Complete paired acceptance cohort and rollback rehearsal.
5. Switch one component, then pause its original counterpart after verification.

Storage recovery has restored operations, not proven all historical integrity.
Capacity monitoring, durable archives, restore verification and gap accounting
are shared prerequisites for dependable consolidation. None of the three source
programs is ready for complete shutdown based on the current evidence.
