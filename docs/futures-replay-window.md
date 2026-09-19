# Futures live replay window repair

September 19, 2026. Both the primary CME stream and isolated COMEX, NYMEX
and CBOT streams requested one-minute replay from `now - 23 hours`.
The live gateway rejected that timestamp during the weekend, reporting
an earliest start of `2026-09-19T07:15:00Z` or `0`. Repeating the same
rolling request kept the entire session, including live BBO, in retry.

The one-minute subscription now uses `start=0`, Databento's documented
request for the full replay window actually retained by that schema.
MBP-1 stays live-only. No historical API download, new entitlement, quote
backfill, strategy change or risk-policy change is introduced.

Reference: https://databento.com/docs/api-reference-live/client/subscribe
(Intraday replay). The pinned Python SDK 0.86.0 also documents this value.
Available replay can be shorter than 24 hours on weekends. It does not
promise a complete 23-hour session; missing source intervals remain gaps.

Regression coverage runs the real collector paths against a gateway stub
that rejects a 23-hour request at the observed weekend boundary. It covers
all four exchanges, empty/nonempty replay and a second connection; retained
bars keep original timestamps and deduplicate, and cannot create a quote.
All eight cases failed on the old code with the reported start-time error.
Existing entitlement rejection, mapping freshness, writer and ingress tests
remain applicable.

After deployment, check that each stream stays connected without repeating
the invalid-start error. Closed-market status is expected; confirm dated
contract mappings and advancing, persisted quotes at the next market open.
Historical unresolved observations are not repaired by reconnecting.
