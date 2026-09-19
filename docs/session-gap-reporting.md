# Complete cash-open session gap reporting

The forward-acceptance futures summary now aggregates every futures trial in
its creation window in SQL. Stock observations cannot evict futures records
from the totals. `counts_complete=true` and `truncated=false` apply to aggregate
counts; the existing 12-row preview retains a separate
`gap_details_truncated` flag, total, and continuation cursor.

Feed health contains a **Session totals and unresolved observations** view.
Select futures or intraday option ideas and an equity session date, then load
or refresh. Leave the date empty to use the latest equity trading day.
**Load more gaps** reaches every unresolved record, ordered by creation time
and stable ID, including records with identical timestamps. Dashboard polling
does not reset this view. Record IDs, strategy, reason and archive/gap evidence
are exposed; no outcome or source record is modified.

Authenticated API: `GET /api/session-gaps` with `asset=future|option`, optional
`session_day=YYYY-MM-DD`, `limit=1..100`, and the returned `cursor`.
The response provides full aggregate counts separately from paginated records,
`total`, `has_more`, `next_cursor`, explicit boundaries and `asof`.

Scope deliberately remains **since equity cash open**, not a 23-hour futures
census. Futures reporting cutoff is 16:00 America/Chicago; option cutoff is
the exchange-calendar equity close. This reporting cutoff is not an entry,
flattening or holiday trading rule. Invalid/non-equity-session dates fail
explicitly rather than silently changing dates.

Pagination pins creation/finish clock bounds to the first page. Counts describe
current saved statuses in that creation window; this is not a durable historical
snapshot or status reconstruction. A refresh includes later failures. Missing
paths remain unresolved and are never included as zero-return losses.

Regression coverage includes 2,100 newer stock records hiding older futures,
205 tied-time gaps across pages, separate option cohorts, invalid/cross-scope
cursors, weekend defaults, authentication and PostgreSQL JSON filtering. UI
checks cover continuation, escaping and filter reset.
