# Task 012 note — Hurricane v2 origin builds (Claude, 2026-11-02 16:00 CT)

Josh attached three v2 files: `hurricane_v2_oscillation-787deployed-161kprofit-May9th21h50.py`,
`hurricane_v2_oscillation-May13th.py`, `hurricane_v2_final_150s-May13th.py`. May-13 and final_150s are byte-identical;
May-9 is the same file minus the 150-s FORCE_EXIT block. Two copies in `2026-11-02-hurricane-v3/`.

## What v2 is
Pure oscillation scalper, no MACD/cone/flow: per strike, rolling 30-*print* min/max; buy when the bar open (ceil-rounded)
is in the bottom 20% of the prior range, target = lo + 0.8·range; exits = ZONE2 (print ≥ range hi), OSC_PROFIT, 5-print
velocity stop, 20-s flat bail, (May-13) 150-s force exit. 10–75 pts OTM, $0.10–$0.80, up to 20 contracts / $400 per entry,
5 concurrent, $10k start, $0.65/side.

## Fill model — the artifact starts here
- Entry: pending limit at `ceil(open/tick)·tick − 1 tick`, filled on a later bar if `low <= lim <= high` (`:318`), **no volume
  condition**, 7-bar life. Moonraker later added `volume >= 3`; Earth/Mars removed the check altogether.
- Exit: every sell `math.ceil`-rounded (`:587` May-9, `:598` May-13); OSC_PROFIT fills at target on `high >= target`.
- No spread. "30 seconds" = 30 prints.
- May-13 FORCE_EXIT snaps *up* unless the last three prints fell monotonically — added because positions were sitting, i.e.
  the oscillation was not oscillating back.

## The $161k headline
787 trades at up to 20 contracts. One tick = $100 at that size, so the buy-a-tick-under + sell-rounded-up mechanics are
worth ~$50–100 per trade before any signal; 787 × that is the headline. Posting 20 contracts one tick under the last print in
a far-OTM 0DTE SPX strike that prints a few contracts a minute is not a fill, it is the book. Defects 1–4 of the 11-02 audit
apply verbatim; 6 (PFMACD) does not exist here; 7 does.

## Why it matters
v2 is the cleanest test of the thesis — "far-OTM 0DTE premium oscillates within a 30-s range and the bottom 20% is a buy" —
with none of the v3 indicator layers confounding it. The 11-02 honest-fill harness ports directly (same `rolling_stats`,
`geiger_zone`, exit ladder minus CONE/FLOW). ~1 h of work. Sequence: Mars catalog run first (same data, same decision rule);
v2-on-honest-fills + placebo second if anything survives. Not started.
