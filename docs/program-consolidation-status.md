# Morning and Smoothers consolidation — September 15, 2026

Compass is the destination for both programs. Improving strategy behavior comes after preserving source behavior and identifying differences. No original alert sender is retired yet.

## Implemented in this increment

- Morning's existing authenticated source feed adds explicit outbox status/attempt/delivery/error fields, including option-edit rows, and option enrichment policy/status/message identity. Outbox payloads, webhook URLs and leases are not exported.
- Compass recomputes Morning ask-to-bid and midpoint returns from retained original option samples, checks contract identity and timestamp ordering, and reports unavailable/mismatched results rather than replacing them with zero. This is calculation parity on source observations, not independent options collection.
- Smoothers quality, signal classification, smoothing and target-helper modules are preserved from cyberstryder/smoothers-new at dbcce5fd9e4d2daa79e52b51bb61599d5202aab3. Quality and monitoring source tests run in Compass. Pure smoothing/classification modules are available for the forthcoming native weekly job, not yet wired to a replacement scheduler.
- A bounded Compass worker recomputes Smoothers quality components from frozen entry inputs and independently checks target touches in retained completed minute bars. Missing earlier bars prevent claiming the first touch. No source status is overwritten.
- Calendar fixtures cover the Labor Day entry shift and Thanksgiving early close. The displayed schedule uses Compass XNYS; reconcile against the source Alpaca calendar before cutover.
- Connected Projects shows replacement checks. A 500-record per-program cap and 100-row detail cap are explicit. The target comparison covers only records from the most recent seven days. Earlier research remains in its source.

## Still required for complete replacements

Morning: native signal/frame intake, independent option selection/enrichment/sampling with the exact source policy, durable notification outbox/edit ownership and replay-safe source cutover; verify an opening session and rollback. Existing history/candle reports and checkpoint monitors remain useful but do not replace these workflows.

Smoothers: configuration/job/research history import; native weekly bars and selector; full-cohort ranking; actual listed-contract selection; premium monitor; durable notifications; first/last-session scheduling and complete weekly paired acceptance. Pure-function ports and quality checks alone are not an independent weekly strategy run.

Both: preserve existing history, verify durable backups/restore of the source databases, then switch one official sender at a time. Do not infer completeness from a rolling feed or a successful deployment.

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
