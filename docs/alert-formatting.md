# Alert source and event formatting

Updated September 14, 2026. This change standardizes presentation in Market Compass, Morning Algo, and Smoothers. Signal rules, order routes, sizing, schedules, delivery destinations, and stored results are not changed.

Every trading alert leads with **SOURCE | EVENT | SYMBOL · DIRECTION**. Its next line distinguishes a signal, simulated trade, source instruction, or observed outcome. Trade levels appear before supporting statistics. Displayed clocks use America/Chicago with the CT label; exchange calendars and provider queries keep their existing timezones.

| Source label | Identifies | Implementation coverage |
| --- | --- | --- |
| SMOOTHERS | The weekly Smoothers strategy | Native entry signals, target hits, weekly closes, and every roster chunk |
| FUTURES | Futures scanner ideas or mirrored Pine instructions | The second line and Source field distinguish Compass simulation from TradingView source alerts |
| MORNING ALGO | Morning ORB / fast-open stock signals | Native signal and attached option quote update |
| UNUSUAL OPTIONS | A setup whose primary rule is unusual flow plus a price breakout | Identifies that scanner family; this change does not add notifications for every raw options print |
| 0DTE OPTIONS | A simulated same-day option selection / position | Explicit BUY CALL or BUY PUT, expiry, option premium levels, and simulated P&L |
| EXPOSURE LEVELS | A primary exposure-level price-break setup | Supporting flow alone does not rename another strategy |
| SWING | Compass multi-session swing research | Includes swing20 closing-breakout entries and their management/exit events; separate from Smoothers |
| END OF DAY ALGO | A distinct end-of-day strategy producer | Reserved explicit category; no verified active producer was found. Do not infer this from message time, a daily-bar signal, or a daily recap |
| INTRADAY STOCKS | Other Compass stock/ETF setups | Technical intraday signals and their simulated lifecycle |
| SYSTEM | Delivery checks and service notices | Test messages retain the existing TEST — NO TRADE prefix |

## Event meanings

- **SIGNAL / ENTRY SIGNAL:** the source condition fired; the reference price is not a broker fill.
- **SETUP:** Compass has a confirmed candidate. The message states its paper-position status and entry-window end.
- **PAPER ENTRY / PAPER EXIT:** modeled position events, including modeled prices and P&L when available.
- **SOURCE ENTRY / SOURCE EXIT / SESSION EXIT:** a mirrored TradingView instruction. Broker fills and bracket execution remain unconfirmed.
- **TARGET HIT:** Smoothers observed the underlying target condition. This is not proof of realized option profit.
- **WEEK CLOSED:** the weekly deadline was reached without a target hit. Stored strategy accounting is unchanged; this label does not assert a verified option loss.
- **OPTION QUOTE UPDATE:** quotes appended to the original Morning Algo signal, not a new trade.
- **INVALIDATED / EXPIRED SETUP / SKIPPED / OPTION SKIPPED / DATA BLOCKED:** a setup or management constraint, not an entry instruction.
- **WEEKLY ROSTER:** a recap of the Smoothers cohort. Continuation messages repeat the source and event labels.

Management and invalidation messages carry the originating setup's metadata. For exits, the exit reason takes precedence over the original entry thesis. Symbol, reference ID, and event time keep related messages recognizable. Morning Algo and Smoothers retain their own senders; Compass identifies its category in the sender name and journal.

## Generated examples

**All prices, signals, IDs, and results below are synthetic formatting fixtures. These are previews, not market observations, trade recommendations, or delivered notifications.** Text and embed fields were captured directly from the changed formatters with network delivery mocked.

### Futures setup

**FUTURES | SETUP | MNQZ6 · SHORT**
[SIMULATED SETUP] — no broker order · Intraday futures
Setup: EMA / VWAP trend pullback
Instrument: Futures contract
Entry reference: 25,000.00 | Stop: 25,025.00 | Target: 24,950.00
Entry window ends: 2026-09-14 09:07:00 CT
Paper position: entered. Option selection is reported separately.
Reason: EMA/VWAP pullback rejected; completed 15-minute bias agrees.
Source: Compass scanner
Ref: demo-trend-01
Event #1 | 2026-09-14 09:05:00 CT

### Futures source instruction

**FUTURES | SOURCE ENTRY | MNQ1! · SHORT**
[SOURCE ALERT] — broker fill unconfirmed · Intraday futures
Setup: MNQ Level Rejection + Trend Speed
Instrument: Futures contract
Source reference: 25,000.00 | Stop: 25,025.00 | Target: 24,962.50
Quantity: 1
Reason: Pine entry instruction. Broker execution is unverified.
Source: TradingView mirror
Ref: demo-source-01
Event #2 | 2026-09-14 09:05:00 CT

### Unusual options-supported setup

**UNUSUAL OPTIONS | SETUP | NVDA · LONG**
[SIMULATED SETUP] — no broker order · Intraday
Setup: Unusual options flow + price breakout
Instrument: Underlying stock / ETF
Entry reference: 100.00 | Stop: 99.50 | Target: 101.00
Entry window ends: 2026-09-14 09:07:00 CT
Paper position: risk or position blocked. Option selection is reported separately.
Reason: Recent unusual options activity and a confirmed underlying price breakout.
Source: Compass scanner
Ref: demo-trend-01
Event #3 | 2026-09-14 09:05:00 CT

### 0DTE paper option entry

