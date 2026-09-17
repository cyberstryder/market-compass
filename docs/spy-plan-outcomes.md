# SPY plan → 0DTE outcome ledger

Protocol `spy-plan-0dte-v1`, frozen September 16, 2026 CT before forward evaluation.
Open `/#spy-study` in the authenticated Compass dashboard. This connects the
existing daily plan and scheduled confirmation messages to prospective,
one-contract SPY option observations. It does not change the 15-minute
confirmation baseline or send additional messages.

## Saved decisions and timing

The scheduled brief transaction registers the exact generated report: pre-open,
opening and conditional later checks. The immutable copy includes report version,
frozen premarket range, confirming candle, data blocks, decision, underlying
brackets, watch-contract references, dated gamma context and SPX/XSP estimates.
Its source key links back to the original report and Discord outbox event.
Pre-open and opening keys link related reports for the session.

Historical reports are inventoried separately, without recreated entries or
returns. A restart cannot overwrite a report assessment or create a second
option observation for the same session. Only the first eligible confirmed report
can create a paper observation; WAIT, NO ENTRY and informational reports have no
assumed option trade.

After each prospective schedule window, the worker records missing/unused slots.
It distinguishes an expected missed check, follow-ups suppressed after a saved
confirmation, follow-ups blocked without an opening WAIT and a disabled brief at
audit time. These are not invented WAIT decisions. The activation timestamp and
a durable session cursor prevent retroactive gap claims before deployment and
allow missed sessions to be audited after a restart. Audit-time configuration is
not proof of the configuration throughout an earlier missing window. A later
report remains a separate record linked by its expected source key.

## Contract and entry rules

| Component | Frozen policy |
| --- | --- |
| Instrument | SPY only; listed same-day expiry, standard 100-share multiplier, whole-dollar strike. OCC-style symbol must agree with underlying, expiry, side and strike. |
| Contract choice | Use the saved report's directional watch contract when present. Otherwise select the nearest observed whole-dollar strike to the report's frozen SPY spot, with lower strike breaking a tie. Freeze the choice at first eligible selection; never switch to a later winner. |
| Chain | Stored by decision time, no older than 180 seconds, and provider pagination complete. The selected contract must remain verified in that chain before entry. |
| Delivery | The original plan event must have a saved `sent` receipt for `spy_morning`, including message ID and confirmation time. A chart-message receipt does not substitute. This proves acknowledged posting, not that a person read the message. |
| Entry clock | First eligible paired quote after that delivery receipt and before the original confirming candle boundary plus 125 seconds. Registration must be within 30 seconds of report generation. No late replay entry. |
| Stock price | Fresh positive-size, uncrossed SPY bid/ask; within the frozen stop/target bracket at entry. The reference, stop and target are not moved to improve the result. |
| Option liquidity | Positive-size, uncrossed bid/ask; bid at least $0.10; spread at most 10% of midpoint and $0.30. Both source quotes must be at/after confirmed delivery and at most five seconds old. |
| Receipt clocks | Entry source, receipt and stored-state times cannot be in the future. A quote stale or future-stamped when stored does not become an eligible entry later. |
| Pre-entry continuity | Each feed has at least five usable samples spanning 20 seconds in the last 30 seconds, latest age at most five seconds and no gap over ten seconds. Truncated evidence does not pass. |
| Modeled purchase | One contract at observed ask plus $0.01, with $0.65 entry fee. Full debit and spread are saved. |

These paper liquidity and continuity gates are stricter than the informational
brief's displayed watch quotes (up to ten seconds old, with a 20% spread flag).
A confirmed price setup or a displayed premium is therefore not necessarily a
modeled entry. The original plan/alert qualification is unchanged. The ledger
records report-to-entry and receipt-to-entry delay, including time spent waiting
for contract data or continuity. No human reaction delay or queue fill model is
claimed.

There is no additional premium stop/target, delta or open-interest selection rule
in this protocol. Delta/IV/OI are saved when supplied as context. Portfolio entry
counts and daily risk ledgers are not used or modified by this separate research
observation. No broker order is created.

