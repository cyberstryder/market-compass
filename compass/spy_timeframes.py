"""Independent, prospective SPY 1/3/5/15-minute timing comparison.

No alerts, portfolio writes, historical entries, or changes to the official plan.
All arms share the same early frozen context and executable option-path model.
"""
import asyncio
import logging
import time
import uuid
from datetime import date, timedelta
from sqlalchemy import Table, Column, String, Integer, Float, JSON, Index, select, update
from .store import meta, identity
from .market import day, session, number
from .spy_brief import bar_context
from .spy_study import Paper as PlanPaper, PROTOCOL as OPTION_PROTOCOL

VERSION = 'spy-timeframes-v1'
TIMEFRAMES = (1, 3, 5, 15)
DEADLINE = 50
PROTOCOL = dict(version=VERSION, timeframes=list(TIMEFRAMES), sessions=20,
    entry='First eligible completed candle close outside a shared frozen premarket range; aligned to cash open, through minute 60. One attempted option trade per timeframe/session.',
    context='Freeze shared PM range, 90% coverage, prior daily ATR and opening-gap context at the first 1-minute check. Full sessions require capture by open+110s. No later range replacement.',
    timing='Observe each due boundary from +5s until +50s. Missing deadlines are gaps, never reconstructed entries; an arm with a missed post-activation check cannot enter later.',
    release='Internal persisted research decision, not Discord delivery. The 15m research arm is a matched comparator, not the official delivered-plan cohort.',
    bracket='Signal close anchors stop at 0.2 x prior daily ATR and target at 0.4 x ATR; identical quote-driven exits for every arm.',
    option={**OPTION_PROTOCOL, 'entry':'First eligible quote after internal research release, before boundary+50s.',
        'contract':'Same-day standard SPY, nearest whole-dollar strike to signal close, ties lower; frozen after selection.'},
    evaluation='First 20 complete exchange sessions after activation. Partial activation day is warmup. Evaluation returns remain hidden until the fixed final close.',
    primary='Compare net one-contract daily returns on days with all four arms fully observed (closed or genuine no-signal). No-signal contributes zero only on these matched days. Report unmatched results separately.',
    measures='Net P&L, profit factor, win rate, closed-equity drawdown, holding time, entry delay, sampled favorable/adverse excursion and profit giveback; opening-gap and time-of-day cuts.',
    uncertainty='After the fixed endpoint and at least 10 matched days: 3,000 paired-session bootstrap resamples, fixed seed 20260918+timeframe, 98.333% percentile intervals for three planned contrasts versus 15m (Bonferroni family alpha 0.05). Small samples remain limited.',
    limitations='Sampled quotes, not actual fills. Continuous quote-driven exits do not measure human screen time. One breakout family and fixed exits cannot establish a universally best timeframe. No automatic promotion.')

days = Table('spy_timing_days_v1', meta,
    Column('id', String(64), primary_key=True), Column('day', String(10), nullable=False),
    Column('version', String(50), nullable=False), Column('payload', JSON, nullable=False))
arms = Table('spy_timing_arms_v1', meta,
    Column('id', String(64), primary_key=True), Column('day', String(10), nullable=False),
    Column('timeframe', Integer, nullable=False), Column('cohort', String(20), nullable=False),
    Column('status', String(24), nullable=False), Column('created', Float, nullable=False),
    Column('updated', Float, nullable=False), Column('payload', JSON, nullable=False))
Index('spy_timing_active', arms.c.status, arms.c.created)
checks = Table('spy_timing_checks_v1', meta,
    Column('id', String(64), primary_key=True), Column('arm_id', String(64), nullable=False),
    Column('boundary', Float, nullable=False), Column('created', Float, nullable=False),
    Column('status', String(30), nullable=False), Column('payload', JSON, nullable=False))
Index('spy_timing_check_arm', checks.c.arm_id, checks.c.boundary, unique=True)
marks = Table('spy_timing_marks_v1', meta,
    Column('id', String(64), primary_key=True), Column('study_id', String(64), nullable=False),
    Column('at', Float, nullable=False), Column('processed_at', Float, nullable=False),
    Column('payload', JSON, nullable=False))
