# Task 003 — Claude's ranked improvement list (main @ 4d0a95e, 2026-10-26)

Baseline: `COMPASS_LOCAL=true pytest -q` on main → **2076 passed, 76 skipped, 0 failed** (7m31s).
Every item: evidence (file:line or measured number), failure scenario, smallest honest test.
Artifacts beside this file: `2026-10-26-replay_matrix-v2.py` (adds `--stop-fill-ticks`, default 2 =
Task 002 rule), `2026-10-26-matrix-summary-stop2tk.json` (full 53-pair matrix re-ranked under the rule).
Lanes respected: nothing here changes a live book; items 1–2 are inputs to Josh's calls.

---

## A. Live-money correctness

### 1. Entry latency, not slippage, is the live book's execution problem — brackets are priced off the wrong entry
**Evidence (measured from Quill's own `from-quill/2026-10-25-stop-slippage-per-trade.csv`, 45 matched round trips):**
broker entry vs paper entry on the 38 equity-micro trades: mean **+1.8 ticks** (−$0.59/trade — unbiased),
median +2, **p75 +16, p90 +32, worst +50 ticks**; **28 of 38 entries differ from the paper price by more
than 4 ticks**, in both directions (FP-012 +32, FP-020 −43, FP-033 −102, FP-044 +50). MCL shows +105 median,
which is the contract-basis wedge Quill already documented, not execution.
**Why it matters:** the bracket's stop and target are transmitted relative to the *planned* entry
(`traderspost.build_payload`, flat `stop`/`target` prices). When the fill lands 30 ticks away, the stop is
no longer the detector's stop (−30 ticks tighter or wider), and when it lands past the stop the bracket
is rejected and the position is naked — exactly 2026-10-20 07:52 MNQ. Mean-zero slippage hides a
geometry error on roughly three quarters of live entries.
**Fix:** send brackets as offsets from fill (TradersPost supports relative stop/target) or re-issue the
bracket from the fill confirmation; separately, record `order_sent_at` and `fill_at` so send→fill latency
is a measured number. **Test:** unit test that `build_payload` emits stop/target as distances when
`relative=True`; integration: for the next 20 live fills, assert `|broker_stop − (fill − planned_stop_distance)| ≤ 1 tick`.

### 2. Fill-convention sensitivity: half the "winning" pairs are winning only inside a 6-tick window
**Evidence:** full matrix re-run under the shared rule (`2026-10-26-matrix-summary-stop2tk.json`) and a
sensitivity sweep X ∈ {minute-close, 0, 1, 2, 4, 6} ticks:

| pair | stops | minute-close | stop−0 | **stop−2** | stop−4 | stop−6 |
|---|---:|---:|---:|---:|---:|---:|
| aoi_fade·MCL | 31 | +1806/1.34 | +3286/1.87 | **+3162/1.81** | +3038/1.75 | +2914/1.70 |
| aoi_fade·MES | 36 | +1014/1.18 | +2397/1.56 | **+2217/1.50** | +2037/1.44 | +1857/1.39 |
| turtle_soup·MCL | 54 | +6012/1.43 | +9596/1.93 | **+9380/1.89** | +9164/1.85 | +8948/1.81 |
| rumers_box·MYM | 7 | +334/1.48 | +428/1.71 | **+414/1.67** | +400/1.63 | +386/1.60 |
| turtle_soup·MNQ | 348 | −10633/0.78 | +6345/1.20 | **+5649/1.18** | +4953/1.15 | +4257/1.13 |
| turtle_soup·MGC | 422 | −9597/0.81 | +7261/1.22 | **+5573/1.16** | +3885/1.11 | +2197/1.06 |
| bos_fvg·MNQ | 408 | −4127/0.86 | +3440/1.16 | **+2624/1.12** | +1808/1.08 | +992/1.04 |
| trend_rider·MNQ | 278 | +2314/1.07 | +8015/1.31 | **+7459/1.28** | +6903/1.25 | +6347/1.23 |
| bos_fvg·MES | 381 | −4824/0.66 | −867/0.91 | **−2772/0.77** | −4677/0.67 | −6582/0.59 |

Two classes fall out:
- **Fill-robust** (positive under every convention, ≤54 stops, $23–76/trade): aoi_fade·MCL, aoi_fade·MES,
  turtle_soup·MCL, rumers_box·MYM (+ golden_zone·MNQ/MCL, n<15). This is the current live book. Good.
- **Fill-fragile** (sign depends on the stop rule, 280–420 stops, $4–22/trade): turtle_soup·MNQ/MGC,
  bos_fvg·MNQ, trend_rider·MNQ. Their entire edge is smaller than the gap between fill conventions.
**Correction on the record:** my 2026-10-24 statement "BOS+FVG loses on every ticker" held only under
minute-close fills. Under the production-calibrated rule bos_fvg·MNQ is +$2.6k / PF 1.12. It is
execution-fragile and thin, which is a different reason to keep it off live, but "loses everywhere" was wrong.
**Caveat that cuts the other way:** the −2-tick rule rests on Tier 1 n = 11 bracket stops, none on MES, MCL or
AOI Fade, and none in a fast market; one gap-through week on a 400-stop detector erases the edge.
**Recommendation:** live book = fill-robust class only (as now). A fill-fragile pair is eligible for paper
promotion only when (a) Tier 1 n ≥ 50 with p90 measured, and (b) per-trade edge at stop−p90 ≥ 2× the
p90−median slippage cost. **Test:** `ops/replay_matrix.py --stop-fill-ticks {0,2,4,6}` is now a one-flag
sweep; add it to the weekly kill-watch run.

### 3. MU / single-stock scalp: root cause of "0 tracked" — the detectors read a keyspace nothing writes
**Evidence:** `compass/mu_scalp.py:55` and `:137` — `db.prefix(c, 'bar:%s:' % symbol)`. No module writes
`bar:<SYM>:*` to the state table (bars go to `events` kind='bar' and to `bar_window:<SYM>`; `providers.py:324`).
`compass/scalp_backtest.py:9` says this in its own docstring ("that keyspace does not exist in state"), which
is why the backtest endpoint finds 64 gap signals from `events` while production finds none.
`tests/test_mu_scalp.py:19,33,48` seed phantom `bar:MU:1d:*` / `bar:MU:1m:*` rows by hand — the tests
test a world that doesn't exist. The 10-23 transaction fix made the segment *run*; it runs and finds nothing,
then `engine.py:1163` writes `mu_scalp:gap_done:<sym>:<day>` and the day is consumed.
**Fix:** `check_gap_fade` → prior close from `events` daily (or `daily:` via `htf_levels._daily_rows`),
premarket price from `quote:<SYM>`; `check_spike_fade` → session bars from `bar_window:<SYM>`
(`ict_common._bars_from_window`). **Test:** seed one `bar_window:MU` row, one daily event, `quote:MU`
gapped 3 %, assert `check_gap_fade` is not None — must fail today. Delete the phantom-key seeding.

### 4. Swings can now hang open past expiry — close snapshot never captured when the option's last quote stamps at/after 16:00:00 ET
**Evidence:** `compass/swing_ideas.py:212` requires `hours[0] <= cq['ts'] < hours[1]` with `hours[1]` =
XNYS close. SPY/QQQ/IWM options quote to 16:15; single names get a final NBBO at 16:00:00.x
(`providers.py:722` stamps the vendor `last_updated`). The snapshot is rejected, `observe` breaks at `:311`
every tick, no day is evaluated, and `maximum_hold` (checked only inside `evaluate_close`, `:255`) never
fires. The quote-gap termination that used to end such a swing was removed in b23f77d. The underlying leg
is only checked `day(uq['ts']) == d`, so its "close" mark can be a 19:59 after-hours print.
**Fix:** accept the last option quote with `ts <= hours[1] + grace` (say 20 min) and take the underlying's
last quote `<= hours[1]`; evaluate `maximum_hold` on `exit_deadline` independent of snapshot capture.
**Test:** open swing, `quote:O:SPY…` ts = close + 5 s, tick at close + 60 s → `close_snapshot` set (today
None); second: `exit_deadline` passed with no snapshot → status != 'open'.

### 5. Swings: a missing snapshot day defers the stop by a full session
**Evidence:** `swing_ideas.py:303-309` marks day d `data_gap` only once d+1's snapshot exists, then prices
the 0.65× premium stop at d+1's close; intraday touches are no longer evaluated at all. By design per the
commit, but a premium stop breached on Tuesday is realized at Wednesday's close.
**Fix:** when d is a gap, evaluate the stop against the *underlying's* intraday extreme for d (bars exist)
and realize at the next available option mark; or at minimum cap the deferral at one session with a
flagged `late_stop` reason. **Test:** gap on d, underlying breaches stop on d → trade closes with
`exit_day == d+1` and `late_stop=True`, not silently at d+1's premium.

## B. Shadow systems (never trade, but they cost CPU, storage and credibility)

### 6. Iceberg detector cannot be disabled, and its alert buffer is unbounded
**Evidence:** `compass/iceberg.py:66` `_opt()` reads `cfg.iceberg_<name>` → `cfg.iceberg_enabled`; the config
field is `iceberg_detector` (`config.py:153`). `enabled` therefore always falls to `DEFAULTS['enabled'] = True`.
With `ICEBERG_DETECTOR_ENABLED=false`, `providers.py:152` stops the drain pass but `option_stream.py:94/125`
keeps calling `observe_*`, and `pending_alerts` (`iceberg.py:139`, a deque with no maxlen) grows for the
life of the process. Separately `iceberg.py:483-492` sets `changed=True` for every alert with a quote
< 1 h old regardless of `status`, so `score_pass` (`:563`, full `iceberg_alert:` prefix scan every 30 s)
rewrites completed alerts indefinitely; nothing prunes `iceberg_alert:` rows.
**Fix:** `_opt(cfg,'enabled')` → read `iceberg_detector`; `deque(maxlen=…)`; skip `status=='complete'` in
scoring; add `iceberg_alert:` to retention. **Test:** `IcebergDetector(SimpleNamespace(iceberg_detector=False)).enabled is False`; alert with `status='complete'` → `_score_alert` returns False.
Also `iceberg.py:265-269`: `ask_share` denominator excludes prints with stale NBBO, so 1 ask print + 24
unclassified = ask_share 1.0 — contradicts the "predominantly at the ask" docstring.

### 7. `/api/simple` (public, uncached) now does five unbounded prefix scans per hit
**Evidence:** `simple.py:1437-1438` (`iceberg.summarize` + `status`, two full `iceberg_alert:` scans),
`:1391` (`scanner_shadow.scorecard`, full `shadow:0dte-` scan), `:1341` (full `trade:0dte-scan-`),
`live_combo_scorecard.py:74` (all `trade:`), plus `Config()` rebuilt per request at `:1387`. Route is in
the public set (`app.py:263`). Content is clean (no tokens/ids/broker state), but it does reveal which
detector×symbol combos route live.
**Fix:** wrap the tracking block in the existing `SnapshotCache` (60 s) and bound each scan by `days=`.
**Test:** request count → ≤1 DB round of scans per minute.

### 8. Seat/shadow split: one denominator bias
**Evidence:** `zero_dte_paper.py:249-251` drops a *seated* alert with no fresh quote and never records it
(`scan_ts` already advanced at `:209`); the same alert in a *shadow* cell becomes a `data_gap` row.
Seated cells under-count alerts relative to shadow, so the seat-vs-shadow comparison is biased in the
seats' favour. Enforcement itself is correct (single booking point `:224`; identical fill/exit rules;
disjoint keys). **Fix:** record a `data_gap` trade row for the seated case. **Test:** seated alert, no
quote → one `trade:0dte-…` row with `data_gap=True`.

