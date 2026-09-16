# Morning and Smoothers consolidation — September 15, 2026

Compass is the destination for both programs. Improving strategy behavior comes after preserving source behavior and identifying differences. No original alert sender is retired yet.

## Implemented in this increment

- Morning's existing authenticated source feed adds explicit outbox status/attempt/delivery/error fields, including option-edit rows, and option enrichment policy/status/message identity. Outbox payloads, webhook URLs and leases are not exported.
- Compass recomputes Morning ask-to-bid and midpoint returns from retained original option samples, checks contract identity and timestamp ordering, and reports unavailable/mismatched results rather than replacing them with zero. This is calculation parity on source observations, not independent options collection.
- Smoothers quality, signal classification, smoothing and target-helper modules are preserved from cyberstryder/smoothers-new at dbcce5fd9e4d2daa79e52b51bb61599d5202aab3. Quality and monitoring source tests run in Compass. These pure modules now also support the isolated native weekly scheduler described below.
- A bounded Compass worker recomputes Smoothers quality components from frozen entry inputs and independently checks target touches in retained completed minute bars. Missing earlier bars prevent claiming the first touch. No source status is overwritten.
- Calendar fixtures cover the Labor Day entry shift and Thanksgiving early close. The displayed schedule uses Compass XNYS; reconcile against the source Alpaca calendar before cutover.
- Connected Projects shows replacement checks. A 500-record per-program cap and 100-row detail cap are explicit. The target comparison covers only records from the most recent seven days. Earlier research remains in its source.

## Native workflows built in this increment

Morning has isolated native signal, checkpoint, v1.3 frame, candle and research tables; scoped authenticated intake; its original validated option policy; an independent option-enrichment worker; and a separate leased minute sampler. Source port: morning-algo-tracker 41ac6c739ab2d5d68154dd4cd0e0acd09ecc5ae6. New table names prevent mixing imported original history with independent work. Fresh source summaries relay new signals during shadow operation; this tests native options but not direct TradingView delivery. Signals older than activation or 120 seconds are not replayed as fresh entries. A recovered checkpoint without an original signal receipt cannot fabricate an option entry. Option receipt and sampling use their real times.

Smoothers has an authenticated current configuration/all-time outcome snapshot, first-session weekly selector, source smoothing/classification/quality calculations, full-cohort ranking, actual listed option selection, batched completed-minute target monitoring, a separate premium worker, and last-session closure with restart recovery. Source math: smoothers-new dbcce5fd9e4d2daa79e52b51bb61599d5202aab3. Calendar comes from Alpaca, including holiday-shifted entry and early closure. Model entries use the first completed hourly bar (10:30 ET on a normal session), with processing starting 10:35 ET. A missed entry window is marked missed; current option prices never stand in for Monday prices. Parameters, quality inputs, entry bar, directions, and hourly-input checksum are frozen with each native result.

A durable shared notification outbox stores previews. The sender requires both `NATIVE_PROGRAM_SEND_ENABLED=true` and per-program accepted Compass ownership with confirmation that the prior sender is paused. Dedicated webhook settings are also required. None is activated in this release. Old shadow intents are never promoted. An ambiguous initial send is held for reconciliation rather than blindly duplicated; an option edit requires the original message identity. Live Discord delivery has not been tested by this release.

### Explicit differences and limits

- Morning's relay supplies signal summaries only; direct frames/checkpoints/research must use `/hooks/native/morning` with scoped bearer auth, or its token-path counterpart, when routing is cut over. The private routing controls prepare and schedule direct-shadow or relay-shadow operation for an actual future stock session. They do not edit TradingView or activate a sender. A scheduled direct session stops the summary relay for that session; rollback cancels future changes or schedules relay mode for the next session. Do not mix event schemas mid-session. The legacy environment relay switch remains an additional disable override. No TradingView changes are required during shadow validation.
- Native reports now calculate stock coverage, six exit rules, research outcomes through 180 minutes, option returns and weekly target/premium results directly from native tables. Optional source snapshots show differences and unknowns separately. The original historical report remains available; native observations never overwrite it.
- Smoothers automatically adopts a fresh validated current configuration snapshot as its first immutable Compass-owned revision. Subsequent edits and restores use optimistic revision checks; started jobs retain their frozen settings. Source aggregates are frozen at ownership, then only native cohorts created after ownership add resolved outcomes. Runtime configuration and native reports no longer require the old app after bootstrap. Older source configuration/job/research history still needs verified backup/restore before retirement.
- Smoothers uses actual listed contracts with a deterministic lower-strike tie break, no synthetic-strike fallback, and strict timestamped bid/ask validation. These can differ from the original choices and must be reconciled. Model entry time and actual option-quote time remain distinct.
- Missing target-monitor coverage remains explicit. Weekly target failure is unresolved when coverage is incomplete; it is never presented as an option loss. Premium valuation is a theoretical model, not a realized fill or guaranteed return. Native premium checks continue after a model's dead flag rather than suppressing later evidence.
- Smoothers notification previews preserve entry/target/closure meaning but are not yet exact copies of its original rich embeds. Sender activation requires preview approval as part of acceptance.

