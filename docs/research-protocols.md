# Remaining research definitions — frozen September 17, 2026

Checklist 07. Registry: `research-protocols-2026-09-17-v1`. These are review definitions for existing observations, not changes to entry, execution, notification ownership or retirement gates. Implementation baseline: `50f3c3f8334160f9d301cd47615f2a6355737e58`. Live records retain their own actual version, settings, source times and configuration digest. A changed version or configuration starts a separate cohort; do not pool it into the frozen primary comparison.

## Evaluation rules

Freeze these definitions before evaluating the designated new sample. The first ten complete exchange entry sessions on/after September 18 and after deployment are the untouched evaluation period for the remaining intraday programs. Do not stop early because results look favorable. Wait for required endpoints, including later swing exits. Earlier data, including September 17, are development and coverage evidence. No historical cohort is reclassified as prospective. Smoothers uses its first two complete native weeks on/after September 21 for operational parity; two weeks do not establish trading efficacy. Existing TM, Swing candidate and SPY protocols retain their own original evaluation periods.

Report received, eligible, assessed, pending/open, completed, excluded and unresolved counts separately, with denominators and units. No qualifying records means an observed zero only when collection/report coverage is established; absent evidence means unknown. A report cap cannot define the evaluation population. Enumerate all matching saved records through pagination or an owner-authorized complete export before the final evaluation. Missing endpoints are excluded with reasons, never zero returns. A zero-exposure selection arm applies only to a deliberately skipped, otherwise eligible opportunity with a valid common measured endpoint.

Use a 95% paired session-cluster bootstrap with 10,000 resamples and a recorded deterministic seed (20260917) for the prespecified difference when at least ten distinct entry sessions have usable endpoints. Cluster all correlated observations from a session together; report symbol/contract concentration and effective session count. Report Smoothers parity descriptively. Secondary endpoints are exploratory, without an automatic winner or multiple-comparison claim. If sample size, coverage or intervals do not support a conclusion, report inconclusive. No profitability threshold, adaptive retuning or automatic promotion is authorized by this protocol. The dashboard does not calculate these confidence intervals yet.

Configured universes and parameters are frozen by the retained record/configuration revision. New symbols introduced after the evaluation starts form a separately labeled cohort. Provider filters and collection ceilings limit the observed universe. Context-only feeds and Obsidian marks do not acquire an invented terminal trading outcome.

## morning

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** morning_stock_report_v1 / saved Pine and configuration digest
- **Universe / population:** Retained non-test baseline signals and separate research candidates; source and native tapes never pooled.
- **Entry:** Saved signal candle close; 1-minute native bars with original source/settings identity.
- **Expiry / strike:** Underlying only; no option contract or expiry in stock comparisons.
- **Exit / checkpoints:** 5/15/30/60-minute closes and +1/-0.5%, +2/-1% brackets with 60-minute timeout; 120/180-minute research endpoints secondary.
- **Fees / slippage:** Gross underlying percentage moves; no executable fill, fees or slippage model.
- **Primary comparison:** 60-minute close versus +1/-0.5% bracket on the same complete, unambiguous 60-candle cohort; other rules exploratory.
- **Exclusions / missingness:** Missing/conflicting candles, changed settings and same-candle ambiguous bracket touches excluded and counted.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/morning-stock-reports.md](morning-stock-reports.md).

## morning-expirations

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** atm_expiration_comparison_v1 / ask_bid_fees_v1
- **Universe / population:** Fresh non-test Morning stock alerts with common complete entry and 5/15/30/60-minute quotes.
- **Entry:** Observed ask after signal within original 120-second window; paired quote clocks within five seconds.
- **Expiry / strike:** Standard CALL; nearest strike, lower strike tie; baseline earliest expiry after today, 0DTE actual same-day listing, 1–3 DTE aliases baseline when eligible.
- **Exit / checkpoints:** Fixed observed bids at 5/15/30/60 minutes; best observed exit is hindsight only.
- **Fees / slippage:** $0.65/side/contract; stress adds $0.01/share/side; no equal-risk claim.
- **Primary comparison:** Paired 60-minute net premium-return difference: 0DTE minus baseline; other horizons secondary.
- **Exclusions / missingness:** Missing listings, stale/pre-signal quotes, incomplete catalogs, timing skew and missing common endpoints.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/morning-expiration-comparison.md](morning-expiration-comparison.md).

