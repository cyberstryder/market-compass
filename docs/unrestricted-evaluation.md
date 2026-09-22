# Independent evaluation is the primary research record

User decision, September 21, 2026: find which setups work across the configured
universe before designing an automated trading account. Account restrictions
must not censor this evidence.

## Implemented

- Setup outcomes v4 measure futures, stock/ETF and scanner-selected 0DTE option
  hypotheses as independent one-unit observations. They do not consult balances,
  daily loss locks, concurrent positions, sizing budgets or entry counts.
- Every scanner rule is recorded before notification cooldown, paper admission,
  or display grouping. Non-alerted candidates also request independent 0DTE and
  configured intraday option-idea evaluation. Distinct source IDs remain distinct
  observations even when they share a ticker, direction or contract.
- Option selection retries bypass account admission, retain their own decision
  evidence, and cannot overwrite or reset a completed selection. Waiting contract
  selections request quote subscriptions; opened option observations remain pinned
  ahead of background subscriptions. Provider subscription capacity is still a
  physical coverage limit and missing quotes are never invented.
- v4 records fresh wide-spread trials with observed entry spreads instead of
  censoring them with the paper liquidity threshold. Fresh executable quotes,
  valid structural invalidation and continuous outcome paths are still required.
- Stock research runs until one minute before cash close and observes exits at
  close. Futures use their full exchange research session. Strategy pattern rules
  and exchange calendars are not replaced by arbitrary entries.
- Setup results defaults to all evaluated candidates; non-alerted catalog research
  is no longer mislabeled exclusively as alert cooldown. Overview shows complete
  setup counts, not a capped recent paper-profit sample. Daily results lead with
  independent futures, 0DTE, options and stock results. Paper-account results are
  an expandable, explicitly constrained benchmark. Missing 0DTE selections have
  separate counts/reasons and do not disappear from the daily report.
- Ask Compass is instructed to distinguish primary independent evaluations from
  paper eligibility and from scheduled SPY methodology.

## Interpretation and boundaries

These are overlapping experiments, not one executable equity curve. Rank setups
within symbol, direction, evaluation version and comparable coverage. Keep losses,
exclusions and unresolved paths visible. Never count an unavailable quote as a loss
or fabricate a historical fill. Existing records and open trials keep their saved
parameters; v4 is prospective. Broader coverage changes the sample and must not be
silently pooled with historical alerted-only cohorts.

Morning, Smoothers, swing, discovery and external-source studies remain independent
of paper-account balances. Their named strategy definitions and measured horizons
remain separate. This change does not claim to generate every conceivable setup,
subscribe to every option strike, or remove market-data access/capacity limits.
No broker execution is introduced. Existing notification routing remains separate
from whether an observation is measured.

## Verification

Regression checks cover 12 overlapping 0DTE entries despite an exhausted account,
a positive entry cap, tiny sizing budget and three existing positions; independent
exits; non-alerted option observation queueing with scanner paper disabled; restart
deduplication; wide-spread observations; and missing quote exclusions.

Release tracking: PR #109 publishes this change through GitHub's browser UI after
the connector failed. Merge requires the complete branch CI checks; live rollout
is verified separately against the deployed revision and v4 research records.
