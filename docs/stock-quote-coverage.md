# Stock quote archives and secondary checkpoints

Stock stream samples now retain journal events across the collected universe,
including the saved watchlist and connected Morning Algo/Smoothers symbols.
Core symbols keep their existing maximum 4 Hz sampling; other stocks retain up
to 1 Hz. Coalescing still keeps the newest quote between writes. No extra provider
requests are introduced. Latest-state and archive writes share a transaction.

Feed health automatically adds `stock_quote_archive`. It distinguishes no quote,
missing archive, archive more than 15 source-time seconds behind latest state,
and retained samples. Missing/behind symbols are named in the health row; all
per-symbol timestamps and status rows are available in the authenticated snapshot.
This is archive coverage, not proof of live freshness or a complete price path.

Secondary review checks each pending 15/30/60-minute window against retained
quotes before using the latest live quote. It chooses the earliest usable source
timestamp in the original 30-second window. The quote must also have been received
inside that window and fresh/valid when received. Event/payload timestamp mismatch,
future-at-receipt timestamps, stale quotes and invalid bid/ask sizes are excluded.
Source time, receipt time and recovery time are kept separately. Missing archives
stay explicitly missing and do not become losses. The dashboard labels retained
versus live checkpoint evidence and shows missing-window reasons.

Queries are restricted to each checkpoint window, capped at 1,200 rows, using the
existing symbol/time index. A cap filled with invalid rows is marked coverage
unknown. Recovery does not replay the entire excursion path: MFE/MAE remain based
on engine samples, as before. Checkpoint midpoint changes remain before costs and
are not stock fills or option profit.

Frozen decisions, original alerts, research entry limits and completed historical
measurements are unchanged. Tracking measurements may recover any still-pending
window with genuine retained evidence. Historical TGT/SBUX quotes that were never
stored cannot be reconstructed by this patch.

Deployment verification: confirm collector, engine and dashboard run the revision;
inspect the archive health row during stock trading; verify advancing TGT/SBUX
journal timestamps and a secondary checkpoint labeled `retained_quote`. Offline
tests alone do not establish production coverage. Full-watchlist history increases
database storage at the existing sampling rates; no deletion or retention policy
is introduced.
