# Task 009 — Quill verification of Claude's earnings-play findings

Quill, 2026-10-29. Code read on production main; bar evidence from the
data-foundation-v2-stocks pull (Alpaca SIP, same feed family as production).

**Item 1 — universe: CONFIRMED.** `engine.py:690-691` calls
`scan_earnings_plays(..., list(OPRA_TICKERS))`; `OPRA_TICKERS`
(tradermatrix_intel.py:31) is exactly 16 names: AAPL, AMD, AMZN, AVGO, CRWV,
GOOGL, META, MSFT, MU, NFLX, NVDA, PLTR, QQQ, SPY, TSLA, USAR. NOW, CAT, MRVL,
LLY, CVNA, DDOG, SNOW are not in it, as Claude said. Membership nuance:
GOOGL, MU, META, AMD ARE in the 16 — for those names today's silence is
explained by items 2–3 and the research-mode policy, not the universe.

**Item 2 — gap baseline: CONFIRMED, with today's GOOGL as the exhibit.**
`earnings_play.py:109-117` takes prev_close = last bar before midnight CT.
The bar stream carries after-hours bars to 18:59 CT. GOOGL 2026-10-28:
RTH close 326.95; 18:59 CT print 292.50 (the AMC earnings move already in);
2026-10-29 first RTH open 286.875. Gap as the code computes it: -1.9%,
under the 3% filter — skipped. Gap vs the RTH close: -12.3%. The filter
never stood a chance. (MU same shape, no report: 986.52 RTH close, 959.00
at 18:59, open 967.99 → computed gap +0.9%.)

**Item 3 — entry window: CONFIRMED.** `earnings_play.py:285-288` admits no
signals before 09:45 CT — 75 minutes after the 08:30 CT open. The file's own
comments disagree ("15 min after 9:30 open" vs "9:45 AM - 10:30 AM CT");
behavior is unambiguous.

**Item 4 — scanner tolerance / observation-gap: not re-verified here**
(stands as Claude read it; today's production payload independently shows
the 0DTE book in research mode: paper entries and paper notifications both
disabled, 0 bookings on 5,213 selection events — the system-level reason
nothing alerted at all today, one layer above the earnings play).

**Replay: internally consistent across its two cuts** (idio-only memo table
vs all-gap report table; the down >=8% cell is the only positive one in
both: +0.068 R all-gap n=402, +0.12 R idio n=268, both halves positive in
each). Headline accepted: post-gap continuation ~0 R on the underlying,
P(continue) 48-54%, two-sided fat tails.

**Quill concurs with the disposition:** do not widen or fix the earnings
play as a momentum play. Items 1-3 are real defects; fixing them would make
a ~0 R underlying edge tradeable through IV-crush 0DTE premium. If the
earnings day is traded at all, it enters as a new volatility-structure
hypothesis through the normal gate. Nothing implemented; awaiting Josh.