## Path, exits and costs

The existing plan's SPY invalidation and 2R target remain frozen. Here R is the
plan's 0.2 times prior 14-day mean true range; the report itself remains the source
of the exact prices. For calls, observed SPY bid tests the lower stop and upper
target; for puts, observed ask tests the upper stop and lower target. Otherwise
exit five minutes before the exchange cash close, including early closes.

Each paired observation uses fresh original stock and option quotes. A gap over
15 seconds in either feed makes the path unresolved. Source timestamps, original
storage times and bounded archive replay allow timely samples to be recovered
after a worker restart. Late backfills cannot fill missing intervals. A session
exit requires a usable pair by cutoff plus five seconds; later prices do not
stand in for the missed exit.

At an observed exit, sell at option bid minus $0.01, floored at zero, and charge
another $0.65. Adverse gaps use the observed option bid; the underlying stop is
not an option-price guarantee. Favorable underlying targets likewise use the
recorded bid, not a theoretical premium. Net P&L is
`100 × (exit premium − entry premium) − $1.30`, and net percentage return divides
that by the initial ask-plus-slippage debit and entry fee.

Every evaluated paired mark is retained with both quote snapshots, source-time
decision, processing time, liquidation value and net return. The ledger preserves
entry/exit evidence, best/worst sampled option return, peak time, first +10/+25/+50%
net milestones, underlying favorable/adverse points and directional stock change.
Stock movement and option P&L are separately labeled. Quotes are sampled, not a
complete tick path; excursions describe only the retained observations. Spread,
slippage and fees are modeled assumptions, not evidence of actual execution.

An unfilled candidate ends excluded with its last entry reason. An entered path
with missing evidence ends unresolved with null P&L, outside completed win/loss
totals. Previously valid partial marks remain inspectable. Closed results alone
must not be used to hide unresolved paths or excluded entries.

## Storage and collection

`spy_plan_checks_v1` stores immutable reports and schedule audits;
`spy_plan_options_v1` stores the linked observation lifecycle;
`spy_plan_marks_v1` keeps paired quote evidence. PostgreSQL state and the original
Discord outbox remain the source of report/delivery provenance. The engine runs
a separately leased worker every two seconds, with reports refreshed each minute.

Selected pending and held SPY contracts use the existing option subscription
budget. All held contracts precede pending candidates, and portfolio holdings
retain priority. Open SPY observations share the existing bounded live quote
recovery: at most four Massive requests and one OPRA batch per cycle, with current
cooldowns and timeouts. No new service, provider plan or broker integration is
added. Pending/open SPY observations keep their stock symbol requested if the
watchlist changes, within the existing stock ceiling. Subscription inclusion is
coverage context; actual usable quotes determine eligibility.

Authenticated read-only endpoints are `/api/spy-study`,
`/api/spy-study/records`, and `/api/spy-study/marks/{observation_id}`. Summary
counts use the complete 30-calendar-day session window, and lifetime saved-report
registration reconciles separately. Report pages show 100 records at a time with
an immutable registration cutoff. Path pages preserve a processing-time cutoff
and use stable source-time/ID cursors; every stored mark can be browsed. Current
observation and delivery states can advance while viewing a pinned report page.

## First use and review

September 17 is the first planned full cash-session acceptance check: verify the
pre-open report, opening check, any conditional later checks, delivery receipts,
fresh contract selection and complete price paths. After the required session
exit and processing, reconcile saved reports to registered records, confirmed
setups to filled/excluded candidates, and entered trades to closed/unresolved
paths. No new trade is required to pass the accounting check.

Compare opening and later checks by coverage, spread, delay, direction and
collection version before examining completed net outcomes. The initial
September 18 review is diagnostic. Freeze this protocol through the September
17–25 intake period, and retain more unchanged sessions when evidence is sparse.
Different confirmation groups occur in different sessions and are not randomized
matched entry-time trials. This ledger does not compare an unobserved faster
candle against the 15-minute baseline. Any timing or selection alternative needs
a declared new version and later untouched evaluation. No automatic alert change
or profitability conclusion follows from a few completed examples.
