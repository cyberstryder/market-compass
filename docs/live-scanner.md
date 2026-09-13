# Continuous research and setup scanner

The user's goal is one continuously monitored workspace spanning the supplied
TraderMatrix, Cipher and Toohotz references. A question is not required to start a
scan. This implementation uses available data interfaces and explicit price
rules; it does not claim to reproduce undocumented proprietary algorithms.

## Implemented

- The uploaded 144-symbol list is stored in `compass/watchlist.txt`. Production
  combines it with STOCK_SYMBOLS. WATCH_SYMBOLS can explicitly replace the saved
  additions. Test/local configurations use only their supplied symbols.
- Alpaca streams prices across that universe. Core quote observations are sampled
  up to 4 Hz; other watchlist quotes update latest state up to 1 Hz. Completed
  minute bars remain the basis of bar-confirmed patterns.
- ES/NQ contract specifications are distinct from MES/MNQ. FUTURES_SYMBOLS must
  include the intended roots on all three application services. Full-size
  contracts use their own multipliers and illustrative fees. Neither full-size
  nor micro specifications constitute a verified prop-firm account model.
- Thirty-one documented TraderMatrix research jobs cover scanners, flow,
  exposure, sector context, market conditions, calendars, disclosures and
  longer-term research. Focused names also request the documented Apex interface.
  Each job fails and retries independently; its last good snapshot is retained.
- Broad research matches can prioritize a symbol for investigation. They do not
  become trade instructions without an explicit price trigger and usable quote.
- A shared vendor request limiter spaces flow, matrix and research requests at
  least 2.6 seconds apart. The vendor's 30/minute allowance is shared with other
  clients using the same key. No request schedule can force a cached source to
  recompute faster than the provider permits.
- Six rule families run in the scanner after 30 consecutive completed minutes:
  opening-range retest, session-extreme
  sweep/reclaim, EMA/VWAP trend pullback, volume-supported range breakout,
  exposure-level crossing, and unusual-flow-plus-price breakout.
- GEX and VEX concentrations are separately identified within vendor totals.
  Previously observed Apex/flip/concentration levels can participate in a price
  crossing rule. No exposure sign is treated as directly observed dealer inventory.
- Cross-market observations include SPY/QQQ and available ES/NQ-family contexts,
  showing alignment, conflict or unknown bias. These are attributed context;
  correlated markets are not counted as independent probability estimates.
- Live scanner, research desk and a strike/expiration heatmap are available in
  the dashboard. Ask Compass receives scoped research, scanner decisions and
  technical observations as well as the existing recorded market context.
- Qualified setups contain modeled entry, explicit invalidation, a 2R target,
  matching rules and evidence clocks. The existing paper engine enforces its
  configured risk/position limits independently. A valid setup can have a
  risk-blocked paper entry; alerts report that distinction.
- Selected option contracts can arrive after a stock trigger. The scanner keeps
  a two-minute pending selection window, with repeated price-invalidation,
  quote-age and risk checks. It never assumes a Friday-only expiry schedule.
- Entries, skips, invalidations and expirations are retained. Delayed Discord
  setup messages explicitly say when their entry window has already ended.

## Timing and freshness

These are implementation limits/targets, not measured end-to-end guarantees:

| Input or action | Policy |
|---|---|
| Scanner service loop | Every two seconds plus processing time |
| Executable quote | At most five seconds old, after the signal |
| Completed-bar trigger | At most 90 seconds old; no unfinished bar |
| Unusual-flow trigger | Vendor event at most 120 seconds old |
| Exposure-level trigger | Dated vendor observation at most 600 seconds old; not vendor-flagged stale |
| New level | Must have been observed before the triggering bar began |
| Focused chains | Target refresh after 45 seconds, subject to pagination and workload |
| Background chains | Rotating target after 15 minutes; not a live full-chain tape |
| Option horizon | Up to 90 calendar days by default; OPTION_CHAIN_MAX_DTE is configurable |
| Vendor research | Independent 60-second to hourly targets; actual source and fetch clocks shown |
| Setup entry window | Two minutes; invalidation ends it earlier |
| Repeat notification | Ten-minute same-symbol/same-direction cooldown |

A publication/calculation clock, an HTTP fetch clock and a quote/event clock
are different facts. Unknown source times stay unknown. A vendor screener with
no source clock can provide research context but cannot prove a current market
condition. Macro releases and disclosure records retain the provider's content;
an automatic event blackout requires a verified event schema and is not claimed.

## Reliability and storage

Live and historical quotes use an atomic source-time comparison, so a late
snapshot cannot replace a newer quote. Bar windows use ordered row locks and
monotonic latest-bar cursors. History pagination is separated into small symbol
batches; bounded multi-row database inserts shorten recovery transactions, and
live bar writes run outside the collector event loop. A bad symbol is isolated
rather than starving later symbols.

The original baseline paper strategies remain versioned separately. Scanner
trades use `compass-scanner-v2` plus the rule name. This version excludes startup
history gaps from recent-range calculations. Secondary conditions on one
symbol are grouped and deduplicated; a repeated print does not become a new trade
because the worker restarted.

Full option-chain snapshots now use `chain_archive` events with
`encoding=gzip+base64-json`; latest chain state remains ordinary JSON. Decode the
base64 bytes, gzip-decompress, then parse JSON. Existing `chain` events remain
unchanged. Broad watchlist quote updates are not all appended as raw quote events;
recorded minute bars, triggering observations, core quotes and trade snapshots
provide the stated evidence. This is not a tick-complete replay archive.

## Activation and verification

- SCANNER_ENABLED=true and RESEARCH_FEEDS_ENABLED=true enable the new components.
- SCANNER_PAPER_TRADES=true permits only simulated entries. There is no broker
  order endpoint or execution adapter.
- Keep FUTURES_SYMBOLS identical on collector, engine and dashboard. Intended
  coverage: ES.c.0,NQ.c.0,MES.c.0,MNQ.c.0.
- The existing configured per-trade risk, realized-loss cap and maximum positions
  remain research settings. Prop-firm trailing drawdown and account reconciliation
  are not implemented, and no account-compliance claim is made.
- Validate advancing provider timestamps and source schemas in an open session.
  Successful deployment and offline fixtures do not establish that every paid
  endpoint is entitled, fresh, complete, or usable as a trading confirmation.

The supplied Cipher material remains a visual/methodology reference. No Cipher
API or proprietary scanner interface has been verified. Missing site-only
capabilities, full order-book/footprint analytics, multi-leg simulation and
weekly/monthly option trade selection remain outside the implemented six rules.
