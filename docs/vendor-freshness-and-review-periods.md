# Vendor freshness and corrected-period review

The September 14 audit found that matrix normalization dropped the vendor's
`freshness.stale` flag. Some downstream consumers checked only event age, and
the flow summary's receipt clock could be advanced by a later recovery page.
This patch preserves the flags and separates source freshness from retrieval.

## Confirmation gates

- Matrix source and successful retrieval must both be within 180 seconds.
- Intraday flow keeps its 120-second event limit and also requires a page-one
  retrieval within 60 seconds. Deeper recovery pages do not renew that clock.
- Swing flow keeps its existing five-minute event rules and requires collection
  within 60 seconds. Pending option entries wait if that collection becomes stale.
- Apex keeps its 600-second source limit and requires retrieval within 600 seconds.
- A vendor stale flag, missing required clocks or future clocks blocks current
  confirmation. `cached=true` alone does not: a fresh cached observation can be
  valid within the existing age limit. Unknown cache flags remain unknown.

Secondary, scanner and swing consumers apply the relevant gates. Independent
price-based setups continue to run. No broker order route or original-system
alert rule is modified. Frozen reviews and completed outcomes are not rescored.
New secondary decisions use `secondary-context-v3`; reports group versions
separately.

Feed health and research coverage show source age, poll age, vendor stale/cache
metadata and whether clocks permit current context. Disclosures, calendars,
fundamentals and institutional positioning remain context-only. No age check
establishes predictive reliability, completeness or actual execution quality.

Successful collections log one bounded `Vendor freshness` line with feed label,
status, source time, receipt time, cache flag and repeated-source count. Logs
contain no price, raw response, API key or connection string. Repeated timestamps
and regressions are diagnostic observations, not proof of a vendor cache defect.
No extra provider polling or cache-busting requests are added. Existing shared
request spacing remains 2.6 seconds. Provider reference:
[TraderMatrix developers](https://www.tradermatrix.pro/developers).

## Review a corrected collection period

In Secondary review, choose **Review start (your device time)** and **Load period**.
The comparison uses candidates decided from that time through load time, with
their currently available measurements. Load again to refresh. **Rolling 30 days**
restores the normal automatic report. Recent review records and input-readiness
panels remain independent of this comparison filter.

The authenticated endpoint `/api/secondary/report?since=<Unix seconds>` provides
the same bounded report, limited to 5,000 reviews within the last 30 days. It
rejects invalid, future and older start times. This is a candidate-period filter,
not historical reconstruction of what was known at a past cutoff.

Counts distinguish supported positive/negative/flat moves, missed positive moves,
avoided negative moves, skipped flat moves, pending, missing and session boundaries.
Retained/live checkpoint evidence counts are included in the response. Missing
observations are excluded, never silently converted to losses. Returns are
direction-adjusted midpoint changes, not option P&L or executable fills.

PR22's collector deployment succeeded at 2026-09-14 19:18:28 UTC (14:18:28 CT).
That is an operational start marker for reviewing later candidates, not proof
that every symbol immediately acquired complete coverage. Actual archive rows
and post-fix outcomes still require authenticated data verification. This build
does not claim a live-data audit was completed through the redacted Railway
connection.

Next: inspect the new diagnostics and corrected-period records after deployment,
then test repeated-entry, first-entry-per-trend and pullback-reset variants in
separate research cohorts while preserving every qualifying setup.
