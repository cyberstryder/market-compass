"""Cadence-scaled quote-gap tolerance for observation continuity checks.

A flat 15s rule was calibrated on index-option liquidity: SPY/QQQ contracts
print constantly, so a >15s hole really does mean a broken feed. Off-core
contracts (BABA, SHOP, HON, ...) routinely go 30-120s between prints in a
healthy market, and the flat rule closed those positions as observation_gap
minutes after entry (BABA 110C, 2026-10-20: quoted at entry, gap-exited at
+3 minutes on a silent stretch).

The tolerance for a symbol is derived from that symbol's own observed
inter-quote intervals: a hole only counts as a gap when it is long relative
to how the contract actually trades. With too few observed intervals the
legacy floor applies unchanged — an unproven cadence earns no grace, so a
missing path stays unresolved exactly as the flat rule judged it (the
suite encodes this: a later winning print must not resurrect a position
whose observation path had an unproven hole).
"""
import statistics

FLOOR = 15.0        # never tolerate less than the legacy flat rule
CAP = 300.0         # never tolerate more than 5 minutes of silence
MIN_SAMPLES = 3     # intervals required before cadence scaling engages
FACTOR = 3.0        # a gap = 3 missed prints at the proven cadence


def intervals_between(timestamps):
    """Positive gaps between consecutive timestamps (input need not be sorted)."""
    ts = sorted(t for t in timestamps if t is not None)
    return [b - a for a, b in zip(ts, ts[1:]) if b > a]


def scaled_tolerance(intervals, floor=FLOOR, cap=CAP):
    """Quote-silence tolerance in seconds for one symbol's observed cadence."""
    clean = [i for i in (intervals or []) if i and i > 0]
    if len(clean) < MIN_SAMPLES:
        return floor
    return min(cap, max(floor, statistics.median(clean) * FACTOR))
