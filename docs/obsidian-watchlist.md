# Obsidian Watchlist

The operator's private feed is configured only on the collector as
`OBSIDIAN_FEED_URL`. Never put its value in source, logs, screenshots or exports.
The connector permits only the supplied provider's HTTPS feed route, disables
redirects, bounds response size, and reports sanitized error types.

The documented `/recent?since=` endpoint is polled every five seconds after each
request completes. Cursor progress commits atomically with retained events. A
restart replays safely; upstream replay retention/completeness is not guaranteed.
Malformed identified events enter quarantine. A malformed page or cursor does
not advance recovery. Events are immutable by feed/event ID.

Each new event creates a watchlist idea, not a provider trade entry or order.
Updates carry provider-reported percentages separately. The guide supplies no
stable parent-idea ID, so updates matching multiple additions of one contract are
marked ambiguous; unmatched updates are retained without inventing a parent.

Active option contracts join the existing bounded Massive subscription queue,
after existing study requests. Their equity symbols join price/history collection
without expanding scanner entry rules. Index option roots are not sent as equity
symbols. Quotes remain subject to current subscription capacity, entitlement and
market hours. No provider quote or fill is invented.

For an idea received within 60 seconds of its source time, option observation
waits for the first fresh independent ask after receipt, including the next market
open if necessary. That observed baseline and its delay are stored explicitly.
Subsequent retained bids show a hypothetical long-option liquidation change.
Both calls and puts use long-option economics. Source-time gaps remain explicit;
sampled extrema are not guaranteed continuous-path maxima. Late historical imports
have no reconstructed baseline or original performance claim.

New timely ideas receive the existing secondary underlying-direction review and
15/30/60-minute measurements, including with/without TraderMatrix comparisons.
Call/put supplies the directional hypothesis; it is not proof of the author's
trade intent. This is an intraday context comparison, not a swing or LEAPS
valuation model. Historical imports do not receive retrospective reviews.
Obsidian adds no Discord notifications or broker orders.

The Obsidian Watchlist dashboard shows the latest 100 ideas/events; all received
records are retained. Tracking and subscription candidates are bounded to the
latest 500 unexpired ideas, with truncation exposed in tracking status.
