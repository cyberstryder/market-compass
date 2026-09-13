# Market Compass

Personal market intelligence and **simulated** trade alerts for futures, 0DTE options, and swings. Separate from Atlas. No broker order API is implemented.

## Services

| Service | Role | Public? |
|---|---|---|
| dashboard | FastAPI dashboard, authenticated API, grounded Q&A | Login and process health only; market data requires authentication |
| collector | Alpaca, Databento, Massive, TraderMatrix collectors | No public domain |
| engine | Deterministic scans, simulated positions, Discord outbox | No public domain |
| Postgres | Durable event journal, current views, state and leases | Private Railway network |

The same Docker image runs each role using COMPASS_ROLE. Sleeping must be disabled. Use one collector replica; the engine and alert dispatcher also use database leases. PostgreSQL startup DDL is serialized with an advisory lock.

## Configure in Railway

Set the following on the **collector**:

- APCA_API_KEY_ID and APCA_API_SECRET_KEY: existing Alpaca market-data credentials; ALPACA_FEED=sip.
- DATABENTO_API_KEY: live GLBX.MDP3/CME entitlement.
- MASSIVE_API_KEY: real-time options quotes/trades and chain snapshots.
- TRADERMATRIX_API_KEY: API subscription; optional for the raw local exposure engine.

Set COMPASS_PASSWORD (16+ characters) on **dashboard**. SESSION_SECRET is optional: the application generates a cryptographically random signing key once, inside PostgreSQL. Set OPENAI_API_KEY on dashboard for grounded Q&A. OPENAI_MODEL defaults to gpt-5-mini and can be changed to a model available to the API account.

Set DISCORD_WEBHOOK_URL on **engine** for the intended private notification destination. No webhook is reused from Atlas. Dashboard alerts work without Discord.

All application services require DATABASE_URL referencing Postgres.DATABASE_URL and the appropriate COMPASS_ROLE. COMPASS_LOCAL must remain false/unset in Railway.

**Keys are never committed or returned by the application's API.** Existing Railway OAuth credentials are redacted; adding the corresponding keys to this independent project is still required.

## First session

1. Confirm all three application deployments and Postgres are healthy.
2. Set provider keys and dashboard access password in Railway.
3. Open Feed health: confirm authentication and actual source event times during an open market.
4. Confirm futures symbols show separate resolved instrument IDs; allow at least 15 complete regular-session minutes before OR signals.
5. Inspect option chain size, OI/Greek coverage and daily OI dates. Unknown dates remain unknown.
6. Verify one entry/exit record and alert delivery. Readiness does not claim a validated live feed simply because HTTP health is green.
7. Keep v0.1 rules frozen while collecting forward-test records.

## Implemented research rules

- orb15-breakout-v1: fresh closed-minute crossing of a complete 15-minute opening range; 1.5×14-bar true-range stop distance; 2R target.
- 0dte-underlying-orb-v1: same underlying signal; eligible same-day long call/put; live bid/ask; premium stop at 30%.
- swing20-v1: completed daily close above preceding 20 daily highs; next-session quote required.

The first two enter only during the US equity regular session, with no new entries in its last 30 minutes. Intraday positions flatten 15 minutes before the equity session close, subject to an executable quote. This is not a prop-firm schedule. Swings carry overnight. Holidays and early equity closes use exchange_calendars. CME status is displayed separately.

Risk defaults: $100 planned risk per entry, $300 daily realized-loss stop, 10 entries/day, three simultaneous positions, max two MES/MNQ contracts or one option contract per entry. Gap losses can exceed planned risk. Correlated positions are capped by count, not beta or stress testing.

## Data behavior and limits

- Alpaca stock SIP stream; seven calendar days of minute history and 120 days of daily history.
- Databento GLBX.MDP3 live minute bars and MBP-1, 23-hour minute-bar replay on connection. No metered historical API call is made. Monday prior-session levels may be incomplete until older history is separately authorized/loaded.
- Stock/futures bid/ask observations sampled up to 4 Hz; options bid/ask sampled up to 1 Hz for a default 100-contract universe. This is not a tick-perfect execution simulator or complete OPRA recorder.
- Massive chain scope: configured underlyings, expirations through 45 calendar days. Pagination is bounded and external next-page hosts are rejected.
- Alpaca OPRA chain/contract metadata is an explicitly labeled fallback if Massive is not configured. Raw options streaming currently uses Massive; Alpaca's MsgPack stream is not implemented in v0.1.
- Futures VWAP is labeled a volume-weighted minute-close approximation; Alpaca uses source minute-bar VWAP when present.
- GEX: gamma × OI × 100 × spot² × 0.01, calls positive / puts negative as an inventory assumption. VEX: Black–Scholes vanna × OI × 100 × spot × 0.01, same assumption. Vanna uses a fixed 4% rate and zero dividends as a research approximation. American-option and very-short-expiry effects are not modeled. No SPX AM-settlement approximation is made.
- Missing OI is null, not zero. Contract metadata and snapshot join failures remain visible in coverage. Greeks/IV timestamp and OI date availability vary; a new HTTP response does not imply new open interest.
- Local flow labels $100k+ prints in selected contracts. It does not infer sweeps, opening/closing, aggressor intent, multi-leg linkage, or full-market unusual activity.
- TraderMatrix envelopes are recorded every ~15 seconds for the first unusual-activity page and ~60 seconds for configured GEX matrices, within a ≤24-request/min paced collector. Paid-account response fixtures must validate normalization and pagination; **site parity is not claimed**.
- No economic-news calendar, news sentiment, order-book strategy, VIX/index feed, runners/partial exits, SPX/SPXW verification, or performance-validated playbooks yet.
- Raw observations accumulate in PostgreSQL. No deletion job is enabled during the test month. Monitor disk; cold object-storage archival/retention is a subsequent operational step. Configure Railway volume backups in its dashboard.
- The append-only journal permits auditing. A dedicated historical strategy replay CLI is not yet implemented.

## Local development

Use Python 3.12, create a virtual environment, install requirements.txt, copy .env.example and export its settings. Set a local access password.

    COMPASS_LOCAL=true COMPASS_PASSWORD=your-long-local-password uvicorn compass.app:app --reload

    COMPASS_LOCAL=true python -m pytest -q

Each Railway Docker build runs the offline safety/behavior tests before producing a runnable image. Tests use synthetic fixtures only and never seed production records.

## TradingView

Copy pine/market_compass_levels.pine into Pine Editor and use a **1-minute chart**. It calculates session levels using TradingView's chart feed. Externally calculated GEX/VEX levels require manual inputs with an as-of label. Pine cannot fetch this application's arbitrary external API. The script must be compiled and visually checked in your TradingView account before use.

See docs/architecture.md and docs/operations.md for boundaries and first-session checks.
