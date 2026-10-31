# Blueprint strategy — scoring audit + testable build (Claude, 2026-10-31)

Source: the pasted "state-machine" transcript (time filter → ATR regime → limit execution; ORB, gap fade, 1:2 R:R, time stop, circuit breaker, friction test). Data: `data-foundation-v1` year-1 micros, desk fill rule (Task 002). Files: `blueprint.py` (harness module, every rule a flag), `run_blueprint.py`, `blueprint_trades.csv`, `blueprint_results.txt`, `blueprint_strategy.pine` (TradingView v6 mirror), `audit_gap.py`, `audit_gap2.py`.

## 1. Was it scored correctly?

### 1a. The 10-27 sweep (Claude harness, Quill-verified) — reproduces, but two things were wrong
- All 30 gap rows re-run from the release CSVs match `sweep_stop2.csv` exactly (n, net, t, H1/H2). Arithmetic is fine.
- **The placebo was not a test for this family.** Gap fade fires once a day at a fixed clock (08:31). The session-matched placebo draws random bars *in the same hour on the same days, same direction* — for a fixed-time signal that is nearly the same trade, so the percentile measures nothing. Re-run with nulls that actually bite:

| sym | n | net | sign-shuffle pct | net from top 3 trades | small-gap control (0.1–0.5 ATR) avg/trade |
|---|---|---|---|---|---|
| MES | 45 | +1,450 | 93.4 | **93%** | −15.4 |
| MNQ | 55 | +4,623 | 90.2 | 68% | +11.6 |
| MYM | 51 | −572 | 38.0 | — | +9.1 |
| MCL | 65 | −1,352 | 26.8 | — | −0.5 |
| MGC | 75 | +5,554 | 72.6 | 65% | −57.2 |

Nothing clears 97.5 on a direction-shuffle null; MES is three days; the threshold has no monotonic effect (MNQ small gaps also positive, MGC small gaps very negative). Verdict unchanged — gap fade is not a survivor — but the original placebo percentiles (63 / 76.5 / 96) should be withdrawn as evidence either way.
- **The sweep tested a different rule than the transcript.** Sweep: |gap| ≥ 0.5 ATR vs prior RTH close, market entry at 08:31, 0.5-ATR stop, target = gap. Transcript: gap ≥ 0.5% *and* open beyond prior day H/L, wait 15 min, limit entry, stop at the 15-min extreme, target prior close. Built below.
- Correction to my 10-30 summary: "flips in H2" was true for MES only; MNQ (+949) and MGC (+440) were small-positive in H2.

### 1b. Production `compass/gap_fade.py` (Quill's code) — four defects
1. **Docstring claims "67.8% win rate on MES/MNQ across 2,967 trades."** No artifact anywhere on the desk; the desk holds ≤1 year of micro bars and a 0.5–2% gap filter yields ~30–50 qualifying days/yr/symbol. The number cannot have come from this data. Treat as unsupported.
2. **It can never book.** `'gap_fade'` is not in `ict_paper.STRATEGY_TAGS`, so `submit()` returns `unknown_detector` on every signal even with `GAP_FADE_ENABLED=true`. The module logs rows and nothing else — "evidence only" by accident, not design.
3. **Stop buffer uses a 14-bar ATR on 1-minute bars** (`_atr(bars)` over the bar window), so "0.25 × ATR beyond the gap extreme" is ~1–3 ticks on MES. If it ever did book, the `ict_paper_min_stop_atr` guardrail (0.5 × the same 1-min ATR) would decline most of them as `stop_too_tight`.
4. `detect()` fires on every scan from 08:45 until flatten with `signal_ts = now` and `entry = OR extreme` regardless of where price is. If (2) were fixed it would re-submit after every 15-min cooldown all day. Also `_prior_rth_close_ts` subtracts a calendar day, so Monday's "prior close" is Friday's 15:59 bar, Tue–Fri use 15:15.

Nothing here changes a live decision (gap fade has no live or paper seat). It does mean the module should be deleted or rewritten before anyone cites it.

## 2. The transcript strategy, built and scored

