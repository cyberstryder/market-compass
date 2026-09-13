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

### Connection diagnostics

- Databento SDK and stream rejections include a bounded, redacted reason in the authenticated Feed health view. API keys and provider URLs are removed before storage.
- Ask Compass reports the OpenAI HTTP status and error code, so invalid keys, missing permissions and exhausted credits can be distinguished. Failed or empty answers do not mark the assistant ready.
- Discord performs a read-only webhook check on worker startup and replaces stale configuration status. A successful check means the webhook is reachable; actual message delivery still requires a queued simulated alert. Startup never broadcasts a synthetic trade.
- Discord POST uses `wait=true` and requires a saved message ID. HTTP 204, missing IDs, rate limits and failures retain the alert in the queue. Feed health displays pending count, oldest queued age and last confirmation. `alert_delivery` journal events retain acknowledgements; duplicates can occur after uncertain network outcomes.
- Closed exchanges display `market_closed` only when the worker heartbeat is current and no provider error is recorded. Source timestamps and task updates remain separate. During open sessions, each configured underlying/future gets its own fresh-quote check.
- Exposure coverage counts gamma/OI/contract inputs independently of the underlying price. Missing price blocks calculation without erasing input counts. Counts of missing fields overlap. Missing vanna inputs stay null at strike level.
- TraderMatrix matrices show the source snapshot time and fetched time separately. Totals and strike concentrations sum populated cells across returned expirations; they are not independently verified walls. Vendor VEX units are unverified. Raw envelopes remain in `matrix_raw`; bounded summaries are supplied to Q&A.
- TraderMatrix flow uses the official documented row shape, a $50k minimum and five-page maximum. The dashboard exposes filters, vendor aggregates, row counts and pagination limits. Stable vendor IDs deduplicate first-observation journal records; latest poll rows may reflect vendor corrections. A live moving feed can shift pages and leave gaps.

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

## Deployment created on 2026-09-13

- Dashboard: https://dashboard-production-c3a7.up.railway.app
- Repository: https://github.com/cyberstryder/market-compass
- Railway: https://railway.com/project/4801dbfb-a7a9-411b-abef-696df393f0b1
- dashboard service: 33e96fc3-e153-4944-a131-b5944c12e902
- collector service: 5a1ad3c7-4090-42d0-954f-a5a55f8e8f9d
- engine service: b5a4b496-4359-45a4-b69e-8b0cfe5f2209
- Postgres service: 38f0ad27-44ae-4a1e-ada8-6de878d46873
- PostgreSQL persistent volume: 5 GB initially, mounted at /var/lib/postgresql/data.
- Provider keys and dashboard authentication have been securely configured. Alpaca SIP and Massive options streams authenticated; Databento CME live connected after the exchange entitlement was activated. TraderMatrix returned paid GEX/VEX matrices; its Sunday `today` flow was empty. OpenAI completed a funded Responses API answer. Discord passed a read-only webhook check.
- Premarket changes passed 35 offline tests plus JavaScript syntax validation before deployment. Fixtures exercise null coverage, matrix alignment, paging overlaps and caps, closed sessions versus failed workers, fresh-quote gating and confirmed Discord delivery. No fixture was inserted into production and no synthetic message was sent.
- First-session checks remain: advancing stock/futures/option source times, actual chain coverage, nonempty vendor flow, complete opening ranges, simulated fills/exits and saved Discord alerts. Current strategies enter only during the US equity regular session, including futures; overnight collection is enabled.
- The authenticated dashboard was previously browser verified. The Pine script still needs compilation and visual validation in the user's TradingView account.
