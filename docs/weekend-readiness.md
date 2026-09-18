# Operational readiness changes — September 18, 2026

Research report aggregation previously ran inside the engine transaction and held its lease. Reports now have their own worker, transaction, lease and freshness health. A report failure cannot roll back completed engine scans. Engine exceptions are logged and a failed health write no longer kills the loop.

Each application PostgreSQL connection uses a 30-second statement timeout, 5-second lock timeout, 30-second idle transaction timeout and, on PostgreSQL 17+, 60-second transaction timeout. Existing URL options (including schema selection) are retained. TCP keepalives and a 30-second TCP user timeout bound broken connections. Shutdown cancels this process's active queries when secure cancellation is supported, drains returned connections and always disposes the pool. Server timeouts also cover abandoned transactions.

Futures bursts lock a contract once and update its latest quote once, while archiving every accepted sample in source order. A 64-sample single-contract burst uses four statements rather than 65. Late quotes cannot regress latest state; duplicate archival keys remain idempotent. This reduces persistence work but does not prove that all infrastructure latency is eliminated.

The separate `provider-minute-inventory-v1` diagnostic compares an explicitly bounded SPY provider response with retained minute timestamps. Complete pagination is required to label a minute absent from the provider. Source omissions and missing collector bars are separate lists. It runs once per minute during the morning window and is exposed as `provider_coverage_comparison` on `/api/spy-morning-brief`. It does not supply synthetic candles, change the 90% entry gate, change Discord plans or rewrite frozen studies.

Run `python -m scripts.smoothers_preflight` with the existing database/provider environment for a read-only next-week check of every enabled configuration, calendar, hourly warmup, daily history and both option directions. It creates no weekly signals or deliveries. Monday's first completed hour and full native weekly acceptance remain live gates. Original Morning and Smoothers sender ownership stays in place.

Validation includes PostgreSQL contention, timeout rollback and subsequent lease acquisition, cancellation/draining, report/engine lease independence, futures archival preservation and source-inventory classification. Provider omissions do not prove odd-lot causation; late historical quotes never repair a missing live path.

References: [PostgreSQL client timeouts](https://www.postgresql.org/docs/18/runtime-config-client.html), [libpq connection settings](https://www.postgresql.org/docs/18/libpq-connect.html), [psycopg cancellation](https://www.psycopg.org/psycopg3/docs/api/connections.html).
