# Feed health lifecycle

The attention banner counts actionable component statuses. It is separate from
process/deployment health and from whether a quote is eligible for an entry.

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
