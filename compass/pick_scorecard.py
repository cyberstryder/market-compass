"""Analyst scorecards: measure what happened after each logged pick check.

Evidence only — no alerts, no auto-admission, no trades. Outcomes are
measured mechanically from daily bars *after* the check time, so there is
no lookahead: only bars with ts > check time are used.

Outcome rules (per check):
- entry: the check's entry, else its Compass spot, else the first daily
  close after the check. When the entry comes from that first close, the
  entry bar itself is excluded from measurement (you cannot trade a close
  and profit inside the same bar).
- target_hit: a daily high/low touched the target within the horizon.
- status: 'win' if the target was hit; 'loss' if the horizon completed
  without a touch; 'open' if fewer than horizon sessions have printed;
  'unknown' when there is no usable entry or no bars at all.
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
    highs = [h for h in highs if h is not None]
    lows = [l for l in lows if l is not None]
    if not highs or not lows or closes[-1] is None:
        return {'status': 'unknown', 'target_hit': None, 'sessions_measured': n,
                'horizon_sessions': horizon_sessions, 'mfe_pct': None,
                'mae_pct': None, 'horizon_return_pct': None, 'entry_used': entry,
                'entry_source': entry_source,
                'basis': 'Bars after the check lack high/low/close data.'}

    target_hit = None
    if target:
        target_hit = any(h >= target for h in highs) if direction == 'long' \
            else any(l <= target for l in lows)

    if direction == 'long':
        mfe = max(highs) / entry - 1
        mae = min(lows) / entry - 1
    else:
        mfe = 1 - min(lows) / entry
        mae = 1 - max(highs) / entry
    horizon_return = (closes[-1] / entry - 1) * sgn
    complete = n >= horizon_sessions

    if target_hit:
        status = 'win'
    elif target:
        status = 'loss' if complete else 'open'
    else:
        status = ('win' if horizon_return > 0 else 'loss') if complete else 'open'

    return {'status': status, 'target_hit': target_hit, 'sessions_measured': n,
            'horizon_sessions': horizon_sessions,
            'mfe_pct': round(mfe * 100, 2), 'mae_pct': round(mae * 100, 2),
            'horizon_return_pct': round(horizon_return * 100, 2),
            'entry_used': entry, 'entry_source': entry_source,
            'basis': ('Target hit within %d sessions.' % horizon_sessions
                      if target_hit else
                      'No target touch in %d of %d sessions.' % (n, horizon_sessions)
                      if target else
                      'Direction-adjusted underlying move over %d of %d sessions; not option P&L.'
                      % (n, horizon_sessions))}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def scorecard(db, c, now, limit=100, horizon_sessions=DEFAULT_HORIZON):
    rows = db.recent(c, 'pick_check', limit=limit)
    checks, groups = [], {}
    for row in rows:
        p = row.get('payload') or {}
        check = {'ticker': p.get('ticker'), 'direction': p.get('direction'),
                 'entry': p.get('entry'), 'target': p.get('target'),
                 'spot': p.get('spot'), 'source': p.get('source') or 'unknown',
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
        checks.append({**check, 'outcome': outcome})
        g = groups.setdefault(check['source'],
                              {'checks': [], 'resolved': []})
        g['checks'].append((check, outcome))
        if outcome['status'] in ('win', 'loss'):
            g['resolved'].append((check, outcome))

    by_source = []
    for source, g in sorted(groups.items()):
        wins = sum(1 for _, o in g['resolved'] if o['status'] == 'win')
        losses = sum(1 for _, o in g['resolved'] if o['status'] == 'loss')
        by_source.append({
            'source': source,
            'checks': len(g['checks']),
            'wins': wins, 'losses': losses,
            'open': sum(1 for _, o in g['checks'] if o['status'] == 'open'),
            'unknown': sum(1 for _, o in g['checks'] if o['status'] == 'unknown'),
            'hit_rate': round(wins / (wins + losses), 3) if wins + losses else None,
            'avg_horizon_return_pct': _mean([o['horizon_return_pct'] for _, o in g['resolved']]),
            'avg_mfe_pct': _mean([o['mfe_pct'] for _, o in g['resolved']]),
            'avg_evidence_score_win': _mean([c_['evidence_score'] for c_, o in g['resolved']
                                             if o['status'] == 'win']),
            'avg_evidence_score_loss': _mean([c_['evidence_score'] for c_, o in g['resolved']
                                              if o['status'] == 'loss']),
        })
    return {'asof': now, 'horizon_sessions': horizon_sessions,
            'by_source': by_source, 'checks': checks}
