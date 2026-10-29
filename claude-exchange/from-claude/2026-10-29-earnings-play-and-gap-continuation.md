# Task 009 — Why the 0DTE book missed 2026-10-29's movers; post-gap continuation replay

Claude, 2026-10-29. Opened at Josh's direction after today's board (NOW −27%, GOOGL −13%, MRVL −14%, CAT −10%, CVNA +16%,
LLY +6% …) and the 0DTE paper book holding none of it.

## Why it missed them (code on main, `d15598f` basis)
1. **Universe.** The only strategy aimed at earnings moves, `earnings_play.py`, runs over `OPRA_TICKERS`
   (`engine.py:678`) — 16 names. NOW, CAT, MRVL, LLY, CVNA, DELL, PANW, MPC, DDOG, NET, SNOW, MA, LMT, SPOT are not in it.
   The 2026-10-15 revert (d35d433) restored the 144-name scanner universe but did not touch this list.
2. **Gap baseline (likely, Quill to confirm).** `earnings_play.py:109-117` takes prev_close = last bar before today. If the
   stock bar stream carries after-hours bars, that is a ~18:59 CT print that already contains the AMC move; open − prev_close ≈ 0,
   under the 3 % filter. Baseline must be the 15:00 CT RTH close. Confirm by reading `intel:earnings:2026-10-28` and the GOOGL /
   MU bar rows around 18:30–19:00 CT yesterday.
3. **Entry window timezone.** `earnings_play.py:285-288` enters 09:45–10:30 **CT**; the comment says "15 min after 9:30 open".
   Open is 08:30 CT. The code waits 75 min.
4. Scanner-side 0DTE rules need a fresh quote and |mid − signal| ≤ 0.5 ATR14; on a −27 % day that tolerance is not met, and any
   entry faces the Task 005 observation-gap termination.
5. Structural: these are overnight gaps. A same-session book can only own the post-open continuation.

## Is the continuation worth owning? Replay on data-foundation-v2-stocks
`2026-10-29-gap-continuation/gap_replay.py`, 79,690 symbol-days. No earnings calendar in the data, so the proxy is the
**idiosyncratic gap** (gap − SPY gap) on days with |SPY gap| < 1 % — earnings-like by construction. Outcomes sign-adjusted
to the gap direction (+ = continuation), bps of the 08:30 open. "e845" = the earnings-play rule as written (enter 08:45 CT,
stop at 15-min OR extreme, 2R target, flatten 14:55); "H1/H2" split at 2025-10-27.

| bucket | n | open→60m | open→close | 08:45→close | P(cont) | e845 R | e845 win | H1 R | H2 R |
|---|---|---|---|---|---|---|---|---|---|
| baseline, idio < 1 % | 53,910 | +3 | −1 | −1 | 0.488 | 0.003 | 0.157 | −0.005 | 0.012 |
| idio ≥ 3 % | 4,619 | −1 | +2 | +5 | 0.487 | 0.007 | 0.384 | −0.053 | 0.050 |
| idio ≥ 5 % | 1,718 | 0 | +1 | +5 | 0.483 | 0.002 | 0.388 | −0.022 | 0.023 |
| idio ≥ 8 % | 680 | −6 | +14 | +24 | 0.513 | 0.032 | 0.406 | 0.039 | 0.026 |
| idio ≥ 12 % | 258 | −33 | 0 | +29 | 0.539 | 0.068 | 0.426 | 0.082 | 0.052 |

- **The continuation premise is not there.** After an earnings-sized idiosyncratic gap the stock's open-to-close move in the gap
  direction averages 0–14 bps with P(continue) 48–54 %. The earnings-play rule returns **+0.00 to +0.07 R per trade on the
  underlying**, win rate 38–43 %, sign-unstable across halves at 3–5 %. That is before a 0DTE ATM option's spread and the IV
  crush that the strategy docstring itself names.
- It is a coin flip with fat tails, not a drift: at idio ≥ 5 %, open→close continuation is **p10 −760 / p50 −19 / p90 +806 bps**;
  27.6 % of days continue > +300 bps and 28.3 % reverse < −300 bps. The post-earnings day is a volatility event, not a direction event.
- Including index-wide gap days makes it worse (all-gap ≥ 3 %: open→close **−25 bps**, 08:45→close −25 — those days mean-revert).
- The only mildly positive cell is ≥ 8 % **down** gaps (e845 +0.12 R, 44 % win, both halves positive, n 268). Small, and
  that is the cell where put spreads and borrow are worst.

## Disposition
Fixing items 1–3 would make the earnings play fire. The replay says it would then trade a ~0 R underlying edge through a 0DTE
option with IV crush. **Recommendation: do not widen or fix it as a momentum play.** If the earnings day is to be traded, the
structure the data supports is a **volatility** trade (straddle/strangle priced against the realised |move| distribution above,
or a defined-risk intraday range-break) — a different strategy, through the gate, not a patch to this one. Quill: items 2–3 are
confirmable from the DB; nothing to implement until Josh decides.
