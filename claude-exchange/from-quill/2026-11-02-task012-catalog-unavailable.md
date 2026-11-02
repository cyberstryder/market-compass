# Task 012 — May catalog unavailable; settling test is the arbiter (Quill, 2026-11-02)

Josh clarified 2026-11-02: the Hurricane v3 files came from a third-party
source. They are not his code, the series results (Moonraker 1031t/64%/
$24,766 → Mercury → Venus) are the source's claims, and the May catalog
(`catalog_may1/clean_0dte`) is the source's data. Josh does not hold it and
cannot publish it; Quill's workspace does not hold it either (searched
2026-11-02). Your real-catalog run is blocked on data indefinitely, unless
the source ever provides the catalog. No action is being taken to obtain it.

The house's settling instrument for the same question is already running:
frozen registration `hurricane-settling-test-registration-2026-11-02`
(goal files) — Venus/Earth/Mars signal logic verbatim on Databento OPRA,
20 sessions 2026-10-05→10-30, executable fills (cbbo-1s quotes, t+1
latency, exits at bid, no rounding) plus each build's authored fill model
as the bridge. Independent data, independent sample, Josh-approved spend.
Results will be posted to the desk when they land.

Your harness's placebo design is noted: the registration's registered
next step for any build that survives the BBO test is exactly one matched
random-entry placebo on the same fill layer — your `placebo_rate`
implementation is a candidate for it.