## smoothers

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** native-smoothers-v1 / frozen weekly configuration revision
- **Universe / population:** All enabled saved configurations (76 at audit), including unfeatured observations; match source by week and ticker.
- **Entry:** Original weekly model reference at 10:30 ET on first exchange session; retain actual generation and receipt clocks.
- **Expiry / strike:** Latest listed expiry from Wednesday through Friday of the signal week, nearest strike with lower-strike tie; source/native contracts compared explicitly.
- **Exit / checkpoints:** First observed stock target touch or original week close; premium valuations and underlying outcomes separate.
- **Fees / slippage:** Stock target states and source/native valuation parity; no modeled option value presented as an executed net return.
- **Primary comparison:** Native/source direction, entry, target, quality tier and terminal-state agreement on matched week/ticker/configuration.
- **Exclusions / missingness:** Ambiguous identities, missing source/native week, missing premium quotes and unresolved stock paths stay separate.
- **Untouched evaluation:** First two complete native weeks starting on or after 2026-09-21 after deployment; wait for both weekly closes. September 14 is migration/development evidence. Delivery and backup gates remain independent.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/smoothers-sender-handoff.md](smoothers-sender-handoff.md).

## intraday

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** setup-outcomes-v3 / sampled-bracket-v2 / compass-scanner-v2
- **Universe / population:** All admitted stock/ETF setup trials including cooldown candidates; group by rule, side, symbol and version.
- **Entry:** Fresh executable post-signal quote; one unit and one adverse tick, frozen invalidation.
- **Expiry / strike:** Underlying shares; no option expiry or strike.
- **Exit / checkpoints:** Outward-rounded stop, 2R capped target, or 15 minutes before cash close.
- **Fees / slippage:** Bid/ask plus one adverse entry/market-exit tick; stock fee placeholder zero.
- **Primary comparison:** Net R per resolved trial by prespecified rule; report alerted and cooldown cohorts separately, with missingness.
- **Exclusions / missingness:** Invalid entry, quote gap over 15 seconds or missing deadline quote; overlapping experiments are not portfolio returns.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/setup-outcomes.md](setup-outcomes.md).

## futures

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** setup-outcomes-v3 / sampled-bracket-v2 / futures-market-assessment-v1 / futures-entry-variants-v1
- **Universe / population:** All admitted dated-contract futures trials, including cooldown and research-only late-session candidates; instruments remain separate.
- **Entry:** Fresh post-signal executable quote, one contract, frozen structural stop; retain no-candidate bar census separately.
- **Expiry / strike:** Resolved dated instrument and multiplier; never substitute continuous chart prices.
- **Exit / checkpoints:** 2R target or observed stop; v3 research trials exit one minute before calendar close. Portfolio/account cutoff is separate.
- **Fees / slippage:** One adverse tick on entry and market/stop exit plus saved instrument fee ($1.50/side for micros, $2.50/side for standard contracts).
- **Primary comparison:** Primary entry contrast: first_per_trend versus repeated, using same trial outcomes and net R per eligible opportunity (skipped=zero exposure). Market-state selection and pullback_reset are secondary.
- **Exclusions / missingness:** Unknown regime, missing/stale bars, unmapped contract, excluded entry or quote gaps; no-candidate bars are not losing trials.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/futures-entry-variants.md](futures-entry-variants.md).

