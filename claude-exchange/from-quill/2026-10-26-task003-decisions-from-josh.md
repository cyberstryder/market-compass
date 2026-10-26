# Task 003 — Josh's decisions on the three open items (2026-10-26)

**Recorded by Quill, 2026-10-26.** Josh reviewed Quill's verification of
Claude's Task 003 list (`from-quill/2026-10-26-task003-verification-and-backlog.md`)
and approved all three decision items **as recommended**:

## Item 1 — Bracket payload (live money): APPROVED, gated on docs support

- **Build relative stop/target brackets IF TradersPost supports them**
  (stop/target expressed as offsets from the eventual fill price instead of
  absolute prices). The docs check is in flight (Quill) — verdict + build
  design land in the goal's hidden files and are summarized back to the desk
  when complete.
- The measurement half ships **regardless** of the docs outcome:
  `order_sent_at` / `fill_at` logging ships with the build, so entry latency
  (today UNVERIFIED as the cause of the fill divergence — the CSV measures
  price difference, not latency) becomes provable from live records.
- No payload change ships before the docs verdict. If relative brackets are
  not supported, the closest supported alternative gets designed and returns
  to Josh before any live-money change.

## Item 2 — Futures promotion policy: ADOPTED

- A futures combo earns (or re-earns) a live seat only with **≥50 Tier-1
  (broker-fill) observations** AND an edge that **survives repricing at p90
  stop slippage**, not just the median.
- This is policy, effective now; it governs future promotions and any
  re-earning of a seat. (Current live seats stand under the kill-watch Josh
  set 2026-10-25; nothing in this adoption unseats them by itself.)

## Item 5 — Swing gap-day stop semantics: CHANGED

- On a gap day, the swing stop now evaluates at the **first available
  snapshot after the gap day**, instead of deferring to the next close.
- This amends the semantics adopted 2026-10-25 with the swings gate surgery;
  the build rides with the item 4 fix (same file, same build).

## Builds launched under the merged backlog (status at decision time)

- **Item 3** (MU scalp phantom keyspace) — build launched.
- **Items 5 (+4)** (swing gap-day stop semantics + close-snapshot window) —
  build launched.
- **Items 6/7/8 bundle** (iceberg flag misread + buffer bound, /api/simple
  scans, seat/shadow denominator bias) — build launched.
- **Item 1 measurement + payload** — docs check in flight (above); build
  follows the verdict.

Desk status: Task 003's decision queue is now EMPTY. Open desk work after
these builds: item 9's boundary-aligned re-run (owed by Quill) and item 10's
Tier-1 re-fit logging (endorsed, backlog).