Index('spy_timing_mark_path', marks.c.study_id, marks.c.at, marks.c.id)


def activate(db, c, now):
    saved = db.get(c, VERSION+':activation')
    if saved is not None:
        return saved
    cursor = date.fromisoformat(day(now))
    schedule = []
    while len(schedule) < 20:
        hours = session(cursor.isoformat())
        if hours and hours[0] > now:
            schedule.append(dict(day=cursor.isoformat(), open=hours[0], close=hours[1]))
        cursor += timedelta(days=1)
    saved = dict(at=now, version=VERSION, sessions=schedule,
        evaluation_end=schedule[-1]['close'], protocol=PROTOCOL)
    db.put(c, VERSION+':activation', saved)
    return saved


def stream_requests(c, now, status):
    rows = c.execute(select(arms.c.payload).where(arms.c.status == status).order_by(arms.c.created)).scalars()
    return [p['contract']['symbol'] for p in rows if p.get('contract') and (status == 'open' or now < p['expires_at'])]


class Paper(PlanPaper):
    marks_table = marks

    def release(self, c, p, now):
        # Never fabricate a delivery receipt or a production report.
        r = c.execute(select(checks.c.payload).where(checks.c.id == p['source_key'],
            checks.c.status == 'confirmed', checks.c.created <= now)).scalar_one_or_none()
        return r['observed_at'] if r and r['observed_at'] == p['signal_time'] else None

    def save(self, c, p, now):
        p['updated_at'] = now
        if p.get('waiting_reason'):
            p['waiting_reason'] = p['waiting_reason'].replace('delivery', 'research_release').replace('report', 'decision')
        if p.get('last_attempt',{}).get('reason'):
            p['last_attempt']['reason']=p['last_attempt']['reason'].replace('delivery','research_release').replace('report','decision')
        c.execute(update(arms).where(arms.c.id == p['id']).values(status=p['status'], updated=now, payload=p))


def regular_bars(db, c, opening, boundary):
    result = {}
    for row in db.get(c, 'bar_window:SPY', []):
        if len(row) < 6 or any(number(x) is None for x in row[:6]):
            continue
        stamp, o, h, l, close, volume = row[:6]
        if (opening <= stamp and stamp+60 <= boundary and (stamp-opening) % 60 == 0
                and 0 < l <= min(o, close) <= max(o, close) <= h and volume >= 0):
            result[stamp] = row[:7]
    return result


def frozen_context(ctx, now, opening, *, partial):
    pm = ctx['premarket']
    blocks = []
    if not ctx['premarket_complete']:
        blocks.append('premarket_coverage_incomplete')
    if not (number(pm['low']) and number(pm['high']) and 0 < pm['low'] < pm['high']):
        blocks.append('invalid_premarket_range')
    if not (number(ctx.get('atr14_daily')) or 0) > 0:
        blocks.append('prior_daily_atr_incomplete')
    if not partial and now >= opening+60+DEADLINE:
        blocks.append('opening_context_capture_missed')
    prior = ctx['prior'].get('c')
    rth_open = ctx['regular'].get('open')
    gap = (rth_open/prior-1)*100 if prior and rth_open else None
    return dict(at=now, premarket=pm, expected_bars=ctx['premarket_expected_bars'],
        atr=ctx['atr14_daily'], data_blocks=blocks, opening_gap_pct=gap,
        gap_regime='unknown' if gap is None else 'up' if gap > .1 else 'down' if gap < -.1 else 'flat')


