# Engine variable audit — pruned detectors (desk item 8, READ ONLY)

From Quill, 2026-10-26. Method: Railway GraphQL `variables` query against
the Engine service (project `4801dbfb…`, environment `711a06df…`, service
`b5a4b496…`), read-only, same stored-token pattern as the deployments
helper. **No variable was changed.** 67 variables total on the service
(matches the post-purge count). Code defaults are from `origin/main`
(`compass/config.py`, `compass/ict_paper.py`) as deployed.

## The four pruned detectors — all four vars still say 'true'

| Variable | Engine value | Code default (`config.py`) | Detector state |
|---|---|---|---|
| `ICT_BOS_FVG_ENABLED` | **`true`** | `"false"` (line 137) | pruned 2026-10-24 |
| `ICT_BOS_GZ_VWAP_ENABLED` | **`true`** | `"false"` (line 138) | pruned 2026-10-24 |
| `ICT_AOI_ZONES_ENABLED` | **`true`** | `"false"` (line 118) | pruned 2026-10-24 |
| `ICT_SMT_DIVERGENCE_ENABLED` | **`true`** | `"false"` (line 117) | pruned 2026-10-24 |

**What that means today, layer by layer (verified in code):**

- *Config:* each flag is `env("ICT_…_ENABLED", "false") == "true"` — the
  env value wins, so all four flags evaluate **True** in the running
  Engine. The code defaults the prune shipped are overridden, exactly the
  stale-var failure the Task 003 brief predicted ("code defaults are
  false since 2026-10-24, but stale Railway vars overrode defaults twice
  this week").
- *Scanning:* `Engine` gates each ICT scan on its cfg flag, so all four
  detectors' scans **run every tick** in production right now — CPU and
  state writes for detectors the book has retired.
- *Booking:* the hard block is elsewhere. `DETECTOR_SYMBOL_ALLOWLIST` on
  main holds **empty sets** for all four (`ict_paper.py` lines 94–97,
  commented "empty blocks re-enable by env"), so `submit()` declines every
  signal they produce: **no paper trades, no TradersPost sends, no live
  exposure from any of the four today.** The scorecard at 2026-10-25
  18:55 CT agrees (zero trades for all four combos in the trailing
  28 days despite the flags being on).

**Risk statement:** the system is safe by the allowlist, not by the
flags. The stale `true`s defeat the prune's defaults, and if an allowlist
is ever repopulated (e.g. a future matrix experiment reaching for these
detectors) they would start trading immediately with no flag change and
no review. **Recommendation (for Josh / the normal change route — not
done here): delete these four variables, or set them to `false`.**

## The deleted detectors (purge of 2026-10-25)

| Variable | Engine value | Note |
|---|---|---|
| `ICT_MORNING_DRIVE_ENABLED` | **absent** | unset 2026-10-25 (Option B) before deletion — verified absent in this read |
| `ICT_ICC_ENABLED` | **absent** | unset 2026-10-25 (Option B) before deletion — verified absent in this read |
| `ICT_CONTINUATION_ENABLED` | `false` | detector deleted; no config field reads it on main — dead var, inert, value consistent. Delete whenever the four above are cleaned |
| (VWAP Fade) | **absent** | no variable exists for it |

## Everything else ICT — consistent with the live-book design

| Variable | Value | Design check |
|---|---|---|
| `ICT_AOI_FADE_ENABLED` | `true` | live {MCL, MES} ✓ |
| `ICT_TURTLE_SOUP_ENABLED` | `true` | live {MCL} ✓ |
| `ICT_GOLDEN_ZONE_ENABLED` | `true` | enabled as a detector; paper-only is enforced in code (`PAPER_ONLY_DETECTORS`, `ict_paper.py` line 113), not by env ✓ |
| `ICT_RUMERS_BOX_ENABLED` | `true` | same paper-only construction ✓ |
| `ICT_FUTURES_PAPER_ENABLED` | `true` | ✓ |
| `ICT_PUSH_ENABLED` | `true` | ✓ |
| `TRADERSPOST_ENABLED` | `true` | ✓ |
| `ICT_TREND_RIDER_ENABLED` | **absent** | code default `"false"` governs → Trend Rider OFF, as designed ✓ |

One observation outside the detector scope: `TRADERSPOST_WEBHOOK_URL_2`
is still set (name only — value never recorded here). After the
2026-10-25 consolidation to a single Tradefy strategy, whether the
secondary webhook leg should be cleared is a TradersPost-cleanup question
for Josh, not a detector issue.

— Quill (audit is read-only; the cleanup itself goes through the normal
change route with Josh's go)
