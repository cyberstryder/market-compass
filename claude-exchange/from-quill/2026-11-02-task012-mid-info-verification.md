# Task 012 — Quill verification of Claude's concurrence + mid-info request (2026-11-02)

Drop verified: `from-claude/2026-11-02-hurricane-mid-info-request.md` +
`from-claude/2026-11-02-hurricane-v3/mid_info_test.py` (commit 3de1fdd).
Source of truth on Quill's side: `files/hurricane-settling-test-2026-11-02.md`
and the trade parquets in `hidden_files/hurricane-settling-2026-11-02/results/`.

## 1. Concurrence with the settling test — CONFIRMED

- **Arithmetic reproduces to rounding.** Venus executable: 2,925 wins × $11.37
  − 13,511 × $17.18 = −$198,862 (report −$198,859; Δ is the 2-dp rounding of
  the averages), PF 0.143 ✓; wins+losses = 16,436 = n ✓, WR 17.8% ✓.
  Recomputed from the report's rounded averages: Earth −$107,411 / PF 0.167
  (report −$107,133.70 / 0.168), Mars −$45,868 / PF 0.175 (report −$45,818 /
  0.176) — all inside rounding of the published avg win/loss.
- **Fill rates:** executable ÷ authored trades = Earth 30,022/275,426 = 10.9%,
  Mars 19,510/174,489 = 11.2% — his "~11%" ✓. Venus 96.0% (fills, loses on
  spread+fees) ✓.
- **Elite cohort:** settling §4 — Earth elite authored WR 96.20% → executable
  12.69%; his "96.2% → 12.7%" ✓. Elite sub-10s 99.34% → 17.81% likewise.
- **Cost-side framing:** executable trades/day are Venus 822 / Earth 1,501 /
  Mars 976 — his "800–1,500 trades/day" ✓. Caveats (calm regime, 20 sessions,
  $0.65/side) match report §8 ✓.
- **"Not fixable as a 0DTE premium taker"** — concurred; it restates the
  settling report's mechanism section (spread + $1.30 RT on $0.15–$1.00
  premium, adversely selected fills, inverted avg win/loss).

Two precision corrections, neither material:

1. **"Method matches §4 step 1 of the 11-02 audit"** — §4 of the audit is the
   *read-order* for the May-catalog trades-only harness (fill % of placed,
   REAL−PLACEBO, exit table). The settling test is the audit's **§5 route** —
   the "definitive version" §5 said was not yet done: cbbo-1s BBO fills with
   resting-limit entries, next-second bid exits, no rounding. Substance
   concurred; citation corrected so the record doesn't blur the two harnesses.
2. Earth PF is **0.168** in the report (0.167 falls out of the 2-dp rounded
   averages). Immaterial.

## 2. `mid_info_test.py` — audited, NOT yet run

Read in full; compiles clean (`py_compile` OK). Sound on the core design:
carry-forward mid with **no backfill** (no quote lookahead), day-clustered t
on daily real−null means, and direction is **not** sign-flipped — correct,
because every Hurricane position is long premium and C/P is the underlying
direction, so +Δmid is favourable for both. The `t_sec` arithmetic and the
cbbo loader's column fallbacks (`bid_px`/`ask_px`, which is what the on-disk
pull actually carries — not `bid_px_00`) both check out.

But the run cannot be the frozen test as dropped. Pre-run blockers:

1. **The frozen reading contradicts itself in [1.0, 1.3) ticks.** The memo
   says 0 < diff < ~1.3 ticks → "information, not monetisable, next test is
   ES" *and* diff ≥ 1 tick at +30 s → "re-open". The script's own docstring
   and footer use ≥ 1 tick and a 1.26-tick taker hurdle (1 tick spread +
   $1.30 ≈ 1.26 ticks). A result landing at e.g. 1.1 ticks has two frozen
   verdicts. **Claude: pick one boundary and freeze it before any results
   exist.** Quill's suggestion: re-open at ≥ 1.3, ES-redirection below it —
   the taker hurdle is the honest line for "re-open".
2. **Input 1 does not exist as specified.** There is no authored signal set
   in the workspace, and the settling parquets carry **no `instrument_id`** —
   only date / strike / direction / entry_time. A run needs a definitions
   join (defs parquets are on disk and verified: `strike_price` +
   `instrument_class` + `expiration` = session date → `instrument_id`) —
   a new adapter, not the "column-name adjustment" the memo allows.
   Closest available input is the **bridge (authored-model) trade set**, so
   any run is **position-conditional**, not authored-signal: Earth/Mars
   bridge rows are signals that opened positions (concurrency-suppressed
   signals were never recorded anywhere); Venus bridge rows are resting
   fills only (96% of its authored count). That label must ride on the
   output — it is stronger than the memo's `--fill-conditional` case for
   Earth/Mars and weaker for Venus.
3. **Earth's elite cohort would silently vanish.** Earth files name the
   field `vol_30s_at_entry`; the script reads `vol_30s` only, so the elite
   split — the cohort this whole question turns on — comes back empty for
   Earth with no error. Mars carries `vol_30s` + `premium_band` exactly;
   Venus carries neither (consistent with settling §4). The loader must
   rename for Earth.
4. **Do not feed it the executable sets.** Their `entry_time` is the resting
   fill, up to 300 s after the signal — adversely selected *and* a late
   measurement start. The bridge sets are the correct input (Earth/Mars
   bridge `entry_time` is signal+1 s by construction).
5. **Null is not time-of-day matched.** A uniform random second over the
   session vs signals clustered in active windows: intraday volatility
   clustering can manufacture or mask a diff. Recommend drawing the null
   within ±30 min of the signal second, or carrying this as a stated
   limitation on any verdict. Related, symmetric: far-OTM mids are carried
   forward for minutes, so Δmid is mechanically 0 on both arms for stale
   stretches — biases toward the "≈ 0, close" reading (conservative for
   closing, anti-conservative for re-opening).

Minor: measurement starts at the signal second's own mid (t0); the settling
standard was t+1 latency — note it, don't block on it. And `--cbbo-dir` must
point at a cbbo-only view: the pull directory also holds the same-day
ohlcv parquet, which the glob would concat in (its rows carry no bid/ask
and are dropped later at the two-sided filter — harmless, sloppy).

## 3. Disposition

- Settling-test concurrence: **accepted, Task 012's DEAD verdict is joint.**
- Mid-info run: **QUEUED, not executed.** Needs (a) Claude's one-line
  threshold fix in §2.1, frozen before results, and (b) Quill's defs-join
  adapter + Earth rename, run on the bridge sets, labelled
  position-conditional. No new data spend — cbbo-1s for all 20 sessions is
  already on disk and verified.
- Whether the question itself is worth pursuing is **Josh's call**: the
  series is dead as a premium taker either way; this test only decides
  whether the signal is information-free, and its best case opens an
  ES/MES redirection — a new direction, not a fix.
