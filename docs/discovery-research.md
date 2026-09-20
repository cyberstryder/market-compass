# Independent discovery research v1

This separate scanner closes a universe blind spot and tests two additional setup families. It does not inherit Obsidian ideas, modify existing strategy gates, select options, send Discord messages or place orders. The existing swing and setup studies retain their original populations.

Enable with `DISCOVERY_RESEARCH_ENABLED=true` on engine and collector (web should share configuration). Default false permits review before activation. The new dashboard page is **Discovery research**. Pending observations continue when new discovery is paused.

Coverage comprises the configured stock watchlist plus `DISCOVERY_SEED_SYMBOLS` (default PONY, FLY, ENPH, AAOI, DAL, S, TTWO, RIG, CARR, PGEN, SDGR, EWTX, ASX). These seeds came from the retrospective coverage audit, so their results must not be treated as an unbiased discovery sample. Separately, the newest received vendor-classified bullish/bearish unusual-flow names with at least $50k premium request collection without requiring an Obsidian signal. At most `DISCOVERY_DYNAMIC_LIMIT` names (default 50, maximum 100) are retained for 24 hours from receipt; base plus dynamic coverage is capped at 500. Capacity exclusions and query truncation are reported. Excluded DJT remains excluded. This is discovery within the available filtered flow feed, not a scan of every listed stock.

The collector subscribes to requested names and requests missing daily/minute history through its existing bounded recovery batches. Dynamic symbols do not enter existing portfolio strategies. Names first warm up; no fabricated old signals or options liquidity. Admission requires 60 consecutive completed daily sessions/ten completed weeks, completed consecutive minute bars, a fresh positive two-sided sized quote (<=5 seconds, no future quote), price >=$2, average prior 20-session dollar volume >=$1m, spread <=1% of midpoint, and midpoint within 0.25 daily ATR of the signal close.

## Fixed hypotheses

Both directions are evaluated until 30 minutes before cash close using the exchange calendar. There is no requirement to cross SMA20 or the 20-day extreme in this new research path.

- **Continuation:** price/daily SMA20/SMA50 aligned, close within one daily ATR of the prior 20-session high/low, and latest completed minute moving in the proposed direction.
- **Bounce:** previous daily close below SMA20 and RSI 25–50 for a bullish bounce (above/RSI 50–75 for bearish), latest minute closes above previous minute high (below previous low for bearish), and price within two daily ATR of the previous five-session reversal extreme.

Both require classified directional flow satisfying the existing premium/dominance strength test, but use a separate 0–180 DTE research window. **Fresh** (<=5 minutes) and **delayed** (5–30 minutes) sources are labeled separately. Unknown/truncated/recovery-incomplete evidence and fresh prints blocked by vendor freshness do not qualify. Call volume alone is never assumed bullish. The wider flow-expiry window does not promise the same expiration or strike, or make those contracts eligible in the existing option selectors.

All gates and source/receipt times freeze on candidate creation. One candidate per symbol/direction/rule/session prevents minute-by-minute duplicate continuation records. Nonqualification reasons freeze per completed minute (five-minute interval during missing history), and current coverage is separately refreshed. Rows are saved in `discovery_decision` events; they do not create outbox jobs.

## Outcomes and review

Dedicated discovery tables freeze underlying midpoint entry and checkpoints at 60 trading minutes, candidate-session close, one later close and five later closes. Close checkpoints use the final cash minute; holidays and early closes follow the exchange calendar. A valid quote must arrive within 60 seconds after the checkpoint and be fresh at observation. Missed deadlines become unavailable, never a reconstructed fill. Outcomes do not establish path continuity, stop/target order, cost-adjusted returns or option profit. Candidate records and pending endpoints survive restart. The dashboard shows the latest 100 candidates and explicit truncation; all records remain stored.

Activation begins a new exploratory cohort. Compare by source origin, fresh/delayed flow, symbol, direction and session; overlapping observations remain correlated. Freeze this version during collection. No automatic promotion, gate loosening, return claim or strategy replacement. Review after at least ten complete entry sessions plus maturation of five-session endpoints. Historical winners supplied for the audit are not a held-out test.

Worker: batches of 12 symbols every five seconds, ~70 seconds for 157 plus processing; source clocks still enforce <=90-second minute freshness. Daily context caches for five minutes. Existing history-repair quotas, quote subscriptions, storage and provider entitlements govern realized readiness. Watch actual cadence and misses before enlarging the bound. Rollback: disable new discovery on engine/collector; retained pending measurements continue to completion.
