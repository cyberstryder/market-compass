# SPY 0DTE morning brief

The requested Arya-style daily plan runs inside Compass and uses the existing
Compass Discord destination. It is an informational alert; existing scanners,
positions, original Morning alerts and Smoothers delivery ownership are unchanged.

Two messages on NYSE session days, using the exchange calendar and Chicago time:

| Update | Chicago | Eastern | Purpose |
| --- | --- | --- | --- |
| Pre-open | 08:20 | 09:20 | Observed premarket range, gamma context, ranked chart levels and conditional plans |
| Opening check | Just after 08:45 | Just after 09:45 | Complete first 15-minute candle, call/put/wait verdict and fresh 0DTE quote references |

The opening check waits up to two minutes for all 15 one-minute bars. If evidence
is still missing, it sends a WAIT/data-gap report. A missed delivery window is not
replayed at midday. Persistent day/phase keys prevent repeated enqueue on restart;
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
puts require a close below its low. Inside the range means WAIT. Stops and targets
use SPY prices with R = 0.2 times the prior 14 completed daily mean true range,
stop 1R and target 2R. Pre-open references are provisional and re-anchor to the
confirmed close. Nearby Apex levels are reaction areas, separate from the bracket.
The completed timing study supports retaining the baseline, not claiming optimality.
This first release sends the opening check, not additional entry updates all morning.

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
exact Discord payload, latest scheduled report and schedule. It does not enqueue.
Set `SPY_MORNING_BRIEF_ENABLED=false` on the engine to stop future briefs; set it
on the collector too to stop index-reference requests. Existing recorded reports
remain available. A reference probe is `SPY_BRIEF_REFERENCE_PROBE=true python -m
compass.spy_brief`; it updates only the index-reference cache and prints a preview,
without creating alerts or changing positions.
