# Weekday candidates v1

Discovery now includes a vendor watchlist and a prospective price-trigger board.
Runs on all regular equity sessions, including Tuesday–Friday. Monday Smoothers
is separate. No new notification, order, or entry rule is introduced.

The board captures the first observed stock setup per ticker/direction/rule per
session from Engine.observe_setup. It freezes available TraderMatrix context
at capture; later corrections do not overwrite it. It links the existing setup
trial and independent option idea by exact IDs. Failures, losses, exclusions and
missing observations remain visible. Option simulations use the existing quote,
spread, fee, slippage and continuity requirements. Their underlying and option
P&L are distinct, and overlapping observations are not a portfolio.

Source/receipt clocks after capture are ineligible. A current symbol match
requires both clocks within 30 minutes and no vendor-stale flag. This is a fixed
research grouping, not a directional confirmation or calibrated success score.
Unknown and delayed context stays visible. Sector context is not asserted to
support a particular ticker without a verified mapping. Earnings and events are
shown as attributed partial vendor evidence; this version does not parse an
unverified date schema into an automatic earnings blackout.

The vendor watchlist shows received accumulation, momentum, pullback and squeeze
rows (100-row display cap, explicit total/truncation). Vendor direction remains
unconfirmed until an existing price setup triggers. New symbols outside the
configured scanner universe are not automatically subscribed or admitted.

The triggered board displays the latest 100 records with a separate 30-day
count. Context is compacted for display/storage, with original key and received
clock; full provider responses remain in the existing archive. It is not a full
historical endpoint reconstruction. Existing intraday rules provide the first
tracked cohort; multi-session discovery/swing studies remain separate, and
new accumulation-driven swing rules are not claimed by this version.

No retrospective candidate creation. The first prospective cohort starts on the
next market session after deployment. Access through Discovery in the dashboard.
