# SWING IDEAS — multi-session option research

This scanner looks across the saved 144-symbol stock/ETF watchlist for daily range breakouts, SMA20 pullback reclaims and daily reversals in both directions. A confirmed underlying setup selects a listed call or put, then waits for an actual fresh, liquid option quote before announcing a simulated entry. The scanner, ledger, alerts and results are independent of Smoothers, Morning Algo, futures, and the intraday OPTIONS IDEAS study.

## Starting hypotheses

| Component | Initial rule |
|---|---|
| Universe | `WATCH_SYMBOLS`, or the saved 144-symbol list in production |
| History | At least 60 consecutive completed daily sessions and 10 complete weeks; today's unfinished daily bar and current week excluded |
| Range breakout | A completed minute crosses the preceding 20-session high/low, aligned with daily SMA20/SMA50 and completed weekly SMA10 direction |
| Pullback reclaim | A completed minute crosses daily SMA20 in the aligned daily/weekly trend |
| Reversal | A completed minute crosses daily SMA20 against the prior daily trend; prior daily RSI 25–55 for calls, 45–75 for puts. Weekly agreement is recorded separately |
| Invalidation | Breakout boundary minus/plus 0.5 daily ATR; reclaims/reversals use preceding five-session extreme plus 0.1 ATR allowance |
| Underlying target | Twice the initial underlying risk; setups requiring more than 3 daily ATR of risk excluded |
| Flow | Observed, vendor-classified bullish/bearish premium over 30 minutes, using 14–60 DTE prints by default; latest aligned print within five minutes |
| Flow confirmation | At least $100k across two distinct prints, or one $250k print; at least twice opposing classified premium |
| Contract | Listed standard 100-share CALL/PUT; 14–60 DTE, prefer 30 DTE, strike within 3% of spot; OI or day volume at least 100; supplied delta magnitude 0.30–0.70 |
| Missing delta | Near-money metadata fallback, explicitly no fabricated Greek |
| Entry | Fresh underlying and option bid/ask, positive displayed sizes; option spread at most 10% of midpoint and $0.30; quote age at most five seconds; bid at least $0.10 |
| Pending deadline | Two minutes after trigger admission; must stay within 0.25 daily ATR of trigger price, with underlying thesis and supporting flow still valid |
| Premium exits | 35% stop / 75% target from entry premium, rounded to cents; these are hypotheses, not optimized parameters |
| Time exit | At most 10 trading sessions including entry, or the session before expiry, whichever is earlier; exit 15 minutes before that session closes |
| Entry window | Cash session until 30 minutes before close |

The daily-level crossing test requires two consecutive completed minute bars, independently of the intraday scanner's 30-minute indicator warmup. The entry model buys one option at the observed ask plus $0.01. Exits use the observed bid minus $0.01 (minimum zero), with $0.65 per contract per side. Favorable target exits are capped at the premium target. Adverse gaps use the observed bid and can exceed the planned stop. Returns include these modeled costs. There are no actual orders and no portfolio entry, position, or daily-loss gate on independent swing research.

Separate trigger bars are separate experiments. Repeated evaluation of the same symbol, side, rule and trigger timestamp is deduplicated. Overlapping observations, including ones in the same option, are not an investable portfolio or independent statistical samples.

## Source coverage and freshness

Existing Alpaca SIP bars provide stock structure; Massive provides chain metadata and selected-contract live quotes; TraderMatrix provides filtered unusual activity. No new paid source or Railway service is required. Source permissions and fresh quotes still determine actual readiness.

Flow is read from the durable deduplicated `flow_records` table, not just the latest 100 dashboard rows. Only records already received with actual source timestamps in the trailing 30-minute window are eligible. The query is bounded to 5,000 records, and truncation/vendor recovery status are stored with each candidate. Totals mean **observed filtered unusual activity**, not total market call/put purchases. CALL alone does not imply bullish intent; only explicit vendor sentiment confirms direction. The model does not establish whether trades open or close positions, identify all spread legs, or infer dealer inventory.

