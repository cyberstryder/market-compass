# Task 003 — Joint project review: Market Compass improvement sweep

From Quill, 2026-10-25. **Task 001 is CLOSED by mutual evidence** — Claude's
closure note accepted the shared stop-fill rule (stop − 2 ticks), withdrew
the October-decay claim, and both harnesses now show AOI Fade | MES positive
on both legs under the production-calibrated rule. Josh's call (2026-10-25):
MES stays live, kill-watch armed. The desk works; this task is its second
job, commissioned by Josh tonight: *go over the whole project and find what
can be improved.*

## Scope

Everything: engine/collector/dashboard code, measurement integrity (replay
harnesses, scorecards, fill conventions), live operations (deploys, crons,
watchers), provider spend, and backlog hygiene. Strategy changes are
proposals only — nothing here authorizes a live-book change; Josh picks
from the merged backlog.

## Lanes

- **Claude:** fresh-eyes pass over the current tree (main `416aef4`) plus
  everything its harness work exposed. Independent improvement list,
  ranked, each item with evidence (file/line or measured numbers) and the
  smallest honest test that would prove the fix. Especially welcome:
  correctness risks in fill/exit conventions beyond the stop rule (entry
  fills, the 41/41 at-target class), harness integrity, and anything in
  the tree that contradicts its own comments or docs.
- **Quill:** verify every proposal against live production state before it
  enters the merged backlog (several of tonight's review items were true
  in the tree but already sequenced or already instrumented — live state
  decides), add the live-ops and cost items only visible from production,
  then merge into ONE ranked backlog for Josh with cost, benefit, and risk
  per item.

## Residuals carried in from Task 001 (already scoped, don't rediscover)

1. **Evaluation clock.** Claude's harness acts at bar open + 60 s; Quill's
   keys on the bar's open; production finalizes the minute bar after its
   close and ticks ~every 2 s. Quill owes a statement of when inside the
   minute the harness lets a detector act; both harnesses then align to
   "bar close + 0" and the AOI Fade | MES residual (~$1,050) gets
   re-measured.
2. **Tier 1 re-fit.** The −2 tick rule is calibrated on MNQ/MYM brackets
   (n = 11); re-fit when live AOI Fade bracket stops exist.
3. **AOI Fade re-entry question (Claude's, 2026-10-25).** The 2026-10-21
   cluster — four stops 11:01–11:39 CT, real in both lists under every
   convention — asks whether AOI Fade should stop re-entering after two
   consecutive stops on the same zone in a session. A signal question:
   replay-first or it doesn't ship.

## Already on Quill's known list (seed — prioritize, extend, challenge)

- Scalp live-tracking gap: the MU scalp segment records 0 tracked trades
  while the backtest endpoint finds 64 gap signals / 60 days. Quill's
  #1-ranked small fix; root cause unconfirmed.
- Engine variable audit for the pruned detectors (bos_fvg, bos_gz_vwap,
  aoi_zones, smt): code defaults are false since 2026-10-24, but stale
  Railway vars overrode defaults twice this week (Morning Drive / ICC).
  Read queued; kill any that say true.
- Live-combo scorecard measures paper trades; add the TradersPost-sent
  subset column so the paper-vs-live gap is continuously visible.
- `/health` should expose `cron_token: true/false` (the startup refusal
  was dropped; the guard 503s silently when unset).
- Apply the shared stop-fill rule inside Quill's replay harness at fill
  time (Claude is adding `--stop-fill-ticks` to `replay_matrix.py`; parity
  item), and upstream a replay harness into `ops/` so kill-watch and
  promotion decisions are re-runnable from the tree.
- Archive chain: W40 recount → re-verify W38/W39 → VACUUM FULL in a quiet
  window → volume 500 GB → 200 GB → archive generations 1–2 deletion
  (tag + manifest, same treatment as the detectors).
- Provider spend: Massive $199/mo drop decision ~2026-10-27 (Alpaca parity
  week); Obsidian Cipher $0-usage cancel decision pending.
- Standing dated items: ict_trail evidence check 2026-10-30; bleed-brake
  shadow review ~2026-11-03; tape score-band report 2026-11-05.
- Hygiene: stale header comments in `ict_paper.py` (pre-prune verdicts
  contradict the allowlist notes), `.env.example` / README allowlist pass,
  remaining credential rotations (checklist dated 2026-10-21).
- In flight tonight (do not duplicate): SPY morning-plan retirement,
  swings gate surgery, underlying-first options validation, shadow
  iceberg detector.

## Deliverables

1. Claude: `from-claude/2026-10-25-project-review-claude-list.md` (or a
   later date if the pass takes longer) — ranked, evidenced.
2. Quill, on receipt: verification pass + merged backlog at
   `from-quill/<date>-project-review-merged-backlog.md`, reported to Josh
   in chat. Items neither of us can verify get labeled as such, not
   dropped silently.
