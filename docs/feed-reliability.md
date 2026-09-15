# Feed reliability correction — September 15, 2026

Options previously awaited each trade/quote database write in the socket reader. The replacement separates socket receipt, subscription refresh, quote persistence and trade persistence. Quotes retain the existing one-second sampling policy, original source time, and a separate socket-read timestamp. Bounded queues fail explicitly on overflow. Failed/in-flight batches remain accounted for in shutdown diagnostics; cancelling a worker waits for its current transaction. Pending records at disconnect remain an explicit continuity limitation, not reconstructed observations.

The installed Databento 0.86.0 client defaults to a shared event loop across Live instances. Compass now supplies a dedicated loop per exchange, avoiding cross-exchange blocking. Mapping writes use the existing background writer. Quotes retain callback timestamps; ingress logs add resetting 30-second event-age and callback-duration maxima alongside lifetime peaks. No freshness thresholds, timestamps, entry/exit rules or historical outcomes are relaxed or rewritten.

Validation: 488 tests pass, including slow option-trade writes while quotes continue, failure propagation, overflow, source timestamp preservation, two-loop stall isolation, mapping/entitlement regressions, and recent-window reset behavior.

Production acceptance: verify per-contract option socket receipt and persistence, then compare new complete and unresolved observations; inspect sustained futures recent-window latency and newly unresolved trials. A healthy process alone does not establish feed continuity. Provider silence and upstream delays can still cause honest unresolved outcomes.
