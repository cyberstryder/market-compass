# Adaptive futures research — first build

Goal: develop strategies across monitored futures from live market evidence. The old MNQ/MGC scripts are not the target or a source of strategy rules.

The first build freezes `futures-market-assessment-v1` on new independent futures trials, using the existing completed-minute feature stream. Exact contracts, strategies, sides, fill versions, and alert cohorts remain separate in the bounded 30-day/10,000-trial report. Earlier trials are not relabeled. The dashboard's Setup Study includes a market-conditions comparison and full records expose the frozen inputs.

Hypotheses (not fitted or validated):
- Trend: EMA9/EMA21 separation at least 0.15 ATR, EMA21 slope in that direction, with VWAP and completed 15-minute bias agreement.
- Expansion: current bar range at least 2 ATR and volume at least 1.5 times the preceding 20-minute average. Takes classification priority over trend.
- Range candidate: absolute EMA separation at most 0.15 ATR and absolute EMA21 one-bar slope at most 0.05 ATR. This does not prove a bounded range.
- Mixed: other ready conditions. Unknown: missing or stale inputs, missing higher-timeframe context, or no matching forward assessment.

Proposed matches: trend pullback and ORB retest with trend direction; volume breakout with expansion direction; session sweep reclaim in possible ranges. Other combinations abstain. Fresh liquid quotes are required for a proposed match. All original experiments continue so selected and abstained groups can be compared. No trading behavior, alert authority, position sizing, or broker execution changes.

Existing trials supply one-contract entry, fixed stop and target, deadline exit, retained-quote outcomes, and illustrative costs. This build does not optimize targets/stops, fit a selector, measure a realizable portfolio, prove profitability, or provide holdout results. Commissions/slippage need calibration before economic conclusions. Aggregate closed-only metrics must be read alongside excluded and unresolved counts and report truncation.

Next: record broader no-setup opportunities and volatility/liquidity baselines; compare exit models; partition by sessions into development and untouched evaluation sets; test correlation-aware portfolio risk and forward paper execution. Keep drawdown and loss clustering central to selection. Historical repair remains secondary to forward evidence.
