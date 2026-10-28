# Futures method search on data-foundation-v1 — 2026-10-27

Claude. Data: release `data-foundation-v1`, `futures-databento-year1` (2025-10-27 → 2026-10-26; MES, MNQ, MYM,
MCL, MGC; ~350k 1-min bars each; 9 degraded days excluded; rolls back-adjusted so levels are continuous).
MES year-2 holdout downloaded, **not opened**. Harness + every result CSV are beside this file.

## Bottom line

**Nothing found.** ~6,000 strategy cells across 15 signal families, every instrument, longs and shorts scored
separately, were tested under the desk's fill rule (next-bar open +1 tick, stop −2 ticks, fees $3 RT, 15:45 CT flatten).
No cell clears a chance-corrected bar. The handful that pass a per-cell gate are isolated — every neighbouring
parameter flips sign — which is what noise looks like, not what an edge looks like.

Two things that *are* there, and why they don't pay on micros:

1. **Intraday momentum (first-30-min → last-45-min, Gao et al.)** — same sign on all five instruments, t≈1.6–2.9 on
   MYM/MES/MCL, but gross ≈ $5–12 per contract per day. Friction is $4–8. Net ≈ $0–3/trade. Real-looking, untradeable at
   micro size (a 1.5-pt ES move).
2. **"Toward VWAP" at a random time loses** (−$10 to −$50 gross per 60-min trade on MNQ/MGC) — i.e. there is
   intraday trend persistence on the VWAP side — but conditioning on anything (a fixed 30-min slot, an extension
   event, a daily-trend filter) does not convert it into a positive gross after the sign is applied at fixed times
   (VWAP-side at 30-min marks: mean t ≈ 0, 7,000 cells, max |t| 3.9 = chance).

## What was tested (family × instrument × direction × parameters)

| Family | Cells | Best t | Note |
|---|---|---|---|
| Time-of-day drift (48 slots × 3 holds, long) | 690 | 2.18 | MGC 04:30–06:30 long cluster = gold's London-open drift; H2 collapses on the h60 cell |
| ORB 5/15/30 min, long & short, 4 bracket geometries | 120 | 2.19 | negative almost everywhere; MGC orb5 short placebo 18th pct |
| Intraday momentum / contra (first 30/60 → last 45/60/90, PM) | 30 + dedicated study | 3.2 (MYM) | sign robust, size ≤ friction |
| Overnight gap fade / follow (≥0.25, ≥0.5 ATR) | 30 | 1.79 | fade ≥0.5 ATR positive on MES/MNQ/MGC in H1 only |
| Session-level fade & break (Asia H/L, London H/L, prior-day H/L, prior RTH close, midnight open, VWAP) | 360 | 1.93 | user's own session-level line; nothing |
| Midnight-open retrace / follow at 08:30 | 20 | 1.34 | nothing |
| VWAP z-extension fade / follow (session & RTH VWAP, 3 thresholds) | 175 | 2.05 | event study: extension → ~0 gross at 15–120 min on all 5 |
| 30/60-min mean reversion & momentum × session × 3 thresholds | 401 | 2.00 | survivors isolated |
| 60-min channel breakout × session | 90 | 1.32 | nothing |
| Narrow-range compression breakout | 20 | 0.38 | negative |
| Daily-trend (10/20/50 SMA) filtered pullback / breakout / fixed-time, with & against | 718 | 1.72 | 0 cells t≥2; against-trend ≈ with-trend |
| VWAP-side persistence at every 30-min slot × 4 holds × 3 min-extension | ~7,000 | 3.9 | gross mean ≈ 0 |
| VIX-regime split (prior close <18 / 18–25) on momentum and VWAP fade | 30 | 2.8 (n=21) | year's VIX range 13.5–31, median 17 — no high-vol regime to test |
| Unconditional session drift, long, 6 segments | 30 | 2.02 | MCL NY-AM long $14/day; MES RTH long $4/day on a +12% year |

Gate used for "survivor": n ≥ 50, both calendar halves positive, t ≥ 1.5, session-matched placebo
(random same-day same-hour same-geometry entries, 200 draws) ≥ 97.5th pct. 4 of 1,936 bracket/horizon cells pass;
≈ 2 expected by chance. Each one's parameter neighbours (adjacent threshold or exit) are negative.

## Why a year of micros can't show a real intraday edge

Per-day SD of the RTH move on one MES micro is ~$227 (MNQ $520, MCL $190, MGC $450, MYM $160). Over ~240 sessions
the minimum detectable daily effect at t=2 is ~2·SD/√240 ≈ 0.13 SD ≈ **$30/day on MES**. Published intraday
effects are 0.02–0.05 SD. Anything real enough to matter at the sizes that exist is below this sample's noise
floor; anything this sample *can* see would be a 3–5× larger edge than the literature has ever documented.
That is also why the desk's 8-week studies kept producing PF 1.5–1.9 that evaporated: at n ≈ 70 the noise band is ±PF 0.5.

## What I would do instead

- Stop searching detector space on 1-min micros with 8-week or 1-year samples. The test cannot pass.
- The only intraday structures with cross-instrument sign stability here are (i) first-30 → last-45 momentum and
  (ii) VWAP-side persistence. Both are ~$5–10 gross per micro. They only clear friction at full-size (ES/NQ/CL) or
  with ≥5 micros per signal, i.e. $25–60 gross vs $8 friction — which is a sizing/account question, not a signal question.
- If the account constraint is no-carry on funded micros, the honest answer from this data is: there is no
  per-trade edge large enough to survive $5–8 friction. Edges that exist in the literature at this frequency are
  10–50× smaller than friction on one micro.
- Keep the MES year-2 holdout sealed. Nothing here is worth spending it on.

## Files
`sim.py` (bracket/horizon simulator, placebo), `feat.py` (features), `families.py` (signal definitions),
`sweep.py`, `placebo_run.py`, `vwap_event.py`, `vwap_side.py`, `imom.py`, `trend_pb.py`; results
`sweep_stop2.csv` (1,936 cells), `placebo.csv`, `vwap_event.csv`, `vwap_side.csv`, `imom.csv`, `trend_pb.csv`.
Re-run: extract the release to `/home/claude/data/year1`, `python3 load.py && python3 sweep.py`.
