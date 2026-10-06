"""Analyst scorecards: measure what happened after each logged pick check.

Evidence only — no alerts, no auto-admission, no trades. Outcomes are
measured mechanically from daily bars *after* the check time, so there is
no lookahead: only bars with ts > check time are used.

Outcome rules (per check):
- entry: the check's entry, else its Compass spot, else the first daily
  close after the check. When the entry comes from that first close, the
  entry bar itself is excluded from measurement (you cannot trade a close
  and profit inside the same bar).
- target_hit: a daily high/low touched the target within the horizon *before*
  any invalidation touch.
- status: 'win' if the target was hit cleanly; 'loss' if the horizon completed
  without a clean touch, or the invalidation level was touched first (a bar
  touching both on the same session scores conservatively as
  invalidation-first); 'open' if fewer than horizon sessions have printed and
  neither level has resolved; 'unknown' when there is no usable entry or no
  bars at all.
- Without a target, a completed horizon resolves on the sign of the
  direction-adjusted horizon return; an incomplete horizon stays 'open'.
- mfe/mae and horizon return are direction-adjusted underlying moves in
  percent. They are not option returns or broker P&L.
"""

DEFAULT_HORIZON = 5


def _bars_after(db, c, symbol, at, limit):
    rows = db.recent(c, 'daily', symbol, limit=limit)
    bars = [r for r in rows if (r.get('ts') or 0) > at]
    return sorted(bars, key=lambda r: r['ts'])


