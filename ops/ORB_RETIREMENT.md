# ORB retirement — 2026-09-26

Production sets `MORNING_ORB_ENABLED=false` and `ORB_SETUPS_ENABLED=false` on dashboard, collector and engine.

The first switch stops Morning source polling, history backfill, native intake and sampling, native deliveries, recurring Morning reports/parity calculations, new secondary reviews and Morning-only data demand. The historical report endpoints and stored records remain available. Smoothers processing continues. Pending Morning native outbox rows are excluded before the queue limit so they cannot starve Smoothers.

The second switch stops new legacy ORB candidates, scanner ORB retests and extended-session futures ORB retests. Other scanner patterns and shared market data remain active. Existing recorded positions and outcome observations can finish.

The standalone Morning Algo Tracker application and its Postgres deployment are stopped; the database volume and source code are retained. Standalone Discord and options processing switches are false. The GitHub source is disconnected to prevent automatic redeployment on future pushes. Storage charges can continue while retained. TradingView alerts are separate and have not been changed.

Rollback requires explicitly restarting the standalone database/application if needed, and setting the relevant Compass switches true with redeployment. Defaults remain true for existing installations and replay tests; production false values must be retained on each service.
