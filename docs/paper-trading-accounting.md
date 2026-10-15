# Paper Trading Accounting — Current State & Unification Plan

**Date:** 2026-10-15  
**Status:** Documentation only. Full unification is a major refactor — DO NOT attempt without careful planning and testing.  
**Audit Reference:** Market Compass Project Audit, P1 "Trade-result validity and research integrity"

## Current Systems (5 separate modules)

### 1. ICT Futures Paper (`compass/ict_paper.py`)
- **Assets:** Futures micros (MNQ, MES, MGC, MCL, MYM, M2K, M6A, M6E)
- **Entry:** Market order at signal price (no slippage model)
- **Exit:** Stop/target evaluated on bar close; 2/2/1 scale-out (T1, T2, Trail)
- **Fees:** None modeled
- **Flatten:** 15:45 CT (futures session)
- **Data gap:** Positions marked `data_gap=True` if no fresh quote; excluded from P&L
- **P&L:** Calculated from entry/exit prices × contract multiplier × quantity

### 2. 0DTE Options Paper (`compass/zero_dte_paper.py`)
- **Assets:** 0DTE options (SPY, QQQ, etc.)
- **Entry:** Buy 1 contract at ASK (not mid, not last)
- **Exit:** Target/stop on premium, or 14:55 CT flatten (SPY) / 2:55pm CT (scanner)
- **Fees:** $0.65/contract (modeled)
- **Target:** Wall-aware (nearest Apex/GEX wall) or 2R fallback
- **Data gap:** GRACE=90s tolerance; >5min gaps mark unresolved
- **P&L:** (Exit premium - Entry premium) × 100 - fees

### 3. MU Paper (`compass/mu_paper.py`)
- **Assets:** MU options (Micron-specific)
- **Entry:** Similar to 0DTE (buy at ask)
- **Exit:** Premium-based stops/targets
- **Fees:** Modeled (same as 0DTE)
- **P&L:** Same as 0DTE

### 4. Trap Paper (`compass/trap_paper.py`)
- **Assets:** Trap patterns (specific setup)
- **Entry/Exit:** Pattern-specific
- **P&L:** Pattern-specific calculation

### 5. Paper Risk (`compass/paper_risk.py`)
- **Purpose:** Portfolio-level risk checks, not a trading system
- **Checks:** Max positions, daily loss (if configured), etc.

## Key Differences (Inconsistencies)

| Aspect | ICT Futures | 0DTE Options | MU Paper | Trap Paper |
|--------|-------------|--------------|----------|------------|
| Entry price | Signal price (market) | Ask | Ask | Pattern-specific |
| Slippage | None | None (ask = fill) | None | None |
| Fees | None | $0.65/contract | $0.65/contract | ? |
| Scale-out | 2/2/1 (T1/T2/Trail) | Single contract | Single contract | ? |
| Flatten time | 15:45 CT | 14:55/2:55pm CT | ? | ? |
| Data gap handling | data_gap flag, exclude from P&L | GRACE=90s, unresolved if >5min | Same as 0DTE | ? |
| Stop evaluation | Bar close | Premium tick | Premium tick | ? |

## Why This Matters

1. **P&L is not comparable across systems.** A +$100 in futures is not the same as +$100 in 0DTE because fees, slippage, and fill assumptions differ.
2. **Win rates are not comparable.** Futures 2/2/1 scale-out has different win/loss definitions than single-contract 0DTE.
3. **Risk is not comparable.** Futures has no fee drag; 0DTE has $0.65/contract fee. Futures can hold overnight (until 15:45); 0DTE flattens intraday.

## Unification Plan (Future Work)

### Phase 1: Document (DONE — this file)
- [x] Document current systems and their differences
- [x] Identify inconsistencies

### Phase 2: Canonical Event Model (NOT STARTED)
Define one event model for all paper trading:
- `Signal`: {symbol, side, entry_price, stop, target, timestamp, source}
- `Entry`: {signal_id, fill_price, quantity, fees, timestamp}
- `Exit`: {entry_id, exit_price, quantity, fees, reason (stop/target/flatten/data_gap), timestamp}
- `Outcome`: {entry_id, pnl, pnl_after_fees, win/loss, holding_time}

All systems should emit events in this format. Reporting should use the canonical model.

### Phase 3: Gradual Migration (NOT STARTED)
- Migrate one system at a time to the canonical model
- Start with the simplest (0DTE or MU), then futures, then trap
- Keep old code running in parallel until new code is verified
- Do NOT attempt a big-bang rewrite — too risky

### Phase 4: Unified Reporting (NOT STARTED)
- Single P&L calculation from canonical events
- Single win-rate calculation
- Comparable metrics across all strategies

## What NOT To Do

- **Do NOT rewrite all 5 modules at once.** This is a major refactor that would break working code (Futures works now).
- **Do NOT change fill assumptions without backtesting.** Changing from "ask" to "mid" or adding slippage would change all historical P&L.
- **Do NOT unify flatten times.** Futures (15:45 CT) and options (14:55 CT) have different session rules for good reasons.

## References
- Audit: `market_compass_project_audit_export.md`, P1 "Trade-result validity"
- Gemini: `gemini-code-1792095745363.md`, "Fragmented Paper Trading"
