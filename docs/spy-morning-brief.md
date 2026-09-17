# SPY 0DTE morning brief

The requested Arya-style daily plan runs inside Compass and uses the existing
Compass Discord destination. It is an informational alert; existing scanners,
positions, original Morning alerts and Smoothers delivery ownership are unchanged.

Each update sends two messages (plan and TradingView prompt) on NYSE session days,
using the exchange calendar and Chicago time:

| Update | Chicago | Eastern | Purpose |
| --- | --- | --- | --- |
| Pre-open | 08:20 | 09:20 | Observed premarket range, gamma context, ranked chart levels and conditional plans |
| Opening check | Just after 08:45 | Just after 09:45 | Complete first 15-minute candle, call/put/wait verdict and fresh 0DTE quote references |
| Later check | Just after 09:00 | Just after 10:00 | Evaluate the newly completed 15-minute candle after an opening WAIT |
| Later check | Just after 09:15 | Just after 10:15 | Continue only while no setup has confirmed |
| Final check | Just after 09:30 | Just after 10:30 | Confirm a setup or finish with NO ENTRY |

Each check starts five seconds after its candle boundary and waits until 110
seconds after it for a late closing bar. The enqueue window ends at 125 seconds.
Missing evidence produces WAIT/data-gap (NO ENTRY at the final check). A missed
check is never replayed later. Follow-ups require a recorded opening WAIT;
a missing intervening check blocks later confirmation. The first confirmed setup
stops all remaining checks, including after restart.
Persistent day/phase keys prevent repeated enqueue on restart;
the existing confirmed-message Discord outbox remains at least once, with stable
event IDs identifying possible timeout retries. Delayed delivery is labeled DATED.

The message includes SPY spot and its clock, previous high/low/close, separate
premarket and regular-session O/H/L/VWAP, recent closing-price direction, latest
minute volume versus the preceding 20 minutes, and the first 15-minute close.
Price bias is descriptive, not a forecast. Partial bar coverage is visible.

Vendor GEX, gamma flip and Apex rankings retain their own observation clocks and
vendor cache policy. Compass calculates a separate call-positive/put-negative
GEX proxy using available gamma/OI, with coverage, expiry scope, largest strike
concentrations, call/put totals and a 0DTE subset. Scope or sign disagreements are
shown. Fetch time is not relabeled as a vendor calculation time. No gamma value is
interpreted as measured dealer inventory or guaranteed support/resistance.

Calls require a completed 15-minute close above the observed premarket high;
puts require a close below its low. Inside the range means WAIT until the final
check, which ends with NO ENTRY. Stops and targets
use SPY prices with R = 0.2 times the prior 14 completed daily mean true range,
stop 1R and target 2R. Pre-open references are provisional and re-anchor to the
confirmed close. Nearby Apex levels are reaction areas, separate from the bracket.
The completed timing study supports retaining the baseline, not claiming optimality.
Checks use the latest completed 15-minute candle, with every regular-session
minute from the open through that boundary required. The first-candle close
remains visible for comparison. Stops and targets re-anchor to the newly
confirming close; 1-minute candles do not trigger an entry. Each check expires
125 seconds after its candle boundary. The chart prompt includes that expiration,
the decision and the next check; it cannot infer a new confirmation from an old
snapshot.

The opening report freezes the observed premarket high and low. Later messages
retain those exact prices. The existing 90% premarket coverage requirement,
recency checks and prior daily ATR gate remain in effect. Recovered bars may
clear a gap only if the observed range matches the frozen range. A changed or
unavailable opening range blocks confirmation and is reported explicitly;
levels are not silently moved. Older saved opening reports supply their
premarket context as the frozen range.

Saved reports and chart companions include `confirmation_kind` (opening or
later), `check_minute`, candle start/end/close and completeness,
`frozen_premarket`, `data_blocks`, `decision_state`, and `session_complete`.
These preserve separate opening/later signal groups for research; they are
observations, not fills or independently measured trade P&L. The historical study
allowed later checks through 10:30 ET. This extension is not a claim of proven
profitability and adds no afternoon entry schedule.

The [prospective 0DTE outcome ledger](spy-plan-outcomes.md), available at
`/#spy-study`, now links these exact reports to one-contract SPY observations.
It waits for the plan's confirmed Discord receipt and eligible actual quotes
within the original check deadline, then follows the frozen underlying bracket
and a session exit. Missing contracts, unfilled entries and unresolved paths
remain explicit. Historical reports receive no reconstructed trades. The ledger
uses its own declared liquidity/continuity gates and retains complete quote
evidence; scheduled plan and chart messages are unchanged.

Only same-day standard 100-share SPY contracts are displayed, nearest whole-dollar
strike to spot with ties lower. Before the open, premiums are withheld. During RTH,
positive-size, uncrossed bid/ask quotes must be no more than ten seconds old;
spreads above 20% of midpoint are flagged. Bid/ask/mid, snapshot delta/IV, and full
ask debit plus a $0.65 illustrative entry fee are displayed. These are watch
contracts, not fills, position sizes or premium-stop guarantees.

SPX estimates equal each SPY level multiplied by the previous session's matched
SPX/SPY closing ratio. XSP estimates equal SPX estimates divided by ten, consistent
with [Cboe's XSP index specification](https://www.cboe.com/tradable-products/sp-500/xsp-options/).
The basis date and source appear in the message. Estimates are underlying chart
references, not tradable option strikes, independent SPX gamma levels or live index
quotes; SPY/index basis can change, including around distributions.

The collector first tries dated [Massive daily reference bars](https://massive.com/docs/rest/indices/aggregates/custom-bars),
then Yahoo Finance raw daily closes if the existing account lacks the required
data. Both symbols must match the immediately preceding session. There is no
hardcoded SPY-times-ten fallback. A missing pair withholds SPX/XSP estimates while
preserving usable SPY information. Each attempt is bounded to four GETs; failed
attempts are spaced one hour apart, and a valid pair is cached for the session.

Four rich Discord cards preserve the complete report within Discord's embed
limits. The existing shared outbox still requires a saved message ID before it
advances. Mentions are disabled.

Authenticated preview: `GET /api/spy-morning-brief` returns the current report,
exact Discord payload, latest scheduled report, schedule and today's
`confirmation_history`. It does not enqueue.
Set `SPY_MORNING_BRIEF_ENABLED=false` on the engine to stop future briefs; set it
on the collector too to stop index-reference requests. Existing recorded reports
remain available. A reference probe is `SPY_BRIEF_REFERENCE_PROBE=true python -m
compass.spy_brief`; it updates only the index-reference cache and prints a preview,
without creating alerts or changing positions.