## Forward acceptance and retirement gates

Morning: verify a qualifying opening session, native contract/quote timestamps, all 60 option sample outcomes (including explicit gaps), frame/checkpoint acceptance and restart behavior; reconcile preview/delivery identities. Then prepare direct intake routing, report independence, a clean-session single-sender cutover and rollback.

Smoothers: verify one complete independent weekly cycle, all enabled ticker outcomes/errors, hourly-bar and configuration differences, option choices, full-cohort ranking, one-minute targets, premium checks and final-session closure. Reconcile differences rather than hiding them in aggregate agreement.

Both: preserve existing history and verify durable source database backups and restoration. Only after accepted forward results may one official sender change at a time. Pause the original application first; retain database and repository through rollback verification. Do not infer completeness from a rolling feed or a successful deployment.

## Concrete retirement inventory

| Asset | Railway identity | Disposition |
|---|---|---|
| Morning application | project 98824a1f-2a42-4023-8159-a98f0657c2f3; service 30af07ca-beae-405e-9356-4646ce90f7c6 | Retain as current sender; pause only after accepted replacement and rollback rehearsal |
| Morning Postgres | service 7201d70c-7963-4bd5-bf29-2a3751b9f53d | Preserve until complete export/restore and source independence verified |
| Smoothers application | project 77861f99-253a-4065-8d68-037dbf6b1f3e; service da2b0ac7-802c-454e-bdcf-8e50a03b77ca | Retain through complete paired weekly cycle |
| Smoothers Postgres | service 00f658af-b22e-4ee4-84db-4588bc8b1970 | Preserve configurations, jobs and research as well as signals |
| cyberstryder/morning-algo-tracker | GitHub repository | Archive after cutover and rollback window; retain history |
| cyberstryder/smoothers-new | GitHub repository | Archive after cutover and rollback window; retain history |

No safe deletion candidate was established among these four services. Other Railway projects are outside this retirement decision; their names alone are not evidence that they are unused. Pausing a sender and deleting its database are separate decisions. Repository archival is preferable to losing development/research history.

## September 16 native ownership and reporting increment

Connected Projects now provides native report filters, JSON downloads, expected Morning alert previews beside original delivery status, a Smoothers configuration editor/revision history/restore control, and Morning routing preparation. All management endpoints require the existing owner session and same-origin checks; scoped source tokens cannot change configuration or routing. No sender is activated by deployment. A separate guarded handoff can arm Morning only after completed direct-session evidence, operator acceptance, confirmation that the original sender is paused, and dedicated sender settings. It takes effect at the following market session. No external TradingView setting changes are made.

Native Morning receipts retain direct-versus-relay provenance with immutable first receipt entries; pre-increment records have unknown provenance. Reports require actual native candles for stock exits and retain missing or ambiguous outcomes. Option references and source quote times remain separate, even when contracts agree. Complete-cohort stock means exclude any signal without all six modeled exits. Reports cap signals/candidates at 200, research tape reads at 50,000, provenance rows at 40,000, and Smoothers source inventory at 500; truncation is explicit. Empty data never satisfies acceptance.

A direct-shadow rehearsal requires an additional TradingView delivery while keeping the original TradingView-to-Morning delivery active. Replacing the primary webhook before official cutover would deprive the original sender of new signals; this build does not do that. A routing plan is prepared before scheduling; both writes require the screen's current revision. Future cancellation leaves the current session unchanged. This enables a reviewable shadow-routing rehearsal. The separate official handoff remains blocked until evidence and operator requirements are satisfied. Existing official senders remain active. Their database backups/restoration and forward opening/weekly acceptance still gate retirement.

### Official Morning handoff and rollback

The handoff plan checks an untruncated, completed direct-intake session: matching source signal fields, matching native/source candle inventories, completed scheduled option observation accounting on the same selected contract (including explicit provider-unavailable outcomes), entry/edit previews and original delivery evidence, and a fresh post-session source-feed check. Identical source candle gaps remain explicit and require operator review; they are not fabricated or treated as pipeline loss. Both programs reporting no available contract is also explicit, preserving stock alerts when options are unavailable. Empty, relay-only, incomplete or unmatched evidence blocks the plan. These automated checks do not claim that the entire TradingView universe is configured; the operator must review that scope and the previews.

Arming additionally requires the operator to confirm the original sender is paused, accept the review, and separately configure the dedicated sender flag/webhook. It is restricted to the next market session after the review, expires after 24 hours or at the opening boundary, and refuses unsettled deliveries. No real handoff is armed in this release. Historical signals predating that boundary cannot become live messages, and prior shadow intents remain shadow. Rollback revokes ownership and cancels queued sends; in-flight outcomes remain ambiguous and cannot be retried or treated as undelivered without reconciliation. The original sender is resumed manually only after those outcomes are checked.
