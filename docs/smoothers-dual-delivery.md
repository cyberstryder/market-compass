# Smoothers dual delivery

Compass is the intended replacement for the separate Smoothers service.
Use its existing guarded weekly handoff after reviewing the current evidence.
Deployment alone does not enable a sender or retire the original service.

On the engine, configure `NATIVE_SMOOTHERS_DISCORD_WEBHOOK` for the current
channel and `NATIVE_SMOOTHERS_SHARED_DISCORD_WEBHOOK` for the shared channel.
Set the primary webhook and `NATIVE_PROGRAM_SEND_ENABLED` on the dashboard
as required by the existing handoff control. Do not paste credentials into
reports, issues or commits. Enable sending only at the accepted weekly boundary.

New validated live Smoothers messages get independent primary and shared
delivery records before the primary network request. They have separate retry
state and Discord receipt IDs. Primary delivery failure does not prevent a
shared attempt. A destination receives follow-ups only if its own entry was
confirmed. Rate-limit retries never repeat an already-confirmed primary send.
Unknown network outcomes remain ambiguous and require reconciliation.

Identical webhook destinations are deduplicated. Changing the shared webhook
does not redirect queued historical copies to a new audience. Historical
shadow or delivered records are never copied merely because sharing is enabled.
Rollback cancels pending sends in both destinations and marks in-flight sends
ambiguous. The native report includes a separate `shared_delivery` receipt list.

The private `/api/strategy-readiness` report presents current Smoothers parity
blockers, non-ORB futures observation counts, weekday-board progress, and SPY
plan/companion receipts. It refreshes every fifteen minutes on the engine and
does not inspect protected return cohorts or change strategy qualification.

The expanded weekday board began after the September 25 close, so its first
prospective session is September 28. Weekday notifications remain disabled
until an actual cohort supports qualification. The existing frozen futures
ten-session and SPY twenty-session studies retain their original endpoints.
