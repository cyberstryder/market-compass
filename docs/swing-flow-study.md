# Swing rejected-candidate comparison

Protocol `swing-flow-study-v1`, frozen September 16, 2026 CT before its forward
evaluation. Open `/#swing-study` in the authenticated Compass dashboard. The
study asks whether the existing flow requirement selects stronger underlying
moves or excludes useful technical candidates. It does not establish that an
option was available, affordable or profitable.

## Population and frozen evidence

Every newly retained `swing_candidate` from the existing Swing scanner is
registered once, keyed by its event ID. The original watchlist, technical
families, session window, option admission, fees, exits and alert rules are
unchanged. Separate trigger bars remain separate, potentially overlapping
observations. No candidate is added solely to produce a study result.

The candidate transaction saves the technical signal, daily/weekly context,
original flow-confirmed flag, admission key and raw pre-gate flow snapshots.
The latest filtered vendor window is refreshed by the existing scanner every
15 seconds. Its source, first-received and last-seen timestamps, expiry policy,
query truncation and vendor recovery state are frozen. Later vendor corrections
cannot replace this assessment. Retained events predating the new capture are
registered as historical inventory, with original evidence preserved and no
invented pre-gate group, entry price or forward result.

The common reference is the observed stock midpoint when the candidate is first
processed, within 30 seconds of retention and 90 seconds of the completed signal
bar. A valid quote has positive bid/ask sizes, positive bid below ask, source time
at or after the signal, age at most five seconds, and source/storage clocks no
later than assessment. Missing references remain unavailable. The original
signal price, stop, target and whether the reference remains within 0.25 daily
ATR are retained as context. References beyond that tolerance are shown as their
own group; this broad comparison does not claim they could pass option entry.

## Flow groups

The observed window contains at most 5,000 received filtered TM records from the
last 30 minutes; detection of a further row marks coverage unavailable. Explicit
vendor bullish/bearish sentiment determines alignment, not CALL/PUT alone. The
captured configured expiry range defaults to 14–60 calendar DTE. Supporting flow
requires at least $100,000 across two distinct IDs, or at least $250,000 from one,
and at least twice the opposing classified premium. These are the existing Swing
rules, not thresholds fitted to this study's outcomes.

| Group | Rule at candidate assessment |
| --- | --- |
| Fresh flow | Supporting flow with the latest aligned print at most 300 seconds old and the existing vendor freshness gate passing. |
| Delayed flow | Same premium, sentiment and expiry requirements; latest aligned print older than 300 and at most 1,800 seconds. A vendor stale flag is recorded without discarding this observed delayed context. |
| Fresh prints / vendor gate blocked | Supporting print no older than 300 seconds, but vendor freshness prevents live confirmation. |
| No confirming observed flow | Sufficient recorded feed coverage, but supporting-flow requirements are not met. This does not mean no market flow occurred. |
| Unavailable flow coverage | Missing/older-than-30-second snapshot, missing/older-than-60-second poll, incomplete recovery, query truncation or future/invalid clocks. No flow conclusion is assigned. |
| Historical inventory | Original pre-gate assessment was not captured. Kept outside prospective comparisons. |

Future source, receipt or correction snapshots are excluded and explicitly make
the affected captured context unavailable. The vendor's $50k minimum filter,
moving pagination and omissions still limit the observed universe; reconciliation
of received IDs is not proof of complete market coverage.

## Outcomes and comparisons

All selections share the same candidate reference, checkpoint schedule and
directional stock-return calculation: `100 × (endpoint / reference − 1)`, negated
for short setups. The primary endpoint is the **fifth later session close**.

| Checkpoint | Target |
| --- | --- |
| `60m` | 60 exchange trading minutes after assessment; carries into the next session if necessary. |
| `close` | Candidate-session close. |
| `1session`, `3session`, `5session` | First, third and fifth later session closes. |
| `9session` | Ninth later close: the tenth session including the candidate day. |

XNYS holidays and early closes apply. Close targets are just before the close,
not an official auction print. A checkpoint uses an archived valid quote sourced
within the five seconds ending at target and stored by target plus five seconds.
It must be fresh both when stored and at target. An excessive quote-query window
is explicitly unavailable. Worker processing begins after target plus ten seconds
and can replay timely archived evidence after a restart. Late backfills cannot
repair a missing checkpoint. Final checkpoint decisions are not overwritten.

The latest stored daily bar for the frozen reference session, available by that
deadline, is compared with the original daily close. Missing reference basis or
a recorded change greater than 1% makes that endpoint unavailable. This detects
known split-adjustment/correction changes; it is not a complete corporate-action
feed. Unobserved corporate actions and ordinary data errors remain review risks.

The primary selections are technical-only, fresh flow, delayed flow, fresh or
delayed flow, any supporting flow including vendor-blocked prints, no confirming
observed flow, and candidates not selected by fresh-flow rules. They overlap and
are not randomized matched groups. Unknown flow coverage is excluded from these
primary comparisons; its price observations remain visible in separate groups.

Report each selection's candidates, measured/pending/unavailable endpoints,
mean directional move, positive fraction, and counts at least +0.5% or at most
−0.5%. Group by flow, rule, side, symbol, receipt session, source age, captured
expiry policy and price tolerance. These are endpoint summaries, not MFE/MAE,
stop/target paths, executable fills, net stock returns or option returns. They
do not account for dividends, borrowing, slippage or fees. Actual simulated
option admissions and outcomes remain in `swing_ideas_v1` and are linked and
counted separately; a pending option is not a fill.

## Coverage, operation and review

Immutable payloads live in `swing_flow_trials_v1`; checkpoint status and outcomes
live in `swing_flow_checkpoints_v1`. The engine runs a separately leased worker
every five seconds, including batched historical inventory registration. Full
30-day receipt-window SQL summaries refresh every minute. Lifetime retained and
registered counts reconcile separately. Individual pages show at most 100 rows,
using stable receipt-time/ID cursors and a pinned assessment cutoff; current
outcomes and option statuses may advance while browsing.

Pending prospective symbols remain requested for stock quotes/history after a
watchlist change. Existing monitored symbols retain priority within the existing
500-symbol ceiling; requested/included/outside counts are visible. This study
adds no option subscriptions, paid data plan, Railway service or Discord sender.
Read-only authenticated APIs are `/api/swing-study` and
`/api/swing-study/records`.

September 17 is the first planned full-session classification and checkpoint
coverage check. For candidates that day, the one/three/five later closes are
September 18/22/24; the tenth session including entry is September 30. Later
candidates mature later. September 18 is an initial diagnostic review, not a
promotion deadline. Original admitted option simulations retain their own
entry-based holding deadlines.

Freeze this version through the initial September 17–25 intake period and wait
for relevant checkpoints to mature before comparing completed cohorts. Report
session/symbol concentration and missingness alongside results; repeated setups
are correlated and aggregate percentages are not independent-sample confidence
estimates. Sparse or unbalanced groups remain inconclusive. Any selection change
must get a new version and later untouched evaluation sessions. No automatic
alert-rule promotion or profitability claim follows from this study.
