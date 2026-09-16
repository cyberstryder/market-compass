"""Copy-ready drawing instructions derived only from the frozen morning report."""
from .spy_brief import stamp


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
        'PM high/low: blue; prior H/L/C: gray; gamma/Apex/GEX: purple; VWAP snapshot: orange dashed; stops: red dashed; targets: green dashed.',
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
    text = prompt(p, now)
    # Rich description supports a complete, copyable code block in one second message.
    # The report bounds Apex/GEX rows and uses fixed labels; no truncation of prices.
    return {'content': '**TRADINGVIEW AI · message 2 of 2 · ' + p['day'] + ' ' + p.get('phase_label', p['phase'].upper()) + '**\nCopy the full block into your chart AI.',
            'embeds': [{'title': 'Draw on the 1-minute and 15-minute charts',
                        'description': '```text\n' + text + '\n```',
                        'footer': {'text': p.get('parent_plan', p.get('id', 'preview')) + ' · Event ' + str(row['id'])}}],
            'username': 'Market Compass · SPY Charts', 'allowed_mentions': {'parse': []}}
