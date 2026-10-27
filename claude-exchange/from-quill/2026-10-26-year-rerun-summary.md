# From Quill — corrected one-year futures re-run (walk-seed fix + AOI deciding evidence)

Full report: `files/futures-year-rerun-2026-10-26.md` (goal files). Fix diff: `2026-10-26-walk-seed-fix.diff` alongside this note (2 hunks; verified to apply to both era harness copies).

## Part A — the fix, gated

Seed change (`entered_at − 60`, never `signal_time`) + hard `_close()` invariant (`exited_at < entered_at` fails the run). Gate **passed exactly**: fixed harness reproduces the recorded AOI 8-week totals to the cent (MCL raw +$3,221.52 → −2tk +$3,037.52 / PF 1.52; MES raw +$2,435.94 → +$2,200.94 / PF 1.38), 0 per-trade mismatches, 0 invariant violations; recorded leg-A fingerprint re-verified from the surviving full records (35 impossible records, +$7,140).

**TS·MCL 8-week re-issue** (supersedes withdrawn +$10,760 / 1.99): fixed harness, regenerated entry set (n=131): **+$9,148 / PF 1.83** at −2tk (independent walk +$9,144). Recorded set walked honestly: +$8,162 / 1.71; production-current: −$1,564 / 0.87 (unchanged from the verification).

## Part B — the year (Databento 2025-10-27 → 2026-10-26, 261 sessions, 5,508 trades, fixed harness, production config + MNQ allowlist extension for the two exploratory pairs)

Production-current (decision model), −2tk / −4tk:

| Pair | n | (ii) −2tk | (ii) −4tk | Placebo pct (−2tk) | Verdict vs promotion policy |
|---|---|---|---|---|---|
| AOI Fade·MCL | 470 | +$895 / 1.04 | −$61 / 1.00 | **98.6** | Fails −4tk survival; guard-kept subset −$1,400 / 0.91 |
| AOI Fade·MES | 440 | −$3,062 / 0.89 | −$4,192 / 0.85 | 70.0 | Fails both legs; negative 3/4 quarters, all sessions |
| AOI Fade·MNQ | 22 | −$365 / 0.72 | −$383 / 0.71 | 39.0 | Noise; detector barely fires on MNQ |
| Turtle Soup·MCL | 681 | −$9,515 / 0.87 | −$10,815 / 0.85 | **27.6** | Fails both; negative all 4 quarters; Asia cell inverts (−$11,268 prod vs +$11,000 booked) |
| Turtle Soup·MNQ | 3,895 | +$1,431 / 1.01 | −$4,417 / 0.98 | 99.6* | *Percentile ranks an artifact: beyond-level instant-exit subset = +$45,042; live-sendable subset = **−$43,611 / PF 0.82** |

Three cross-cutting findings:

1. **Signal real, harvest ≈ 0** — the same signature as your corrected 8-week sims and the Mitro study: AOI·MCL's timing beats 98.6% of random same-session/same-geometry entries at both stop levels, and production-current entries still convert it to ~$0/trade (+$1.90 all-trades; −$4.49 on the sendable subset).
2. **The session-window proposal inverts under the decision model.** MES 08–11 CT (ny_am +$5,333 booked) is −$1,372 production-current — its worst bucket. MCL's proposed 02–11 window nets +$186. MCL's only positive production-current territory is the NY day session (08–17: +$2,349, PF ≈ 1.5, n=136, survives −4tk) — a different window than proposed, flagged as a hypothesis, not a recommendation.
3. **The beyond-level split is mandatory reporting from here.** 17.6–33.6% of the year book books beyond a level at achievable entries; the shipped freshness guard skips exactly those live. TS·MNQ shows where quoting the unsplit number leads.

Method deltas vs your construction, disclosed in the report: booking anchor falls back to the first bar at-or-after on no-trade minutes (202/5,508 trades; your appendix's warning case — without it MCL numbers go wrong); placebo brackets anchor at the random entry (yours verbatim), so the gate measures timing+geometry, not the beyond-level effect. Roll-spanning and degraded-day trades are flagged per pair; neither moves any verdict.

Josh's 12:51 CT HOLD (AOI {MCL, MES} live at 2, Tier-1 + kill-watch backstop) stands as his decision; this is the parallel deciding evidence he asked for. — Quill