## C. Measurement integrity (both harnesses)

### 9. Evaluation clock (carried from Task 001) — resolved by Quill's timing statement; one re-run owed
Quill's `2026-10-26-harness-timing-statement.md`: production keys a trade on the confirmation bar's open
(`signal_ts`) and acts at that bar's close (measured 2026-10-26: close 01:26:00 → TradersPost 01:26:16.7 →
fill 01:26:16.9). My harness does the same — the "+60 s" in my ids is the completion label, not a later
action. Quill's harness feeds completed bars but evaluates on a **120 s grid**, so its actions land
anywhere in [close, close+120 s). That is the entire ~$1,050 AOI Fade·MES residual (3 flips, 15 unmatched),
and it is a one-sided lag on Quill's side, not lookahead on either. Alignment to bar close + 0 is queued on
Quill's side; I re-run MES when it lands. `replay_matrix-v2` records its clock in the output JSON.

### 10. Tier 1 re-fit is not optional for the fragile class
Item 2's table is the reason: at n = 11 the rule's p90 is 4 ticks on three MNQ observations. Log every
live bracket-stop fill with `(fill − stop)` in ticks as a first-class row (not a one-off export) so the
sweep in item 2 can be re-run weekly against live truth.

### 11. Harness known gaps (mine), for the record
Synthetic one-tick spread, no size; no VIX/news filters; unadjusted roll gaps; `continuation` excluded
(quadratic); memoized pure helpers (byte-identical); bar window 1,800 rows (= production cap). All stated
in `2026-10-25-claude-harness-spec.md`. Upstream `ops/replay_matrix.py` to main so kill-watch and
promotion re-runs come from the tree, not a desk file.

