# Research admin coverage audit — September 17, 2026

Checklist 08. Audited against main `50f3c3f8334160f9d301cd47615f2a6355737e58`.
`research-admin-v2` adds a coverage section and frozen protocol to every active
card. It distinguishes absent report evidence, observed zero, complete totals
within a stated scope, explicit truncation, and previews with unknown overflow.
Report age is not provider source age. Unavailable source clocks stay unknown;
per-feed clocks remain in Feed health.

| Program | Summary scope | Detail / calculation bounds | Denominator and limitation |
| --- | --- | --- | --- |
| TM receipts | Full 30-day receipt-window SQL; separate lifetime inventory | 100 per paginated detail request | Prospective records, complete scores and completed checkpoints separate; vendor filtered inventory is not the whole market |
| Morning stock | Default latest 100 signals and 100 research candidates; owner API maximum 200 per section | Independent truncation flags; exact ticker/stream/date filters precede limits | Original/native signal counts cannot be subtracted across different scopes; sample slots are not valid quotes |
| Morning expiration | Same bounded native signal selection | Native signals/candidates 100, receipts 40,000, research bars 50,000, source inventory 100; explicit nested flags | Complete paired signals are the comparison denominator; shared contract aliases are not independent trades |
| Smoothers | Native current-week status counts and stored source inventory are separate | Native report 100, API maximum 200; source comparison 500; explicit flags | Weekly native outcome states do not prove source delivery; source receipt export is unrecorded |
| Swing option ideas | Full 90-day SQL status/group counts | Latest 100 plus all active; no overflow flag on recent preview | Admitted option observations, not rejected technical candidates; unresolved is separate |
| Intraday option ideas | Full 30-day SQL status counts | Latest 100; no overflow flag | Full status totals remain valid even if the recent records omit older closed trades |
| Swing candidate study | Full 30-day receipt SQL and lifetime registration | 100 per paginated request | Technical candidates and underlying checkpoints; historical inventory separate |
| Stock/ETF setups | Full 30-day SQL grouped summary | Combined asset preview 100 with `records_has_more`; paginated records | Closed, open, excluded and unresolved separate; overlapping trials are not portfolio returns |
| Futures setups | Same full-window summary, filtered by dated instrument | Same detail pagination | Market assessment bar census, setup trials and variant selections use separate units |
| Secondary review | Up to 5,000 reviews within selected 30-day window | Latest 100 per source; pending earliest 100; those previews have no overflow flags | Verdict counts are assessments, not completed outcomes; bounded report flag must be checked |
| Obsidian | Full lifetime idea/event SQL counts | Latest 100 each; active tracking at most 500 with flag | Detail overflow derived from matching full inventory counts; no terminal win/loss rule |
| SPY study | Full 30-day session SQL and lifetime saved-plan registration | Reports and quote marks paginated, at most 100/request | One option observation/session; plan decisions, delivery receipts and completed paths separate |
| 0DTE portfolio | All current positions; recent trade list is not a full-history study | Latest 100 trades across all assets, no overflow flag; 24-hour retries 5,000; skips 1,000, both flagged | Option subset of recent trades cannot establish lifetime 0DTE totals; retry and skip cohorts overlap |
| Vendor research/exposure | Configured feeds only | Source-dependent pagination/cache bounds | Context inventory only; scanner heartbeat does not establish provider freshness or standalone outcomes |

The API remains authenticated and read-only. Missing setup/secondary cache
placeholders no longer display as measured zero results. Download status contains
the same protocol and coverage fields as the cards. It remains a scoped report,
not a complete database export. Unimplemented overnight trend and end-of-day
programs are explicitly listed separately.

Remaining evaluation work is to collect the prospective sessions, reconcile
matching units, export any full cohorts that exceed report caps, and calculate
the prespecified comparisons and uncertainty. This audit does not declare any
strategy superior or authorize retirement of a source service.