**0DTE OPTIONS | PAPER ENTRY | NVDA 100P · BUY PUT**
[SIMULATED] — no broker order · Same-day expiry
Setup: Opening-range breakout / retest
Instrument: PUT option · expiry 2026-09-14 · NVDA260914P00100000
Simulated entry premium: $1.50 | Stop premium: $1.05 | Target premium: $2.40
Quantity: 1
Modeled initial risk: $48.30
Source: Compass scanner
Ref: demo-option-01
Event #4 | 2026-09-14 09:05:00 CT

### Swing paper entry

**SWING | PAPER ENTRY | SPY · LONG**
[SIMULATED] — no broker order · Multi-session swing
Setup: 20-day closing breakout
Instrument: Underlying stock / ETF
Simulated entry: 100.00 | Stop: 99.00 | Target: 102.00
Quantity: 10
Reason: Completed daily close above preceding 20 daily highs
Source: Compass engine
Ref: demo-swing-01
Event #5 | 2026-09-14 09:05:00 CT

### Morning Algo stock signal

**MORNING ALGO | SIGNAL | NVDA · CALL BIAS**
Signal only · Morning intraday
Setup: Opening-range breakout · 1m confirmation
Stock reference: $100.0000 | Local RVOL: 1.50x
Signal time: 2026-09-14 09:05:00 CT
Source: TradingView · legacy confirmed · v1.0.0
Ref: demo-morning-01
Confirmed stock signal; price is a reference, not a fill.
Option quote: retrieving from Alpaca…

### Morning Algo option quote update

**OPTION QUOTE UPDATE | NVDA $100 CALL · 2026-09-18**
Bid $1.50 | Ask $1.60 | Mid $1.55
Cost at ask: $160.00 / 100-share contract, before fees
OPRA quote: 2026-09-14 09:05:01 CT · observed after the stock signal
Nearest listed expiry after today; closest strike. Quote only; no fill assumed.

### Smoothers entry

**SMOOTHERS | ENTRY SIGNAL | META · PUT**

Signal only · Weekly swing. Underlying levels and option quotes are references, not broker fills. Featured rank uses target / ATR; the score is not a win probability.

| Field | Value |
| --- | --- |
| Underlying Entry | $100.00 |
| Underlying Target | $98.00 |
| Strike | $100.00 |
| Option Reference Premium | $1.50 |
| DTE at Entry | 4 days |
| Est return | +20% |
| Quality Tier | A+ • Featured #01 • Overall #01 • Score 87.0/100 |
| Target / ATR | 0.300 |
| ATR component | 0.0/100 |
| Reliability | 0.0/100 |
| Live WR | 5W/2L (71%) |
| Backtest WR | 75% |
| WR (last 4) | 3W/1L (75%) |

Smoothers · Weekly swing · Signal #42

### Smoothers target hit

**SMOOTHERS | TARGET HIT | META · PUT**

Exit signal · Weekly swing · Official 1-minute monitor. Target Level is the strategy trigger; 1m High/Low/Close show the completed touch bar. Live option quote is an estimate, not a guaranteed fill.

| Field | Value |
| --- | --- |
| Direction | PUT |
| Strike | $100.00 |
| Underlying Entry | $100.00 |
| Target Level | $98.00 |
| Target Minute | Mon 10:00:00 CT |
| Alert Sent | — |
| Option Entry Reference | $1.50 |
| Option Quote | Unavailable — target hit is still official |

Smoothers · Weekly swing · Signal #42

### Smoothers week closed

**SMOOTHERS | WEEK CLOSED | META · PUT**

Target not reached by the weekly deadline. This is a strategy outcome, not a verified option loss.

| Field | Value |
| --- | --- |
| Direction | PUT |
| Strike | $100.00 |
| Underlying Entry | $100.00 |
| Underlying Target | $98.00 |
| Underlying Close | $99.00 |
| Result | Target not hit |

Smoothers · Weekly swing · Signal #42

### Smoothers weekly roster

**SMOOTHERS | WEEKLY ROSTER**

Weekly stock-option signals · Sun Sep 13, 2026 · 21:46 CT
30 observations | PUTs: 30 CALLs: 0 | Bias: bearish
A+ = top 10 eligible signals by target / ATR (lower first). WATCH = research only. Cost and return are option estimates; stock levels are references. Quality score and recorded win rates are not win probabilities.

🌟 **A+ FEATURED (30)**

**DEMO0 PUT · F01**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO1 PUT · F02**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO2 PUT · F03**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO3 PUT · F04**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO4 PUT · F05**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO5 PUT · F06**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO6 PUT · F07**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO7 PUT · F08**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

**DEMO8 PUT · F09**
Stock $100.00 → target $98.00 | Strike $100.00
Option cost $150 | Est return +20% | ATR 0.300 | Score 87.0 | Backtest 75% | Recorded 0W/0L (—)

## Verification

- Existing Compass suite: 139 checks passed initially; the remaining delivery-test prefix check passed after preserving its established test marker.
- Morning Algo: 104 passed locally; six PostgreSQL-specific checks require its existing CI database job.
- Smoothers: 53 passed locally.
- Offline preview checks covered source categories, BUY PUT labeling, source-reference versus simulated-fill distinctions, metadata continuity, exit reasons, delayed setups, and message length.
- Roster checks confirmed that every synthetic row was retained once, continuation headers identified Smoothers, and oversized blocks stayed within the message limit.
- No preview was posted to Discord. GitHub CI and Railway deployment outcomes are recorded in the implementation PRs.