`blueprint.py` — one position at a time, every rule a `CFG` flag:
- **L1 time**: entries 08:30–15:00 CT, flatten 15:45.
- **L2 regime**: prior-session ATR5 > ATR20 → TREND (ORB); else MR (gap fade). **Chop**: skip day if ATR14 < 60-session 25th percentile.
- **L3 execution**: limit orders only; a limit fills only when the bar trades *through* it by 1 tick; stops fill −2 ticks; $3 RT.
- **ORB**: 30-min range; first 1-min close outside before 11:00 arms a limit at the edge (60-min validity); stop = OR mid; target 2R.
- **Gap fade**: gap ≥ 0.5% of price and open beyond prior H/L; after the 08:30–08:45 bar, limit at 25% retrace into that bar, stop 1 tick past its extreme, target prior RTH close; R:R ≥ 2 required; limit valid to 10:30.
- 120-min time stop, $150/contract daily breaker, max 2 trades/day.

### Full blueprint, desk fills (1 micro, $)
| sym | n | net | avg | WR | PF | DD | t | H1 | H2 | shuffle pct | gap n/net | ORB n/net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| MES | 55 | +459 | 8.4 | .38 | 1.18 | −631 | 0.56 | +291 | +168 | 70.9 | 6 / +188 | 49 / +271 |
| MNQ | 77 | +789 | 10.2 | .38 | 1.12 | −1,544 | 0.39 | −269 | +1,058 | 63.8 | 17 / −292 | 60 / +1,080 |
| MYM | 61 | −114 | −1.9 | .38 | 0.95 | −568 | −0.19 | −37 | −77 | 40.9 | 10 / −242 | 51 / +128 |
| MCL | 78 | −120 | −1.5 | .33 | 0.95 | −443 | −0.19 | +6 | −127 | 44.6 | 25 / −181 | 53 / +61 |
| MGC | 48 | −246 | −5.1 | .25 | 0.94 | −900 | −0.16 | +320 | −566 | 44.6 | 19 / +240 | 29 / −486 |

Friction test (+1.5 tk/side): MES +272, MNQ +691, MYM −194, MCL −347, MGC −387. The equity-index pairs don't *collapse*, but they were never above noise to begin with (t ≤ 0.56).

### Ablations (what each layer actually does)
- **Regime switch off** (both setups every day): MES +1,482 (t 1.23) — the switch *hurt* MES because ORB was positive on MR days too; MNQ −959, MGC −3,297. The ATR5/ATR20 split is not sorting days into "trend" and "chop" in any usable way.
- **Gap fade alone, transcript gates**: 28–92 trades/yr; stop rate 86–90%, win rate 10–14%; only MES positive (+198, t 0.32). Relaxing the gates (no beyond-H/L, R:R ≥ 1) makes it worse on all five (MCL t −2.6, MGC t −3.2). The 15-min-wait + limit-retrace entry fills late and gets stopped on the 15-min extreme most of the time.
- **ORB alone (limit retest)**: MES +302, MYM +366, MNQ −5,922, MGC −5,160. Same answer as the 120-cell ORB sweep on 10-27.
- **ORB with fill-at-level regardless** (optimistic bound, not tradable): MES +2,555 t 1.77 — i.e. the *best case* for the ORB leg, with free fills, is still under t 2. The limit-retest non-fill is not the thing hiding an edge.
- **Chop filter off**: adds ~55 days per symbol, no improvement anywhere (MES +387, MCL −834).

### Verdict
The blueprint is a risk-management wrapper around two setups Compass has already tested, and it scores the way they did: equity-index micros ~break-even to +$10/trade with t < 0.6, metals/oil negative, no component survives a direction-shuffle null, and the regime layer doesn't separate days. 1 year × 1 micro can't see an edge under ~$12/trade, and this one, if it exists, is inside that band.

## 3. How to test it yourself
- **Harness**: `python3 run_blueprint.py` from the fsearch dir (needs `sim.py`, `feat.py`, release parquet). Edit `CFG` or pass a dict to `bp.run(sym, {...})` — e.g. `dict(use_regime=False, orb_rr=1.5)`.
- **TradingView**: `blueprint_strategy.pine` on a 1-min MES/MNQ chart. Same inputs as the flags. Expect TV to score *higher* than the harness: TV fills limits on touch (harness needs trade-through) and fills stops at the level (harness −2 ticks). Set "Extra slippage ticks/side" = 1.5 for the friction test.

## 4. What's still untested from the transcript
Intraday spread/stat-arb (MES–MNQ, MGC–MCL). Not built. Two legs = ~$6 RT friction against z>2 excursions that are typically a few ticks on index pairs; expect "undetermined" at n≈250. It is the only line in the transcript Compass hasn't scored.
