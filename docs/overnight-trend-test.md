# Full-session futures alerts: Lucid evaluation and funded accounts

Requested September 14, 2026. Status: research specification; no new strategy or
daily profit stop has been activated. Original futures automation is unchanged.

## Account and session constraint

The user confirmed **Lucid Trading, $50,000 LucidFlex evaluation**, with a funded
profile also required for the eventual transition. No position may carry between
exchange trading sessions. Any purchased optional daily loss limit, current
account loss floor and daily profit objective have not yet been confirmed. Do
not infer these from the nominal $50,000 size.

Lucid's published regular-session schedule requires Flex positions closed by
4:45 p.m. Eastern and allows trading again at 6:00 p.m. Eastern, Sunday through
Thursday. In Chicago that is a 3:45 p.m. cutoff and 5:00 p.m. reopen. The source
labels Eastern time as "EST"; implement named exchange/account timezones and
validate daylight-saving transitions rather than hard-coding a UTC offset.
Holiday closes override normal hours.
[Lucid allowed trading times](https://support.lucidtrading.com/en/articles/11404729-allowed-trading-times).

For this study, use **3:42 p.m. America/Chicago** as the regular-day flatten
deadline, and the earlier exchange close minus three minutes on shortened days.
Block new entries during flattening and the daily closure. Confirm zero open
positions and cancel residual orders; a flatten instruction alone is not proof
of closure. The next session requires a fresh signal and an explicit risk reset.

"Overnight" here means trading during the night inside a single futures session:
for example, entry at 2 a.m. and exit at 5 a.m. It never means holding through the
daily cutoff or using the full multiday selloff as one trade. Midnight does not
end the futures risk day. Once the chosen daily goal is secured, keep that account
locked against new entries for the rest of the exchange session, even if the US
cash session has not opened. Continue detecting and recording later setups as
observation-only.

The published 50K Flex max-loss amount is $2,000 with an end-of-day trailing
calculation; the account's current floor and optional daily limit must be
verified before deriving executable risk limits. The published evaluation target
is $3,000 with a 50% consistency rule. Funded Flex has no consistency percentage
and uses a scaling plan. Keep these stage-specific rules separate.
[Lucid drawdown](https://support.lucidtrading.com/en/articles/12945815-lucidflex-drawdown),
[evaluation](https://support.lucidtrading.com/en/articles/12945790-lucidflex-evaluation-account),
[funded account](https://support.lucidtrading.com/en/articles/12945795-lucidflex-funded-account).

These are documented design constraints, not a change to any running alert,
broker connection, existing account limit or existing strategy cutoff.

## One setup engine, two account profiles

Use the same market observations and versioned setup definitions for evaluation
and funded research. Apply account-specific sizing and eligibility afterward;
changing account stage does not itself establish a different trading edge.

| Control | Evaluation: current account | Funded: future account |
| --- | --- | --- |
| Objective | Track progress toward the published $3,000 evaluation target | Preserve the account and evaluate repeatable net results; no evaluation target |
| Consistency | Track largest profitable day divided by total profit against 50% | No Flex consistency percentage |
| Published contract ceiling | 4 minis / 40 micros; not a recommended position size | Scaling: 2 minis / 20 micros at $0–999 profit, 3 / 30 at $1,000–1,999, 4 / 40 at $2,000+ |
| Loss control | Current trailing loss floor, any purchased daily loss limit, and a separately chosen internal risk reserve | Reconcile the current floor and balance, including payouts, and apply the funded scaling tier |
| Entries and exits | Compare fixed-target and trailing-exit variants on matched signals | Reuse qualified setups; compare exit policies without assuming funded status warrants larger risk |
| Session boundary | Flatten before the daily cutoff; fresh eligibility next session | Same no-carry rule |

The evaluation consistency rule is a qualification condition, not an automatic
daily profit breach: an unusually large winning day can require more total
profit before passing. Track it separately from the user's optional daily goal.
Funded scaling updates at the end of the session, not immediately when intraday
profit crosses a tier. It can decrease after losses or payouts. Actual platform
limits remain authoritative; the research controller must not infer extra size
from unrealized gains or an unknown account state.
[Lucid consistency](https://support.lucidtrading.com/en/articles/12945805-lucidflex-consistency-percentage),
[Lucid scaling plan](https://support.lucidtrading.com/en/articles/12945808-lucidflex-scaling-plan).

Keep evaluation and funded simulation ledgers distinct. A live account can have
only one selected stage, changed explicitly after its new account rules and
state have been verified. Every strategy on that account must share its risk
budget and lock state. A futures account lock does not mute options alerts or
other systems trading separate accounts.

## Objective

Detect tradable trends, breakouts and confirmed reversals throughout the allowed
futures session, including Asia, Europe, the US open and the daytime session.
Overnight trend capture is one playbook in the wider alert system. Test capturing
a portion of an MES or MNQ trend while Josh is asleep, protecting the profit, and
stopping new account entries once the selected daily objective is secured. Keep
observing subsequent daytime opportunities even after that account is locked.
Favor few trades, controlled drawdown and low profit giveback. No target requires
a trade on every session. The submitted chart covers August 17–20; it is
an example of the desired behavior, not proof of one executable trade. Its price
axis is cropped, so no point count or P&L is inferred from the image.

## Verified starting point

| Component | Current behavior | Gap for this objective |
| --- | --- | --- |
| MNQ v0.10.3 | Session/overnight level rejection in Trend Speed direction, one MNQ, fixed 1.5R bracket, 60 three-minute bars maximum, two entries per session | Not a dedicated trend-continuation entry or trailing exit |
| MGC v0.7.3 | One MGC, range/MFI short, fixed 1R bracket, 60-bar timeout | Different setup; no shared account risk state |
| Compass scanner | Full futures session support; opening-range retests, sweep/reclaims, EMA/VWAP pullbacks, volume breakouts, exposure crossings and flow-confirmed breakouts | Uses one-minute technical triggers; no dedicated three-minute trend cohort or stage-aware account controller |
| Compass simulator | Fixed 2R target, loss and position-count limits | No runner, account-wide realized-profit stop, or broker reconciliation |
| Futures source integration | Custom Pine alert messages mirrored into Compass | Bracket fills and actual broker P&L are absent; an exit instruction cannot prove a fill |

The existing scripts use full electronic sessions and flatten at 15:42 CT on
their supported regular sessions. Compass currently uses a separate 15:45 CT
research cutoff. The new study uses the LucidFlex calendar and 15:42 CT buffer
specified above; it must not silently treat these different schedules as equivalent.

Live feed check on September 13, 2026 at 22:13:43 America/Chicago (September 14
UTC): Compass reported `MES.c.0` resolved to `MESZ6` as ready with a 0.3-second
quote age, and `MNQ.c.0` resolved to `MNQZ6` as ready with a 0.1-second quote age.
Both micro contracts are already covered by the live futures feed. This is a
point-in-time data check, not verification that the proposed runner or shared
account controller is running.

## Alert families and implementation status

The ES/NQ family shares setup logic, with contract-specific prices, tick values,
costs and sizing for ES, NQ, MES and MNQ. The initial runner study compares MES
and MNQ in parallel. Test long and short directions separately, and retain each
family's identity even when several conditions support the same trade idea.

| Family | Evidence for a candidate | Current status |
| --- | --- | --- |
| Trend continuation | Completed trend bias and EMA/VWAP pullback followed by continuation | One-minute scanner exists; three-minute entry and trailing runner are proposed |
| Range or consolidation breakout | Price clears the established range with supporting volume | Scanner exists; sustained-trend exit comparison is proposed |
| Opening-range breakout/retest | Break and retest of the US opening range | Scanner exists; inherently tied to the opening window |
| Liquidity sweep/reclaim reversal | Session extreme is swept and reclaimed with the detector's confirmation | Scanner exists |
| Failed-breakout reversal | Break fails, price closes back into the range, and a retest/structure trigger confirms failure | Proposed separate detector |
| Trend reversal | Prior trend loses structure, then an opposite structure break and failed reclaim confirm a transition | Proposed separate detector; extension or one contrary candle is insufficient |
| Range rotation | A defined range boundary rejects with confirmation during a range regime | Proposed; volume-profile variants require a separately verified profile implementation |
| Exposure/flow-supported move | Price trigger agrees with sufficiently fresh exposure or unusual-flow evidence | Existing families where coverage is available; stale options context is not live confirmation |

Trend, range and transition classification should choose eligible playbooks.
Reversals must be tested separately from trailing exits; a trend exit is not
automatically a signal to open the other direction. Publish one primary alert
for overlapping same-direction signals, with supporting conditions attached,
while preserving every candidate in the research ledger. A confirmed opposing
setup invalidates an incompatible pending idea; it must not automatically flip
an open broker position.

## MES versus MNQ comparison

Treat the user's observation that MES trends look smoother as a testable
hypothesis. MES tracks the S&P 500 and MNQ tracks the Nasdaq-100; lower volatility
or a differently scaled chart does not establish better trend persistence or
better net trading results. Use the same versioned setup definitions and
confirmation timeframes for both, with separate results by contract, direction,
setup family and Asia/Europe/US window. Compare matched observation windows and
retain no-signal periods; do not select only the clean moves visible afterward.

| Contract | Dollars per index point | Minimum tick | Dollars per tick |
| --- | --- | --- | --- |
| MES | $5 | 0.25 point | $1.25 |
| MNQ | $2 | 0.25 point | $0.50 |

[CME MES specifications](https://www.cmegroup.com/markets/equities/sp/micro-e-mini-sandp-500.contractSpecs.html),
[CME MNQ specifications](https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html).

Place invalidation using each market's own structure and volatility, then apply
the same dollar-risk ceiling. Do not copy an MNQ point stop onto MES. For unit
comparison, 20 MES points and 50 MNQ points each represent $100 with one contract,
before fees and slippage; these are not selected stop distances. Start with
one-contract diagnostics and compare results in both net dollars and initial-risk
units, recording actual initial risk. A shared ceiling does not make every trade
exactly equal-risk; reject candidates that cannot fit the budget after costs.

Measure retracement relative to volatility, false-breakout and stop-out rates,
trend capture, MFE giveback, net expectancy after instrument/session-specific
costs, and worst drawdown. Report directional efficiency over fixed matched
windows as absolute net close change divided by the sum of absolute consecutive
close changes. Mark zero-movement windows as undefined and exclude incomplete
windows rather than filling data gaps. Freeze window lengths and detector
parameters before the holdout comparison; do not favor MES simply because its
raw point swings are smaller. Forward shadow results must support any eventual
priority between the two markets.

Both contracts represent equity-index exposure. Preserve all independent
candidates for research, but treat simultaneous same-direction candidates as
competing for one account risk budget, not as automatic diversification. For the
first combined simulation allow only one of MES/MNQ open at a time, with a
predeclared selection rule based on information available at the trigger. Do not
choose the winner after seeing the move or double the attempt/risk allowance
because a second symbol is available. This proposed cross-instrument account
policy is not implemented by the current scanner and does not change the
independent MNQ/MGC automation.

## Alert lifecycle and account eligibility

Monitor throughout permitted futures hours. Technical alerts occur after the
required bar closes and eligible fresh quotes arrive; a two-second scan loop
does not turn a three-minute confirmation into an instantaneous entry.

Record candidate, confirmed simulated entry, invalidation/skip, stop update,
simulated exit and account-lock events. Actionable alerts should state symbol,
direction, setup family, session, trigger time, quote time/age, reference entry,
initial stop, target or trailing policy, risk estimate, and evaluation/funded
eligibility. Clearly distinguish simulated instructions from broker fills.
Use persistent event IDs so restarts do not resend an entry or reset account risk.

After a daily profit or loss lock, continue recording the market and qualifying
setups. Any subsequent notification must say **observation only — account
locked**, with the reason; do not emit another account-entry instruction. This
preserves daytime learning after an overnight trade finishes the account's day.
Account locks remain effective until a verified reset for the next permitted
exchange session, not merely until midnight or the cash open. Preserve other
projects' independent alerts.

Provide a morning recap of overnight observations and a session recap covering
daytime activity. These are planned outputs, not newly scheduled notifications.
No alert by itself opens or manages a position while the user is asleep; that
requires separately enabled and verified execution after shadow testing.

## First test design

1. Study MES and MNQ in parallel, both directions, with dated unadjusted contract
   data and explicit roll handling. Study Asia, Europe and US periods separately
   within the same exchange trading day. Do not reset daily risk at midnight.
2. Require completed 15-minute trend direction. Form the entry on a completed
   three-minute continuation bar after an EMA/VWAP pullback. Evaluate a separate
   consolidation-breakout candidate for trends that do not pull back. Keep their
   identities and results separate; do not optimize both from this screenshot.
   Apply these tests during the day as well as overnight. Add failed-breakout
   and confirmed trend-reversal cohorts separately, rather than treating every
   trend exit as a reversal entry.
3. Fill only on an eligible quote after the trigger, with spread, fees and adverse
   slippage. Define initial invalidation before entry. Start with one micro
   contract per trade, no averaging down or pyramiding, and at most two attempts
   per exchange session across the combined MES/MNQ simulation. Use the existing
   $100 planned-risk ceiling as a research comparison, not as a new live account
   instruction. Reject a stop that cannot fit the study budget after costs;
   preserve its rejected candidate record for comparison.
4. Compare exits on the same accepted signals: the existing fixed target against
   one predefined volatility/structure trail. Update a trail only after the
   confirming bar has completed; the tightened stop becomes effective afterward.
   Never use a later bar high/low to award an earlier stop fill. Record stale-data
   intervals and unresolved exits instead of inventing fills.
5. Compare one selected daily objective against a no-profit-cap baseline. The
   dollar objective has not been chosen by the user. Values such as $200, $300 or
   $400 are examples for discussion, not approved live settings. When a goal exit
   is requested, latch the session against new entries; report the actual net
   result after the exit rather than assuming the threshold was achieved. A
   separate loss stop and account drawdown reserve always remain in force.
6. Flatten at the earlier of 15:42 CT or three minutes before a shortened exchange
   close. A trend continuing into another exchange session requires a new eligible
   entry. Carrying a position through the closure is never an eligible result.
7. Use a separate durable research ledger, independent of existing strategy
   positions and performance. Compare evaluation and funded policy results from
   the same observed candidates. Emit simulated entry, stop-update, exit and
   daily-lock events with timestamps and reasons. Provide morning and session
   reviews of what was seen, entered, skipped, earned or lost, and whether the
   daily goal was met. Continue the observation ledger after an account lock.

Overnight entry eligibility must rely on live futures data. Last cash-session
options GEX/VEX and unusual-flow data can be dated context; they must never be
presented as fresh overnight confirmation or block the test merely because the
options market is closed. No new subscription is established as necessary for
the MES/MNQ price-based study. Both live feeds were verified above; historical
entitlement and coverage still need verification for each replay window.

## Evidence required

Use the pictured period only for development inspection, then chronological
unseen sessions and a forward shadow test. Compare net P&L, worst drawdown, MAE,
MFE, profit giveback, costs, attempts/session, rejected signals, overnight vs US
results, evaluation consistency, funded tier eligibility, and fraction of
sessions finishing at the goal. Report each setup family's results separately
and combined after conflict handling and account limits. Track losses on choppy
overnights and how often the trail exits before a trend resumes. A visually large
move or one strong week is not validation. Reserve any paid historical request
for a separately bounded window and known cost; no new backfill was requested here.

Before unattended real orders, a shared account controller must reconcile actual
broker positions, working protective orders, fills, net session P&L and prop
drawdown. It must coordinate every strategy using that account, reject duplicate
entries and entries during uncertain state, recover after restarts, and confirm
flattening. Independent Pine local locks cannot enforce this across MNQ and MGC.
Compass remains observation-only until that capability is implemented and tested.

## Source references

Inspected saved originals: `MNQ_Level_Rejection_Speed_v0.10.3_TradersPost_JSON.pine`
and `MGC_Range_MFI_v0.7.3_TradersPost_JSON.pine`, September 9, 2026; Compass
`compass/scanner.py`, `compass/engine.py`, `compass/futures.py`, and
`docs/project-connections.md` at base `ad4aff409a68589d233ae617bb31bd9c0877e07a`.
Rechecked scanner coverage in `docs/live-scanner.md`, `compass/scanner.py`,
`compass/engine.py`, `compass/futures.py`, and `compass/config.py` at
`7dcd462df86fa802a7f1f03299c91ef7566a07f0`. Lucid account-stage rules checked
September 14, 2026; purchased limits and the dashboard's actual account state
still require account-specific reconciliation.

MNQ is $2 per index point per contract: a hypothetical 150-point capture with one
contract is $300 gross before fees and slippage. This is unit arithmetic, not a
measurement of the screenshot or a forecast.
[CME contract specifications](https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html).
TradingView distinguishes custom `alert()` events from order-fill alerts and runs
saved alerts on its servers:
[TradingView alerts documentation](https://www.tradingview.com/pine-script-docs/concepts/alerts/).
