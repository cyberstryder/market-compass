# Stock observation acceptance audit

The engine audits existing stock archives and Morning Algo/Smoothers reviews every
ten minutes, separately from decision loops. It changes no signals, frozen reviews,
measurements, positions, or provider requests. The only write is its own cached report.

Corrected period starts September 14, 2026 at 19:43:11 UTC, the first whole second
after the PR23 collector rollout; both retention and freshness fixes were deployed.
The boundary is clamped to 30 days. Performance comparisons include only v3 stock
decisions after this boundary. Earlier decisions with post-fix checkpoint deadlines
are included only in checkpoint verification, never rescored as new decisions.

The authenticated /api/observation-audit endpoint exposes the bounded report:
full collected-universe archive status, names without post-fix archives, first/last
TGT/SBUX source and receipt clocks and usability, corrected-period comparisons,
checkpoint status/source counts, and TGT/SBUX review diagnostics.

Supported positive/negative/flat and skipped positive/negative/flat outcomes remain
separate at 15/30/60 minutes. Missing, pending, overdue and session-boundary
observations are explicit. Underlying midpoint moves are not trade or option P&L.
Zero corrected-period reviews means no comparison evidence. Archive endpoints
prove retention exists, not a complete path or a successful checkpoint.

Queries inspect at most 5,000 reviews from the earlier of today's cash open and the
corrected boundary (within 30 days), disclosing truncation, and indexed latest-quote
probes for at most 500 stocks. Private runtime logs contain bounded summaries and
at most 200 rows per comparison/checkpoint/focus section; log limits are explicit.
The authenticated endpoint retains the complete bounded report. No original
webhook bodies, credentials, or account details are logged.
