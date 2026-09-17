"""Describe the scope of saved reports without inferring missing denominators."""


def age(now, stamp):
    return round(now-stamp, 1) if isinstance(stamp, (int, float)) else None


def section(name, scope, limit=None, truncated=None, observed=True, count=None):
    return dict(name=name, scope=scope, limit=limit, count=count,
        truncated=truncated if observed else None,
        status=('unobserved' if not observed else 'truncated' if truncated is True
                else 'complete_scope' if truncated is False else 'coverage_unknown'))


def coverage(key, data, supplements):
    now = data['asof']
    native = supplements.get('native_report', {})
    original = supplements.get('morning_report', {})
    sections, stamp, source_stamp = [], None, None
    states = {}
    basis = 'Counts describe received evidence, not all market opportunities.'
    if key in ('tm-flow', 'swing-flow-study', 'spy'):
        item = data.get({'tm-flow':'tm_study', 'swing-flow-study':'swing_study', 'spy':'spy_study'}[key], {})
        stamp = item.get('at')
        full = item.get('coverage') == 'full_window'
        sections = [section('Study totals', '30-day receipt window' if key != 'spy' else '30-day session window',
            truncated=False if full else None, observed=stamp is not None),
            section('Record detail', 'Paginated records; a page is not the cohort', 100,
                item.get('records_has_more'), observed=stamp is not None)]
        if key == 'tm-flow':
            source_stamp = data.get('matrix', {}).get('matrix:unusual_activity', {}).get('source_ts')
        basis = 'Prospective assessments, historical inventory and outcome checkpoints have separate denominators.'
    elif key in ('morning', 'morning-expirations', 'smoothers'):
        stamp = native.get('at')
        if key == 'morning':
            flags = original.get('truncated', {})
            sections.append(section('Original stock report', 'Latest matching signals and candidates; 100 each, API maximum 200',
                100, any(flags.values()) if flags else None, original.get('at') is not None,
                original.get('signals')))
        flags = native.get('smoothers_truncated' if key == 'smoothers' else 'morning_truncated', {})
        limits = {'native':100, 'source':500} if key == 'smoothers' else {
            'signals':100, 'candidates':100, 'receipts':40000, 'research_bars':50000, 'source_inventory':100}
        for name, limit in limits.items():
            sections.append(section('Native report: '+name.replace('_', ' '),
                'Selected week' if key == 'smoothers' else 'Latest matching records; clocks and source/native tapes remain separate',
                limit, flags.get(name), stamp is not None))
        states = (native.get('smoothers', {}) if key == 'smoothers' else
                  native.get('morning', {}).get('coverage', {}))
        basis = ('State counts describe the selected native week.' if key == 'smoothers' else
                 'Native stock coverage states count signals. Option slots, contracts and complete paired signals are different units.')
    elif key in ('intraday', 'futures'):
        item = data.get('setup_study', {})
        stamp = item.get('at')
        sections = [section('Setup totals', 'All stored trials in the 30-day window',
            item.get('limit'), False if item.get('coverage') == 'full_window' else item.get('truncated'), stamp is not None),
            section('Record detail', 'Combined stocks/futures preview; individual records paginated', 100,
                item.get('records_has_more'), stamp is not None)]
        basis = 'Asset-filtered totals use the complete saved summary; overlapping trials are not a portfolio.'
    elif key in ('option_ideas', 'swing_ideas'):
        item = data.get(key, {})
        stamp = now if 'counts' in item else None  # SQL counts computed for this response.
        sections = [section('Status totals', ('90' if key == 'swing_ideas' else '30')+'-day SQL window',
            truncated=False, observed=stamp is not None),
            section('Record detail', 'Latest 100 records'+(' plus all active' if key == 'swing_ideas' else '')+'; separate from status window',
                100, observed='records' in item, count=len(item['records']) if 'records' in item else None)]
        states = item.get('counts', {})
        basis = 'The detail query has no overflow flag; full-window SQL status counts remain independent of that preview.'
    elif key == 'secondary':
        item = data.get('secondary', {})
        stamp = item.get('report_at')
        window = item.get('window', {})
        sections = [section('Review totals', 'Selected window within the last 30 days', 5000,
            window.get('truncated'), stamp is not None, window.get('reviewed') if stamp is not None else None),
            section('Review detail', 'Latest 100 per source family', 100, observed='reviews' in item),
            section('Pending detail', 'Earliest deadlines; total queue size is not reported', 100,
                observed='pending' in item, count=len(item['pending']) if 'pending' in item else None)]
        states = item.get('counts', {}) if stamp is not None else {}
        basis = 'Verdicts are assessments, not completed return observations. Detail/pending overflow is unknown.'
    elif key == 'obsidian':
        item = data.get('obsidian', {})
        stamp = now if 'total_ideas' in item else None
        for name, count_key in (('ideas', 'total_ideas'), ('events', 'total_events')):
            total = item.get(count_key)
            sections.append(section(name.title()+' inventory', 'Lifetime SQL count', truncated=False,
                observed=total is not None, count=total))
            sections.append(section(name.title()+' detail', 'Latest records', 100,
                total > 100 if total is not None else None, name in item,
                len(item[name]) if name in item else None))
        sections.append(section('Active tracking', 'Unexpired contracts', 500,
            item.get('tracking', {}).get('truncated'), bool(item.get('tracking'))))
        basis = 'Inventory is complete within retained storage; observed premium marks have no terminal win/loss protocol.'
    elif key == 'zero-dte':
        audit = data.get('forward_acceptance', {})
        selection = audit.get('zero_dte', {})
        stamp = selection.get('at')
        sections = [section('Portfolio trades', 'Latest 100 across all instruments; option subset is not a 0DTE lifetime total', 100,
            observed='trades' in data, count=len(data['trades']) if 'trades' in data else None),
            section('Retry diagnostics', 'Last 24 hours; retained diagnostics only', 5000,
                selection.get('truncated'), stamp is not None),
            section('Notification skips', 'Last 24 hours; overlaps retry candidates', 1000,
                selection.get('notification_skips_truncated'), stamp is not None)]
        basis = 'No trade overflow flag is supplied. Historical risk state cannot be inferred from current portfolio capacity.'
    else:
        item = data.get('scanner', {})
        stamp = item.get('status', {}).get('at')
        sections = [section('Context inventory', 'Configured source responses only; vendor pagination can be partial',
            observed='feeds' in item, count=len(item['feeds']) if 'feeds' in item else None)]
        basis = 'Use per-feed source/receipt clocks in Feed health; a scanner heartbeat is not a source freshness clock.'
    return dict(report_at=stamp, report_age_seconds=age(now, stamp),
        source_age_seconds=age(now, source_stamp), sections=sections,
        states=states, basis=basis,
        complete_scope=bool(sections) and all(s['status']=='complete_scope' for s in sections))
