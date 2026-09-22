# Quote collection and observation recovery v5

This release keeps research independent of paper accounts and preserves source
quote timestamps, existing freshness limits, brackets, and original outcomes.

1. Recovery reads all active option studies (including scanner 0DTE, scheduled
   SPY, SPY timing, swing and post-gap follow-ups). Options keep their bounded
   Massive/OPRA fallback. A separate Alpaca stock batch recovers stale active
   underlyings, up to 32 symbols per cycle, five-second per-symbol cooldown and
   two-second HTTP timeout. Provider backoffs are separate; no indicative feed.
2. Active contracts are pinned before pending candidates and background chains.
   Already selected active contracts keep their places. Excess active demand is
   listed explicitly as `active_missing`; subscription requests are not claimed
   to be acknowledgements or fresh data. Active underlyings precede the broad
   stock universe. Follow-ups release demand at their original deadline.
3. New v5 setup and paired option observations have a 30-second processing grace
   before a detected gap becomes permanent. Replay is retried from the last
   verified source time. The 15-second continuity threshold does not change.
   Only original fresh-at-storage quotes qualify; late historical data cannot
   repair a live result. A gap stops evaluation before any later target/stop can
   be awarded. Quote writes commit in at most 32-row transactions. Socket,
   source and completed-storage clocks remain distinct in health diagnostics.
4. Confirmed gaps create independent follow-ups through the original study
   deadline, including swing gaps. Fresh later quotes are sampled at most once
   per minute, with unavailable sides explicitly listed. These are measurements,
   not replacement entries/exits; original P&L remains null and outcome unresolved.
   Existing historical unresolved observations are not retroactively restarted.
5. Daily results contains complete collection-version cohorts with opened,
   completed, unresolved, gap counts and recovered processing gaps; separate
   post-gap sample counts; active subscription shortfalls; and current storage
   latency. Latest follow-up detail is bounded to 25 records and labeled as such.
   Cohort age and market mix differ: an early change in gap percentage does not
   establish improved strategy returns or a statistically proven reliability gain.

Validation: regression tests cover late-visible original storage, stale storage,
a target beyond an unknown interval, prospective follow-up samples, expiration,
underlying fallback, provider backoff and capacity ordering. The full suite also
runs PostgreSQL lock-order tests and the packaged production application.

Live acceptance requires the deployed version on all services, original-clock
stock recovery receipts, active subscription coverage/shortfalls, and advancing
v5 observations. Sustained improvement requires mature subsequent sessions;
missing quotes and provider silence remain explicit, never counted as passes.
