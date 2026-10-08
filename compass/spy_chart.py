"""Copy-ready drawing instructions derived only from the frozen morning report."""
from .spy_brief import stamp
from .market import number
from .store import events
from sqlalchemy import select


def minimal_prompt(report):
    """Minimal level block for TradingView AI — just the prices, no instruction bloat.
    
    Josh 2026-10-08: the full prompt is way too much. Just need the levels to draw.
    """
    p = report
    lines = [f"Draw these SPY levels as horizontal rays:"]
    for r in p.get('chart_levels', []):
        spy = r.get('spy')
        if spy is not None:
            lines.append(f"{r['label']}: {spy:.2f}")
    # Add the plan triggers if available
    ctx = p.get('context', {})
    return '\n'.join(lines)


def prompt(report, now):
    p = report
    dated = now >= p.get('expires_at', 0)
    lines = [f"Compass {p['day']} {p.get('phase_label', p['phase'])} | as of {stamp(p['generated_at'])}",
             'Valid until ' + stamp(p.get('expires_at')) + '; reference only afterward.']
    if dated:
        lines.append('EXPIRED / REFERENCE ONLY: do not draw an active entry signal. Refresh the plan before trading.')
    lines += [
        'Draw these underlying-price levels on BOTH 1-minute and 15-minute charts (not monthly).',
        'Use ONLY the column matching the chart: SPY, SPX or XSP. For any other symbol, stop and ask. Never mix columns.',
        'Update or create one Compass group for this date; replace its levels with this snapshot and preserve all my other drawings. Reuse synced drawings across panes; do not duplicate.',
        'Use horizontal rays from the as-of time; do not backdate signals. Label every ray with its name and price.',
        'Keep each name paired with its listed price. If labels overlap, offset only their text with leader lines; never move or merge price levels.',
        'PM high/low: blue; prior H/L/C: gray; VWAP snapshot: orange dashed; stops: red dashed; targets: green dashed.',
        'VWAP here is a static snapshot, not a live VWAP indicator. Premarket levels are provisional before 09:30 ET and frozen afterward.',
        'All SPX/XSP levels are ESTIMATES: include EST in their labels. They are not independently confirmed index signals.',
        'SPY | SPX EST | XSP EST',
    ]
    for r in p.get('chart_levels', []):
        values = [f"{r[k]:.2f}" if r.get(k) is not None else '—' for k in ('spy', 'spx_estimate', 'xsp_estimate')]
        lines.append(r['label'] + ': ' + ' | '.join(values))
    if not p.get('chart_levels'):
        lines.append('No usable levels: draw nothing; do not invent prices.')
    m = p.get('mapping', {})
    if m.get('status') == 'available':
        lines.append(f"Estimate basis: {m['reference_day']} matched closes; SPX=SPY×{m['ratio']:.6f}; XSP=SPX/10. Basis may change intraday.")
    else:
        lines.append('SPX/XSP mapping unavailable: draw SPY only; do not approximate missing index levels.')
    lines += [
        'Snapshot decision: ' + p['decision'] + ('. Historical only.' if dated else '.'),
        'Show the snapshot decision and as-of time in a visible chart note. A stop line is not an entry trigger.',
        'SPY confirmation: completed 15-minute close above frozen PM high for CALL or below frozen PM low for PUT. Checks: 08:45, 09:00, 09:15, 09:30 CT; stop after the first confirmation.',
        '1-minute candles are viewing context only: no 1-minute entry trigger and no intrabar breakout confirmation.',
        'Label stops/targets by CALL or PUT. They are conditional unless that side is confirmed in this snapshot; never draw both as active trades.',
        'Only a current CALL/PUT SETUP CONFIRMED snapshot may show an entry arrow. WAIT, NO ENTRY, reference-only, incomplete or expired plans get reference levels only. Never infer another confirmation.',
        'Draw underlying levels only, never option premiums or order instructions.',
    ]
    candle = p.get('context', {}).get('confirmation_candle')
    if candle:
        lines.append('This check uses the candle ending ' + stamp(candle['end']) +
                     '. Only a new Compass message can confirm a later candle.')
    if p.get('next_check_at'):
        lines.append('Next Compass check: ' + stamp(p['next_check_at']))
    elif p.get('session_complete'):
        lines.append('Morning checks finished for this session.')
    if not p.get('context', {}).get('premarket_complete'):
        lines.append('Premarket coverage incomplete: label PM levels INCOMPLETE; not eligible for confirmation.')
    return '\n'.join(lines)


