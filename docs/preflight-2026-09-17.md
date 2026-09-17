# Opening preflight — September 17, 2026

Checks observed 07:17–07:28 America/Chicago unless otherwise stated. Evidence:
authenticated Compass Feed health/Connected projects/Research admin, TradingView
saved alerts, Railway deployment status, metrics and logs. This is a timestamped
pre-open observation; regular-session option acceptance remains due after open.

## 12 — Storage and archives

- Railway PostgreSQL volume: 50 GB. Latest physical use 19.627 GB, about 39.3%,
  leaving 30.373 GB. Compass database plus accessible WAL: 18.64 GB at 07:18:49.
  Different measures are expected: volume usage also includes filesystem and
  other database overhead.
- Recent 24-hour sampled volume range: 13.815 to 19.627 GB, approximately
  5.812 GB growth, or 0.242 GB/hour. Linear headroom is about 5.2 days at this
  rate; it is a planning estimate, not a capacity guarantee. Review daily and
  prepare a capacity/retention decision before 70% usage. No deletion performed.
- Largest relation: events, approximately 17,467.5 MB including 3,054 MB indexes.
- Scheduled archive runs every five minutes. The 07:20 run completed a read-back
  receipt at 07:21:14: frontier 74,500,000; source high ID 74,591,826; pending ID
  span 91,826 (not a row count); tail 91,142 rows / 15,674,780 compressed bytes.
- Receipt flags: durable copy verified, isolated restore verified and receipt
  read-back verified. Four audit passes, zero repairs, cleanup disabled.
  Cross-chunk completeness remains false. Bounded event receipts do not prove a
  full database backup or restore of every table. Archives do not free the source
  volume while cleanup remains disabled. Checklist 09–11 remains open.

## 13 — Channel reconciliation

Ten dedicated Compass routes point to ten distinct destinations. Their combined
pending queue was zero. The last inspected confirmation was event 74,548,092 at
07:10:18 CT with a stored Discord message ID. Connected webhooks without a new
qualifying event are not represented as verified new message deliveries.

| Channel | Producer / sender | Expected messages |
| --- | --- | --- |
| #morning-algo | Original Morning Algo Tracker; original official sender | Initial Morning stock message and subsequent edit with option/cost observations |
| #smoothers | Original New Smoothers; original official sender | Featured weekly entry, roster, target and weekly closure updates |
| #spy-0dte-plan | Compass SPY scheduler and Discord outbox | One 08:20 daily plan and one TradingView drawing prompt; opening and conditional follow-up decisions |
| #futures | Compass engine/scanner and outbox | Qualified futures setups and simulated management; raw linked futures observations also currently route here |
| #options-0dte | Compass portfolio engine and outbox | Admitted same-day simulated option entry/management/exit |
| #options-ideas | Compass intraday option-ideas worker and outbox | New 1–21 DTE idea, milestones, exit or explicit data gap |
| #swing-ideas | Compass Swing worker and outbox | Multi-session option idea, milestones, reopen and final state |
| #unusual-options | Compass scanner and outbox | Qualifying price-confirmed unusual-flow setup; not every stored TM receipt |
| #exposure-levels | Compass scanner and outbox | Qualifying exposure/price setup |
| #intraday-stocks | Compass scanner and outbox | Qualifying stock/ETF setup |
| #compass-research | Compass secondary/setup-result routing and outbox | Secondary assessments, primary setup results and original-program research mirrors |
| #compass-system | Compass system route and outbox | Service notices and explicitly requested tests |

Redundant observer notices are the raw linked-futures `project_observation`
messages (`project:futures`) added during fresh webhook ingestion with
`notify=True`. Intake is useful for the secondary comparison; its informational
Discord copy overlaps the original TradingView signal. This audit identifies it
without disabling the two futures observer alerts or changing intake. Morning
and Smoothers source ingestion uses `notify=False`; do not mistake secondary
assessments for duplicate official source sends. Original Smoothers exports no
Discord receipts; its channel history is still needed for full weekly acceptance.

## 14 — Preflight