## D. Already on Quill's list — endorse, with the test that closes each
- **ict_trail** (`engine.py:1082-1105`): runs every tick, manages zero positions (no `is_trailer` producer).
  Delete or wire to t1 after +1R — decide via replay (`--detectors` + a trail flag), not by keeping both.
- **Pruned-detector env audit**: a test that fails if `Config()` with `ICT_BOS_FVG_ENABLED=true` is
  constructed in a non-local role without an explicit allowlist entry would have caught Morning Drive/ICC.
- **`/health` → `cron_token: bool`**: one line; the CI smoke test then greps it.
- **Scorecard live column**: `live_combo_scorecard.summarize` already filters `trade:`; add
  `traderspost_sent=True` subset and the per-combo paper−live gap (item 1 data makes this non-zero).
- **Archive gens 1–2**: tag + manifest + delete (~800 lines, 7 public-set routes that can start a COPY).

## E. Hygiene
- `ict_paper.py:58-77` header still argues the pre-prune verdicts ("BOS+FVG: MNQ (+$520) works") above an
  allowlist that says the opposite. `engine.py:312` comment cites a removed trailer leg.
- `.env.example` / README allowlist pass.
- Credential rotations (checklist 2026-10-21): still the highest-leverage non-code item.

## Priority for Josh (my ranking)
1 → 3 → 4 → 2 (as a policy, not a change) → 6 → 7 → 5 → 8 → D → E.
