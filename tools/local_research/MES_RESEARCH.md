# MES opening-hour research protocol

Status: proposed experiment, not an implemented strategy or demonstrated edge.
Local runs and gap audits default to MES. Other instruments remain available by
explicit --symbols selection; production collection/scanning is unchanged.
Existing MES results have clean observed coverage but no preliminary passes.

## Question

Can information available at each decision time distinguish a range suitable
for mean reversion from directional acceptance suitable for continuation during
08:30–09:30 America/Chicago (the first hour of the US cash equity session)?
The claimed opening-hour edge is a hypothesis. No label may use the future day's
high, low, closing trend, or a still-forming bar.

## Small, fixed experiment

- Start with a three-minute decision interval and one MES contract throughout.
  Keep one- and five-minute variants as later sensitivity checks, not a search
  for whichever chart happens to win.
- Use only completed bars: opening-range location after its first 15 minutes,
  range breaks that hold versus close back inside, directional efficiency, and
  distance from session VWAP when trustworthy volume is available. Missing
  required data means unknown, not an invented indicator value.
- Define range, directional and uncertain conditions with fixed rules developed
  on development data. Uncertain conditions allow no entry. Freeze thresholds,
  stop/exit rules and confirmation timing before new validation.
- Compare the same mean-reversion baseline against a regime filter, and then
  against that filter plus at most one separately confirmed continuation entry
  after a stopped reversion. A losing trade alone must not trigger a reversal.
- Entries are restricted to 08:30–09:30 CT. Define the post-entry exit deadline
  before testing. Simulate closed-bar signals with next-bar fills and preserve
  stop-first handling of ambiguous bars and existing gap censorship.
- Keep contract size and per-trade risk rules independent of prior losses.
  Do not switch to ES to recover a loss. Account for the total session loss from
  both legs and compare against simply stopping after the first loss.

## Evidence required

Report net expectancy after ordinary and stressed costs, trades and sessions,
drawdown, daily uncertainty, results without the best day, and each regime's
sample size. Report the incremental continuation leg separately and the whole
session together. Record rejected signals and missing-data blocks as well.

Earlier validation/later results have already been inspected. They can help
exploration but cannot be reused as untouched confirmation. Freeze a small rule
set and evaluate additional unseen sessions; no automatic promotion to trading.
The current nine-family common-exit screen does not implement this protocol's
regime routing, opening-hour entry restriction, or sequential continuation leg.
