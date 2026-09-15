# September 14 evening observation corrections

DJT is excluded from active stock collection, scanner membership, and Obsidian
option subscriptions/reviews. Original feed events and historical audit records
are retained. Existing unrelated positions are not liquidated by this exclusion.

New intraday option ideas use option-ideas-v2 and paired-recorded-quotes-v1.
Completed records and legacy open observations keep their original model.
Option and underlying archives are merged in source-time order, including both
updates at equal timestamps before evaluation. Replay starts after observed entry.
Both quotes must have been fresh when recorded and must be current at evaluation.
The loaded prefix of either archive bounds consumption; incomplete batches cannot
jump to latest prices. Genuine gaps still resolve as missing observations, not
losses, and stops/targets/fees remain unchanged. Historical bars never repair live
quote paths. This fixes worker-lag losses of evidence, not absent provider quotes.

Remaining periodic futures health and mapping-renewal SQL runs on the background
writer instead of the SDK callback. Startup mappings still establish identity
before data use. Writer queue/commit diagnostics are persisted for acceptance.
The 15-second quote-path rule remains unchanged. Current minute history readiness
and genuine absent bars still require live verification; no synthetic OHLC bars
or historical quote fills are introduced.

Swing technical candidates are retained before the existing flow-entry filter,
with frozen flow coverage. During cash hours the latest per-symbol scan reason is
also preserved separately from overnight status. Daily recovery now includes the
full swing universe's 60-session/10-week requirement, still within the existing
three-request/pass and eight-symbol/batch limits. Entry rules and notifications
are unchanged. No technical candidate is promoted to a trade because flow is absent.

The first forward-acceptance-v1 engine pass stores an immutable correction time.
Feed health shows before/after option creation cohorts, failure reasons, swing
readiness, and technical candidates lacking flow. Its authenticated API includes
bounded failure details and futures persistence diagnostics. A once-per-minute
private audit verifies production behavior. Fresh secondary assessments and later
measurements accumulate prospectively; old verdicts are never rewritten. Closing
old missing observations is not evidence of a new model's success.