## option_ideas

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** option-ideas-v2 / paired-recorded-quotes-v2 / quote-continuity-v1
- **Universe / population:** All retained intraday option idea candidates from configured stock scanner; independent one-contract observations.
- **Entry:** Qualifying fresh stock/option quotes within two minutes of trigger and before close minus 30 minutes.
- **Expiry / strike:** Standard CALL/PUT; 1–21 DTE prefer 7, strike within 3%, OI/day volume >=100, supplied delta magnitude 0.30–0.70; saved missing-delta fallback.
- **Exit / checkpoints:** 30% premium stop, 50% capped target, original underlying bracket, or close minus 15 minutes.
- **Fees / slippage:** Ask+$0.01 to bid-$0.01, $0.65/side; overlapping positions are not an account.
- **Primary comparison:** Net premium return by frozen setup/direction on resolved v2 paths; report exclusion and unresolved rates beside returns.
- **Exclusions / missingness:** Unavailable contract/stream slot, failed liquidity/continuity, trigger invalidation and >15-second live gaps.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/options-ideas.md](options-ideas.md).

## swing_ideas

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** swing-ideas-v1
- **Universe / population:** Admitted swing option candidates from saved technical/flow rules; rejected technical candidates belong to separate flow study.
- **Entry:** Fresh stock/option quotes within original two-minute admission window; cash session until close minus 30 minutes.
- **Expiry / strike:** Standard CALL/PUT; 14–60 DTE prefer 30, nearest qualifying strike within 3%, recorded liquidity/delta rules.
- **Exit / checkpoints:** 35% premium stop, 75% capped target, underlying bracket, or 10 sessions including entry / session before expiry, whichever earlier.
- **Fees / slippage:** Ask+$0.01 to bid-$0.01, $0.65/side; adverse overnight gaps retained.
- **Primary comparison:** Net premium return by frozen setup/direction; compare coverage and gap incidence, not underlying-study returns.
- **Exclusions / missingness:** Missing open-market path beyond 60-second budget, metadata/reference changes or missing deadline quotes unresolved.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/swing-ideas.md](swing-ideas.md).

## secondary

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** secondary-context-v4
- **Universe / population:** Received Morning, Smoothers, TradingView futures, native futures and Obsidian candidates; source families separate.
- **Entry:** Same timely review-time underlying midpoint for every selection arm; original 60-second availability window.
- **Expiry / strike:** Underlying direction only; original option identity retained as context.
- **Exit / checkpoints:** 15/30/60-minute checkpoints; no carry across cash close/futures research deadline; weekly source states separate.
- **Fees / slippage:** Underlying movement before costs; neither fills nor option profit.
- **Primary comparison:** 60-minute directional move per eligible candidate: supported filter (unselected=zero exposure) versus all; TM/level removal contrast secondary.
- **Exclusions / missingness:** Late input, insufficient required context, missed quote window, session boundary; same reference and endpoint for supported/rejected.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/secondary-review.md](secondary-review.md).

## obsidian

**Status:** frozen observation definition; frozen 2026-09-17.

- **Model:** watchlist_ideas_v1 / retained unversioned option observations
- **Universe / population:** Retained provider ideas and revisions; exclude configured DJT from active tracking; update messages do not mean failure.
- **Entry:** First fresh option ask after timely receipt; may wait for open; preserve anchor delay and late imports.
- **Expiry / strike:** Exact source-listed OCC contract; no changed strike/expiry or synthetic replacement.
- **Exit / checkpoints:** Current observed bid and sampled extrema through expiry; no fixed exit/terminal win rule implemented.
- **Fees / slippage:** Ask-to-bid liquidation percentage before fees/slippage; vendor reported percentages separate.
- **Primary comparison:** Observation coverage and source-update association only. No win-rate or trading-edge claim until a separately versioned exit protocol exists.
- **Exclusions / missingness:** Late imports, unanchored paths, >15-second gaps, ambiguous revisions, historical reconstructed evidence separate.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/obsidian-watchlist.md](obsidian-watchlist.md).

## zero-dte

**Status:** frozen review definition; frozen 2026-09-17.

