# Futures feed comparison v1

Protocol frozen 2026-09-18 UTC. Version: `futures-feed-comparison-v1`.
This is a separate prospective study. It does not restart, retune or replace the
existing ten-session futures entry-variant protocol dated 2026-09-17.

## Population and fixed period

All new independent intraday futures setup trials across ES/MES, NQ/MNQ, YM/MYM,
GC/MGC, SI/SIL and CL/MCL, including cooldown and late-session research candidates.
The nine existing setup families continue unchanged. Actual dated contract,
strategy version, direction, fill version and alert cohort stay separate.

The worker saves a durable activation timestamp and the next ten complete
exchange sessions using the existing CMES research calendar and its close cap.
The first evaluation session must open at or after activation. Partial-session
observations before it are warmup/development, never evaluation. Membership is
frozen by entry session. This version stops capturing at the tenth session close;
extending or changing it requires a new version. No historical reconstruction.

All feed observations and policy decisions are frozen once at trial creation.
Duplicate signals keep the original evidence. Both policies reuse the same
original executable entry, stop, target, quote path, adverse slippage and net fees.
The study never changes trial admission, source ownership, alerts or account risk.

## Prespecified policies

| Policy | Population | Select when |
|---|---|---|
| Price only | All 12 futures | The original price trial was admitted |
| Cross-market | Six index futures | Both SPY and QQQ have a completed 15-minute bias aligned with the setup direction and a current midpoint on that side of VWAP |
| Flow | ES/MES use SPY; NQ/MNQ use QQQ | At least one qualifying aligned vendor flow observation and no opposing observation |
| Combined (primary filter) | ES/MES and NQ/MNQ | Both cross-market and flow policies select |

Cross-market context requires the cash equity session open, ready features no
older than 90 seconds, positive VWAP, a known higher-timeframe bias and valid
two-sided quotes no older than five seconds. A zero bias or disagreement is a
known skip. Missing/stale/forward-dated inputs are unknown.

Flow uses the existing TraderMatrix polled snapshot, not a claimed full tape.
Its latest source event must be at most 120 seconds old and successful receipt at
most 60 seconds old. Vendor-stale flags and clock ordering are enforced.
Qualifying rows match the proxy, have source age <=120 seconds, score >=85 and
premium >=$100,000. Duplicate vendor IDs count once. All qualifying snapshot rows
are considered; only the eight-row evidence preview is capped. Bullish/bearish
sentiment maps to long/short. Opposing or mixed known sentiment skips; neutral or
no qualifying aligned rows in a current snapshot skips. Unrecognized sentiment
is unknown. Stale/empty-event snapshots remain unknown, not a negative forecast.

Combined decisions require both feeds known even if one already skips. This
keeps both arms on common available evidence. Unmapped policies are explicitly
not applicable. No invented equity/flow proxy is assigned to metals or energy.
Price-only, time-of-day and regime studies cover those instruments throughout.

## Time, market condition and exposure context

Entry clock bins use `America/Chicago`, including DST: overnight 17:00–07:00;
preopen 07:00–08:30; opening 08:30–10:00; midday 10:00–13:00; afternoon 13:00–15:00;
late 15:00 to the research close. Calendar entry gates still apply. These are
fixed clock bins, not proof that volume has been normalized by historical time
of day. Market state reuses the frozen existing assessment: trend, expansion,
range_candidate, mixed or unknown. No retrospective regime relabeling.

GEX/VEX records the sign and sum of all supplied strike values separately for
each metric and the applicable SPY/QQQ proxy. Freshness requires cash hours,
source and receipt age <=180 seconds, valid clock order and no vendor-stale flag.
A missing strike metric makes that metric unknown. These sums describe the
vendor's supplied strike set, not full-market coverage or dealer inventory.
Neither sign is an entry-direction filter. Exposure-conditioned comparisons are
exploratory associations and do not establish incremental predictive value.

## Endpoint, denominators and interpretation

Primary contrast: combined filter minus price only, **net R per common eligible
resolved opportunity**, grouped by exact contract/setup/direction/fill model and
alert cohort. Selected opportunities reuse original R; known skips contribute
zero exposure. A skipped loss can improve the difference without making the
filtered strategy profitable. Report baseline and filtered R alongside the delta.

Unknown/not-applicable feed decisions exclude that opportunity from both arms of
that contrast. Excluded original entries are counted separately. Open and
unresolved paths are not zero returns. Both selected and skipped paths require a
resolved original endpoint for paired outcome statistics. Counts cover all
retained trial rows in the fixed period, not only a UI preview. Raw entry evidence
and original outcome remain available in the individual trial record.

Returns from evaluation rows remain hidden in this report until the tenth session
closes. Coverage can be monitored. Do not retune rules using evaluation outcomes
elsewhere in the application. Warmup results are development evidence only.

For the primary combined contrast, report a deterministic 2,000-resample
session-cluster bootstrap percentile interval only after the fixed period ends,
all known-decision paths leave open status, and at least ten distinct entry
sessions have usable paired outcomes in that exact group. Each draw resamples
whole sessions and takes the ratio of summed differences to summed opportunities.
Fewer sessions means inconclusive. Missing paths are still reported and may bias
complete-case results. Intervals are unadjusted across many contract/setup groups;
they do not establish a validated profitable strategy. Cross-market-only,
flow-only, time/regime and exposure cuts are exploratory. A discovered winner
needs a separately frozen replication period and multiplicity-aware review.

No portfolio return claim: trials overlap, standard and micro contracts share
market information, and filters can change exposure. Account risk limits remain
separate. The original frozen futures protocol and its cohorts remain intact.
