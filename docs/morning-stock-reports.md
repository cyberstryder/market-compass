# Morning stock research reports

Connected Projects → Morning stock research → Load report calculates results from
the retained Morning history mirror. Filter by exact ticker, stream and inclusive
New York start/end dates. Each section returns the latest 100 matching records;
the owner-only API accepts 1–200 per section. Filters precede limits, and truncation
is explicit. Download JSON exports exactly the selected report scope and evidence
status, not a complete database or all-market success rate.

`GET /api/projects/morning/report` uses the existing owner session. Query parameters:
`ticker`, `stream`, `start`, `end`, `limit`, `download`. Empty sections and partial
import status remain visible. A five-minute engine audit logs a bounded default
report summary to confirm the production calculation path runs.

## Preserved source semantics

- Stock native-candle coverage and modeled 5/15/30/60-minute closes.
- +1%/−0.5% and +2%/−1% stock brackets, with a 60-minute time exit. Missing
  candles stop measurement; same-candle stop/target touches remain ambiguous.
- All six stock rules share the same complete, unambiguous 60-candle comparison
  cohort. No fees/slippage or executable option returns are implied.
- Research candidates have 5/15/30/60/120/180-minute paths. Each horizon's averages
  require its own complete contiguous path. Partial-path extrema are descriptive
  fragments and do not enter those averages. Candidate counts are correlated.
- Linked baseline research extends 120/180-minute paths only after matching source
  identity/settings. Conflicting native/research candles are removed from extended
  statistics and explicitly listed. The original native 60-minute path is retained.
- Version, configuration digest, stream and Chicago entry-time groups stay separate.

Pure calculations in `compass/morning_schema/outcomes.py` are pinned unchanged to
Morning `f459740a452a3cdf17fbb62af3d38edc1fc63c4d`. Golden results in
`tests/fixtures/morning_outcomes_golden.json` were generated with the original
Morning report functions against the same synthetic events. Tests compare native
and candidate results, missing/ambiguous paths, complete extended baselines,
cross-tape conflicts, filters, cohort exclusions and authenticated downloads.

This is calculation parity on shared fixtures, not verified complete source
inventory, live forward parity, or evidence Compass improves the originals.
Source delivery/option histories, direct event intake, strategy generation and
notification ownership remain pending. Research collection and original sends
continue independently. Missing history is never converted into a win or loss.
