# Observation gaps — root cause and the fix (for Quill to implement)

Claude, 2026-10-26 16:10 CT. Josh asked why the options books still produce `observation_gap` /
`data_gap` exits after the 10-19 pinning and 10-20 tolerance changes. Evidence from main @ 4d0a95e.

## Why they persist
`observation_gap` is not a feed outage. It is the measurement rule — a **continuous paired quote path**
(option AND underlying) from entry to exit — applied to contracts that cannot satisfy it.

1. **The rule.** `option_ideas.py:268-283` (`observe_pair`): a hole > `opt_tol`/`und_tol` on either leg →
   `finish(..., 'observation_gap')`, trade dead, pnl None. Tolerance = 3 × the contract's observed print
   cadence, floor 15 s, cap 300 s (`gap_tolerance.py:21-24`). 0DTE: exit mark older than 120 s → `data_gap`
   (`zero_dte_paper.py:308`); no quote ≤120 s at booking → silently never booked (`:249-251`).
2. **The data cannot meet it on single names.** `gap_tolerance.py` docstring: off-core contracts go
   30–120 s between prints in a healthy market (BABA 110C 2026-10-20: quoted at entry, gap-exited at
   +3 min). SPY/QQQ print continuously. The rule was calibrated on index options and runs on a
   160-name universe.
3. **Supply is capped.** One 1 Hz stream (`option_stream.py:98`) on ≤400 contracts since 10-22
   (`MASSIVE_OPTION_STREAM_CONTRACTS`), 16 focus names (`config.py` option_focus). Open-position
   contracts are pinned (`zero_dte_paper.py:430`) but a contract rotated out before pinning, or beyond
   the cap, has no quotes at all.
4. **Late-stored quotes are disqualified.** `quote_path.recorded_path` (`quote_path.py:21-30`) accepts a
   row only if `payload.ts == row.ts`, `row.ts <= row.received`, and `fresh(q, row.received)`; anything
   written late is `stale_or_invalid_when_recorded` / `late_storage` and removed from the path. Under DB
   load (every incident week since 10-06) quotes arrive on time, are persisted seconds late, and are then
   rejected → a hole that did not exist on the wire.
5. **Batch drops are silent to the path.** `persist_batch` (`option_stream.py:307-312`,
   `alpaca_option_stream.py:373-393`) drops whole batches after retries and only increments a counter
   inside the health payload; the stream stays `receiving`. Those minutes are holes.

Net: gaps rise with DB load and with the share of non-index names, and the book answers each hole by
killing the trade. Raising tolerance moved the threshold; it did not change what a hole means.

## The fix (ranked; 1 is the real one)
1. **A hole pauses measurement; it does not terminate the trade.** Same semantics the swing book adopted
   for a missing daily snapshot. Implementation: in `observe_pair`, on a hole set `measurement_paused_from`
   and keep the position open; resume on the next paired quote; record `gap_seconds_total` on the trade;
   mark `data_gap=True` only if the hole spans the exit event itself (stop/target/flatten crossed with no
   quote inside ±tol). Exclusion then applies to the *exit*, not to the trade's existence.
2. **Exit on the underlying; price the option by model between prints.** Underlying quotes are 4 Hz on
   core names and never gap the way options do. Stop/target/flatten evaluated on the underlying path;
   option P&L marked with `smoothers_blackscholes` from the underlying + last observed IV, corrected to the
   option print whenever one arrives within tol. This removes the paired-continuity requirement entirely.
3. **Accept late-stored quotes on the recorded path.** In `recorded_path`, treat `socket_read_at` (already
   on the payload, `quote_path.py:29`) as the receipt time; require `fresh(q, socket_read_at)` not
   `fresh(q, received)`. The `late_storage` bucket becomes usable rows. One-line change; add a test with a
   row received 8 s after its socket read.
4. **Treat persist drops as a health failure.** `dropped_batches > 0` in any 5-minute window →
   `health:<stream>` status `degraded` with its own row (stock_stream already does this,
   `stock_stream.py:127-136`); surface it in the exclusion stats so a day with drops is labeled.
5. **If the book stays option-quote-based, restrict it to SPY/QQQ (and at most the 3 seated single names)
   and subscribe their chains ±5 strikes continuously** — the only configuration where the current rule is
   satisfiable. Everything else shadow-only on the underlying (which is where the signal should be scored
   anyway; see 2026-10-26-what-must-change-all-books.md §3–4).

## Smallest tests that prove it
- option_ideas: open trade, 90 s hole in option quotes, then a target print → under (1) the trade closes
  `target`, not `observation_gap`; `gap_seconds_total == 90`.
- quote_path: a quote with `socket_read_at = ts + 1`, `received = ts + 9` → counted usable under (3), rejected today.
- zero_dte_paper: open trade, no option quote for 200 s, underlying crosses stop → under (2) closes `stop` with
  modeled premium and `exit_model='bs'`; today: waits, then `data_gap` at flatten+900.
- Metric to publish beside every options stat: `excluded / (closed + excluded)`; the book is unmeasured
  when it exceeds 10 %.

## What this does not fix
The signal. Pausing instead of terminating makes the books *measurable*; it does not make them profitable.
The underlying-first validation (Task 003 §4, 2-year 1-min pull) is still the gate.
