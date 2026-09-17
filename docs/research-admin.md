# Research admin

Open `/#research-admin` after signing in to Compass. The page summarizes existing
evidence and links to each program's reports. Search by program, filter unfinished
work or studies with completed counts, and download the current status as JSON.
The authenticated read-only endpoint is `/api/research-admin`.

Collection, assessment, measured outcomes and Discord delivery are separate.
There is no blanket research hold on alerts. Existing qualification, notification
and original-sender policies still apply. This page adds no trading rule, source
request, broker action, database migration or notification.

| Stream | Stored evidence and reporting scope |
| --- | --- |
| TraderMatrix unusual options | Received $50k+ API records and correction events; frozen first-receipt TM score and a separate Compass rubric for new records. Full 30-day score/checkpoint coverage, plus lifetime received/registered reconciliation. Missing inputs and historical inventory stay separate. |
| Morning Algo | Source and native signal inventory, stock reports and option sample slots. Bounded report coverage remains explicit. |
| Morning expiration comparison | Latest 100 native signals by default; common complete quote cohorts, separately from sample-slot inventory. Cached report refreshed by the existing worker. |
| Smoothers | Stored configuration count, source history and current native weekly observations. Full weekly comparison remains required for retirement. |
| Swing Ideas | 90-day status totals for admitted ideas; technical candidates without flow are shown separately from trades. |
| Intraday Options Ideas | 30-day status totals for admitted ideas. Option expiration and holding period are distinct. |
| Stock / ETF and futures setup studies | Separate instrument groups; all stored trials in the 30-day window; individual details paginated in groups of 100. Unresolved/excluded paths are not losses. |
| Secondary comparison | 30-day report, at most 5,000 reviews. Original-program/Obsidian candidates, including rejected verdicts; does not cover every raw TM print. |
| Obsidian | Stored idea/event inventory; detailed recent and historical audit views retain their own assumptions. |
| SPY daily plan | Latest saved plan and today's four possible confirmation checks; no complete live option-outcome ledger. |
| 0DTE simulations | All current option positions plus option trades within the latest 100 portfolio trades across instruments. Feed health also reports recent retry diagnostics, exclusions and portfolio risk context. Not lifetime totals. |
| Market research/exposure | Latest status per configured context feed; collection does not imply all returned symbols receive outcomes. |

Unknown measurements remain `null`, distinct from observed zero. The completed
study count includes cards with explicit closed/paired counts; it is not a total
of all research evidence or a profitability claim. Worker/report clocks describe
observation activity, not entry eligibility. Flow confirmation uses the existing
two-minute intraday and five-minute swing rules, including vendor stale flags.

Production evidence lives in Compass PostgreSQL. Storage accounting and the
archive scheduler status are visible, but a scheduler heartbeat does not prove a
backup or successful restoration. Sender ownership, route health and pending
delivery confirmations remain independent from research coverage.

Open `/#tm-study` for the new received-record study. Every valid new ID gets an
assessment, including delayed and rejected records. Complete scores, incomplete
inputs, scheduled/missing measurements and historical inventory have separate
counts. Its uncalibrated rubric and primary comparison are frozen in
`docs/tm-scoring-study.md`; later-session outcomes are required before judging
TM-only, Compass-only and combined selection. Stock movement is not option profit.

## 0DTE selection diagnostics

Feed health shows retries updated during the last 24 hours (maximum 5,000,
explicitly marked if truncated). Each candidate contributes its last saved
selection reason, rather than counting every retry as an opportunity. Reasons
include chain availability, actual same-day listings, underlying price/quote
gates, option quote age, spread and portfolio risk. Detailed contract rejections
are preserved even when retries are quiet, and survive expiration. Unchanged
diagnostics are stored at most every five seconds; changed reasons and fills
are saved immediately. Notification behavior is unchanged.

Historical retry records without detailed evidence remain unknown. Separately
counted skip notifications can overlap retry candidates and are capped at 1,000
with an explicit flag. The panel also reports current open positions and the
cash-date/current futures-risk-date ledgers separately. Current risk is context,
not evidence of the risk state at an earlier rejection. These diagnostics must
be checked against real candidates during the next cash session.
