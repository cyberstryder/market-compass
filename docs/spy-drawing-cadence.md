# SPY drawing-message cadence

Send one drawing companion with the first usable daily plan, normally premarket. Routine status checks continue without the drawing companion or the repeated chart-copy table. Their heading no longer promises a second message.

Compare later snapshots against the last drawing queued in the durable outbox, not the immediately preceding status report, so small changes can accumulate into a material update. The threshold is max($1 SPY, 15% of the daily ATR in that drawing). SPX/XSP movement is converted to SPY-equivalent units. Common anchor levels and VWAP are compared; routine confirmation-close additions and exposure rank reshuffles alone do not trigger a drawing. Material exposure-level moves, newly available entry stop/target levels, and newly available SPX/XSP estimates qualify for an update.

The daily baseline and optional chart event are persisted atomically with the regular plan. Restart and retry deduplication remain intact. The first run after upgrade honors an existing same-day chart event. A new session receives a new initial drawing. No historical alert is resent, and no entry, risk, delivery-receipt or research rule changes.

Validation covers one chart across preopen plus all four routine checks, preserved regular plans, cumulative changes from the drawing baseline, exact/scaled thresholds, estimate conversion, rank-only changes, new entry-risk levels, same-day upgrade recovery and next-day reset. Existing delivery tests retain independent retry and acknowledgement of a required chart message.
