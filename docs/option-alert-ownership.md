# Consistent option alert ownership

This changes publication, not strategy selection, exit rules or research admission.

| Category | Owner rule |
| --- | --- |
| SPY 0DTE | SPY options expiring the entry session; existing SPY plans/drawings remain context |
| Options 0DTE | Other same-session expirations |
| Options Intraday | Later expiration, intraday holding plan |
| Options Swing | Multi-session holding plan |
| Options LEAPS | Explicit LEAPS plan or swing contract with at least 365 calendar days remaining |
| Smoothers | Native Smoothers multi-session lifecycle |

The 365-day boundary is an internal routing convention, not a new LEAPS strategy. No LEAPS producer or new trading strategy is introduced. Futures remain independent.

Equity trade entries require an OCC option symbol and a finite two-sided quote with positive bid, ask above bid, source age 0–10 seconds and spread at most 25% of midpoint. Entry events expire after 120 seconds or their earlier producer deadline. Existing producer gates still apply. Incomplete/stale observations remain research with a saved withholding reason, never a passed trade check.

One active idea per underlying, direction, category and holding plan owns the initial trade ID. Other scanner strategies or alternate contracts in that lane attach as supporting research. They cannot close the primary trade or add returns to its published performance. Opposite directions within the same underlying/holding plan are withheld for review with a linked conflict ID. After closure, a new entry may be admitted. Distinct intraday, swing and LEAPS plans remain distinct opportunities.

Primary updates and exits retain the category and trade ID. Repeated unchanged updates are suppressed. Entries are checked again at delivery; updates without a confirmed entry are suppressed. Ambiguous option send outcomes are held for receipt reconciliation instead of blindly resent. A Discord rate limit is retryable. This favors avoiding duplicate actionable alerts over assuming an unknown delivery failed.

Daily results reports unique admission, confirmed delivery, unresolved/unpublished counts and primary modeled P&L for resolved, confirmed-delivered entries. It is not brokerage P&L. Supporting studies remain in the existing research sections. History before activation is not reclassified. Suppression and conflict reasons are counted separately.

Native sender ownership and accepted handoff epochs remain mandatory. This change does not pause or replace external Morning/Smoothers senders. Original external messages are outside this ledger until a separately accepted handoff. Native Morning observations have multiple research horizons but no selected execution exit, so they remain research rather than becoming unmanaged option trades. Native Smoothers uses its existing underlying-target/Friday rule; absent executable option outcome is unresolved, not a win.

No test messages, orders or changes to profitability thresholds are required to validate this change. Configure `DISCORD_OPTIONS_LEAPS_WEBHOOK_URL` and `DISCORD_SMOOTHERS_WEBHOOK_URL` if those Compass routes are used; existing shared fallback behavior remains available. Read route manifests to determine whether each destination is dedicated, shared or awaiting configuration.