Alpaca history is split-adjusted. Daily and weekly context uses exchange sessions, including holiday weeks. Missing required daily bars blocks discovery. The scanner checks 12 symbols per two-second turn (roughly 24 seconds plus processing for 144), refreshes flow from storage every 15 seconds, and refreshes daily calculations every five minutes. Fresh quotes are required at actual entry, regardless of scan cadence. Coverage timestamps show achieved cadence; vendor polling and publication delay remain upstream limits.

Contract requests join the existing Massive stream budget. Existing portfolio contracts and **all open intraday/swing contracts precede all pending candidates**. Capacity-starved candidates wait and eventually become excluded rather than claim an option fill. Open swing chains receive recurring metadata refreshes, including names outside the normal focus cap. The existing default stream budget is retained; subscription state alone never proves a usable quote.

## Overnight observation and outcomes

The durable `swing_ideas_v1` ledger survives restarts. Scheduled nights, weekends, holidays and early closures contribute zero to the observation-gap clock. During a closure the dashboard labels the last observed mark as a previous quote. At the next session, fresh quotes and refreshed contract metadata must arrive within a 60-second **open-market** continuity budget, including any missing seconds at the previous close.

Reopening records contain prior and current option/stock source timestamps and the observed price changes. The system uses those quotes for exit decisions. Missing open-session paths, missing holding-deadline quotes, changed contract terms or a material change to the split-adjusted reference price become `unresolved`; they do not enter win/loss statistics. This is a sampled-quote study, not a tick-complete reconstruction. The prior-session comparison is the last observed bid/midpoint, not an official closing auction price. Rare unchanged/locked quotes and unavailable chain history may prevent a valid outcome.

Each simulation records the frozen technical/flow evidence, contract, prices, fees, expiry, holding deadline, best/worst observed net return, milestones, session gaps and final reason. New entries, first +10/+25/+50% net-return milestones, session reopens and final outcomes use the distinct **SWING IDEAS** Discord category. Repeated messages are deduplicated by event identity; transport retries can still duplicate a delivered event. Delayed messages are labeled historical. No test trades are broadcast during deployment.

The private dashboard includes recent ideas plus all active observations, readiness for every watchlist name, and 90-day outcome groups by setup and direction. The grounded assistant can explain recorded swing evidence. Results stay separate from the original systems and are not used to automatically tune or replace them.

No earnings-avoidance rule or fundamental valuation model is asserted by this first version. Daily/weekly technical context plus attributed flow is the entry model. These are forward-test hypotheses; no edge or profitability has been established.

## Operations

The existing engine service runs a separately leased swing worker; collection stays in the collector service and the owner-only dashboard reads shared PostgreSQL state. New table creation uses the existing startup advisory lock. No schema changes to original ledgers are required.

Alpaca's socket reader is independent of database flushing. Quote bursts coalesce to the latest source timestamp per symbol while maintaining existing 4 Hz core / 1 Hz broad sampling limits. Completed bars and corrections are batched by symbol and minute. A bounded bar overflow is an explicit feed error, never a silent missing-history success. Quote freshness is measured at stored source time; an active socket alone does not make a quote usable. Massive option quote writes also run outside the collector event loop.

Configuration: `SWING_IDEAS_ENABLED=true`, `SWING_IDEAS_ALERTS=true`, `SWING_MIN_DTE=14`, `SWING_MAX_DTE=60`, `SWING_TARGET_DTE=30`, `SWING_MAX_HOLD_SESSIONS=10`. `OPTION_CHAIN_MAX_DTE` must cover the maximum swing DTE (existing default 90). Disabling new swing ideas still manages existing open observations. Disabling swing alerts suppresses new swing messages while retaining the ledger.

Validation includes both directions and all three technical families; source-time/flow exclusions; contract/liquidity gates; discovery-to-subscription-to-entry; unchanged portfolio counters; restart deduplication; weekend, holiday, DST and early-close clocks; adverse/favorable gaps; metadata changes; missed sessions; time exits; priority for held options; owner-only state access; and the full existing suite.

Provider references: [Alpaca historical stock bars](https://docs.alpaca.markets/us/reference/stockbars), [Massive option-chain snapshot](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot).
