# September 15 data-quality follow-up

Chart export evidence from issue #42 is keyed by exchange ticker, exact signal
clock and script version. The seven original CSV hashes and all 48 minute slots
are retained in compass/chart_gap_evidence.json. Report annotations intersect
that evidence with current missing minutes; subsequently present minutes are
explicit evidence disagreements. No candle, receipt, outcome or complete-cohort
eligibility changes. Retrospective timestamp absence does not prove delivery
success, lack of exchange trades, or the reason for sparse charts.

Legacy v1.2 traces inspect immutable imported checkpoint envelopes and matching
research sessions. Native candles found in a packet but absent from the inventory
are reported as recoverable import mismatches. Research overlaps are separate and
are never promoted into native history. Absent checkpoint receipts do not prove
webhook failure. The September 11 audit has a fixed date filter and 200-signal
limit so new signals cannot displace the original investigation; truncation is
explicit. Reports and logs retain receipt hashes, horizons and missing-slot lists.

Options feed validation uses opened_at >= 2026-09-15 15:42:34 UTC, conservatively
after PR47 collector deployment ab6d97d7-5414-4591-b06c-1e8bc4d721ca became healthy
at 15:42:33.300 UTC. It excludes observations already open under the old collector.
The query remains limited to 24 hours / 5,000 records with truncation disclosed.
This bounded forward cohort validates observation completion, not profitability
or a causal before/after improvement claim.
