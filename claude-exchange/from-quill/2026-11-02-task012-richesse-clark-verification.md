# Task 012 addendum verification — Richesse and Clark (Quill, 2026-11-02)

Verified against the desk copies in `from-claude/2026-11-02-hurricane-v3/`,
which are **byte-identical to Josh's originals** on Quill's side
(md5: richesse `112c8ca4f859a3393a18d9d5e5b67678`, 1,639 lines;
clark `90297dcbcc269b6dd290151c31543cb2`, 2,004 lines — Claude's line
counts exact). Both files compile (`py_compile` clean). Nothing was run
live; neither file can even start its live path here — both import
`hurricane_cmdlog` unconditionally and none of `hurricane_cmdlog.py`,
`hurricane_heads.py`, `broker_hub.py`, `hurricane_hotkeys.py` are on the
desk or in Josh's drop folder, so Claude's "not attached, not audited"
stands exactly.

## §1 — No fill fix applied: CONFIRMED in substance

- Instant fill at `lim`, no touch/fill check, `fill_delay_secs=1`
  hard-coded: richesse backtest `:1131-1135`, clark `:1168-1172`;
  live paths richesse `:1455`, clark `:1707` (same values).
- Every sell ceil-rounded: richesse `_monitor` `:938` and EOD `:1169`;
  clark `_monitor` `:975` and EOD `:1206`.
- `OSC_PROFIT` books `exit_target` on `curr>=target or high>=target`
  (richesse `:883-884`, clark `:920-921`) — a touch, no volume condition.
- Float site intact in both (`rup=math.ceil(bar_open/tick)*tick`,
  richesse `:802`, clark `:839`). Re-checked the arithmetic: float64
  `ceil(0.30/0.05)=6`; a float32-sourced 0.30 (`0.30000001192…`) gives 7 —
  same position as the 2026-11-02 Task 012 verification.
- **Precision correction (citations only, conclusions unaffected):**
  Claude's §1 line refs are Clark's numbers applied to both files. In
  richesse, `:1169-1172` is the EOD ceil block, not entry creation
  (`:1131-1135`), and `:975` sits inside the `_trade()` record builder,
  not the monitor (ceil is `:938`). Clark's refs are exact as cited.

## §2 — Richesse SPX cone / live divergence: CONFIRMED

- `spx_cone` is constructed only in `LiveMarsEngine` (`:1317`); the
  backtest class `HurricaneV3` never constructs it. `_entry` reads it via
  `getattr(self,'spx_cone',None)` (`:738-745`), so in backtest
  `cone_blended` stays 0.5 and `dual_agree` (requires `>0.50` or `<0.50`
  plus `spread_ticks>=min`) can never be true. Backtest ≠ live signal:
  confirmed.
- Live feeds the cone with `tick_1s(new_spx, new_spx, new_spx)` in
  `update_spx` (`:1371`) and with the strike±premium proxy in `on_bar`
  (`:1385-1396`; Claude cited `:1399-1411`). Confirmed — a proxy, not SPX.
- Live `on_bar` creates the position at `po['lim']` and logs
  `entry_filled(dbid, po['lim'])` (`:1453-1470`; Claude cited
  `:1403-1421`). One nuance Claude's wording skips: a CmdLog
  `entry_sent` conviction gate (`:1450`) precedes creation and can veto —
  but it observes no price or touch; it is not a fill check. The shadow
  P&L assuming fills stands as Claude described.
- `StreamingPFMACD` flushes at 6 *records* (`tick_1s` `:1276-1281`,
  `len(self._buf)>=6`), fed per option bar across strikes in the live
  path. Confirmed (Claude cited `:1263-1267`).

## §3 — Clark: CONFIRMED, with two corrections and one addition

Confirmed as cited: `ClarkState.quotes` per-strike bid/ask/size
(`:1288-1298`); banner verbatim at `:1963` ("cbbo-1s recording only —
gates nothing"); quotes flow to `ClarkBook` samples
(`clark_samples.csv` via `dump_csv`) and to OI-VWMACD, **never to fill
pricing** — `_entry`/`_monitor` price off the trade tape only. Band D
$1.05–$3.00 (`:311` max_bid 3.00, `:382` band bounds), $300/head cap in
sizing (`:579`). Commission $1.55/fill (`:325`) = $3.10 RT vs richesse's
$0.65/fill = $1.30 RT — Claude's "+$1.80/trade" arithmetic is exact.
`--live` block (`:1966-1980` as cited): `BrokerHub(IBKRPort(port=a.port,
market_exits=not a.limit_exits …))` + `HeadRunner`; `--port` default
7497 = TWS paper (`:1897`); `market_exits=True` is the default
configuration (`--limit-exits` off).

- **Correction 1 — the gate flag is `--oivw`, not `--oivw-gate`.**
  `main_live` sets `cfg.oivw_enabled = a.oivw` (`:1948`), where
  `--oivw` is `store_true` (`:1884`), so the live runner is
  observation-only by default as Claude said. But the config *default*
  is `oivw_enabled=True` (`:384`), and in `_entry` (`:760-763`) a ready
  OIVWMACD **does** gate when enabled. Net: same practical conclusion
  for the live entry point, different mechanism than stated. Note the
  same cbbo feed that "gates nothing" (ClarkBook) also feeds OIVWMACD,
  which can gate under `--oivw` — direction only, still never pricing.
- **Correction 2 / addition — Clark is NOT "Richesse + three things" on
  the signal, and its own header claim is false.** Clark's header says
  the strategy section is "byte-identical to
  hurricane_v3_mars_richesse.py". It is not: a full diff shows Clark
  **deletes Richesse's SPX cone entirely** (no `spx_cone` anywhere in
  the file) and restores the original Mars dual cone — `cone_blended`
  from the per-strike slow cone, `dual_agree` = slow and fast cones on
  the same side of 0.50, `best_ceil=min(slow,fast)` — byte-for-byte the
  Mars original's cone block. Consequences: (a) the §2 backtest/live
  cone divergence is Richesse-only; Clark's live and backtest compute
  the same cone signal; (b) any Richesse backtest result is not
  evidence about Clark either — the two "newer builds" do not share a
  direction signal. Claude's addendum does not flag this.

## Recommendations: CONCURRED, scope unchanged

1. No `--live` (paper or real) until an honest-fill run is read —
   concurred. Nothing in either build changes the fill question.
2. Pricing Clark's shadow fills from its own cbbo stream is the right
   fix shape; Claude writing it stays gated on the helper modules
   actually arriving (they are required just to import the live path).
3. Unify backtest/live signal — concurred, and per Correction 2 this
   now spans three signal variants (Mars dual cone, Richesse SPX cone,
   Clark dual cone + optional OIVW gate), not two.

Quill's frozen settling test (Venus/Earth/Mars, 20 Oct sessions,
executable-quote fills) is unchanged by this drop: Richesse/Clark are
not in the frozen registration and would need a second one. SPX stays
off for Compass live books. Nothing here needs a production change;
the only open item remains Josh's call on the catalog/run route from
the original Task 012 memo.
