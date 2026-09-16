# Smoothers sender handoff

Deployment does not activate official delivery. Keep the original Smoothers app
running and sending during a complete native weekly comparison. A missed week
cannot qualify. Configurations remain owned and versioned by Compass.

## Evidence and operator controls

Use **Native Morning and Smoothers → Native report** to inspect the review week.
The report includes source signal/quality/rank differences, contract and
configuration comparisons, target evidence, premium observations, and the actual
queued entry, roster, and terminal message previews.

Use **Smoothers sender handoff** to prepare the immediately following weekly
cohort. Both dates identify weeks by Monday, even when Monday is a holiday.
Compass calculates the first actual exchange session. Preparation requires the
review week's final exchange close plus one hour. Activation is forbidden once
the new week opens, and plans expire after four days.

The evidence gate requires a complete first-session selection for all enabled
configurations, the same owned configuration revision, matching source/native
inventories and signal/quality/contract/configuration fields, resolved native
outcomes with complete minute coverage, reconciled target timestamps, retained
premium attempts for longer-lived priced contracts, current-format previews,
and a fresh post-week source feed check. Truncated or missing evidence blocks
activation. Evidence is checked again on activation; changes require a new
review. Routine imports of identical source observations do not invalidate it.

The original feed **does not export Discord delivery receipts**. Its delivery
status is `unrecorded`, never inferred from a signal row. The operator must check
the original channel's entries, full roster, target hits, and weekly closures
and explicitly attest to that review before activation. Quote snapshots can
differ because their observation times differ; the report retains both source
and native observations for review. A target hit is not an option fill.

Before arming, configure the dedicated `NATIVE_SMOOTHERS_DISCORD_WEBHOOK` and
`NATIVE_PROGRAM_SEND_ENABLED=true` on the engine and the dashboard environment
used by the guarded control, and verify the original sender is paused. No
credentials are returned by the dashboard. The handoff endpoint does not pause
or resume external services. All three confirmations are required. Deployment
alone sets none of them and never changes ownership.

The accepted boundary applies to both the **event timestamp and its originating
weekly cohort**. A late target or closure from the original sender's week stays
shadow. Existing previews are never promoted. New eligible events use durable
idempotent keys and Discord message receipts. Missing receipts, interrupted
sends, and unknown POST outcomes require reconciliation rather than a blind
retry. Payloads retain observation/quote times; they do not label queue time as
the actual delivery time.

## Rollback

Select **Stop Compass Smoothers sending**. The operation revokes ownership and
cancels pending Smoothers sends. Requests already in flight become `ambiguous`
even if their response arrives after rollback; inspect the stored receipt and
the channel before resuming the original sender. The operation leaves Morning
ownership untouched. Never resume both senders at once.

This initial handoff supports a fresh weekly boundary, not midweek transfer of
active positions. Re-arming requires another accepted clean weekly comparison;
it does not replay old messages. Configuration edits are blocked between arming
and freezing the first owned cohort; roll back first if its settings must change.

## Message contract

- A+ entries retain underlying references, selected weekly contract/expiration,
  option premium/time, estimated return, quality components/ranks, historical
  records, and backtest win rate where available.
- The roster retains every selected ticker, grouped A+/A/B/WATCH, and splits at
  Discord-safe message lengths. WATCH remains research only.
- Targets show the completed one-minute high/low/close, trigger, target minute,
  observation time, and a newly requested option snapshot. Unavailable quotes
  remain explicit. A quote return is a reference, not realized profit.
- Weekly closures use the final completed regular-session minute when retained.
  Missing coverage produces `UNRESOLVED`, never a fabricated target failure.

All sends remain POST messages, matching the original Smoothers notification
pattern. This change does not modify weekly expiry selection, smoother inputs,
target rules, A+ ranking, or the separate SPY 0DTE workflow.