def measure_outcome(db, c, check, now, horizon_sessions=DEFAULT_HORIZON):
    symbol = str(check.get('ticker') or '').upper()
    direction = str(check.get('direction') or '').lower()
    if direction not in ('long', 'short'):
        raise ValueError("direction must be 'long' or 'short'")
    at = check.get('at') or 0
    entry = check.get('entry')
    target = check.get('target')
    entry_source = 'given' if entry else None
    if entry is None:
        entry = check.get('spot')
        entry_source = 'spot' if entry else None

    bars = _bars_after(db, c, symbol, at, horizon_sessions + 2)
    if entry is None and bars:
        entry = (bars[0].get('payload') or {}).get('c')
        entry_source = 'first_close' if entry else None
    if entry is None or not bars:
        return {'status': 'unknown', 'target_hit': None, 'sessions_measured': 0,
                'horizon_sessions': horizon_sessions, 'mfe_pct': None,
                'mae_pct': None, 'horizon_return_pct': None, 'entry_used': entry,
                'entry_source': entry_source,
                'basis': 'No usable entry or no daily bars after the check.'}

    if entry_source == 'first_close':
        bars = bars[1:]
        if not bars:
            return {'status': 'unknown', 'target_hit': None, 'sessions_measured': 0,
                    'horizon_sessions': horizon_sessions, 'mfe_pct': None,
                    'mae_pct': None, 'horizon_return_pct': None,
                    'entry_used': entry, 'entry_source': entry_source,
                    'basis': 'Entry bar excluded; no later bars yet.'}
    bars = bars[:horizon_sessions]
    n = len(bars)
    sgn = 1 if direction == 'long' else -1

    def _num(p, key):
        try:
            return float((p or {}).get(key))
        except (TypeError, ValueError):
            return None

    highs = [_num(b.get('payload'), 'h') for b in bars]
    lows = [_num(b.get('payload'), 'l') for b in bars]
    closes = [_num(b.get('payload'), 'c') for b in bars]
    highs_f = [h for h in highs if h is not None]
    lows_f = [l for l in lows if l is not None]
    if not highs_f or not lows_f or closes[-1] is None:
        return {'status': 'unknown', 'target_hit': None, 'sessions_measured': n,
                'horizon_sessions': horizon_sessions, 'mfe_pct': None,
                'mae_pct': None, 'horizon_return_pct': None, 'entry_used': entry,
                'entry_source': entry_source,
                'basis': 'Bars after the check lack high/low/close data.'}
    highs, lows = highs_f, lows_f

    invalidation = check.get('invalidation')
    try:
        invalidation = float(invalidation) if invalidation not in (None, '') else None
    except (TypeError, ValueError):
        invalidation = None

    target_hit = None
    inval_hit_first = False
    if target:
        # First-touch bar indices, chronological. A bar that touches both
        # target and invalidation can't be ordered from daily bars, so it
        # scores conservatively as invalidation-first.
        if direction == 'long':
            tgt_idx = next((i for i, h in enumerate(highs) if h >= target), None)
            inv_idx = (next((i for i, l in enumerate(lows) if l <= invalidation), None)
                       if invalidation else None)
        else:
            tgt_idx = next((i for i, l in enumerate(lows) if l <= target), None)
            inv_idx = (next((i for i, h in enumerate(highs) if h >= invalidation), None)
                       if invalidation else None)
        target_hit = tgt_idx is not None
        inval_hit_first = inv_idx is not None and (tgt_idx is None or inv_idx <= tgt_idx)

    if direction == 'long':
        mfe = max(highs) / entry - 1
        mae = min(lows) / entry - 1
    else:
        mfe = 1 - min(lows) / entry
        mae = 1 - max(highs) / entry
    horizon_return = (closes[-1] / entry - 1) * sgn
    complete = n >= horizon_sessions

    if target_hit and not inval_hit_first:
        status = 'win'
    elif target:
        # Invalidation-first resolves immediately as a loss: the setup
        # failed, no need to wait out the horizon.
        status = 'loss' if (complete or inval_hit_first) else 'open'
    else:
        status = ('win' if horizon_return > 0 else 'loss') if complete else 'open'

    if target_hit and not inval_hit_first:
        basis = 'Target hit within %d sessions.' % horizon_sessions
    elif inval_hit_first:
        basis = 'Invalidation hit before the target.'
    elif target:
        basis = 'No target touch in %d of %d sessions.' % (n, horizon_sessions)
    else:
        basis = ('Direction-adjusted underlying move over %d of %d sessions; not option P&L.'
                 % (n, horizon_sessions))

    return {'status': status, 'target_hit': target_hit and not inval_hit_first,
            'invalidation_hit_first': inval_hit_first,
            'sessions_measured': n,
            'horizon_sessions': horizon_sessions,
            'mfe_pct': round(mfe * 100, 2), 'mae_pct': round(mae * 100, 2),
            'horizon_return_pct': round(horizon_return * 100, 2),
            'entry_used': entry, 'entry_source': entry_source,
            'basis': basis}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def planned_r(check, entry_used=None):
    """Planned risk/reward multiple from entry/target/invalidation.

    Long: (target - entry) / (entry - invalidation).
    Short: (entry - target) / (invalidation - entry).
    Uses the effective entry (entry_used when the check had none) so the
    multiple reflects the price the setup could actually have gotten.
    Returns None when the levels don't define a positive risk and reward.
    """
    try:
        entry = float(entry_used if entry_used is not None else check.get('entry'))
        target = float(check.get('target'))
        inv = float(check.get('invalidation'))
    except (TypeError, ValueError):
        return None
    direction = str(check.get('direction') or '').lower()
    if direction == 'long':
        risk, reward = entry - inv, target - entry
    elif direction == 'short':
        risk, reward = inv - entry, entry - target
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 2)


def realized_r(outcome, pr):
    """Realized R for a resolved setup: +planned on a clean target hit,
    -1 (full planned risk) on a loss. None when unresolved or unmeasurable."""
    if pr is None:
        return None
    status = (outcome or {}).get('status')
    if status == 'win':
        return pr
    if status == 'loss':
        return -1.0
    return None