def companion(report):
    return {**report, 'status': 'spy_chart_prompt', 'parent_plan': report['id'],
            'id': report['id'] + ':chart'}


def delivery_payload(row, now):
    p = row['payload']
    # Minimal block: just the levels for TradingView AI (Josh 2026-10-08).
    # The full prompt with 30 lines of instructions is available via API.
    text = minimal_prompt(p)
    return {'content': '**SPY Levels · ' + p['day'] + ' ' + p.get('phase_label', p['phase'].upper()) + '**\n```\n' + text + '\n```',
            'username': 'Market Compass · SPY Charts', 'allowed_mentions': {'parse': []}}



def drawing_changes(previous, report):
    """Compare against the last queued drawing, not the last routine plan."""
    atr = number(previous.get('context', {}).get('atr14_daily')) or 0
    threshold = max(1.0, .15 * atr)
    def levels(p):
        return {('VWAP' if r['label'] in ('PM VWAP', 'RTH VWAP') else r['label']): r for r in p.get('chart_levels', [])
                if number(r.get('spy')) is not None}
    before, after = levels(previous), levels(report)
    changes = []
    ignored = ('First 15m close', 'Confirmation close')
    for name in before.keys() & after.keys():
        if name in ignored or name.startswith(('Apex #', 'GEX #')):
            continue
        old, new = before[name], after[name]
        # Compare all rendered columns in SPY-equivalent units.
        for column in ('spy', 'spx_estimate', 'xsp_estimate'):
            a, b = number(old.get(column)), number(new.get(column))
            scale = a / old['spy'] if a is not None and old['spy'] else None
            if a is not None and b is not None and scale and abs(b-a)/abs(scale) >= threshold-1e-8:
                changes.append(name + ' moved materially')
                break
    # Exposure rank changes alone must not cause a repeat of identical levels.
    for prefix in ('Apex #', 'GEX #'):
        a = [r['spy'] for k,r in before.items() if k.startswith(prefix)]
        b = [r['spy'] for k,r in after.items() if k.startswith(prefix)]
        if a and b and any(min(abs(x-y) for y in a) >= threshold-1e-8 for x in b):
            changes.append(prefix[:-2] + ' levels moved materially')
    if report.get('decision_state') == 'confirmed':
        side = 'Call' if report['decision'].startswith('CALL') else 'Put'
        for name in (side+' stop', side+' target'):
            if name in after and name not in before:
                changes.append(name+' became available for entry')
    if (previous.get('mapping', {}).get('status') != 'available'
            and report.get('mapping', {}).get('status') == 'available'):
        changes.append('SPX/XSP drawing estimates became available')
    return threshold, sorted(set(changes))


def prepare_drawing(db, c, report):
    """Daily durable outbox baseline; called in the same transaction as the plan."""
    key = 'spy-chart:last:'+report['day']
    previous = db.get(c, key)
    if previous is None:
        # Honor drawing messages queued by releases predating this policy.
        previous = c.execute(select(events.c.payload).where(
            events.c.kind=='alert', events.c.source=='spy_brief', events.c.symbol=='SPY',
            events.c.payload['status'].as_string()=='spy_chart_prompt',
            events.c.payload['day'].as_string()==report['day'])
            .order_by(events.c.id.desc()).limit(1)).scalar_one_or_none()
    usable = any(number(r.get('spy')) is not None for r in report.get('chart_levels', []))
    threshold, changes = drawing_changes(previous, report) if previous else (None, [])
    send = usable and (previous is None or bool(changes))
    report['drawing_update'] = dict(send=send,
        reason='initial_daily_drawing' if send and previous is None else
               'material_level_change' if send else 'no_material_change' if usable else 'no_usable_levels',
        threshold_spy=threshold, changes=changes,
        baseline_id=previous.get('id') if previous else None)
    if send:
        db.put(c, key, report)
    elif previous is not None and db.get(c, key) is None:
        db.put(c, key, previous)
    return send
