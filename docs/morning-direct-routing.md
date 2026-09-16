# Morning direct shadow routing

Compass accepts the original Morning payloads at `/hooks/native/morning` with
Bearer authorization, or `/hooks/native/morning/{token}` for TradingView.
Set a dedicated, random `NATIVE_MORNING_INTAKE_TOKEN` on the public dashboard
service. The source comparison feed continues using `MORNING_INTEGRATION_TOKEN`.
The dedicated credential replaces the legacy credential for intake only.
Neither credential appears in routing responses or this runbook.

Clone the active Morning TradingView alert so its saved Pine snapshot and all
inputs match. Keep the original alert and its webhook active. Change only the
clone's name and webhook destination to Compass; enable webhook delivery and
disable app, toast, sound and email notifications on the clone. Preserve the
Stock List universe, one-minute interval, Regular session, Any alert() function
call condition and open-ended expiration. Do not substitute the current chart's
script or reconstruct its settings from defaults.

After confirming both alerts are active, use Connected projects → Morning
routing preparation to prepare and schedule `direct_shadow` for the next actual
stock session. The schedule stops Compass's source-summary relay for that
session and later sessions, preventing that relay from creating an entry before
its direct packet. The original Morning application remains the official sender.
Native Discord sending remains disabled.

At the opening, compare direct entry receipts, expected previews, source
deliveries and option choices. Retain the alert through all later frames and
research follow-ups. After collection finishes, compare candle inventories and
scheduled option observations. An active alert plus an HTTP credential check
does not establish successful live TradingView delivery; that requires actual
session receipts. Never inject synthetic live entries to pass the comparison.

## Reversal

- Before the effective session: cancel future routing changes in Compass, then
  pause only the Compass shadow alert. The original alert remains active.
- After the effective session starts: preserve the current session's provenance;
  prepare and schedule `relay_shadow` for the next actual stock session and pause
  only the Compass copy before that session begins. Any current-session gap stays
  visible. Do not silently mix routes mid-session.
- If a webhook credential is exposed, replace the dedicated dashboard variable
  and the Compass copy's URL together. This does not rotate the source-feed token
  or change the original TradingView webhook.

Official sender handoff and application retirement remain separately gated by
completed forward evidence. No handoff is activated by these routing steps.