def scorecard(db, c, now, limit=100, horizon_sessions=DEFAULT_HORIZON):
    rows = db.recent(c, 'pick_check', limit=limit)
    checks, groups = [], {}
    for row in rows:
        p = row.get('payload') or {}
        check = {'ticker': p.get('ticker'), 'direction': p.get('direction'),
                 'entry': p.get('entry'), 'target': p.get('target'),
                 'invalidation': p.get('invalidation'),
                 'spot': p.get('spot'), 'source': p.get('source') or 'unknown',
                 'pattern': p.get('pattern'),
                 'evidence_score': p.get('evidence_score'),
                 'at': p.get('at') or row.get('ts')}
        try:
            outcome = measure_outcome(db, c, check, now, horizon_sessions)
        except ValueError:
            outcome = {'status': 'unknown', 'target_hit': None,
                       'sessions_measured': 0, 'horizon_sessions': horizon_sessions,
                       'mfe_pct': None, 'mae_pct': None,
                       'horizon_return_pct': None, 'entry_used': None,
                       'entry_source': None, 'basis': 'Unusable check record.'}
        pr = planned_r(check, outcome.get('entry_used'))
        rr = realized_r(outcome, pr)
        full = {**check, 'outcome': outcome, 'planned_r': pr, 'realized_r': rr}
        checks.append(full)
        g = groups.setdefault(check['source'],
                              {'checks': [], 'resolved': []})
        g['checks'].append((full, outcome))
        if outcome['status'] in ('win', 'loss'):
            g['resolved'].append((full, outcome))

    by_source = []
    for source, g in sorted(groups.items()):
        wins = sum(1 for _, o in g['resolved'] if o['status'] == 'win')
        losses = sum(1 for _, o in g['resolved'] if o['status'] == 'loss')
        rrs = [c_.get('realized_r') for c_, o in g['resolved']
               if c_.get('realized_r') is not None]
        by_source.append({
            'source': source,
            'checks': len(g['checks']),
            'wins': wins, 'losses': losses,
            'open': sum(1 for _, o in g['checks'] if o['status'] == 'open'),
            'unknown': sum(1 for _, o in g['checks'] if o['status'] == 'unknown'),
            'hit_rate': round(wins / (wins + losses), 3) if wins + losses else None,
            'avg_horizon_return_pct': _mean([o['horizon_return_pct'] for _, o in g['resolved']]),
            'avg_mfe_pct': _mean([o['mfe_pct'] for _, o in g['resolved']]),
            'avg_planned_r': _mean([c_.get('planned_r') for c_, o in g['resolved']]),
            'expectancy_r': round(sum(rrs) / len(rrs), 2) if rrs else None,
            'total_r': round(sum(rrs), 2) if rrs else None,
            'avg_evidence_score_win': _mean([c_['evidence_score'] for c_, o in g['resolved']
                                             if o['status'] == 'win']),
            'avg_evidence_score_loss': _mean([c_['evidence_score'] for c_, o in g['resolved']
                                              if o['status'] == 'loss']),
        })

    # Pattern-level slice: which setup patterns are useful and which aren't.
    # Only checks that carry a pattern (e.g. Flash Agentic cards) contribute.
    by_pattern = []
    pgroups = {}
    for c_, o in [(c_, o) for g in groups.values() for c_, o in g['checks']]:
        if not c_.get('pattern'):
            continue
        pg = pgroups.setdefault((c_['source'], c_['pattern']), [])
        pg.append((c_, o))
    for (source, pattern), rows in sorted(pgroups.items()):
        resolved = [(c_, o) for c_, o in rows if o['status'] in ('win', 'loss')]
        wins = sum(1 for _, o in resolved if o['status'] == 'win')
        losses = len(resolved) - wins
        rrs = [c_.get('realized_r') for c_, o in resolved
               if c_.get('realized_r') is not None]
        by_pattern.append({
            'source': source, 'pattern': pattern,
            'checks': len(rows), 'wins': wins, 'losses': losses,
            'open': sum(1 for _, o in rows if o['status'] == 'open'),
            'hit_rate': round(wins / (wins + losses), 3) if wins + losses else None,
            'avg_horizon_return_pct': _mean([o['horizon_return_pct'] for _, o in resolved]),
            'avg_planned_r': _mean([c_.get('planned_r') for c_, o in resolved]),
            'expectancy_r': round(sum(rrs) / len(rrs), 2) if rrs else None,
            'total_r': round(sum(rrs), 2) if rrs else None,
            'r_count': len(rrs),
        })
    return {'asof': now, 'horizon_sessions': horizon_sessions,
            'by_source': by_source, 'by_pattern': by_pattern, 'checks': checks}
