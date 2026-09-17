"""Read-only inventory of research capabilities and their recorded evidence.

Counts keep the source report's window. Collection, assessment, measured outcomes
and notification delivery are deliberately separate; no readiness is inferred
from a running process, a connected webhook or a nonempty vendor feed.
"""
from .instruments import future_root
from .flow_recovery import freshness as flow_freshness
from .market import day

VERSION = 'research-admin-v1'


def total(values):
    return sum(v for v in values.values() if isinstance(v, (int, float)))


def activity(worker, now, enabled=None, limit=180):
    if enabled is False:
        return 'disabled'
    stamp = worker.get('at')
    if stamp is None:
        return 'awaiting_evidence'
    # The shared-state response reads several tables while workers keep writing.
    # A heartbeat can land just after the response's initial clock. This grace
    # applies only to activity labels, never to market-data entry eligibility.
    return 'observing' if -5 <= now-stamp <= limit else 'stale'


def metric(label, value):
    return {'label': label, 'value': value}


def build(data, supplements, policy):
    now = data['asof']
    projects = data.get('projects', {})
    native = projects.get('native_programs', {})
    morning = native.get('morning', {})
    smoothers = native.get('smoothers', {})
    native_report = supplements.get('native_report', {})
    source_report = supplements.get('morning_report', {})
    source_projects = {p['project']: p for p in projects.get('projects', [])}
    delivery = data.get('delivery', {})
    routes = {r['route']: r for r in delivery.get('routes', [])}
    rows = []

    def add(id, name, family, stage, collection, assessment, outcomes, metrics,
            window, tab, *, route=None, alert_policy='', owner='Compass',
            checked_at=None, issues=(), next_step='', version=None, measured=None):
        dest = routes.get(route, {})
        health = dest.get('health', {})
        problems = list(issues)
        if stage == 'stale':
            problems.append('The last worker/report observation is stale; check Feed health.')
        if dest.get('pending', 0):
            problems.append('Messages are waiting for Discord confirmation.')
        if health.get('status') in ('error', 'blocked', 'not_configured'):
            problems.append('The alert destination needs attention.')
        rows.append(dict(id=id, name=name, family=family, stage=stage,
            collection=collection, assessment=assessment, outcomes=outcomes,
            metrics=metrics, window=window, href='/#'+tab, checked_at=checked_at,
            issues=problems, needs_attention=bool(problems) or stage in
                ('awaiting_evidence', 'not_built', 'disabled', 'stale'),
            next_step=next_step, version=version, measured=measured,
            alerts={'policy': alert_policy, 'owner': owner, 'route': route,
                'channel': dest.get('channel'), 'destination': dest.get('destination_mode'),
                'health': health.get('status', 'unobserved'),
                'pending': dest.get('pending'),
                'last_confirmed_at': (dest.get('last_confirmation') or {}).get('at')}))

    flow = data.get('matrix', {}).get('matrix:unusual_activity', {})
    freshness = flow.get('flow_freshness', {})
    recovery = flow.get('recovery', {})
    tm=data.get('tm_study',{})
    tm_counts=tm.get('counts',{})
    tm_inventory=tm.get('inventory',{})
    flow_issues=[] if tm.get('version') else ['The TM receipt study has not reported yet.']
    if tm_inventory.get('unregistered_records',0):flow_issues.append('Historical inventory registration is still catching up.')
    if tm_counts.get('insufficient_data',0):flow_issues.append('Some new records lack inputs for a complete Compass score; inspect coverage.')
    if tm.get('overdue_checkpoints',0):flow_issues.append('Due price checkpoints are waiting for the study worker.')
    if tm.get('version') and activity(tm.get('worker',{}),now)!='observing':
        flow_issues.append('The TM study worker has no current heartbeat; check Feed health.')
    if freshness and not freshness.get('eligible_for_live_confirmation'):
        flow_issues.append('Flow is not eligible for current entry confirmation: '+freshness.get('status', 'unknown')+'.')
    if flow.get('limited') or recovery.get('estimated_gap', 0):
        flow_issues.append('Vendor-page recovery is incomplete; collection is partial.')
    add('tm-flow', 'TraderMatrix unusual options', 'Flow research',
        activity({'at': flow.get('received')}, now, bool(flow.get('received')) or policy.get('matrix'), 120),
        'Returned API records are stored by vendor ID; repeated IDs update the saved record and retain correction events. The feed requests $50k+ activity.',
        'New records freeze TM score and a separate versioned Compass rubric at first receipt processing. Missing inputs remain incomplete; older inventory is separate.',
        'Underlying checkpoints at 15/60 trading minutes and session closes compare TM-only, Compass-only and combined selection. They are not option returns.',
        [metric('Unique records stored today', flow.get('unique_rows')),
         metric('Vendor count in latest response', flow.get('total')),
         metric('Latest trade age (seconds)', freshness.get('event_age')),
         metric('Estimated missing IDs', recovery.get('estimated_gap')),
         metric('Prospective assessments, 30d',tm_counts.get('prospective')),
         metric('Complete Compass scores, 30d',tm_counts.get('scored'))],
        'Feed counts: session '+str(flow.get('day') or day(now))+'; study: full 30-day receipt window plus separate lifetime inventory.', 'tm-study',
        route='unusual_options', alert_policy='Enabled for qualifying setups when the scanner is on. Requires score 85+, $100k+, flow age ≤2 minutes and a price breakout.' if policy.get('scanner') else 'Scanner disabled; no new setup alerts.',
        checked_at=flow.get('received'), issues=flow_issues,
        next_step='Verify first-receipt coverage, then review later-session matched outcomes by score, delay and expiry. Keep the rubric fixed during evaluation.',
        version=tm.get('version'),measured=sum(r.get('count',0) for r in tm.get('outcome_counts',[]) if r.get('horizon')=='60m' and r.get('origin')=='prospective' and r.get('status')=='completed'))

    morning_issues = []
    if any(source_report.get('truncated', {}).values()):
        morning_issues.append('The current stock report is limited to its most recent matching records.')
    morning_issues.append('Opening-session intake and delivery reconciliation remain separate from strategy performance.')
    add('morning', 'Morning Algo', 'Original programs', activity(morning, now),
        'Original signals and research imports are retained alongside independent native signals, bars and option samples.',
        'Versioned stock-exit comparisons and a separate Compass secondary verdict; the original alert is not rewritten.',
        'Stock checkpoints, sampled option observations, missing-data coverage and native/source discrepancies are available in the reports.',
        [metric('Original records stored', source_projects.get('morning', {}).get('record_count')),
         metric('Native signals stored', morning.get('signals')),
         metric('Native option sample slots', morning.get('samples')),
         metric('Signals in stock report', source_report.get('signals'))],
        'Stored signal/sample inventory plus the latest bounded stock report; sample slots are not proof of usable quotes.', 'projects',
        owner=native.get('morning_sender', 'unobserved'),
        alert_policy='The displayed sender owns official alerts; research comparisons are separate from that delivery policy.',
        checked_at=morning.get('at'), issues=morning_issues,
        next_step='Review stock/option coverage, cost-adjusted results and matched-session delivery evidence.', version=morning.get('version'))

    expiration = native_report.get('expiration_comparison', {})
    pairs = expiration.get('pairs', [])
    paired = next((p.get('paired') for p in pairs if p.get('left') == 'baseline' and p.get('right') == 'zero_dte'), None)
    expiration_issues = []
    if paired in (None, 0):
        expiration_issues.append('No complete current-contract versus 0DTE comparison cohort is recorded in this report yet.')
    if any(native_report.get('morning_truncated', {}).values()):
        expiration_issues.append('The native report has a bounded or incomplete section; see the detailed coverage.')
    add('morning-expirations', 'Morning option expiration comparison', 'Comparison studies',
        activity({'at': native_report.get('at')}, now, limit=300),
        'Forward samples for the current contract, 0DTE and 1–3 DTE are saved separately. Some variants share the same contract.',
        'Compares spread, fees, quote quality and fixed exits at 5, 15, 30 and 60 minutes. No winning expiration has been selected automatically.',
        'Matched entry/exit quote times and a complete common cohort are required. Missing observations stay excluded; best observed exits are hindsight.',
        [metric('Native 0DTE tracks stored', morning.get('expiration_tracks')),
         metric('Native 0DTE sample slots', morning.get('expiration_samples')),
         metric('Signals with comparison inputs', expiration.get('collected')),
         metric('Complete paired signals', paired)],
        'Native report: latest 100 signals by default. Counts of tracks/sample slots are stored inventory, not completed comparisons.', 'projects',
        alert_policy='Research only; does not change Morning contracts or official alerts.', checked_at=native_report.get('at'),
        issues=expiration_issues, next_step='Accumulate complete forward pairs and compare costs and fixed exits on the same signals.',
        version=expiration.get('version') or morning.get('expiration_study'), measured=paired)

    sw_workers = smoothers.get('workers', {})
    sw_at = max((w.get('at') or 0 for w in sw_workers.values()), default=0) or None
    sw_issues = ['A full native weekly cycle and delivery comparison are required before retiring the original service.']
    if smoothers.get('errors'):
        sw_issues.append('The current native weekly run has recorded errors.')
    add('smoothers', 'Smoothers weekly', 'Original programs', activity({'at': sw_at}, now),
        'Weekly source signals and native observations are retained. Configuration ownership and revisions are shown in Connected projects.',
        'Weekly selection and quality tiers are compared with the original, with target checks and option valuations kept separate.',
        'Weekly target states, premium observations and unresolved paths are stored; stock target hits are not realized option returns.',
        [metric('Original records stored', source_projects.get('smoothers', {}).get('record_count')),
         metric('Configurations stored', smoothers.get('config_count')),
         metric('Enabled tickers', smoothers.get('enabled_tickers')),
         metric('Native weekly records', total(smoothers['signals']) if 'signals' in smoothers else None)],
        'Native week '+str(smoothers.get('week') or 'not observed')+'; original record count spans stored history.', 'projects',
        owner=smoothers.get('sender', 'unobserved'), alert_policy='Weekly official alerts remain under the displayed owner.',
        checked_at=sw_at, issues=sw_issues, next_step='Finish the complete weekly comparison and review unmatched records.',
        version=str(smoothers.get('config_revision')) if smoothers.get('config_revision') is not None else None)

    for key, name, tab, route, days, description in (
        ('swing_ideas', 'Swing Ideas', 'swing-ideas', 'swing', 90, 'Daily and weekly price setups with classified 14–60 DTE flow; fresh quotes are required for entry.'),
        ('option_ideas', 'Intraday Options Ideas', 'option-ideas', 'options_ideas', 30, 'Intraday price setups select eligible 1–21 DTE contracts; option expiry is not the holding period.')):
        item = data.get(key, {})
        counts = item.get('counts', {})
        issues = []
        if counts.get('unresolved'):
            issues.append('Some observation paths are unresolved and excluded from win/loss results.')
        if key == 'swing_ideas':
            issues.append('Only admitted swings receive option tracking; technical candidates without flow are saved without a full rejected-candidate outcome study.')
            if flow and not flow_freshness(flow, now, 300)['eligible_for_live_confirmation']:
                issues.append('Current flow timing prevents new swing confirmation.')
        add(key, name, 'Independent ideas', activity(item.get('worker') or {}, now, item.get('enabled')),
            description, 'Rule-based qualification with frozen evidence; no independently calibrated probability score.',
            'Admitted option observations track bid/ask returns, modeled fees, best/worst observed moves and exit reasons.',
            [metric('Idea records stored', total(counts) if 'counts' in item else None),
             metric('Pending / open', counts.get('pending', 0)+counts.get('open', 0) if 'counts' in item else None),
             metric('Closed outcomes', counts.get('closed', 0) if 'counts' in item else None),
             metric('Unresolved paths', counts.get('unresolved', 0) if 'counts' in item else None)],
            str(days)+'-day status totals; detailed pages show bounded recent records. Excluded records are not trades.', tab,
            route=route, alert_policy='Qualified ideas and management alerts enabled.' if policy.get(route+'_alerts') else 'Idea notifications disabled.',
            checked_at=(item.get('worker') or {}).get('at'), issues=issues,
            next_step='Review coverage and outcomes by setup and direction before choosing rules.',
            version=(item.get('worker') or {}).get('version'), measured=counts.get('closed', 0) if 'counts' in item else None)

    study = data.get('setup_study', {})
    for futures, id, name, route in ((False, 'intraday', 'Stock / ETF setup research', 'intraday'), (True, 'futures', 'Futures setup research', 'futures')):
        groups = [g for g in study.get('groups', []) if bool(future_root(g.get('symbol', ''))) == futures]
        stats = {k: sum(g.get(k, 0) for g in groups) for k in ('total', 'closed', 'wins', 'unresolved')}
        issues = ['Some paths are unresolved; missing observations are not counted as losses.'] if stats['unresolved'] else []
        if study.get('truncated'):
            issues.append('The setup report reached its 10,000-record limit; these are bounded cohort results.')
        add(id, name, 'Setup studies', activity(study.get('worker') or {}, now, study.get('enabled')),
            'Qualifying price setups and qualifying cooldown candidates receive separate experiments, independent of portfolio entry limits.',
            'Versioned setup rules; '+('market-state and entry-variant comparisons are also recorded.' if futures else 'TM/exposure context is supporting evidence when available.'),
            'Underlying/instrument target, stop, modeled net result, duration and favorable/adverse excursion. These are not option returns.',
            [metric('Setup trials', stats['total'] if 'groups' in study else None),
             metric('Closed outcomes', stats['closed'] if 'groups' in study else None),
             metric('Positive modeled outcomes', stats['wins'] if 'groups' in study else None),
             metric('Unresolved paths', stats['unresolved'] if 'groups' in study else None)],
            str(study.get('window_days', 30))+'-day report; '+('all stored trials in this window; individual records paginated in groups of 100.' if study.get('coverage') == 'full_window' else 'saved summary coverage has not yet been verified as complete.')+' Overlapping trials are not portfolio returns.', 'setup-study',
            route=route, alert_policy='Setup and primary-result alerts enabled when scanner is on; cooldown candidates are still measured.' if policy.get('scanner') else 'Scanner disabled.',
            checked_at=(study.get('worker') or {}).get('at'), issues=issues,
            next_step='Compare resolved outcomes and observation gaps across setup, direction and market conditions.',
            version=study.get('version'), measured=stats['closed'] if 'groups' in study else None)

    secondary = data.get('secondary', {})
    sec_status = secondary.get('status', {})
    add('secondary', 'Compass secondary comparison', 'Comparison studies', activity(sec_status, now, sec_status.get('enabled')),
        'Reviews received Morning, Smoothers, linked futures and Obsidian candidates, with the original evidence retained.',
        'Supported / watch / rejected / insufficient-data verdicts. Paired comparisons remove TM flow and levels while keeping the same candidate and prices.',
        'Supported and rejected candidates share 15/30/60-minute underlying checkpoints; Smoothers also has weekly source comparisons.',
        [metric('Reviewed in report', secondary.get('window', {}).get('reviewed')),
         metric('Supported', secondary.get('counts', {}).get('supported')),
         metric('Rejected', secondary.get('counts', {}).get('rejected')),
         metric('Insufficient data', secondary.get('counts', {}).get('insufficient_data'))],
        '30-day secondary report, limited to 5,000 reviews; this comparison does not cover every raw TM unusual-flow record.', 'secondary',
        route='research', alert_policy='Timely secondary notices enabled.' if sec_status.get('alerts_enabled') else 'Secondary notices disabled or not observed.',
        checked_at=secondary.get('report_at'), issues=['TM-wide scoring and outcome comparisons are outside this study.'] +
            (['The secondary report reached its 5,000-review limit.'] if secondary.get('window', {}).get('truncated') else []),
        next_step='Review added winners, avoided losses and missed moves at common decision-time prices.', version=secondary.get('version'))

    obs = data.get('obsidian', {})
    add('obsidian', 'Obsidian Watchlist', 'External ideas', activity(obs.get('status', {}), now, limit=300),
        'Provider watchlist additions and update events are retained with their original times.',
        'Separate underlying-direction reviews and observed option-price tracking; provider-reported gains are not treated as measured returns.',
        'Forward option observations where quotes exist; the historical audit has separate assumptions and coverage.',
        [metric('Stored idea records', obs.get('total_ideas')), metric('Stored update events', obs.get('total_events'))],
        'Stored inventory totals; detail pages show the latest 100 ideas/events and disclose historical audit scope.', 'obsidian',
        alert_policy='Captured for research; secondary notices for Obsidian are suppressed.', checked_at=obs.get('status', {}).get('at'),
        next_step='Review independently observed option coverage and separate it from provider claims.')

    spy = supplements.get('spy', {})
    latest = spy.get('latest') or {}
    add('spy', 'SPY daily plan and confirmation', 'Daily plans', 'scheduled' if policy.get('spy') else 'disabled',
        'Daily levels, chart instructions and scheduled opening/follow-up decisions are saved.',
        'Opening confirmation and conditional follow-ups through 09:30 CT; SPX/XSP projections remain estimates.',
        'Saved plans and confirmation decisions. This is not a complete daily option-entry/outcome or timing-comparison ledger.',
        [metric('Confirmation checks saved today', spy.get('checks_today')),
         metric('Latest decision', latest.get('decision'))],
        'Today’s saved confirmation checks and latest scheduled plan; historical timing research is separate.', 'research-admin',
        route='spy_morning', alert_policy='Scheduled plan and chart messages enabled.' if policy.get('spy') else 'Scheduled messages disabled.',
        checked_at=latest.get('generated_at'), issues=['A complete live plan-to-option-outcome comparison is not implemented here.'],
        next_step='Review saved confirmation evidence alongside the separate timing research.')

    trades = [p for p in data.get('trades', []) if p.get('asset') == 'option']
    positions = [p for p in data.get('positions', [])
                 if p.get('asset') == 'option' and p.get('status') == 'open']
    selection=data.get('forward_acceptance',{}).get('zero_dte',{})
    add('zero-dte', '0DTE portfolio simulations', 'Independent ideas',
        activity(data.get('workers', {}).get('worker:engine', {}), now),
        'Selected same-day option positions and closed simulations are stored under the portfolio risk rules.',
        'Underlying trigger, contract eligibility, executable quote and available portfolio capacity are required.',
        'Selected-position modeled returns. Portfolio limits mean this is not a study of every possible 0DTE opportunity.',
        [metric('Open option positions', len(positions) if 'positions' in data else None),
         metric('Option trades in recent view', len(trades) if 'trades' in data else None),
         metric('Retries with saved reasons, 24h',selection.get('instrumented')),
         metric('Retries missing detail, 24h',selection.get('missing_diagnostics'))],
        'All current positions plus the latest 100 portfolio trades across instruments; counts here are not lifetime totals.', 'trades',
        route='options_0dte', alert_policy='Simulated entry and management notifications under existing portfolio rules.',
        checked_at=data.get('workers', {}).get('worker:engine', {}).get('at'),
        next_step='Review Feed health → 0DTE selection diagnostics for saved exclusions and portfolio risk. Validate new diagnostics during the next equity session.', measured=None)

    feeds = data.get('scanner', {}).get('feeds', [])
    add('vendor-research', 'Market research and exposure', 'Context feeds',
        'collecting' if policy.get('research') else 'disabled',
        'Vendor screeners, exposure levels, sector context and other research responses are stored with source and receipt clocks.',
        'Dated context can support a price setup; a fetched response is not automatically an entry or a Compass rating.',
        'Linked setup and secondary studies evaluate selected uses; not every vendor screener result receives independent outcome tracking.',
        [metric('Configured research feeds', len(feeds)), metric('Current feed reports', sum(f.get('status') == 'current' for f in feeds))],
        'Latest status per configured source; source freshness and collection health are separate.', 'research',
        route='exposure', alert_policy='Exposure price setups use their dedicated channel; raw context feeds are not broadcast automatically.',
        checked_at=data.get('scanner', {}).get('status', {}).get('at'),
        next_step='Inspect individual source coverage and the studies that actually use its evidence.')

    audit = data.get('forward_acceptance', {})
    storage = data.get('storage', {})
    return {'version': VERSION, 'asof': now, 'streams': rows,
        'counts': {'streams': len(rows), 'attention': sum(r['needs_attention'] for r in rows),
                   'with_measured_results': sum((r['measured'] or 0) > 0 for r in rows),
                   'discord_pending': delivery.get('pending')},
        'storage': {'database': policy.get('database', 'unobserved'),
            'status': storage.get('status', 'unobserved'), 'checked_at': storage.get('at'),
            'database_bytes': storage.get('database_bytes'),
            'archive_scheduler': storage.get('scheduled_archive', {}).get('status', 'unobserved')},
        'flow_research': {'records_rated_by_compass': tm_counts.get('scored'),
            'prospective_assessments':tm_counts.get('prospective'),
            'all_record_outcomes':tm.get('outcome_counts'),
            'coverage':tm.get('coverage','awaiting_first_report'), 'tm_score_preserved': True,
            'inventory':tm_inventory,'incomplete_assessments':tm_counts.get('insufficient_data'),
            'historical_inventory':tm_counts.get('historical_inventory'),'at':tm.get('at')},
        'swing_audit': {'since': audit.get('since'), 'checked_at': audit.get('at'),
            'technical_candidates': audit.get('swing_technical_candidates'),
            'without_flow_confirmation': audit.get('swing_without_flow'),
            'truncated': audit.get('swing_candidates_truncated')},
        'notes': ['Saved data, assessed candidates, usable measurements and delivered alerts are different counts.',
            'Filters can suppress alerts now. There is no blanket pause until research is profitable.',
            'A score is not a probability of profit. Completed observations do not establish an improved strategy.',
            'Missing results remain unknown; stock movement is separate from option returns and simulated fills.',
            'Raw-data coverage is limited to received sources and configured universes. A matching vendor count does not prove complete capture.']}


def snapshot(db, c, cfg, data):
    from .spy_confirmation import reports_for_day
    reports = reports_for_day(db, c, day(data['asof']))
    return build(data, {'native_report': db.get(c, 'native-program-report-v1:summary', {}),
        'morning_report': db.get(c, 'morning_report:status', {}),
        'spy': {'latest': db.get(c, 'spy-brief:latest'), 'checks_today': sum(bool(p) for p in reports.values())}},
        {'database': db.engine.dialect.name, 'matrix': bool(cfg.matrix), 'scanner': cfg.scanner,
         'research': cfg.research, 'spy': cfg.spy_morning_brief,
         'swing_alerts': cfg.swing_alerts, 'options_ideas_alerts': cfg.ideas_alerts})
