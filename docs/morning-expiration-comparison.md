# Morning expiration comparison v1 — 2026-09-16

Morning stock direction is not an option recommendation. The earlier audit had
no 0DTE contracts and cannot rank their outcomes against the current selection.
This addition collects prospective comparisons and exposes costs; it does not
choose a new winning strategy.

## Selection and evidence

For each fresh, non-test stock alert, query the actual active standard 100-share
CALL catalog from signal day through 14 calendar DTE, within the existing 20%
strike band. Fully search at most three 1,000-contract pages; incomplete catalogs
produce unavailable evidence. Existing baseline remains earliest listed expiry
strictly after today, then nearest strike with the lower strike breaking ties.
The research arm selects nearest strike expiring that day. The 1–3 calendar DTE
arm is the baseline itself when its expiry qualifies; otherwise unavailable.
Never infer a same-day listing from a weekday or from the ticker's other expiries.

Use one OPRA snapshot request for all distinct selected symbols. Store policies,
contract identity, entry stock price, source/fetch timestamps, sizes, Greeks/IV,
and cost-model version. Quotes must satisfy the existing positive-size, fresh,
noncrossed OPRA checks. New comparison entries also require quote timestamp at
or after the signal and completion within 120 seconds. Unavailable entries remain
unavailable; subsequent quotes cannot repair them retrospectively.

Additive expiration_tracks_v1 and expiration_samples_v1 tables store the 0DTE
arm. Baseline tables continue to store the original arm. The near-expiry alias
reuses baseline samples. Freeze references and contracts; schedule 60 observations
once, with signal-relative minute identity. One shared, bounded 100-contract
batch services both tapes. Row locks, expiring leases and conditional commits
support restart/concurrency. A slot is sampled 5–30 seconds after its due time;
new study quote timestamps must fall between due time and due + 30 seconds.
Late or unavailable quotes remain explicit gaps. Histories create no Discord rows.

## Cost calculations and reports

Assume buying at initial ask, selling at observed bid, 100 shares per contract,
and $0.65 commission/fee allowance per side ($1.30 round trip). This is a research
assumption, not the user's actual broker fee schedule. Net return divides net
P&L by ask premium plus entry fee. Stress P&L subtracts another $0.01/share/side
($2 per contract). Prices are quote observations, not fills, and do not account
for all execution costs or the trader's actual entry delay.

Spread cost = (ask − bid) × 100. Approximate underlying cost hurdle =
(ask − bid + 1.30 / 100) / delta, only with a finite positive call delta <= 1.
This fixed-delta estimate ignores IV, theta and gamma changes and is not a target.
The existing >20% of midpoint spread flag remains visible. There is no new
unvalidated quality threshold suppressing stock alerts.

Both reports show contract availability, premium, spread, fees, delta/moneyness,
fixed 5/15/30/60-minute net results, observed best minute and giveback to 60m.
Best observed results are hindsight and may miss intraminute extremes. Each
aggregate pair uses the same alerts at all four horizons, with both entry and
exit quote timestamps within five seconds across arms. Missing data is excluded,
not assigned a zero or loss. Per-contract dollars and premium-normalized returns
do not establish equal dollar risk or equal delta exposure. No automatic winner,
policy promotion, trading order or investment recommendation is produced.

## User-facing behavior and ownership

Original Morning continues its single initial stock message plus one edit of
that message. The edit includes costs and comparison availability. The Compass
native preview uses the same format; notification ownership/routing is unchanged.
No TradingView script, saved alert, webhook, Discord channel or credential changes
are required. No ad hoc Discord test messages are sent.

Compass's native Morning report owns its own references and tapes independently
of source exports. Native report JSON includes the comparison and raw 0DTE slots.
The original Morning dashboard also displays the comparison, and full-data ZIP
includes expiration-comparison.json with references, results and raw 0DTE slots.
Normal date/ticker filters apply; truncated displays are not full-history studies.
Historical alerts lacking an entry comparison say not_collected. The source and
native quote times remain separate; they must not be pooled as independent trades.

## Validation, rollout and rollback

Tests cover actual-expiration availability, alias identity, paired batching,
invalid/stale/pre-signal quotes, absent delta, fees and stress costs, missing-data
exclusions, timing skew, restart recovery, PostgreSQL concurrent claim deduplication,
source single-message edits, native shadow ownership, filters and raw exports.
Run each repository's full CI suite including its dedicated PostgreSQL database.
Normal deployment performs additive schema creation before ready; no source data
is deleted or rewritten. Reverting the application commit stops new comparison
collection and retains existing tables/data. Sender ownership does not change.
First new eligible live alerts are still needed to verify provider availability
and complete 60-minute comparison tapes after deployment. No historical study
can be completed from quotes this feature has not collected yet.
