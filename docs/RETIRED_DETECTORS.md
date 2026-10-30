# Retired detectors — restore manifest

Four futures detectors were deleted on 2026-10-25 (Josh's decision,
Option B: unset the stale production flags first, verify, then delete).
The full pre-deletion tree is preserved under the annotated git tag
**`pre-detector-purge`** (= commit
49bc48dd467df1ec958d13c35f8076f29f3d541e).

Nothing here is unrecoverable. To bring a detector back, restore its
files from the tag and re-apply its wiring (config fields, engine scan
block, ict_paper flag map) — either by hand from the lists below or by
reverting the deletion merge commit on main and pruning what you do
not want. If the detector should run in production again, also re-set
its Engine env var on Railway.

## Morning Drive (Riley Coleman morning-drive fade)

- Module: `compass/morning_drive.py` (339 lines)
- Test: `tests/test_yt_morning_drive.py` (255 lines, 11 tests)
- Config flag: `ict_morning_drive` (`ICT_MORNING_DRIVE_ENABLED`)
- Wiring removed: config field; engine scan block + `morning_drive`
  health-tuple name; `ict_paper.DETECTOR_FLAGS['morning_drive']`;
  `pick_check` ICT evidence pillar; `GET /api/yt-morning-drive` endpoint
  in `compass/app.py`; detector-select option in
  `compass/static/index.html`.
- Why dead: `BAR_LIMIT = 30000` (needs ~14 RTH sessions of 1m bars),
  but `compass/providers.py` hard-truncates the stored bar window to
  the last 1,800 bars (fallback `db.recent` limit is 500). The Oct 24
  bar-window audit proved it structurally cannot fire in production,
  and it never did.
- Production flag note: the Engine variable
  `ICT_MORNING_DRIVE_ENABLED` was still `"true"` (overriding the false
  code default) until 2026-10-25, when it was unset in Phase 1 of this
  retirement and production was verified unchanged (health revision,
  live-combo scorecard, same image digest).
- Restore:
  `git checkout pre-detector-purge -- compass/morning_drive.py tests/test_yt_morning_drive.py`

## ICC (tradesbysci)

- Module: `compass/icc.py` (355 lines)
- Test: `tests/test_yt_icc.py` (327 lines, 20 tests)
- Config flag: `ict_icc` (`ICT_ICC_ENABLED`)
- Wiring removed: config field; engine scan block + `icc` health-tuple
  name; `ict_paper.DETECTOR_FLAGS['icc']`; `pick_check` ICT evidence
  pillar; `GET /api/yt-icc` endpoint in `compass/app.py`;
  detector-select option in `compass/static/index.html`.
- Why dead: `BAR_LIMIT = 15000` (needs 7,200 1m bars / 120 1H bars of
  structure) against the same 1,800-bar production cap — same Oct 24
  bar-window audit verdict: structurally unable to fire, always was.
- Production flag note: the Engine variable `ICT_ICC_ENABLED` was
  still `"true"` until it was unset alongside Morning Drive's on
  2026-10-25 (Phase 1, verified as above).
- Restore:
  `git checkout pre-detector-purge -- compass/icc.py tests/test_yt_icc.py`

## Continuation (ICT)

- Module: `compass/continuation.py` (287 lines)
- Test: `tests/test_ict_continuation.py` (232 lines, 16 tests)
- Config flag: `ict_continuation` (`ICT_CONTINUATION_ENABLED`)
- Wiring removed: config field; engine scan block + `continuation`
  health-tuple name; `ict_paper.DETECTOR_FLAGS['continuation']`;
  `ict_paper.DETECTOR_SYMBOL_ALLOWLIST['continuation']` (was the empty
  set — disabled entirely); `pick_check` ICT evidence pillar;
  `GET /api/ict-continuation` endpoint in `compass/app.py`;
  detector-select option in `compass/static/index.html`.
- Why dead: disabled at both layers before deletion (code default
  false, Engine override false, empty allowlist since the Oct 14
  mix/match analysis found it lost on every symbol). In the full-month
  production-config matrix its one standout cell reversed: −$724,
  PF 0.96. Named on the approved futures-prune dead-code list.
- Restore:
  `git checkout pre-detector-purge -- compass/continuation.py tests/test_ict_continuation.py`

## VWAP Fade

- Module: `compass/vwap_fade.py` (240 lines)
- Test: none dedicated (incidental coverage only);
  `tests/test_config_detector_defaults.py` referenced its flag and was
  adjusted to drop `VWAP_FADE_ENABLED` from its retired-programs list.
- Config flags: `vwap_fade_enabled` (`VWAP_FADE_ENABLED`),
  `vwap_fade_sd_entry` (`VWAP_FADE_SD_ENTRY`), `vwap_fade_sd_stop`
  (`VWAP_FADE_SD_STOP`), `vwap_fade_session` (`VWAP_FADE_SESSION`)
