# All-day options ideas

Market Compass evaluates completed-minute price setups across its configured stock
watchlist during the cash session. The options ideas layer follows newly triggered
stock setups from the same scanner, including opening-range retests, EMA/VWAP
pullbacks, sweeps/reclaims, volume breakouts and confirmed flow/exposure breaks.
It does not copy or claim knowledge of Cipher's proprietary selection rules.

## Contract selection

- New ideas are accepted from the cash open until 30 minutes before the exchange
  close. Holiday and early-close dates use the existing exchange calendar.
- Defaults: listed expirations 1–21 calendar days away, preference nearest 7 DTE,
  strikes within 3% of the current underlying. The existing 0DTE process is separate.
- Only verified standard 100-share contracts with consistent symbol, strike,
  expiration and call/put metadata can qualify.
- Prior daily open interest or current reported day volume must be at least 100.
  OI is a daily liquidity screen, not a live unusual-flow measure.
- Where delta is supplied, absolute delta must be 0.30–0.70. Missing delta is not
  invented; selection can use near-money strike distance without it.
- A recent chain (at most 120 seconds) seeds up to four subscription candidates.
  Both the stock and option need valid two-sided quotes at most five seconds old.
- The option must be selected for streaming, have positive quote sizes, bid of at
  least $0.10, and a spread no wider than both $0.30 and 10% of its midpoint.
  Requested subscription state alone never establishes a usable live quote.
- New intraday entries use `quote-continuity-v2`: review the last five minutes
  of original fresh-at-storage quotes, require at least 15 distinct timestamps
  spanning 90 seconds, and reject any observed interior gap over 15 seconds.
  The existing 30-second check still requires five samples across 20 seconds,
  a latest age of at most five seconds, and no interior gap over ten seconds.
  Rank passing contracts by smaller five-minute maximum gap, longer observed
  span, then more samples; use recent cadence and prior metadata order for ties.
  A new contract must build sustained evidence within the existing deadline.
- The stock must remain inside its original invalidation/target and within half
  an ATR (minimum $0.04) of its trigger reference. Candidates expire after two
  minutes if no qualified option entry is available.

A lightweight task reconciles subscriptions every two seconds independently of
chain HTTP requests. Existing simulated option positions, open idea contracts and
pending candidates precede rotating background contracts within the configured
stream capacity. Lack of a stream slot cannot manufacture an option entry.

## Alerts and observations

The distinct category is **OPTIONS IDEAS**:

- **NEW IDEA:** contract, call/put, strike, expiration, option reference entry,
  underlying invalidation/target, premium stop/target and source quote time.
- **OPTION UPDATE:** first observed crossing of +10% or +25% net modeled option
  return. If one observation crosses both, one message reports the higher
  milestone and both are recorded. Milestones survive restarts.
- **OPTION EXIT:** modeled exit, reason and net dollar/percentage result.
- **DATA GAP:** missing observation coverage; final result remains unresolved.

These are independent one-contract intraday experiments. Account entry/position/
loss limits do not change their outcomes, and overlapping results are not an
account equity curve. The source scanner retains its existing ten-minute
same-direction notification cooldown. All original source alerts stay separate.

The entry model is observed ask plus $0.01. Exit marks use observed bid minus
$0.01, floored at zero, with illustrative $0.65 per-side fees. Returns divide net
P&L by entry premium times 100 plus the entry fee. These are sampled quotes and
modeling allowances, not broker fills or claims about exchange tick sizes.

V1 exit hypotheses: 30% premium stop, 50% premium target, original underlying
invalidation or target, or 15 minutes before cash close, whichever is observed
first. A favorable jump is capped at the modeled premium target. Stop gaps use
the adverse observed bid. The contract's expiration is not its holding period.
No option is carried overnight by this study.

A gap longer than 15 seconds in usable option or underlying observations becomes
unresolved. No missing price path is reconstructed and no stale mark is assigned
as a final win/loss. Disabling new ideas continues management of existing open
observations. Delayed new-idea notifications are explicitly historical.

## Dashboard and records

The Options ideas tab shows the last 100 records and 30-day status counts,
including pending, open, closed, excluded and unresolved states. It exposes the
quoted entry, marks, final result, best/worst observed net returns, timestamps,
sample coverage and source setup ID. The permanent option_ideas_v1 table preserves
the full records; original stock setup outcomes and portfolio tables remain separate.

The private /api/state response and grounded assistant include this feed.
There is no new public endpoint, provider key or broker route.

## Configuration

OPTION_IDEAS_ENABLED=true
OPTION_IDEAS_ALERTS=true
OPTION_IDEAS_MIN_DTE=1
OPTION_IDEAS_MAX_DTE=21
OPTION_IDEAS_TARGET_DTE=7

The existing option chain horizon must cover the configured idea horizon.
The implementation uses the existing Massive option quote stream and current
Alpaca stock stream; it creates no new paid subscription.

## Provider references

- [Massive option chain snapshots](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot)
  document contract metadata, Greeks, OI and quote fields.
- [Massive option quote stream](https://massive.com/docs/websocket/options/quotes)
  documents real-time bid/ask updates and the 1,000-contract connection ceiling.
  Compass keeps its smaller configured stream budget.

## Validation

Automated checks cover scanner-to-idea creation, call/put selection, metadata and
expiry rejection, stale/future/wide/empty quote rejection, subscription pinning
without chain HTTP, independent outcomes under portfolio limits, modeled returns,
target/stop gaps, restart deduplication, data gaps, intraday exit and private access.
Live operational status is logged as counts without credentials. A running worker
does not by itself prove that a new qualifying option idea has occurred.
