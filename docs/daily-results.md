# Daily results and session verification

The authenticated **Daily results** page and `GET /api/daily-results?session_day=YYYY-MM-DD`
provide complete aggregate counts for a selected trading day. `download=true` exports JSON.
There is no latest-100 cutoff. Native Morning signal processing uses 200-row keyset batches.

Paper entries use entry time; realized dollars use exit time, including positions entered
on an earlier day. All three existing paper accounts retain their 17:00 Chicago risk-day
rollover. Independent futures observations use the existing full CME research session;
options and stock observations use equity cash hours. Date defaults select the latest
opened futures session, including Sunday evening. A date that has not opened is rejected.
No entry, risk, session, sizing, or exit rules change.

The report separates paper accounts, independent futures/stock trials, intraday option
ideas, swing option ideas, the scheduled SPY plan, native Morning quote measurements,
and Smoothers weekly underlying target outcomes. TraderMatrix, swing flow comparison,
and Discovery expose daily research/cohort checkpoint completeness; these counts are
not trade win rates. Independent experiments overlap and cannot be added to account P&L.
Missing quotes or closed rows without P&L are explicitly counted, not converted to losses.
Statuses are current saved outcomes, not reconstructed historical status snapshots.

Both quiet paper decisions and alerted rejections are included. Post-lock futures
research starts at the first recorded daily-loss rejection, not an inferred exact lock
time. An absence of recorded blocks is labeled as such. SetupStudy already starts before
paper entry checks and advances independently; regression tests preserve this behavior
for overnight and cash-hour signals under the same locked portfolio. Trial counts alone
do not prove continuous quote coverage. Existing session-gap tools provide gap detail.

Native Morning is the daily signal census. The original-source archive remains a
separate mirror, explicitly labeled; missing mirror records never erase native signals
or become synthetic imports. Daily summaries report original counts and unmatched native
IDs. Stock coverage and 5/15/30/60-minute option measurements cover all selected signals.
Each horizon has its own quote-available cohort, not the matched expiration comparison
available in the existing native report. Smoothers remains a current weekly cohort.

The verification panel reports saved SPY daily evidence, confirmation decisions and data
blocks, queued drawing reasons, and unchanged-drawing suppressions. Legacy drawing
messages remain labeled legacy; the report does not rewrite history or claim delivery.
Ask Compass retains a small daily latest-lookup record per symbol/horizon with requested
and returned expirations and fresh quote counts, without prompts or credentials. These
records validate provider lookup, not generated answer quality or trade eligibility.
No next-session pass is claimed until that session's actual observations arrive.

Checks cover full aggregates, SQL on SQLite/PostgreSQL, entry-versus-exit dates, overnight
inclusion, research after loss blocks, missing P&L, native counts over 100, option quote
identity, API authentication, invalid/future dates, and lookup evidence. Existing SPY
ATR/drawing and collector-owned 0DTE/LEAPS regression suites remain required.
