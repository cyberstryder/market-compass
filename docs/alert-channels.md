# Compass alert channels

The layout has **10 Compass delivery families plus the 2 original-program channels**. These are recommended channel names, not a claim that Discord channels have already been created. Compass Feed health → Alert channels shows the actual route mode, verified channel ID, queue and confirmations. `/api/alerts/routes` exposes the same authenticated inventory without webhook credentials.

| Proposed channel | Messages | Set this variable on the Compass engine |
| --- | --- | --- |
| `spy-0dte-plan` | SPY morning plan, then the TradingView AI drawing prompt | `DISCORD_SPY_MORNING_WEBHOOK_URL` |
| `futures` | Futures setups, source observations and simulated management | `DISCORD_FUTURES_WEBHOOK_URL` |
| `options-0dte` | Other Compass same-day option simulations | `DISCORD_OPTIONS_0DTE_WEBHOOK_URL` |
| `options-ideas` | Existing all-day option ideas, 1–21 DTE | `DISCORD_OPTIONS_IDEAS_WEBHOOK_URL` |
| `swing-ideas` | Multi-session stock and option ideas | `DISCORD_SWING_WEBHOOK_URL` |
| `unusual-options` | Unusual flow plus a confirmed price setup | `DISCORD_UNUSUAL_OPTIONS_WEBHOOK_URL` |
| `exposure-levels` | Exposure-level price setups | `DISCORD_EXPOSURE_WEBHOOK_URL` |
| `intraday-stocks` | Intraday stock and ETF setups/simulations | `DISCORD_INTRADAY_WEBHOOK_URL` |
| `compass-research` | Secondary reviews, independent setup results, Morning/Smoothers mirrors | `DISCORD_RESEARCH_WEBHOOK_URL` |
| `compass-system` | Service notices and explicit delivery tests | `DISCORD_SYSTEM_WEBHOOK_URL` |
| `morning-algo` | Official Morning Algo alerts | Keep original app's `DISCORD_WEBHOOK_URL` |
| `smoothers` | Official weekly Smoothers alerts and management | Keep original app's `DISCORD_WEBHOOK_URL` |

`end_of_day_algo` is a reserved presentation category with no active producer. It does not need another channel now. Existing legacy swing and swing-option ideas share one family, while source identity remains in every message. A futures secondary review goes to research; a futures signal or management message stays in futures.

## Connect the channels

1. In Discord, create the text channels you want and one incoming webhook per channel (channel settings → Integrations → Webhooks). Reuse existing dedicated Morning/Smoothers channels; they do not require replacement.
2. Put each Compass webhook URL in the corresponding **engine service** variable above. Dashboard and collector do not need those credentials. Save/redeploy the engine; it verifies destinations with read-only GETs and displays the Discord channel IDs. Setting the same channel for several routes will still combine their messages.
3. The Feed health delivery-test selector can send a clearly labeled test to one chosen route. It never creates a trade. No test is sent automatically.
4. Until dedicated webhooks are configured, `DISCORD_SHARED_FALLBACK=true` (default) preserves delivery to the existing `DISCORD_WEBHOOK_URL`. The inventory explicitly says `shared_fallback`, so it cannot be mistaken for completed channel separation. Set it to `false` if unconfigured families should remain queued instead. An invalid explicit URL always holds that route; it does not silently fall back.

No connected Discord channel-management capability was available during this change. The existing incoming webhooks can deliver messages but cannot create or move channels. Channel creation and the new URLs are the remaining connection step. [Discord webhook documentation](https://docs.discord.com/developers/resources/webhook).

## SPY message pair

On equity session days, the existing 08:20 Chicago pre-open brief and 08:45:05 opening check each enqueue two durable messages in the same transaction:

1. Full SPY 0DTE morning plan with source comparisons and dated SPX/XSP estimates.
2. One copy-ready TradingView AI block for **1-minute and 15-minute** charts. It uses exactly the frozen report's levels, chooses the matching instrument column, labels estimated index levels, preserves unrelated chart drawings, treats VWAP as a snapshot, and marks unavailable/incomplete/expired data. This prepares a prompt to paste; it does not invoke TradingView or draw automatically.

The 1-minute view adds no entry rule. The first completed 09:30–09:45 ET candle remains the confirmation baseline. Stops and targets remain conditional unless that side is confirmed. Estimated SPX/XSP levels are chart references derived from matched previous-session SPY/SPX closes, not independently confirmed index signals or option strikes. The SPY brief remains 0DTE only; other existing strategies keep their expiration policies.

Preview both messages at `/api/spy-morning-brief`. The second payload includes a copyable text block; prices are never truncated to make it fit a short message.

## Delivery, migration and reversal

`discord_jobs_v1` holds each alert's stable route and independent saved-message receipt. Migration starts after the old acknowledged event boundary, without replaying acknowledged history. Each route advances separately; a failed route cannot block another. Within a route, the brief must be acknowledged before its chart message is attempted. Retries of the second message do not resend an acknowledged first message. Both the scheduler and message keys survive process restarts.

Discord must return a saved numeric message ID (`wait=true`) before acknowledgement. Network ambiguity can still produce duplicate Discord messages; event IDs identify retries. No exactly-once claim is made.

To reverse a destination change, restore that route's previous webhook variable, or remove the dedicated variable to use the shared fallback. Sent receipts remain sent; pending messages use the current destination. Do not revert to the old single-cursor binary while route gaps exist: it cannot understand out-of-order receipts and may replay messages. Drain the queues before a binary rollback.

No native ownership, original application job flags, TradingView intake routes, orders, or retirement controls are changed. Morning Algo Tracker and smoothers-new remain the official senders until their existing acceptance and cutover gates are met. Their future native sender webhooks and ownership-gated outbox remain separate from these presentation routes.
