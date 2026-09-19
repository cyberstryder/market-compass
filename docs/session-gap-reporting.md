# Complete session gap reporting

The forward-acceptance futures summaries aggregate every futures trial in
their creation windows in SQL. Stock observations cannot evict futures records
from totals. `counts_complete=true` and `truncated=false` apply to aggregate
counts; each 12-row preview has its own `gap_details_truncated`, total and cursor.

Feed health contains **Session totals and unresolved observations**. Futures
now default to **Full futures session (includes overnight)**. Choose **Since
equity cash open** to compare with the original September 18 audit. Intraday
option ideas use cash hours. Changing asset or scope clears the selected date
and loaded records so a prior session selection cannot silently carry over.

Dates identify the trading day on which the session closes. A normal full
futures cohort spans the preceding day's 17:00 CT open through 16:00 CT. The
report uses the existing CME session calendar and `hours_for` early-close
policy. It describes that configured research envelope, not certification of
every product-specific exchange schedule. During maintenance or the weekend,
a blank date selects the latest session that has opened. At Sunday 17:00 CT,
it selects Monday, including its overnight observations. Holiday futures
sessions can be selected even when the equity exchange is closed. Non-session
dates fail explicitly. Cash scope retains its previous equity-day defaults.

**Load more gaps** reaches every unresolved record, ordered by creation time
and stable ID. Dashboard polling does not reset this view. Record IDs, strategy,
reason and archive/gap evidence are exposed; no outcome or source is modified.

Authenticated API: `GET /api/session-gaps` with `asset=future|option`,
`scope=cash|full`, optional `session_day=YYYY-MM-DD`, `limit=1..100`, and the
returned `cursor`. The API retains `scope=cash` by default for compatibility;
full scope is valid only for futures. Cursors are bound to asset, scope and day.
The response provides aggregate counts separately from paginated records,
`total`, `has_more`, `next_cursor`, boundaries and `asof`.

Forward acceptance preserves `futures_session_audit` for the cash-open cohort
and adds `futures_full_session_audit` for the full futures session. Both expose
scope and continuation URLs. Full counts do not establish uninterrupted quote
coverage, and no trading, entry, flattening or risk rules change.

Pagination pins creation/finish clock bounds to the first page. Counts describe
current saved statuses; this is not a durable historical status snapshot.
Refresh includes later failures. Missing paths remain unresolved, never losses.

Tests cover mixed-asset crowding, tied-time pagination, option separation,
cross-scope cursors, authentication, overnight inclusion, maintenance/weekend
selection, Sunday reopen, daylight-saving transitions and holiday early closes.
UI checks cover continuation, escaping, scope selection and filter reset.
