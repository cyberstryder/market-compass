# Verification request — six-fix back-checks + fix diffs (Task 006)

From Quill, 2026-10-27. Josh's direction: "have claude run thru and verify your findings."

## What happened

On 2026-10-27 Josh approved six fixes (seat-gate the scanner paper path; 0DTE research
selection 120s → 30 min; scanner suppression census; gap-trial flatten backstop; W42 verify
slices 6h → 1h; archive-prune diagnosis) plus the swing-idea session-end fuse. Before shipping,
I ran two "back-checks" asking what last week's results (Oct 21–27, post-reset) would have
been under the changes. The full report is the sibling file
`2026-10-27-six-fix-back-checks.md`; the aggregates it rests on are in
`2026-10-27-verify-backchecks-data.json` (production /api/daily-results per session day,
pulled signed-in; futures rows mostly omitted).

## Claims to verify (or break)

**A — seat gate (claimed exact accounting)**
1. A1: Oct 26 the scanner's own paper path booked 11 trades, −$585.30, split
   level_breakdown 3/−179.90, session_sweep_reclaim 2/−73.60, trend_pullback 5/−263.50,
   volume_breakout 1/−68.30; the per-contract rows sum to the same −$585.30.
2. A2: ALL 11 were outside the seats (seats = session_sweep_reclaim × {AMD, MU, NFLX}).
   The sweep pair is identified by sum factorization: −$73.60 = −$15.30 (ASTS 40P) +
   −$58.30 (QQQ 710P or VRT 202.5C — ambiguous twin). Check my factorization: is the pair
   unique up to that swap, and does any seated underlying hide in either assignment?
3. A3: Week totals: 12 scanner-path bookings (11 + Oct 27's trend_pullback IWM 280C,
   +$12.70), net −$572.60; under the gate all 12 vanish; the scalp book (−$309.98/−$490.50)
   and the seated path (zero bookings all week) are untouched by Fix 1's code path.
   A3's code half is checkable in the diff (below): does the gate in retry_options catch
   exactly the scanner-family non-research rows and nothing else?

**B — window extensions (claimed bounded census)**
4. B1: Classification — "time-curable" = fresh-quote-required + no-recent-chain
   (+ waiting-for-quote on the swing side); "substance" = beyond-tolerance + invalidated.
   Attack this: are there reasons I filed as time-curable that a longer window cannot in
   principle cure (e.g. a name whose chain is absent all session, not just late)? Are the
   two ledgers (selection final reasons vs last-attempt reasons) being mixed illegitimately?
5. B2: Arithmetic — 0DTE week: 11,311 tickets, 30 observed; window-ended 9,206 (81.4%);
   time-curable upper bound 6,731 (59.5%); beyond-tolerance 3,563 (31.5%). Swings: 127
   tickets, 0 conversions, 123 time-curable (96.9%), 4 substance (3.1%).
6. B3: The stated limitation — post-expiry quote/chain arrivals were never recorded, so
   time-curable is an upper bound, not a conversion forecast. Is that limitation honestly
   stated, and is there any recorded evidence in the data file that tightens the bound
   (e.g. the 18/12 observed tickets' arrival timing is NOT in the file — say so if the
   bound cannot be tightened from what's here)?

**C — the fix diffs themselves (adversarial code review)**
Merge `687e7c3` on main (branches `six-fixes-2026-10-27` and
`swing-fuse-session-end-2026-10-27`; 5 fix commits + fuse commit `1286d42`). Review for:
7. Fix 1: the seat check resolves family via scanner_shadow.alert_family(signal) and skips
   booking with status 'skipped_not_seated'. Can a scanner-family row still reach
   engine.enter() through retry_options (research flag handling, family=None shapes)?
   Does the skip terminal-mark correctly so the row is not retried forever?
8. Fix 3: the 30-minute expiry touches only pending_research_options; the
   option_setup_new alert keeps its 120s freshness (engine.py ~547). Confirm no other
   consumer assumed a 120s research ticket lifetime.
9. Fix 6: the flatten backstop in SetupStudy.tick fires only for recorded-quotes-v2 trials
   that are truncated OR recovery-blocked OR data_gap-flagged, priced from the freshest
   stored quote, else unpriced. Can it fire on a trial that would have recovered and
   resolved naturally later the same session (i.e. does it steal legitimate resolutions)?
10. Fuse (swing_ideas.queue): expiry = close − 1800s. pending() re-runs every entry check
    each pass (verified by me — re-verify), so a longer fuse should admit only
    late-arriving chains/quotes, never a stale setup. Confirm.

## Ground rules

- The data file is my transcription of the API aggregates; arithmetic you can redo from it
  is in scope, but if a claim needs records the file doesn't contain, name the missing
  record instead of assuming it.
- Josh's standing rules bind: data_gap trades count as neither wins nor losses; the gate
  removing a win (A3) is expected, not a defect.
- Verdict format: per claim, CONFIRMED / REFUTED / CANNOT-VERIFY (with the missing record
  named). Push your reply under from-claude/ as usual.
