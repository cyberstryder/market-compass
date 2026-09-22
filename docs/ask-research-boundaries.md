# Research evidence and strategy boundaries

September 22, 2026. Ask Compass direction questions were being answered as
scheduled SPY entry checks. This is a context and policy boundary defect, not
proof that the market feed was down or that the strategy engines need replacing.

## Corrected in ask-research-v2

- A question-scoped context distinguishes market research, entry review, and
  explicit scheduled-plan review. Normal direction/options research excludes
  paper positions, loss budgets, limits and fills. They cannot consume the
  context budget or produce an automated-entry warning for a direction question.
- Direction research gets dated SPY chart levels without the archived plan's
  WAIT/NO ENTRY state, option snapshot availability or synthetic stop/target plan.
  The original saved brief and explicit entry/plan reviews remain unchanged.
- Local exposure now reaches the model. Its source, timestamp, coverage and
  assumptions travel with a labelled 12-strike largest-absolute-GEX sample.
  This is not complete chain coverage or a verified list of dealer walls.
- Named-symbol questions no longer include every unrelated futures quote/level.
  Technical context is kept intact with a larger section budget. Timestamped
  market evidence precedes paper-account and administrative reports.
- Context-budget omissions are identified separately from sections not requested.
  Per-section supplied/original byte counts support troubleshooting. A packaging
  omission is not a provider outage. Entry completeness is only applicable to
  questions requesting entry or scheduled-plan review.
- Answer policy asks for a supported directional lean, horizon and qualitative
  confidence. It uses VWAP, EMA slope/alignment, completed-bar structure, volume
  and available exposure/flow; it must not fabricate a probability or guarantee.
  A future candle is not missing data. Contract quote absence does not veto an
  underlying thesis. A saved opening candle does not establish current momentum.

## Restriction audit and system design

| Layer | Finding / disposition |
| --- | --- |
| Independent scanner evaluation | PR #109 already records configured stock/futures/0DTE candidates before paper admission and notification gates. Keep prospective versions separate. |
| Ask Compass | Correct the context/policy boundary here. Analysis must not inherit portfolio admission or scheduled-plan eligibility. |
| Scheduled SPY | Its confirmation times and terminal NO ENTRY define that named strategy. Preserve them and evaluate independent alternatives separately. |
| Option ideas and swing | Expiry, liquidity, contract-selection and exit rules define the measured strategy. Changing them is a new version/cohort, not removal of a paper-account restriction. |
| Secondary review | Spread and freshness filters define a comparison cohort; the original source observation is separate. Do not conflate a rejected secondary review with a missing original signal. |
| Discovery and collectors | Universe, subscription, pagination and processing budgets still limit physical coverage. These need visible counts/gaps, not fabricated observations or claims of unlimited collection. |
| All studies | Source freshness, identity, valid brackets and continuous quote paths are measurement requirements. Missing evidence remains excluded/unresolved rather than invented P&L. |

The appropriate overhaul is separation of evidence collection, independent
research, named strategy decisions, paper-account benchmarks and notifications.
A wholesale rewrite is not justified by these findings. This audit does not claim
every external running alert has been verified end to end. Future capacity or
strategy experiments should retain their exclusions and versioned populations.

## Validation

Regression tests exercise both Ask endpoints with fresh SPY technicals, partial
exposure, a saved WAIT/missing-options brief, and many unrelated futures quotes.
They verify the actual model payload retains SPY evidence, excludes irrelevant
account gates, preserves timestamps and stays within the bounded input size.
Explicit entry and scheduled-plan questions retain their original evidence.
The real model answer is verified separately after deployment; mocked output is
not a test of model behavior or directional accuracy.
