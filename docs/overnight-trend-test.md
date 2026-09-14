# Overnight trend capture and daily profit stop

Requested September 14, 2026. Status: research specification; no new strategy or
daily profit stop has been activated. Original futures automation is unchanged.

## Objective

Capture a tradable portion of an overnight MNQ trend while Josh is asleep, protect
the profit, and stop taking new trades once the selected daily objective is
secured. Favor few trades, controlled drawdown and low profit giveback. No target
requires a trade on every session. The submitted chart covers August 17–20; it is
an example of the desired behavior, not proof of one executable trade. Its price
axis is cropped, so no point count or P&L is inferred from the image.

## Verified starting point

| Component | Current behavior | Gap for this objective |
| --- | --- | --- |
| MNQ v0.10.3 | Session/overnight level rejection in Trend Speed direction, one MNQ, fixed 1.5R bracket, 60 three-minute bars maximum, two entries per session | Not a dedicated trend-continuation entry or trailing exit |
| MGC v0.7.3 | One MGC, range/MFI short, fixed 1R bracket, 60-bar timeout | Different setup; no shared account risk state |
| Compass scanner | Overnight futures session support; EMA/VWAP pullback and volume-breakout candidates with completed 15-minute bias | Uses one-minute technical triggers; no dedicated three-minute trend cohort |
| Compass simulator | Fixed 2R target, loss and position-count limits | No runner, account-wide realized-profit stop, or broker reconciliation |
| Futures source integration | Custom Pine alert messages mirrored into Compass | Bracket fills and actual broker P&L are absent; an exit instruction cannot prove a fill |

The existing scripts use full electronic sessions and flatten at 15:42 CT on
their supported regular sessions. Compass currently uses a separate 15:45 CT
research cutoff. The new study must set an explicit account calendar and flatten
buffer; it must not silently treat these different schedules as equivalent.

## First test design

1. Use MNQ first, both directions, with dated unadjusted contract data and explicit
   roll handling. Study Asia, Europe and US periods separately within the same
   exchange trading day. Do not reset daily risk at midnight.
2. Require completed 15-minute trend direction. Form the entry on a completed
   three-minute continuation bar after an EMA/VWAP pullback. Evaluate a separate
   consolidation-breakout candidate for trends that do not pull back. Keep their
   identities and results separate; do not optimize both from this screenshot.
3. Fill only on an eligible quote after the trigger, with spread, fees and adverse
   slippage. Define initial invalidation before entry. Start with one MNQ, no
   averaging down or pyramiding, and at most two attempts per exchange session.
   Use the existing $100 planned-risk ceiling as a research comparison, not as a
   new live account instruction. Reject a stop that cannot fit the study budget.
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
6. Flatten before the configured account cutoff, including early closes. A trend
   continuing into another exchange session requires a new eligible entry.
7. Use a separate durable research ledger, independent of existing strategy
   positions and performance. Emit simulated entry, stop-update, exit and daily-
   lock events with timestamps and reasons. Provide a morning review of what was
   seen, entered, skipped, earned or lost, and whether the daily goal was met.

Overnight entry eligibility must rely on live futures data. Last cash-session
options GEX/VEX and unusual-flow data can be dated context; they must never be
presented as fresh overnight confirmation or block the test merely because the
options market is closed. No new subscription is established as necessary for
the MNQ price-based study; current data entitlement and coverage still need
verification for each replay window.

## Evidence required

Use the pictured period only for development inspection, then chronological
unseen sessions and a forward shadow test. Compare net P&L, worst drawdown, MAE,
MFE, profit giveback, costs, attempts/session, rejected signals, overnight vs US
results, and fraction of sessions finishing at the goal. Track losses on choppy
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

MNQ is $2 per index point per contract: a hypothetical 150-point capture with one
contract is $300 gross before fees and slippage. This is unit arithmetic, not a
measurement of the screenshot or a forecast.
[CME contract specifications](https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html).
TradingView distinguishes custom `alert()` events from order-fill alerts and runs
saved alerts on its servers:
[TradingView alerts documentation](https://www.tradingview.com/pine-script-docs/concepts/alerts/).
