# Operations: first test session

## Activation

All required code and service roles can be deployed without buying another server. GitHub stores versioned code; Railway runs the processes continuously.

1. Open the Railway Market Compass project, production environment.
2. Set COMPASS_PASSWORD on dashboard (minimum 16 characters). The app creates its own independent random session-signing secret inside Postgres unless SESSION_SECRET is explicitly provided.
3. Add Alpaca, Databento, Massive and optional TraderMatrix credentials to collector using the exact names in .env.example.
4. Add OPENAI_API_KEY to dashboard if Q&A is wanted. A ChatGPT subscription is separate from API billing.
5. Add the intended private DISCORD_WEBHOOK_URL to engine. The app sends real simulated-decision alerts after it is configured; there is no synthetic test broadcast on startup.
6. Check all application services have DATABASE_URL = a Railway reference to Postgres.DATABASE_URL. Do not copy database passwords into source.
7. Confirm sleep mode is off and each service has one replica. Stock, option and futures streams must remain connected while markets are open.
8. Set Railway volume backups and resource/spend monitoring in the Railway dashboard.

## Tomorrow's acceptance criteria

- Prices are from the selected provider/feed and source timestamps advance during the expected market session.
- MES/MNQ observations retain the resolved contract instrument ID.
- The first 15 regular-session minutes are present before an opening-range decision.
- A missing option OI/Greek field is represented as missing; option coverage is assessed on the declared union of metadata and snapshot contracts.
- The selected-contract count and limits are visible. Large prints are not described as complete unusual activity.
- Trade entries have signal, decision and quote timestamps. Entry is at bid/ask plus adverse slippage, never an old chart level.
- Every new simulated position has a stop, target, quantity, rule version and a journal event.
- A missing exit quote produces an unresolved management warning.
- Alerts identify themselves as simulated and carry stable event IDs.
- Q&A uses stored timestamps and admits missing evidence.
- Check at least one open-session run before marking a provider as validated. /api/readiness intentionally stays live_validated=false until a subsequent reviewed validation change.

## Failure response

| Symptom | Action |
|---|---|
| HTTP 401/403 | Check key, market-data plan and exchange entitlement; do not downgrade the feed silently |
| HTTP 429 | Keep backoff; reduce scope/poll frequency or resolve plan limit |
| Databento connection fails | Verify live GLBX.MDP3 plan and CME nonprofessional/professional declarations |
| No option quotes | Verify Massive real-time options plan and selected contracts; snapshot access alone is insufficient |
| Low GEX coverage | Inspect null OI/Greeks and OI dates; do not replace nulls with zero |
| Partial prior-session levels | Wait for enough history or authorize bounded historical backfill |
| Red collector, green server | Process health is not feed health; use timestamps in Feed health |
| Webhook error | Fix destination; journal stays available and dispatcher retains its cursor for retry |
| Storage growth | Reduce option universe, add compressed object archive, and review retention before deleting evidence |

## Simulation limits

This initial deployment is for forward-test evidence, not a claim that the supplied discretionary playbooks have been reproduced. No broker credentials are used to submit orders. Strategies share recorded data and Q&A context, but v0.1 does not allow unverified vendor exposure to veto or create a trade.

Filled trades can differ from real fills because of quote sampling, size units, exchange conditions, queue position and latency. Futures fees ($1.50 per side per contract) and options fees ($0.65 per side per contract) are explicit placeholders in engine.spec and must be matched to the intended future brokerage before evaluating net performance.

Stock quantity uses a maximum of 100 shares; futures maximum two MES/MNQ; options one contract. The $100 risk and $300 daily realized-loss values are test defaults. They are not individualized recommendations or hard loss guarantees.

## Data costs to activate separately

The previous planning estimate was Databento CME Standard plus Massive real-time Options and optional TraderMatrix API, using the existing Alpaca plan. No new data subscription was purchased during implementation. Confirm current plan scope and price in the provider checkout before activating the one-month test. Railway usage and OpenAI API usage are additional.

## Rollback and exports

All deployment source is in a private GitHub repository. Railway can redeploy a previous commit. Database records survive application redeploys. Download small journal slices from the authenticated /api/events endpoint; use PostgreSQL backup/export for full records. Never delete or rewrite a test run to improve its reported results.
