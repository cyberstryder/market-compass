# Expanded TraderMatrix collection

September 25, 2026: adds research-only collection from documented API routes.
Uses the existing key, request limiter, raw response journal, independent retries,
last-good snapshots and research catalog. No new alerts or trading thresholds.

Added scope: accumulation-watch (the eleventh screener), screener definitions,
put/call context, sector flows/mapping/technical states and ticker map, event odds,
within-fund divergence, ETF positioning and the broad quality universe. Up to 12
focused symbols also request gamma detail, intraday/daily Apex history,
cross-expiration Apex context, repeated flow, ticker flow/analytics, pre-earnings
positioning, quality detail, institutional/divergence and congressional history.
GEX history is restricted to documented tracked core SPX/SPY/QQQ; constituent
flows cover all eleven sector ETFs. Every new feed is explicitly research-only.

New attempts share a durable 30-second minimum interval (at most two per minute),
within the existing global 2.6-second request spacing. Cadences in the catalog
are targets, not promised refresh rates. Initial acquisition is staggered; a
large focus list can take over an hour. Failures consume budget and retry with
existing capped exponential backoff. Source caches cannot be sped up by polling.
Dynamic focus can change; prior snapshots remain retained when a symbol leaves.

New observations cannot write scanner matches or focus priority, become Apex
trade levels, or qualify as live confirmations. Existing scanner and weekly
Smoothers behavior is unchanged. Historical response clocks never substitute for
receipt time or prove a point-in-time backtest. Cross-expiration evolution is
not daily history. Event odds are not forecasts produced by Compass.

Raw vendor responses are retained by matrix_request before normalized display
bounds (500 rows and other existing limits). Vendor response truncation or
pagination may still limit completeness; no all-market guarantee is made.
No optional query tuning or automatic POST screener runs are introduced. Search,
translations, aggregate stats and duplicative visualization routes are omitted;
collection of every API route is not itself a trading objective.

Validation: local scheduler/retention/isolation tests plus existing scanner tests.
Live entitlement, empty responses, source clocks and freshness must be inspected
per feed after deployment. Successful deployment alone is not data validation.
