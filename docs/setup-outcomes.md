# Independent setup outcomes

The September 13–14 simulation used a shared ten-entry cap. It filled ten trades
by 22:14 CT and continued producing setups without further portfolio fills.
That capped ledger cannot evaluate every opportunity in the overnight session.

`SHADOW_MAX_ENTRIES=0` now means unlimited portfolio entries. A positive value
remains available for a later account simulation. Existing risk history stays
intact; the separate portfolio's daily-loss and concurrent-position limits remain.

## Forward experiments

The **Setup results** screen reads the new `setup_trials_v1` ledger. Every
qualifying intraday stock/ETF or futures candidate starts its own one-unit
experiment without reading the portfolio's loss, entry, position or sizing gates.
Actual options and weekly swings retain their existing ledgers; a stock outcome
does not establish an option profit. Original Morning, Smoothers and TradingView
strategies are unchanged.

The scanner preserves separate experiments for simultaneous matching rules and
for qualifying candidates during alert cooldown. The report separates alerted
setups from cooldown candidates. Source IDs deduplicate retries and survive
restarts. Version, direction and dated contract remain separate report groups.
Old alerts are not reconstructed into forward trades. Activation is persisted.

Each trial freezes a fresh post-signal bid/ask entry plus one adverse tick, an
explicit invalidation rounded outward to the instrument's tick, a 2R target,
one-unit multiplier and illustrative fees. A stop requires an observed executable
bid (long) or ask (short), with adverse gap/slippage charged. A target fill is
capped at the target even if the observed quote improves beyond it. Session exits
use the configured intraday deadline, no later than 15:45 CT for futures and
earlier for an exchange early close. These are research settings, not a Lucid
funded-account controller.

Measurements are sampled executable-quote simulations, not tick-complete fills.
An observation gap exceeding 15 seconds, or a missing session-exit quote, ends
the experiment as **unresolved**. It is excluded from win rate. No later quote
from another session fills it. The study does not invent the ordering of touches
between samples. Excluded entries, open trades and unresolved paths remain visible.

Results include wins/losses/breakeven after modeled costs, target/stop counts,
mean R, duration, sampled favorable/adverse excursion and quote gaps. A target
touch and net profitability are separate fields. Overlapping independent trials
are not an account equity curve. Reports cover the latest 30 days, at most 10,000
trials, disclose truncation and show the latest 100 individual records; all rows
remain retained. Owner-authenticated full-record endpoints expose each trial.
Primary alerted experiments emit separate **SETUP RESULT** notifications.

## Additional futures

Optional `EXTRA_FUTURES_SYMBOLS` defaults to MGC/GC, SIL/SI and MCL/CL, using
`.v.0` prior-day-volume selection. Two independent COMEX/NYMEX live tasks verify
access through the existing Databento key. Rejections are reported per exchange;
they cannot add instruments to, terminate or replace the original index stream.
The existing historical-download budget and core history scope are unchanged.
Optional products warm up from the live service's intraday replay and ongoing bars.
Replayed minute bars are written in bounded batches; current bars publish
immediately. This avoids a separate database round trip for every old bar during
reconnect and keeps replay work from delaying live quote processing.

Data access is confirmed only by received per-contract quotes. A configured
symbol or mapping alone does not establish entitlement or trading readiness.
Use Feed health for the live evidence. Each resolved raw contract and instrument
ID is stored; expired mappings cannot be used for new signals. When the live
provider sends undefined interval bounds, a fresh quote activates a 30-second
mapping lease that must be renewed by further live quotes. Known expired
intervals remain invalid. Index contracts
keep their original quarterly roll policy. New commodity contracts use their
provider mapping instead of applying an index quarterly calendar.

Commodity studies use a labeled **08:30 CT research** opening range in addition
to the Globex range, not an asserted commodity pit-session opening. They do not
inherit the index 15:15–15:30 halt. Their secondary reviews do not assume SPY/QQQ
direction confirms a gold, silver or oil trade. Paired micro/full-size commodity
context remains attributed observation.

Specification references:

- [Databento continuous symbology and mapping intervals](https://databento.com/docs/standards-and-conventions/symbology)
- [Databento live symbol mapping](https://databento.com/docs/examples/symbology/live-symbol-mapping)
- [CME Micro Gold specifications](https://www.cmegroup.com/markets/metals/precious/e-micro-gold.contractSpecs.html)
- [CME metals product guide](https://www.cmegroup.com/markets/metals/metals-product-guide.html)
- [CME Micro WTI / WTI specifications](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq)