- **Model:** 0dte-selection-v1 / saved ORB or scanner strategy / sampled-bracket-v2
- **Universe / population:** Same-day portfolio option candidates and admitted positions; shared loss, risk, position and entry limits retained.
- **Entry:** Fresh post-trigger bid/ask within original pending deadline and available portfolio capacity.
- **Expiry / strike:** Listed same-day standard CALL/PUT chosen by existing engine; actual contract metadata and selection reasons retained.
- **Exit / checkpoints:** Stop distance max($0.05, 30% of observed ask), 2R target or close minus 15 minutes; original portfolio management model retained.
- **Fees / slippage:** Saved bid/ask adverse tick model and $0.65/side/contract.
- **Primary comparison:** Selection coverage and cost-adjusted closed-position results; no attribution of a historical rejection to current risk state.
- **Exclusions / missingness:** Expired retries, missing diagnostic history, quote/liquidity rejection, portfolio capacity and unresolved management.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/research-admin.md](research-admin.md).

## vendor-research

**Status:** frozen context definition; frozen 2026-09-17.

- **Model:** source-specific context versions and clocks
- **Universe / population:** Configured vendor screeners/matrices, local exposure and received responses; not the full market.
- **Entry:** No entry; preserve source timestamp, receipt timestamp, cached/stale flags and missing cells.
- **Expiry / strike:** N/A: context inventory, not contract selection.
- **Exit / checkpoints:** No standalone return endpoint.
- **Fees / slippage:** N/A: no fill or P&L model.
- **Primary comparison:** Freshness, availability and linked setup/secondary coverage; no standalone screener success rate.
- **Exclusions / missingness:** Missing clocks, stale values, partial pagination and unverified vendor methodology remain explicit.
- **Untouched evaluation:** First 10 complete exchange sessions beginning on or after 2026-09-18 and after this protocol is deployed. Lock membership by entry session; wait for all required endpoints. Earlier observations are development evidence. Coverage diagnostics may be inspected; do not retune using evaluation outcomes.
- **Uncertainty:** Report eligible, measured and missing counts by session and symbol; overlapping records and shared contracts are correlated. Report a 95% session-cluster bootstrap interval for a prespecified difference only with at least ten distinct entry sessions with usable outcomes; otherwise inconclusive. Intervals are a review requirement, not an existing dashboard calculation.

Implementation and existing evidence: [docs/vendor-freshness-and-review-periods.md](vendor-freshness-and-review-periods.md).

## tm-flow

**Status:** existing frozen protocol; frozen 2026-09-16.

- **Model:** tm-receipt-study-v1
- **Primary comparison:** 60 trading-minute common-score directional underlying comparison.
- **Untouched evaluation:** Retain the original dated protocol and activation cohort; this registry does not restart or reclassify it.

Implementation and existing evidence: [docs/tm-scoring-study.md](tm-scoring-study.md).

## swing-flow-study

**Status:** existing frozen protocol; frozen 2026-09-16.

- **Model:** swing-flow-study-v1
- **Primary comparison:** Five later session closes on common candidate-time price references.
- **Untouched evaluation:** Retain the original dated protocol and activation cohort; this registry does not restart or reclassify it.

Implementation and existing evidence: [docs/swing-flow-study.md](swing-flow-study.md).

## spy

**Status:** existing frozen protocol; frozen 2026-09-16.

- **Model:** spy-plan-0dte-v1
- **Primary comparison:** Delivered confirmation to same-day one-contract net outcome; one observation/session.
- **Untouched evaluation:** Retain the original dated protocol and activation cohort; this registry does not restart or reclassify it.

Implementation and existing evidence: [docs/spy-plan-outcomes.md](spy-plan-outcomes.md).

## September 24 intraday option admission amendment

The September 24 intraday option admission amendment has its own
`research-protocols-2026-09-24-options-v2` definition and `quote-continuity-v2`
cohort. It adds sustained five-minute quote review with 90 seconds of minimum
observed history; the original option definition above stays frozen. The next
ten complete cash sessions beginning on or after September 25 after deployment
form the new review window. September 24 is transition evidence. See
[option reliability](option-reliability.md#september-24-sustained-contract-qualification-quote-continuity-v2).

## Programs without an implementation

Overnight trend/trail testing remains a specification in [overnight-trend-test.md](overnight-trend-test.md). Its dedicated three-minute trend, trail and daily profit-goal implementation is not active. Existing futures setup trials are a separate program. End-of-day is a reserved program without an active producer or approved entry/exit model. Neither is counted among the fourteen operational cards or marked as built by this registry.
