"""Research and optional account benchmarks have separate admission and messaging."""

VERSION = 'research-first-v1'
PAPER_STATUSES = {'entered', 'open', 'closed', 'skipped', 'options_skipped', 'management_blocked'}


def policy(cfg):
    return dict(version=VERSION, mode='paper_benchmark' if cfg.paper_trading else 'research',
        paper_entries_enabled=cfg.paper_trading,
        paper_notifications_enabled=cfg.paper_trading,
        independent_research_enabled=cfg.setup_study,
        basis='Every configured setup is evaluated independently. Account limits and notification cooldowns do not gate research. Missing evidence remains explicit. Existing paper positions are managed silently to their original exits while new paper entries are paused.')


def paper_message(row):
    return row.get('source') == 'engine' and row['payload'].get('status') in PAPER_STATUSES


def research_notice(db, c, signal, trial, now):
    """Only claim tracking when the saved study row proves its state."""
    if not trial:
        data = dict(signal, study_status='unavailable', reason='Independent study disabled or unsupported')
    else:
        data = dict(signal, **{k: trial.get(k) for k in
            ('entry', 'stop', 'target', 'qty', 'asset', 'version', 'entry_quote_ts')})
        data.update(setup_trial_id=trial['id'], study_status=trial['status'],
            reason=trial.get('reason') or signal.get('reason'),research_basis=trial.get('basis'))
    data.update(status='research_observation', mode='RESEARCH')
    db.append(c, 'alert', 'research', signal['symbol'], now, data, 'research-setup:'+signal['id'])


def observe_daily_breakout(db, c, signal, now, clock=None):
    """Retain legacy daily-breakout signals in the independent swing endpoint study."""
    from sqlalchemy import select
    from .store import events, identity, swing_trials
    from .swing_study import register, VERSION
    key = 'daily-breakout-research:'+signal['id']
    # The daily close is the thesis; entry observation begins when detected.
    observed = dict(signal, source_signal_time=signal['signal_time'], signal_time=now,
        rule='daily_breakout', stop=signal['signal_price']-signal['stop_distance'])
    db.append(c,'swing_candidate','engine_daily_research',signal['symbol'],now,
        dict(signal=observed,flow_confirmed=False),key)
    event=c.execute(select(events).where(events.c.key==key)).mappings().one()
    # Registration follows the append/read. The scan-start clock must not
    # precede the event's receipt or freeze quote freshness before a slow write.
    checked=clock() if clock else max(now,event['received'])
    register(db,c,event,checked,snapshot=dict(at=checked,rows={},matrix={},coverage={}))
    trial_id=identity(VERSION,event['id'])
    row=c.execute(select(swing_trials).where(swing_trials.c.id==trial_id)).mappings().one()
    p=row['payload']
    return dict(id=trial_id,status=row['status'],entry=p.get('entry_mid'),reason=p.get('entry_reason'),
        version=VERSION,asset='stock',qty=None,stop=None,target=None,
        basis='Directional underlying checkpoints at 60m, close, and 1/3/5/9 sessions; not bracket fills')
