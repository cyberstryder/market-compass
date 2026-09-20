# SPY daily-plan v3: provider-verified premarket range

Effective prospectively after deployment on 2026-09-20. This is an explicit alternative to the daily plan's original 90% clock-minute coverage rule, not a reduced minute threshold. Frozen timeframe-comparison research continues to call the original strict bar_context and retains its original protocol.

The September 18 opening report observed 296/330 minutes, one below its 297 minimum. A September 20 read-only SIP query returned the same 296 bars and range (759.60–764.16). A separate 5Min query returned all 66 premarket intervals and the same overall range. These later observations do not establish what was available in real time or reconstruct a missed trade.

During the morning window the existing inventory collector validates and recovers actual one-minute bars. After the cash open it also requests actual five-minute SIP bars for 04:00–09:30 New York. The daily plan can use an alternate coverage basis only when:

- Both responses are unpaginated, timestamp-aligned and contain valid, finite OHLCV.
- All 66 five-minute intervals exist.
- Each five-minute high and low matches the corresponding observed one-minute bucket.
- The proof is from the current session, received no more than 120 seconds ago, and the stored minute timestamps/highs/lows still exactly match its evidence.

No missing minutes are interpolated. Preopen checks keep the original rule. Changed frozen ranges, missing RTH minutes, 15-minute confirmation, price freshness, options eligibility and risk rules remain enforced. Existing reports are never rewritten. New plan paper observations record report_version and premarket_coverage_basis; outcome groups expose both dimensions so the changed eligibility can be evaluated separately.

If provider responses are partial, invalid, stale, or disagree, the alternate path stays blocked. Source clock coverage counts remain visible in the report, along with the actual eligibility basis and verification details. The range verification uses two aggregations from the same SIP provider; it is not independent vendor confirmation or evidence of predictive accuracy.

Tests cover sparse minutes, complete five-minute verification, missing buckets, mismatched ranges, pagination, invalid OHLC/timestamps, stale/future/wrong-session evidence, idempotent real-bar recovery and preservation of the strict research gate. The first live session remains the end-to-end operational check.
