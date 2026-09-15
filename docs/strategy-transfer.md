# Transfer to Compass — phase 1

Compass owns a shared, versioned source ledger and independently measures Morning
Algo's 5/15/30/60-minute underlying checkpoints. This is a shadow monitor, not yet
a replacement signal generator, option selector, official notification service,
or order executor. Original services remain authoritative.

`strategy_tracking_v1` keys each record by project and original source ID, freezes
its first registered payload, and keeps its latest source payload. Full source
revisions are appended idempotently as `strategy_revision` events. Stream,
script version, source settings and original identities remain available. No
previously discarded revision is reconstructed. Historical registration is
explicit and does not count as forward acceptance.

The engine runs the monitor every five seconds under a database lease. Existing
source records are registered in batches of 200. Live imports register on change.
Historical bootstrap cannot overwrite a concurrent newer import. A checkpoint
uses the first valid retained underlying quote from due time through due+5s,
recorded by due+10s and fresh when recorded. After due+10s it finalizes observed
or missing. Restart replay uses this fixed cutoff; late backfills cannot repair
it. Registration after a checkpoint means not observed, even if history exists.
These are endpoint measurements, not continuous-path/peak/stop observations.

Source entry to Compass midpoint returns are compared with source candle-close
returns only when source coverage is continuous and timestamp is within five
seconds of the scheduled checkpoint. Price-basis differences are displayed in
percentage points, not labeled strategy failures or win rates. Missing source
points, missing Compass quotes, source timing/coverage mismatch, and changes to
entry/side/version/stream/settings remain explicit. Source updates may complete
a comparison without changing the frozen Compass measurement.

The authenticated Connected Projects page exposes status and latest 30 Morning
comparisons; its existing API snapshot includes latest 100. Counts cover all
registered sources. A stale worker timestamp is not evidence of an active worker.
No cutoff, win rate, or replacement-readiness claim is inferred from this panel.

## Next migration stages

1. Observe the first forward Morning cohort and reconcile alert IDs and checkpoint
   gaps. This feed cannot discover alerts never recorded by the original source.
2. Add direct, authenticated Morning event intake and candle-history validation;
   compare it with source imports before changing TradingView destinations.
3. Port original option selection and sampling policies (same-day expiries remain
   excluded), source-bar research, report/export and notification outbox; test
   restart behavior and delivery ownership. This phase does not implement them.
4. Switch Morning official notifications only after complete reconciliation and
   a reversible cutover. Port Smoothers weekly/holiday scheduling and one-minute
   exits next, verifying a full weekly cycle.
5. Futures execution comes last: actual broker fills/positions, idempotent orders,
   risk limits, kill switch, recovery, paper verification, then authorized live
   cutover. Keep TradingView/TradersPost routes unchanged until then.

Rollback this phase by reverting its commit; retain the additive ledger/history.
It creates no orders or notifications. All future official senders must have
single ownership per strategy; running the shadow does not transfer ownership.
