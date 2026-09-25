# Feed health lifecycle

The attention banner counts actionable component statuses. It is separate from
process/deployment health and from whether a quote is eligible for an entry.

- The container replaces its startup shell with the application process, so
  deployment shutdown signals reach the application's cleanup handlers. The
  image smoke test checks both startup and graceful shutdown, including a clean
  exit and the completed application-shutdown log. This allows import and report
  ownership to be released promptly on normal deployment handoffs.
- Research reports claim ownership in a short transaction, calculate from a
  read-only snapshot without holding the lease row lock, and publish only if
  their per-calculation token still owns an unexpired lease. Contenders wait at
  most 100 milliseconds for row locks and retry without replacing the active
  worker's health. `running` is written atomically with a completed report;
  `refreshing` never makes an old completed report current. A report snapshot
  older than 90 seconds remains `stale` even if retries continue. Cancellation
  releases only the current calculation's lease and prevents late publication.
- Stock history keeps its prior completed scan while the hourly refresh runs.
  `refreshing` is routine maintenance, not a new coverage gap. New batches without
  a completed scan remain `partial`; provider failures remain errors. A refresh
  more than 15 minutes overdue (75 minutes since the last completed batch) is
  `stale`, as is a history task that has stopped reporting for 90 seconds. Full
  overlapping scans, durable page cursors and per-record price checks remain.
- Morning and Smoothers source imports run on a dedicated loop. Committed pages
  and validated ETag responses report `syncing`; only a finished scan reports
  `connected`. No page progress for 25 seconds or no scan completion within 120
  seconds is a failure, even if the worker remains alive. Imports renew ownership
  during pagination and release their own leases on shutdown; a crash still
  allows the bounded lease to expire. Database batches contain at most 25 rows,
  and ETags advance only after the whole page commits, so retries are safe.
- Obsidian's retrospective audit reports completion from its durable job state,
  including after a restart or disabling the one-time audit. A disabled,
  unfinished audit says `disabled`; an enabled audit identifies missing
  configuration. Recorded access failures remain errors. Completion means the
  audit checked its ideas; individual history coverage remains in its results.
- Stock recovery publishes a current status on every cycle. No active demand or
  valid current quotes produces `idle`. Pending retries/remaining missing quotes
  remain `partial`; failed requests and their backoff remain errors. Collector
  heartbeat and task-age checks detect a stopped recovery lane.
- Cash-index option roots, including VIX, are excluded from equity quote/history
  demand and the stock archive denominator. Their option contracts, vendor
  research and original outcome records remain retained. No ETF quote is
  substituted for an index.
- Outside the equity session, successful TraderMatrix polling with old/no events
  displays `market_closed`. Source timestamps and event freshness are retained;
  live-confirmation eligibility is false. Stopped polling, bad clocks, explicit
  vendor staleness, provider errors and incomplete collection remain warnings.
  Normal freshness checks apply again when the session opens.

This changes health reporting and provider-symbol classification. It does not
repair missing historical prices or relax quote freshness and outcome rules.
