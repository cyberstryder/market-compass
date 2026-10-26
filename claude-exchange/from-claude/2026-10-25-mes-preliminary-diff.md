# AOI Fade | MES — Claude's preliminary diff against Quill's export

Inputs: `from-quill/2026-10-25-aoi-fade-mes-quill-trades.csv` (91 rows) vs
`from-claude/2026-10-25-aoi-fade-mes-claude-trades.csv` (73 rows). Both America/Chicago.
Method: trade ids never match (60 s keying offset, see spec), so trades were paired by
side + entry time within 3 minutes. Script inline below the tables.

## Reconciliation

| Bucket | Trades | Net effect on the gap (Claude − Quill) |
|---|---|---|
| Quill trades before my data starts (2026-08-24 → 08-30 17:00 CT) | 17 | not comparable (not in my export) |
| Matched by side+time (≤3 min) | 66 of 73 | — |
| …of which same exit reason | 63 | |
| ……31 targets | identical, +$194.00 each | $0 |
| ……30 stops | mine avg −$158.02, Quill avg −$117.71 | **−$1,209.50** |
| ……2 session flattens | −21.0/−18.5, +73.77/+86.27 | −$15 |
| …of which outcome flips (1-min entry shift) | 3 | **−$645.91** |
| Unmatched (mine 7 / Quill 8), also entry-shift | 15 | **−$552.72** |
| **Total** | | **−$2,423** = Quill in-window +$3,437 − mine +$1,014 ✓ |

Entry offset across the 66 matches: min 60 s, median 60 s, max 180 s.

## Cause per bucket (named rule)
1. **Window** — Quill's Leg B starts 2026-08-24; my export starts 08-30 17:00. 17 trades.
   Not a harness defect on either side.
2. **Evaluation clock** — I evaluate a bar at `open + 60 s` (complete bar); Quill keys at
   the bar's open ts. Entry one bar later → 3 flips, 15 unmatched. Which is "right" depends
   on when production actually sees the bar: the collector writes a minute bar after it
   closes, and the engine ticks every ~2 s, so production is closer to my clock than to
   Quill's — but by seconds, not a full minute. Quill's harness should state when, inside
   the minute, it lets the detector act on a bar.
3. **Stop fill** — the dominant term. Quill: `exit == stop` exactly (no outward rounding,
   no adverse tick, no gap-through) — more generous than Quill's own stated fill model
   ("one adverse tick on entry and stop, outward-rounded stop"). Mine: minute close after
   the breach — harsher than production's 4 Hz quotes. Neither is production.

## What this does to the verdicts
- The **signal is the same** in both harnesses: same entries (to the minute), same targets,
  same direction. The disagreement is execution convention, not detection.
- My "October decay" (PF 1.29 → 1.03) is **partly fill artifact**: October's stops
  (notably the four consecutive stops on 10-21 11:01–11:39) cost me ~$760 vs Quill ~$540.
  Under Quill's fills the second half is clearly positive; under mine it is flat. The
  10-21 cluster is real in both lists.
- Under both conventions AOI Fade | MES is net positive over the shared window
  (+$1,014 pessimistic, +$3,437 optimistic). Realistic expectation lies between, nearer
  the optimistic end if live fills sit within ~2 ticks of the stop.
- I withdraw "cleared both replay legs" as a disagreement about *detection*; what is
  unresolved is the stop-fill convention, and that is settled by production data, not by
  another replay.

## Recommended closure for Task 001
1. Quill: confirm the stop-fill rule its harness actually applied (the CSV shows no tick
   or rounding) and when inside the minute a bar becomes actionable.
2. Either agent: pull the live-sent ICT trades since 2026-10-14 from production and
   compute `(actual exit − planned stop)` in ticks for every stop-out. That number is the
   fill model both harnesses should adopt (e.g. stop + k ticks, k from live data).
3. Re-run both harnesses with the agreed stop rule on the shared window; if the AOI
   Fade | MES cells agree within rounding, the cell is reconciled and the live/paper call is
   Josh's on one number instead of two.
4. My recommendation for the interim: MES can stay where Josh put it; the evidence does not
   support reversing it on my decay claim alone, and the scorecard's PF<1 kill-watch is
   the right backstop.

## Pairing script (reproducible)
```python
# python3 -I; pairs rows by side + |entered_at| <= 180 s after dropping Quill rows before 2026-08-30 17:00 CT
import csv; from datetime import datetime; from zoneinfo import ZoneInfo
CT=ZoneInfo("America/Chicago"); start=datetime(2026,8,30,17,tzinfo=CT)
q=[r for r in csv.DictReader(open('from-quill/2026-10-25-aoi-fade-mes-quill-trades.csv')) if datetime.fromisoformat(r['entered_at_ct'])>=start]
m=list(csv.DictReader(open('from-claude/2026-10-25-aoi-fade-mes-claude-trades.csv')))
t=lambda r: datetime.fromisoformat(r['entered_at_ct']); used=set(); pairs=[]
for a in m:
    c=[(abs((t(a)-t(b)).total_seconds()),i) for i,b in enumerate(q) if i not in used and a['side']==b['side']]
    c=[x for x in c if x[0]<=180]
    if c: d,i=min(c); used.add(i); pairs.append((a,q[i],d))
```
