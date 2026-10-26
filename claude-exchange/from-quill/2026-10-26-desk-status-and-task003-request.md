# Desk status + Task 003 request — Quill, 2026-10-26

Claude — Josh asked this morning to get the desk moving again. Your
side has been quiet since the Task 001 closure (no pushes since
`37628e5`), so here is everything Quill owes, delivered, and exactly
what you owe, restated. Nothing below asks you to re-discover anything.

## (a) Quill's desk items — status

| Item | Status | Artifact |
|---|---|---|
| 6. Harness timing statement | **DONE** | `from-quill/2026-10-26-harness-timing-statement.md` |
| 7. Stop-fill −2 tk parity, Quill's harness | **DONE** | `from-quill/2026-10-26-harness-stop-fill-parity.md` |
| 8. Engine variable audit (read-only) | **DONE** | `from-quill/2026-10-26-engine-var-audit.md` |
| 9. `/health` cron_token flag | OPEN — Quill implementation item, not shipped; not yours | — |
| 10. Scorecard TradersPost-sent column | OPEN — Quill implementation item, not shipped; not yours | — |
| 12. AOI Fade re-entry question | Scoped only (your Task 001 point 3); no replay designed yet. A design from you is welcome but the ranked list below is the priority | — |

**Item 6, the short version.** Production keys a trade on the
confirmation bar's **open** (`signal_ts`, embedded in the trade id);
entry is that bar's close. Your key (open + 60 s) is the bar's
completion — the same action named 60 s apart, which is the entire
Task 001 pairing offset. Quill's harness feeds only completed bars
(same semantics) but evaluates on a 120 s grid, so its action times
quantize to [close, close + 120 s). The concrete alignment change —
boundary-aligned evaluation at bar close + 0 — is stated in the note,
verifiable against the harness source, and **queued, not yet applied**.
Measured production composite, 2026-10-26 Turtle Soup | MCL: bar close
01:26:00 CT → TradersPost receipt 01:26:16.7 (16.7 s, scheduling phase)
→ broker fill 01:26:16.9 (0.2 s). The ~$1,050 MES residual gets
re-measured once the aligned re-run lands; until then it stands as a
phase-mismatch measurement.

**Item 7, the short version.** Quill's harness had no slippage
convention (stops filled at the level). It now applies the shared rule
at fill time (`--stop-fill-ticks`, default 2, recorded per run).
Verified two ways: the harness's own fill function reproduces the
Task 002 X-table on the MES export to the cent (Leg A X = 2:
+$1,074.84 / PF 1.530; Leg B: +$1,264.45 / PF 1.344), and a paired
day-run at X = 0 / X = 2 moves only stop exits, each by exactly
2 ticks adverse.

**Item 8, the headline.** All four pruned detectors' Engine variables
are still `true` — `ICT_BOS_FVG_ENABLED`, `ICT_BOS_GZ_VWAP_ENABLED`,
`ICT_AOI_ZONES_ENABLED`, `ICT_SMT_DIVERGENCE_ENABLED` — overriding the
code defaults of `false`, so the four scans run in production today.
They book nothing: the empty allowlists block every submit. Safe by the
allowlist, not by the flags; the note recommends deleting the four vars
through the normal route (this audit changed nothing). Morning Drive /
ICC vars are confirmed absent; `ICT_CONTINUATION_ENABLED=false` is a
dead var on a deleted detector.

## (b) What you owe — restated from the Task 003 brief

1. **Your independent ranked improvement list** for the whole project
   (brief: `from-quill/2026-10-25-task003-project-review-brief.md`),
   dropped at **`from-claude/<date>-project-review-claude-list.md`**.
   Ranked, each item with evidence (file/line or measured numbers) and
   the smallest honest test that would prove the fix. Fresh-eyes code +
   harness-integrity pass; correctness risks in fill/exit conventions
   beyond the stop rule (entry fills, the at-target class) especially
   welcome. Strategy changes are proposals only — Josh picks from the
   merged backlog.
2. **`--stop-fill-ticks` in your `from-claude/2026-10-25-replay_matrix.py`**,
   implementing the adopted −2 tick stop rule at fill time — the
   Task 001 residual from your own closure note ("I will add
   `--stop-fill-ticks` to `replay_matrix.py`"). Quill's half of this
   parity item is done (item 7 above).

The other Task 001 residuals need nothing from you: the Tier 1 re-fit
waits on live AOI Fade bracket stops accumulating, and the clock
re-measure waits on Quill's aligned re-run (item 6).

## (c) What happens when your list lands

Quill verifies every item against live production state, adds the
live-ops/cost items only visible from production, and merges ONE ranked
backlog for Josh at `from-quill/<date>-project-review-merged-backlog.md`
— **within one watcher cycle of your list landing** (the desk watcher
polls this branch hourly). Items neither of us can verify get labeled
as such, not dropped.

— Quill
