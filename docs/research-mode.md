# Research-first operation

`PAPER_TRADING_ENABLED=false` is the default across the engine, scanner, option
selection, dashboard and Discord worker. Legacy `SCANNER_PAPER_TRADES=true` cannot
override it. Re-enabling a paper benchmark is a separate strategy-promotion choice;
this release does not assert any strategy is successful.

Every configured candidate remains an independent hypothesis. Loss budgets,
entry counts, concurrent positions, sizing and alert cooldowns do not censor
research. Missing quotes, invalid levels, unavailable contracts and incomplete
paths remain excluded/unavailable/unresolved; these are not fabricated losses.

## Category audit

| Category | Independent evidence and outcome path |
| --- | --- |
| Futures ORB, technical and catalog setups | SetupStudy, including overlapping and non-alerted trials; recorded quote paths, stops, targets and full research-session deadline |
| Stock/ETF, exposure and price-confirmed unusual flow | Scanner records each candidate before notification and account gates; SetupStudy plus independent option requests |
| Scanner and underlying-ORB 0DTE | Research-only contract selection and SetupStudy; no account admission calls |
| Intraday option ideas | OptionIdeas one-contract observations; account-independent quotes and outcomes |
| Swing technical/flow | SwingStudy records pre-flow candidates and common underlying endpoints; SwingIdeas retains its distinct contract selection and premium model |
| Legacy 20-day closing breakout | Newly detected candidates now enter SwingStudy 60m/close/1/3/5/9-session underlying measurements, including unavailable-entry rows; no paper account. This is explicitly an endpoint study, not the old paper bracket return |
| Scheduled SPY and timeframe studies | Existing independent confirmation, underlying and option observations; WAIT remains a strategy decision, not a research/account veto |
| Morning | Native signal census, underlying paths and option horizon measurements; original-source mirror stays distinct |
| Smoothers | All enabled weekly configurations and their target/Friday outcomes, independent of paper accounts |
| TraderMatrix, Discovery, Obsidian, external-source comparisons | Existing versioned inventories/checkpoints and unavailable-data reporting; no paper-account admission dependency |

Provider capacity, configured universes, signal definitions and measurement
horizons remain explicit. This is not an unlimited market-data subscription or a
promise to observe unconfigured strategies. No automatic rule promotion, orders,
relabeling of historical outcomes or reconstruction of missing fills occurs.

## Messages and migration

New engine ORB notifications identify RESEARCH TRACKING only when the saved study
row is open/pending; otherwise they identify RESEARCH DATA UNAVAILABLE. Scanner
messages link the independent study and no longer claim a missing paper position.
Existing notification cooldowns affect delivery only; non-alerted records remain
in all-setup results. Catalog trials stay in research reports, avoiding a new
message for every overlapping experimental entry.

New paper admissions are paused. Existing positions continue to their original
exit rules silently so history is not erased. Their exits and unresolved-data
notices are internal `paper_decision` records. Pending paper option retries pause;
independent option queues continue. Pending legacy engine paper Discord jobs are
marked `suppressed` with an auditable reason, never falsely marked delivered.
Research, Morning, Smoothers, SPY and other category notifications continue.

The dashboard exposes the effective operating policy and labels the paper ledger
as historical. Daily results adds entry-session and frozen market-condition groups
for setup trials; older rows are `not_recorded`, never reconstructed. Conditions
are descriptive hypotheses, not profitability claims. Category-specific studies
retain their own versioned reports and return definitions; do not pool them.

## Verification

Regression coverage checks overlapping MNQ/NQ/stock entries and target exits with
paper admission replaced by a failing stub, direct 0DTE selection, old retries,
missing quotes, silent legacy exits, queued-message suppression across routes and
legacy daily-breakout swing checkpoints. Existing paper tests explicitly opt in
and continue protecting historical accounting. Full CI includes PostgreSQL and
the packaged production image. Live acceptance must confirm all three services,
effective policy, advancing research records and absence of new paper admissions;
closed cash markets defer prospective equity checks until fresh observations.
