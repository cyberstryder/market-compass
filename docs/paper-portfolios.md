# Separate simulated paper portfolios — September 18, 2026

Policy `paper-portfolios-v2` separates futures, portfolio 0DTE options, and the
existing stock/swing simulation. Each paper account retains the configured $300
daily realized loss limit, $100 maximum initial risk per entry, three open
positions, and configured entry-count limit (zero means unlimited). These are
separate simulated budgets, not a single real-money account or a combined $300
loss guarantee. Fill, fee, contract, signal, quote, and session gates are unchanged.

## Activation and preserved history

The leased engine initializes `paper_risk:v2:<risk day>:<portfolio>` atomically.
The existing exchange risk day and 17:00 Chicago rollover are retained. Before
initialization, retained legacy trades are assigned by saved asset; entries count
on their actual entry risk day, realized P&L on their actual exit risk day. Their
sum must reconcile with the old `risk:<day>` entries and realized balance to within
$0.000001. Unclassified trades, missing P&L, a totals mismatch, or a partial v2
ledger block admissions pending investigation. There is no fallback loss reset.
Exits continue to be managed even when admissions are blocked.

Opening balances, source trade IDs, the old ledger snapshot, reconciliation totals,
and initialization time are saved with each account. Original trade records and
combined ledger states are not rewritten during migration. Existing unresolved
positions retain their original admission policy and receive separate v2 exit
accounting metadata when they close. New entries and exits record their policy
and portfolio. Restarting does not re-import or double-count history. After
activation, only v2 account balances are updated; the old combined ledger is
explicitly historical, not the active loss gate. A software rollback requires an
explicit risk reconciliation; never run the legacy engine against frozen ledgers.

The September 18 preflight found ten closed futures trades totaling -$426 and no
option entries on the current risk day. If still reconciled at activation, futures
must remain loss-locked at -$426 and options begin at $0, without clearing losses.

## Research interpretation

This is a new portfolio admission policy, not a profitable-strategy claim. The
original September 17 zero-DTE definition remains frozen. V2 entries form a
separate cohort; September 18 is transition/development evidence. The v2 review
uses the first ten complete cash sessions beginning on or after September 21,
2026, after deployment, and only v2 option admissions. Report candidates,
admissions, exact rejection gates, unresolved positions, net P&L after saved costs,
and missingness. Do not pool with legacy combined-risk admissions or use this
policy change to restart the TM, Swing, SPY, setup, or futures-feed study windows.
Those independent observation studies continue under their existing protocols.
