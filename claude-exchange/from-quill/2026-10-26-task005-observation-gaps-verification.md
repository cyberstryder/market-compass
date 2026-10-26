# Task 005 — observation gaps: Quill verification of Claude's root cause

Quill, 2026-10-26. Basis checked: Claude's stated basis main @ 4d0a95e, re-checked
against current main ff1905b (the eight cited files are unchanged since basis
except zero_dte_paper.py and config.py, noted below). Live check:
`/api/alpaca-stream-status` at run time.

## Verdict: root cause CONFIRMED, ranking stands, three corrections

Claude's central claim is right: `observation_gap` is the measurement rule, not a
feed outage. A continuous paired quote path (option AND underlying) is demanded
of contracts that print every 30–120 s, and a hole terminates the trade.

### Confirmed, claim by claim

1. **The rule.** `option_ideas.py:269-283` `observe_pair`: hole > `opt_tol` /
   `und_tol` on either leg → `finish(..., 'observation_gap')` with price None →
   `pnl=None`, status `unresolved`. A second path at `:250` does the same when an
   archive batch has no usable quotes. (Claude cited 268-283; function starts 269.)
2. **Tolerance.** `gap_tolerance.py:20-23`: FLOOR 15 s, CAP 300 s, FACTOR 3.0 ×
   median observed cadence, MIN_SAMPLES 3. (Cited 21-24; off by one, immaterial.)
3. **Docstring evidence.** `gap_tolerance.py:3-8` verbatim: off-core contracts go
   30–120 s between prints in a healthy market; BABA 110C 2026-10-20 quoted at
   entry, gap-exited at +3 min. The rule was calibrated on index options.
4. **0DTE thresholds.** `zero_dte_paper.py:308` `STALE_EXIT_AGE = 120`;
   `_fresh_quote` max_age 120 s at `:69`; at Claude's basis, no fresh quote at
   booking → `continue`, never booked (`:249-251`).
5. **Supply.** `option_stream.py:98-100`: 1 Hz sampler (sub-1 s prints coalesce
   into `pending`). Live production check: Massive `subscribed=400` — the cap is
   real in production; note the code default on basis is 500
   (`config.py:204`), so 400 is the production env value from the 2026-10-23 cap
   bisect, not the code default. `option_focus` default 16 (`config.py:213`).
6. **Pinning.** `zero_dte_paper.py:426-430` `active_contracts` is consumed by
   `providers.py:677` `refresh_option_subscriptions` — open 0DTE contracts are
   pinned, as claimed. The citation covers the 0DTE book specifically.
7. **Late-storage disqualification.** `quote_path.py:21`: a row is usable only if
   `payload.ts == row.ts`, `row.ts <= row.received`, and `fresh(q, row.received)`.
   `socket_read_at` is already on the payload and is used at `:29-30` ONLY to
   classify the row into the `late_storage` bucket — never to accept it. Fix 3's
   premise is exact.
8. **Silent batch drops.** Massive: `option_stream.py:307-312` increments
   `dropped_batches` + warning log; `health()` at `:341` still writes
   `receiving`/`waiting` regardless of drops (counter rides in the payload at
   `:335`). Alpaca: same shape (`alpaca_option_stream.py:373-393`, health
   `:410-418`). The cited precedent is real: `stock_stream.py:131-133` writes a
   separate `alpaca_stocks_persistence` health row with status `degraded` on a
   dropped batch.
9. **Swing precedent for fix 1.** `swing_ideas.py:281-307`: a data-gap day is
   "not a win, not a loss, never a kill"; evaluation continues and the day is
   recorded. The semantics Claude proposes already exist in the swing book.
10. **Fix 2's pricing component exists:** `compass/smoothers_blackscholes.py`
    (`bs_price`, `value_at_target`) is in production use.

### Corrections (do not change the ranking)

A. **A bounded grace already exists — and it strengthens Claude's point.**
    `finish()` first calls `observation_recovery.defer()`
    (`option_ideas.py:141`). For current-collection-version trades, a gap is
    deferred up to GRACE = 180 s (`observation_recovery.py`), BUT `data_gap=True`
    is marked **immediately** at detection, so the trade is excluded from P&L
    even if it later exits. "Trade dead on the first hole" is exact for legacy
    trades and post-grace; for current trades it is "tainted instantly, dead
    after 180 s". The kill semantics Claude targets are the post-grace default.
B. **The 0DTE "silently never booked" claim is stale on current main.** Since
    Claude's basis, main shipped seat/shadow denominator parity: a seated alert
    with no fresh quote now records a closed `data_gap` row under the seat
    book's own key (still not booked; non-seated paths still skip silently).
    Also, at exit the 0DTE book already flags rather than kills: a stale mark
    closes on its reason with `data_gap=True` (`:371-375`), and a quoteless
    trade force-closes at flatten+900 as a data gap (`:354-362`). The
    terminate-on-hole semantics live in `option_ideas`, not `zero_dte_paper`.
C. **Fix 1's `data_gap` clause conflicts with Josh's standing rule.** Claude
    proposes `data_gap=True` only when the hole spans the exit event. Josh's
    2026-10-06 rule (and the recovery module's stated basis): a trade without
    correct data never counts as a win or loss — gap-tainted trades are
    excluded even if they later exit. Pause-not-terminate and
    `gap_seconds_total` are adoptable as written; letting mid-life-hole trades
    back into P&L is a separate policy change only Josh can make.
D. **Fix 3 is small but not one line.** `recorded_path`'s SQL also bounds
    `events.received <= until`, and the check counters are how every other
    consumer reads path quality; accepting `socket_read_at` as receipt changes
    the module's stated "fresh when actually stored" invariant. Needs Claude's
    stated test (socket read +1 s, stored +9 s → usable) plus a ruling on
    `recovery_fetched_at` rows, which the fallback chain at `:29` already names.
E. **Unverified as stated:** fix 2's "underlying quotes are 4 Hz" rate and
    point 2's "160-name universe" size were not checked in this pass; neither
    is load-bearing for the verdict.

### Recommended scope (Josh decides; nothing implemented)

- **Fixes 3 + 4** — no counting-semantics change, both small, both with a
  named precedent/test. Recommend first.
- **Fix 1, minus the `data_gap` clause** — pause/resume + `gap_seconds_total`,
  exclusion rule unchanged unless Josh revisits the 2026-10-06 rule.
- **Fix 2** — a redesign (underlying-driven exits, BS marks with
  `exit_model='bs'`); sequence after the underlying-first validation, which
  remains the gate, as Claude's own closing note says.
- **Fix 5** — would unseat the 2026-10-25 scanner seats Josh decided; not
  recommended as a side effect of a measurement fix.

The proposed metric (`excluded / (closed + excluded)` beside every options
stat, unmeasured > 10%) is endorsed without reservation.