- All seven Compass services reported successful deployments; dashboard,
  collector and engine run PR 79/main `50f3c3f8334160f9d301cd47615f2a6355737e58`.
  Original Morning and Smoothers deployments also reported successful status.
- Stock source, receipt and storage clocks advance. At the 07:20 sample, the
  retained source-age maximum was 0.18 seconds and persistence age maximum
  2.19 seconds across the sampled stock audit. Buffer: 14 quotes, zero bars.
  These are different measures from an individual SPY latency observation.
- Futures feeds on CME, CBOT, COMEX and NYMEX advance. Observed source ages
  ranged roughly 0.5–5.2 seconds; queues were zero and recent maximum queue age
  approximately 0.27–0.56 seconds.
- Option subscriptions are running and candidates/held contracts retain their
  priorities. Option persistence queue was zero. The option market is closed;
  requested subscriptions and prior-session quotes cannot certify today's entry
  or continuity coverage. Inspect actual fresh paired quotes after 08:30.
- Stock archive coverage caught up to 179/179 collected symbols. The entry
  scanner and saved Morning stock list remain distinct universes.
- TM matrix snapshots were approximately four hours old under their off-hours
  cache policy. Unusual flow's latest event was about 16.5 hours old. Current
  live-confirmation gates remain stale/ineligible; polling does not make the
  source timestamps fresh. New first-receipt assessment coverage needs RTH data.
- Discord pending deliveries: zero in the inspected snapshot. Worker success
  and zero queues do not establish missing scheduled future messages.

Both saved TradingView Morning alerts were inspected, including notifications:

| Field | Original | Compass shadow |
| --- | --- | --- |
| Saved alert | ORB Morning Tracker v1.3.0 | Compass Morning v1.3 - baseline shadow |
| State | Active, open-ended | Active, open-ended |
| Script and inputs | Legacy confirmed, baseline, 5, 60, 2, 1.2, 9, 21, 3, 1, 0.3, 20, 10 | Matching original |
| Universe | Stock List, 144 symbols | Stock List, 144 symbols |
| Timeframe / session | 1 minute / Regular | 1 minute / Regular |
| Trigger | Any alert() function call | Any alert() function call |
| Notifications | Webhook only | Webhook only |
| Destination | Original Morning app, `/hooks` | Compass dashboard, `/hooks/native/morning` |

Credentials and complete secret-bearing webhook URLs are intentionally omitted.
The separate 163-name combined watchlist is not the saved alert universe. Both
dialogs were cancelled after inspection; no alert edits were saved. TradingView
also displays its generic possible-repainting warning, which does not establish
a mismatch between these saved alerts or prove a historical/live parity result.

## Due later

07:50–07:52 refresh: SPY quote age 0.3 seconds; all twelve futures roots showed
ready quotes around 0.4–0.7 seconds. Discord queue remained zero, latest confirmed
event 74,694,027 at 07:45:04 CT. Stock maximum receipt-to-storage delay was 1.29
seconds in the sampled audit; option queue zero, futures queues 0–2 and recent
maximum queue ages below one second. Quiet premarket symbols are not assumed to
have fresh quotes just because SPY is current.

Three health warnings were resolved to their evidence: TM flow event staleness;
Obsidian historical reconstruction lacks API credentials; strategy tracking
retained a 22-hour-old `OperationalError` despite successful minute logs through
07:51. The latter was a health-reporting defect: successful worker cycles did not
clear the old error. Recovery health is now written in the same transaction as
the successful lease-owning cycle; a failed or nonowning cycle cannot clear it.
This does not rewrite observations or assert that historical gaps are repaired.
Obsidian live idea collection and unavailable historical reconstruction remain
separate, and no credentials or paid history were added.

08:20: verify exactly one daily SPY plan plus one drawing prompt and their saved
delivery confirmations; verify the actual levels on both 1-minute and 15-minute
charts. 08:45: inspect the saved opening confirmation. 09:00/09:15/09:30 are
conditional application follow-ups; a correctly skipped slot is different from
a missed required check. Reconcile actual Morning intake and new option-data
coverage after the relevant 5/15/30/60-minute endpoints. None was marked passed
using pre-open evidence.
