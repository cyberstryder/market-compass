# Keep recorded-quote decisions on their evidence clock

The setup evaluator caches one archived quote path per symbol in a scan. That
path is bounded by its `archive_check.checked_at` cutoff. Previously it moved
the decision clock forward after the query and again for later trials sharing
the cache. A slow query could therefore turn unqueried time into an apparent
15-second quote gap, even when the collector had retained a complete path.

Recorded-quotes-v2 trials now use the archive cutoff for every decision against
that cached history. The saved `observation_evidence_at` makes that cutoff
explicit. Quotes arriving after the cutoff are handled by the next scan;
processing latency alone cannot terminate the old snapshot as a missing path.
Terminal `finished`/gap-check clocks describe the evaluated evidence cutoff,
not transaction commit time. Genuine missing paths, stale-at-storage quotes,
future timestamps, truncation, target/stop and session rules retain their guards.
Legacy latest-quote trials retain their existing behavior.

The regression first failed against the old code: two overlapping trials were
marked unresolved after a simulated slow archive read while fresh quotes were
stored every four seconds. With the fix both remain open and the next scan
replays those retained quotes. A second test preserves a real gap already
present at the cutoff. Existing late-backfill, missing-path and fill tests pass.

This is a prospective evaluator repair. Historical outcomes are not rewritten,
and it does not establish that the September 18 overnight gaps were caused by
this defect. Their inspected saved clock differences were small; their causes
remain separate investigation work. No portfolio risk or execution code changes.