- Wiring removed: the four config fields; engine scan block. It had
  no display endpoint, no `DETECTOR_FLAGS` or allowlist entry, and no
  health-tuple name — it emitted summaries only and could never
  submit trades.
- Why dead: the worst family in the full strategy×ticker matrix
  (−$24,322); set default-off on 2026-10-23/24 (Josh's config-defaults
  decision) because a summaries-only detector is wasted scan time.
- Restore:
  `git checkout pre-detector-purge -- compass/vwap_fade.py`

## ICT Trail runner (ict_trail) — retired 2026-10-30

Not a detector: the central trailing-stop manager for ICT paper
positions (activate a 2x ATR trailing stop at 1R profit on the
`is_trailer` runner leg of the old 2/2/1 scale-out). Deleted on
2026-10-30 (Josh's decision, after the one-week check below returned
verdict (b)).

- Module: `compass/ict_trail.py` (157 lines)
- Test: none dedicated (no test referenced the module).
- Config flag: none — the runner segment ran unconditionally on
  every engine tick.
- Wiring removed: the `Engine.tick` runner segment (its own
  transaction segment: lazy import of `manage_all`, the
  `health:ict_trail` 'running' write on trailed positions, and the
  `health:ict_trail` error-row write on failure). Nothing else
  referenced it: no config field, no endpoint, no `DETECTOR_FLAGS`
  entry, no display surface. Two historical comments in the MU
  scalp block below the segment were updated to point here.
- Why dead: since the 2026-10-18 single-leg simplification
  (fb54017), `ict_paper` books exactly one non-trailer leg per
  signal and no production code sets `is_trailer=True`, so
  `manage_all` iterated an always-empty leg set. The one-week
  check Josh set on 2026-10-24 (Decision 2 of 6) verified against
  live production markers for Mon 2026-10-26 – Fri 2026-10-30:
  0 trailer legs created, 0 trailed, $0 P&L effect, all symbols
  (evidence record:
  `hidden_files/ict-trail-check-2026-10-30/result.md` in the goal
  workspace). `ict_paper` leg creation is unchanged — single-leg
  booking stays exactly as-is.
- Deliberately retained (not wiring): the `exits()` sibling
  handling that ratchets a `:trail` position's stop when a t1/t2
  leg target-hits, the `:trail` suffixes in `ict_paper`'s
  close/cleanup loops, `simple.py`'s legacy 2/2/1 leg display, and
  the `traderspost` trailer-leg bracket conversion — these
  describe or guard stored history and shared paths, never fire
  without a trailer leg, and were out of this retirement's scope.
- Restore: `git checkout 998e6b8 -- compass/ict_trail.py` and
  re-add the `Engine.tick` runner segment from the same tree
  (visible in this retirement commit's diff). A restore only does
  something if trailer-leg creation is also restored in
  `ict_paper` — the Oct 18 decision Josh would be reversing.

## Test adjustments beyond the dedicated files

Three enumeration-style tests listed the retired detectors and were
adjusted minimally, purposes unchanged:

- `tests/test_config_detector_defaults.py`: dropped the removed env
  names and assertions (see the VWAP Fade entry above); env-override
  coverage re-pointed to `ICT_TREND_RIDER_ENABLED`.
- `tests/test_ict_paper.py::test_each_detector_submits_with_own_tag`:
  detectors whose tags remain in STRATEGY_TAGS but whose flag mappings
  are gone are now asserted to decline with reason `paper_disabled` —
  the test pins the retirement invariant.
- `tests/test_vix_collector.py::test_engine_dispatch_calls_vix_gate`:
  the source-regex set of `_should_run`-gated dispatch names dropped
  the three retired detectors (3 breakout + 4 fade remain).

## Deliberately retained references (not wiring)

These name registries and classification tables still mention the
retired detectors on purpose — they describe stored history, and the
detectors can no longer reach them:

- `ict_paper.STRATEGY_TAGS` entries (`continuation`, `morning_drive`,
  `icc`): identity tags of stored paper trades; imported by
  `compass/simple.py`, used by the correlation guard and historical
  attribution.
- `alert_format.ICT_SETUP_NAMES` (`ict-continuation`,
  `yt-morning-drive`, `yt-icc`) and the event-kind label maps in
  `compass/app.py`: display names for historical records and alerts.
- `regime.DETECTOR_STYLE` entries and the `vix_regime` breakout set:
  classification tables consulted only for detectors that submit or
  run; the VIX tests pin classifier behavior by name.
- `tier_a_b` DETECTOR_SOURCES entry for `ict_continuation:latest`
  plus its row-unwrap handling: tier_a_b consumes the stored snapshot
  artifact (its tests pin that behavior); the snapshot simply freezes
  now that nothing writes it.
- Historical comments (the ict_paper allowlist history block,
  engine.py's vwap_fade KeyError lesson) are kept as history.
