# TradingView AI companion — synthetic later-confirmation example

**SYNTHETIC FIXTURE PRICES. This is a formatting example, not a live plan or trading signal.** The opening check was WAIT; the second candle confirmed.

```text
Compass 2026-09-16 LATER CHECK · 09:00 CT | as of Sep 16 09:00:07 CT
Valid until Sep 16 09:02:05 CT; reference only afterward.
Draw these underlying-price levels on BOTH 1-minute and 15-minute charts (not monthly).
Use ONLY the column matching the chart: SPY, SPX or XSP. For any other symbol, stop and ask. Never mix columns.
Update or create one Compass group for this date; replace its levels with this snapshot and preserve all my other drawings. Reuse synced drawings across panes; do not duplicate.
Use horizontal rays from the as-of time; do not backdate signals. Label every ray with its name and price.
Keep each name paired with its listed price. If labels overlap, offset only their text with leader lines; never move or merge price levels.
PM high/low: blue; prior H/L/C: gray; gamma/Apex/GEX: purple; VWAP snapshot: orange dashed; stops: red dashed; targets: green dashed.
VWAP here is a static snapshot, not a live VWAP indicator. Premarket levels are provisional before 09:30 ET and frozen afterward.
All SPX/XSP levels are ESTIMATES: include EST in their labels. They are not independently confirmed index signals.
SPY | SPX EST | XSP EST
PM high: 760.50 | 7564.97 | 756.50
PM low: 759.50 | 7555.03 | 755.50
Prior high: 762.00 | 7579.89 | 757.99
Prior low: 758.00 | 7540.11 | 754.01
Prior close: 760.00 | 7560.00 | 756.00
RTH VWAP: 761.00 | 7569.95 | 756.99
Gamma flip: 761.50 | 7574.92 | 757.49
First 15m close: 760.00 | 7560.00 | 756.00
Confirmation close: 761.00 | 7569.95 | 756.99
Apex #1: 759.00 | 7550.05 | 755.01
Apex #2: 762.00 | 7579.89 | 757.99
Call stop: 760.20 | 7561.99 | 756.20
Call target: 762.60 | 7585.86 | 758.59
Put stop: 760.30 | 7562.98 | 756.30
Put target: 757.90 | 7539.11 | 753.91
Estimate basis: 2026-09-15 matched closes; SPX=SPY×9.947368; XSP=SPX/10. Basis may change intraday.
Snapshot decision: CALL SETUP CONFIRMED.
Show the snapshot decision and as-of time in a visible chart note. A stop line is not an entry trigger.
SPY confirmation: completed 15-minute close above frozen PM high for CALL or below frozen PM low for PUT. Checks: 08:45, 09:00, 09:15, 09:30 CT; stop after the first confirmation.
1-minute candles are viewing context only: no 1-minute entry trigger and no intrabar breakout confirmation.
Label stops/targets by CALL or PUT. They are conditional unless that side is confirmed in this snapshot; never draw both as active trades.
Only a current CALL/PUT SETUP CONFIRMED snapshot may show an entry arrow. WAIT, NO ENTRY, reference-only, incomplete or expired plans get reference levels only. Never infer another confirmation.
Draw underlying levels only, never option premiums or order instructions.
This check uses the candle ending Sep 16 09:00:00 CT. Only a new Compass message can confirm a later candle.
Morning checks finished for this session.
```
