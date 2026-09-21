# Ask Compass expiration-aware options research

Ask Compass research is independent of the scheduled SPY plan, intraday/swing workers, and paper portfolio entry budgets. Worker DTE settings and zero-entry budgets describe those simulations; they do not prohibit analysis. Scheduled NO ENTRY decisions remain terminal for their own strategy.

Option questions get a read-only provider lookup for their requested expiration window. Supported requests include 0DTE, numeric DTE/ranges, ISO expiration dates, approximate day/week/month/year horizons, and LEAPS (365–1095 calendar days). Dates use America/New_York. Unspecified option expirations sample today through three years; the answer must disclose that scope. Invalid, past, or beyond-three-year dates require clarification instead of silently substituting an expiration.

Research uses the configured Massive or Alpaca adapter with explicit date bounds, independently of the background chain's 90-day default. Up to three scoped symbols are fetched concurrently, each with an 18-second deadline and four pages per endpoint. Incomplete pagination and omitted symbols are explicit. Up to two near-spot contracts at each of four sampled expirations are supplied per symbol, filtered to the requested call/put side. This is a bounded sample, not an exhaustive scan or recommendation ranking. No strategy config, portfolio, order, or saved chain is changed.

Each candidate preserves source, expiration, strike, bid/ask, quote timestamp, multiplier and available dated open interest. Quotes are marked fresh only when valid, nonfuture and no more than 60 seconds old at fetch completion; this is a research label, not execution eligibility. Failed providers and missing/stale quotes limit contract-specific claims but do not prevent analysis of supplied underlying evidence. Entire option evidence sections survive context packing intact or are explicitly omitted.

The answer must distinguish market thesis, contract evidence and automated eligibility, answer the requested horizon, and never offer nonexistent follow-up execution tools. Both JSON and streamed endpoints use this policy. Regression tests cover the reported QQQ 0DTE question, LEAPS, other horizons, exchange dates, invalid dates, provider range/pagination, quote validity, context packing and both endpoints.

## Split-service deployments

The web service can request evidence through the shared database when market credentials are held only by the collector. A short-lived, leased request carries the parsed window and up to three symbols. The collector performs the read-only provider lookup and returns the evidence; no credentials move to the web service. Requests time out after 25 seconds and are removed on completion/cancellation; stale queue records and leases are cleaned after two minutes. Provider errors/timeouts remain explicit research limitations. A regression test uses a credential-free web configuration and a separate credentialed collector.