def candidate(p, check, frozen, closing):
    price, now = check['close'], check['observed_at']
    side = check['side']
    sign = 1 if side == 'long' else -1
    risk = .2*frozen['atr']
    return {**p, 'source_key':check['id'], 'status':'pending', 'signal_time':now,
        'signal_price':price, 'underlying':'SPY', 'underlying_side':side,
        'underlying_stop':price-sign*risk, 'underlying_target':price+sign*2*risk,
        'expires_at':check['boundary']+DEADLINE, 'flatten_at':closing-300,
        'selection_reference':price, 'report_contract':None, 'contract':None,
        'entry':None, 'exit':None, 'pnl':None, 'return_pct':None, 'samples':0,
        'max_gap_seconds':0, 'best_pct':None, 'worst_pct':None, 'peak_at':None,
        'underlying_best_points':None, 'underlying_worst_points':None, 'milestones':[],
        'attempts':0, 'waiting_reason':'fresh_quote_after_research_decision_required',
        'exit_reason':None, 'finished_at':None, 'time_bucket':check['time_bucket'],
        'gap_regime':frozen['gap_regime'], 'check_minute':check['minute'],
        'release_basis':'internal_research_decision', 'protocol':PROTOCOL}


class Study:
    def __init__(self, db, cfg):
        self.db, self.cfg, self.owner = db, cfg, uuid.uuid4().hex

    def ensure_day(self, c, label, activation, now):
        key = identity(VERSION, label)
        saved = c.execute(select(days.c.payload).where(days.c.id == key)).scalar_one_or_none()
        if saved:
            return saved
        opening, closing = session(label)
        cohort = 'evaluation' if label in {s['day'] for s in activation['sessions']} else 'warmup'
        saved = dict(id=key, day=label, opening=opening, closing=closing, cohort=cohort,
            activation=activation['at'], frozen=None)
        c.execute(self.db.insert(days).values(id=key, day=label, version=VERSION, payload=saved))
        for tf in TIMEFRAMES:
            p = dict(id=identity(VERSION, label, tf), version=VERSION, day=label,
                timeframe=tf, cohort=cohort, status='waiting', created_at=now,
                updated_at=now, checked_minute=0, blocked_checks=0, missed_checks=0,
                checks=0, preactivation_checks=0, frozen_context_id=key)
            c.execute(self.db.insert(arms).values(id=p['id'], day=label, timeframe=tf,
                cohort=cohort, status='waiting', created=now, updated=now, payload=p))
        return saved

    def check_arm(self, c, p, d, now, context):
        paper = Paper(self.db, self.cfg, clock=lambda:now)
        opening = d['opening']
        previous_minute = p['checked_minute']
        for minute in range(p['checked_minute']+p['timeframe'], 61, p['timeframe']):
            boundary = opening+minute*60
            bars = {}
            if now < boundary+5:
                break
            if boundary+5 < d['activation']:
                status, blocks, close, side = 'before_activation', [], None, None
            elif now >= boundary+DEADLINE:
                status, blocks, close, side = 'missed', ['check_deadline_missed'], None, None
            elif p['missed_checks']:
                status, blocks, close, side = 'blocked', ['earlier_check_missed'], None, None
            else:
                frozen = d['frozen']
                bars = regular_bars(self.db, c, opening, boundary)
                history = set(bars) == {opening+i*60 for i in range(minute)}
                if not history and now < boundary+DEADLINE-3:
                    break  # Wait for a late completed bar, never an intrabar signal.
                blocks = list(frozen['data_blocks']) if frozen else ['shared_context_missing']
                if not history:
                    blocks.append('regular_session_history_incomplete')
                if not context or context['status'] != 'available':
                    blocks.append('price_evidence_stale_or_missing')
                close = bars.get(boundary-60, [None]*5)[4]
                side = ('long' if close > frozen['premarket']['high'] else
                        'short' if close < frozen['premarket']['low'] else None) if not blocks else None
                status = 'blocked' if blocks else 'confirmed' if side else 'wait'
            record = dict(id=identity(p['id'], minute), arm_id=p['id'], timeframe=p['timeframe'],
                day=p['day'], cohort=p['cohort'], minute=minute, boundary=boundary,
                candle_start=boundary-p['timeframe']*60, observed_at=now,
                status=status, data_blocks=blocks, close=close, side=side,
                time_bucket='first_15' if minute <= 15 else 'minutes_16_30' if minute <= 30 else 'minutes_31_60',
                frozen_context_id=d['id'])
            record.update(candle_bars=[bars[t] for t in sorted(bars) if t>=boundary-p['timeframe']*60],
                history_bar_count=len(bars),expected_history_bars=minute,
                history_digest=identity([bars[t] for t in sorted(bars)]) if bars else None)
            c.execute(self.db.insert(checks).values(id=record['id'], arm_id=p['id'], boundary=boundary,
                created=now, status=status, payload=record).on_conflict_do_nothing(index_elements=['id']))
            p.update(checked_minute=minute, checks=p['checks']+1,
                blocked_checks=p['blocked_checks']+(status == 'blocked'),
                missed_checks=p['missed_checks']+(status == 'missed'),
                preactivation_checks=p['preactivation_checks']+(status == 'before_activation'))
            if status == 'confirmed':
                p.update(candidate(p, record, d['frozen'], d['closing']))
                paper.save(c, p, now)
                return
        if p['checked_minute'] == 60:
            p.update(status='not_observed' if p['preactivation_checks']==p['checks'] else
                'data_blocked' if p['blocked_checks'] or p['missed_checks'] else 'no_signal',
                finished_at=now)
        if p['checked_minute'] != previous_minute:
            paper.save(c, p, now)

    def tick(self, now):
        from .spy_timeframe_report import report
        with self.db.tx() as c:
            if not self.db.lease(c, VERSION, self.owner, 120):
                return
            activation = activate(self.db, c, now)
            # Finite frozen census also records entire missed sessions after downtime.
            labels = sorted({day(activation['at']), *(s['day'] for s in activation['sessions'])})
            saved_days = {d['day']:d for d in c.execute(select(days.c.payload)).scalars()}
            for label in labels:
                hours = session(label)
                if not hours or hours[0]+65 > now:
                    continue
                if label not in saved_days:
                    saved_days[label] = self.ensure_day(c, label, activation, now)
            waiting = c.execute(select(arms.c.payload).where(arms.c.status=='waiting')).scalars().all()
            for label in sorted({p['day'] for p in waiting}):
                d = saved_days[label]
                hours = (d['opening'],d['closing'])
                context = None
                minute = int((now-hours[0])//60)
                due_now = any(p['day']==label and minute>p['checked_minute'] and minute%p['timeframe']==0 for p in waiting)
                if day(now) == label and due_now and 5 <= (now-hours[0])%60 < DEADLINE and now < hours[0]+3600+DEADLINE:
                    context = bar_context(self.db, c, now)
                    if d['frozen'] is None:
                        d['frozen'] = frozen_context(context, now, hours[0], partial=d['cohort']=='warmup')
                        c.execute(update(days).where(days.c.id == d['id']).values(payload=d))
                for p in (p for p in waiting if p['day']==label):
                    self.check_arm(c, p, d, now, context)
            paper = Paper(self.db, self.cfg, clock=lambda:now)
            rows = c.execute(select(arms.c.payload).where(arms.c.status.in_(('pending','open')))).scalars().all()
            for p in rows:
                if p['status'] == 'pending':
                    paper.pending(c, p, now)
                else:
                    paper.observe(c, p, now)
            self.db.put(c, VERSION+':worker', dict(at=now, version=VERSION, active=len(rows)))
            if now-self.db.get(c, VERSION+':report', {}).get('at', 0) >= 60:
                result = report(self.db, c, now, activation)
                self.db.put(c, VERSION+':report', result)
                logging.getLogger('uvicorn.error').info('SPY timeframe coverage: %s',
                    dict(at=now, activation=activation['at'], evaluation_end=activation['evaluation_end'],
                         counts=result['coverage'], timeframes=list(TIMEFRAMES)))
        self.db.health('spy_timeframes', 'running', 'Independent 1/3/5/15-minute SPY research', version=VERSION)

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.tick, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger('uvicorn.error').exception('SPY timeframe study failed')
                await asyncio.to_thread(self.db.health, 'spy_timeframes', 'error', type(error).__name__)
            await asyncio.sleep(2)
