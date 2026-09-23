from . import observation_recovery as recovery
"""Independent forward trials for intraday stock/futures setups, one unit each.

Trials never read or write portfolio risk/positions. Overlapping setups are
separate experiments, not a realizable account equity curve. Only observations
received after activation may open a trial; missing paths are not reconstructed.
"""
import logging
import asyncio
import time
import uuid
from sqlalchemy import Table, Column, String, Float, JSON, Index, select, update
from .store import meta, identity
from .market import fresh, number, session, day, CT
from datetime import datetime
from .futures import futures_session, research_session
from .instruments import tick_price
from .simulation import bracket, exit_price, FILL_VERSION, FILL_DESCRIPTION
from .quote_path import recorded_path
from . import futures_variants, futures_assessment, futures_feed_study, futures_feed_reporting
from .setup_reporting import summaries, WINDOW_DAYS

VERSION = 'setup-outcomes-v4'
MAX_GAP = 15
trials = Table('setup_trials_v1', meta,
    Column('id', String(64), primary_key=True), Column('source_id', String(100), nullable=False),
    Column('symbol', String(100), nullable=False), Column('strategy', String(150), nullable=False),
    Column('side', String(10), nullable=False), Column('version', String(50), nullable=False),
    Column('status', String(24), nullable=False), Column('started', Float, nullable=False),
    Column('finished', Float), Column('payload', JSON, nullable=False))
Index('setup_trials_active_symbol', trials.c.status, trials.c.symbol)
Index('setup_trials_started', trials.c.started)


class SetupStudy:
    def __init__(self, db, cfg, clock=None):
        self.db, self.cfg = db, cfg
        self.clock = clock
        self.report_owner = uuid.uuid4().hex

    def start(self, c, signal, now, spec, alerted=True, primary=True):
        if not self.cfg.setup_study or spec['asset'] not in ('future', 'stock', 'option') or signal.get('track') == 'swing':
            return None
        if spec['asset']=='option' and not signal.get('strategy','').startswith('0dte-'):
            return None
        key = identity(VERSION, signal['id'])
        previous = c.execute(select(trials.c.id).where(trials.c.source_id == signal['id'])).first()
        if previous:
            return previous[0]
        activation = self.db.get(c, 'setup_study:activation')
        if activation is None:
            activation = {'at':now, 'version':VERSION}
            self.db.put(c, 'setup_study:activation', activation)
        q = self.db.get(c, 'quote:'+signal['symbol'])
        if self.clock:
            now = self.clock()
        if self.db.get(c, 'setup_study:model_activation:'+VERSION) is None:
            self.db.put(c, 'setup_study:model_activation:'+VERSION, {'at':now,'version':VERSION,'fill_version':FILL_VERSION})
        future = spec['asset'] == 'future'
        hours = research_session(now, signal['symbol']) if future else None
        cash = session(day(now)) if not future else None
        deadline = hours['flatten_at'] if future else (cash[1] if cash else now)
        opened = hours['entry_open'] if future else bool(cash and cash[0] <= now < cash[1]-60)
        cause = None
        stamp = number(signal.get('signal_time'))
        if stamp is None or not 0 <= now-stamp <= 90:
            cause = 'Signal is not a fresh completed-bar candidate'
        elif not opened:
            cause = 'Outside the configured research entry session'
        elif not fresh(q, now) or not stamp <= q['ts'] <= now:
            cause = 'Fresh two-sided quote after the signal is required'
        long = signal['side'] == 'long'
        entry = stop = target = risk = None
        if cause is None:
            try:
                prices = bracket(signal, q, spec)
                entry,stop,target = (prices[k] for k in ('entry','stop','target'))
                risk = prices['distance']*spec['multiplier']+2*spec['fee']
            except ValueError as error:
                cause = str(error)
        p = {**{k:signal[k] for k in ('id','symbol','side','strategy','rule','signal_time','signal_price','track','underlying','underlying_side') if k in signal},
            **spec, 'id':key, 'source_id':signal['id'], 'version':VERSION, 'qty':1,
            'status':'excluded' if cause else 'open', 'reason':cause,
            'started':now, 'finished':now if cause else None, 'entry':entry, 'stop':stop, 'target':target,
            'evaluation_policy':'independent-all-setups-v1', 'entry_spread':q['ask']-q['bid'] if fresh(q,now) else None, 'initial_risk':risk, 'flatten_at':deadline, 'alerted':alerted, 'primary':primary,
            'fill_version':FILL_VERSION, 'fill_model':FILL_DESCRIPTION, 'observation_model':'recorded-quotes-v2',
            'replayed_samples':0,
            'last_quote_ts':q['ts'] if fresh(q, now) else None, 'samples':0, 'max_gap_seconds':0,
            'mfe_r':0, 'mae_r':0, 'pnl':None, 'r_multiple':None,
            'basis':'Independent one-unit trial; sampled executable quotes, one adverse entry/stop tick, illustrative fees; not account P&L'}
        p['collection_version']=(q or {}).get('collection_version','unversioned')
        if p['collection_version'] in ('option-reliability-v5','option-reliability-v6'):recovery.enable(p)
        cash_hours=session(day(now))
        context=signal.get('context',{})
        p['research_context']={
            'version':'entry-context-v1', 'frozen_at':now,
            'session':'cash' if cash_hours and cash_hours[0]<=now<cash_hours[1] else 'overnight',
            'hour_ct':datetime.fromtimestamp(now,CT).hour,
            'market_state':futures_assessment.assess(context,now).get('state','unknown'),
            'inputs':{k:context[k] for k in ('asof','vwap','ema9','ema21','htf15_bias','atr14','rvol20') if k in context}}
        if future:
            p['market_assessment'] = futures_assessment.freeze(self.db, c, signal, now, q, spec)
            p['research_context']['market_state']=p['market_assessment'].get('state','unknown')
            p['entry_variants'] = futures_variants.classify(self.db, c, signal, now, cause is None)
            comparison = futures_feed_study.freeze(self.db, c, signal, now, cause is None, p['market_assessment'])
            if comparison is not None:
                p['feed_comparison'] = comparison
        c.execute(self.db.insert(trials).values(id=key, source_id=signal['id'], symbol=p['symbol'],
            strategy=p['strategy'], side=p['side'], version=VERSION, status=p['status'], started=now,
            finished=p['finished'], payload=p).on_conflict_do_nothing(index_elements=['id']))
        return key

    def save(self, c, p):
        c.execute(update(trials).where(trials.c.id == p['id']).values(
            status=p['status'], finished=p.get('finished'), payload=p))

    def finish(self, c, p, now, reason, price=None, quote_ts=None):
        if price is None and recovery.defer(self.db,c,p,now,reason):
            self.save(c,p)
            return
        if price is None:recovery.register(self.db,c,p,now,reason)
        else:recovery.clear(p)
        p.update(status='closed' if price is not None else 'unresolved', finished=now,
                 exit_reason=reason, exit=price, exit_quote_ts=quote_ts, elapsed_seconds=now-p['started'])
        if price is not None:
            p['pnl'] = (price-p['entry'])*(1 if p['side']=='long' else -1)*p['multiplier']-2*p['fee']
            p['r_multiple'] = p['pnl']/p['initial_risk']
            p['outcome'] = 'win' if p['pnl']>0 else 'loss' if p['pnl']<0 else 'breakeven'
        else:
            p['outcome'] = 'unresolved'
        self.save(c, p)
        if p['alerted'] and p['primary']:
            self.db.append(c, 'alert', 'setup_study', p['symbol'], now,
                {**p, 'status':'setup_result', 'mode':'SIMULATED', 'study_status':p['status']}, 'setup-result:'+p['id'])

    def tick(self, c, now):
        if not self.cfg.setup_study:
            return
        futures_feed_study.activate(self.db, c, now)
        if self.db.get(c, 'setup_study:activation') is None:
            self.db.put(c, 'setup_study:activation', {'at':now, 'version':VERSION})
        rows = c.execute(select(trials.c.payload).where(trials.c.status == 'open')).scalars().all()
        quotes = {}
        paths = {}
        for p in rows:
            symbol = p['symbol']
            if symbol not in quotes:
                quotes[symbol] = self.db.get(c, 'quote:'+symbol)
            q = quotes[symbol]
            observed = self.clock() if self.clock else now
            if p.get('observation_model') == 'recorded-quotes-v2':
                if symbol not in paths:
                    after = min(r['last_quote_ts'] for r in rows if r['symbol'] == symbol)
                    paths[symbol] = recorded_path(self.db,c,symbol,after,observed)
                path, truncated, archive_check = paths[symbol]
                p['archive_check'] = archive_check
                # The cached history only covers this cutoff. Advancing the
                # decision clock after a slow query (or for another trial)
                # would treat time we never queried as missing market data.
                observed = archive_check['checked_at']
                p['observation_evidence_at'] = observed
                p['evidence_checked_at'] = observed
                if truncated and not path:
                    p['gap_detail'] = {'reason':'recorded_batch_contains_no_usable_quotes','checked_at':observed}
                    self.finish(c,p,observed,'observation_gap')
                    continue
                for recorded in path:
                    if recorded['ts'] <= p['last_quote_ts']:
                        continue
                    p['replayed_samples'] += 1
                    self.observe(c,p,recorded,observed,recorded=True)
                    if p['status'] != 'open' or recovery.blocked(p):
                        break
                if recovery.blocked(p):
                    self.save(c,p)
                    continue
                if p['status'] == 'open' and not truncated:
                    self.observe(c,p,q,observed)
                elif p['status'] == 'open':
                    p['replay_pending'] = True
                if p['status'] == 'open':
                    recovery.clear(p)
                    self.save(c,p)
                continue
            self.observe(c,p,q,observed)
        self.db.put(c, 'setup_study:worker', {'at':now, 'version':VERSION})

    def refresh_report(self, c, now):
        if not self.cfg.setup_study:
            return
        feed_activation = self.db.get(c, futures_feed_study.KEY)
        report = self.db.get(c, 'setup_study:report', {})
        if now-report.get('at', 0) >= 30:
            report = report_for(c, now)
            report['feed_comparison'] = futures_feed_reporting.report(c, trials, now, feed_activation)
            self.db.put(c, 'setup_study:report', report)
            logging.getLogger('uvicorn.error').info('Futures market research: model=%s cohorts=%s census_contracts=%s', VERSION, len(report['market_assessment']['groups']), sum('snapshot' in v for v in self.db.prefix(c,'futures_research:').values()))
            groups = report['entry_variants']['groups']
            logging.getLogger('uvicorn.error').info(
                'Futures entry variants: version=%s cohorts=%s selected=%s unknown=%s unresolved=%s',
                futures_variants.VERSION, len(groups),
                {name:sum(g['selected'] for g in groups if g['variant']==name) for name in futures_variants.NAMES},
                sum(g['unknown'] for g in groups if g['variant']=='first_per_trend'),
                sum(g['unresolved'] for g in groups if g['variant']=='repeated'))

    def report_tick(self, now=None):
        now = time.time() if now is None else now
        with self.db.tx() as c:
            if self.db.lease(c, 'setup_study:report', self.report_owner, 60):
                self.refresh_report(c, now)

    async def run_reports(self):
        while True:
            try:
                await asyncio.to_thread(self.report_tick)
                self.db.health('setup_reports', 'running', 'Research reports refreshed independently of engine scans')
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('uvicorn.error').exception('Research report refresh failed')
                try:
                    self.db.health('setup_reports', 'error', 'Report refresh failed; see runtime logs')
                except Exception:
                    logging.getLogger('uvicorn.error').exception('Cannot record research report health')
            await asyncio.sleep(30)

    def observe(self, c, p, q, observed, recorded=False):
        # Legacy trials keep their original latest-quote method. V2 may
        # replay quotes that were already recorded fresh by the collector.
        checked = q['recorded_at'] if recorded else observed
        last = p['last_quote_ts']
        if not fresh(q, checked) or q['ts'] > checked or q['ts'] <= last:
            if observed-last > MAX_GAP and not recorded:
                p['gap_detail'] = {'last_quote_ts':last, 'checked_at':observed,
                                   'latest_quote_ts':(q or {}).get('ts'), 'reason':'no_usable_quote'}
                self.finish(c, p, observed, 'observation_gap')
            return
        gap = q['ts']-last
        p['max_gap_seconds'] = max(p['max_gap_seconds'], gap)
        if gap > MAX_GAP or q['ts'] > p['flatten_at']+5:
            p['gap_detail'] = {'last_quote_ts':last, 'next_quote_ts':q['ts'],
                               'checked_at':observed, 'gap_seconds':gap, 'reason':'missing_recorded_path'}
            self.finish(c, p, observed, 'session_quote_missing' if observed >= p['flatten_at'] else 'observation_gap')
            return
        p['last_quote_ts'] = q['ts']
        p['samples'] += 1
        long = p['side']=='long'
        executable = q['bid'] if long else q['ask']
        marked = (executable-p['entry'])*(1 if long else -1)*p['multiplier']-2*p['fee']
        p['mfe_r'] = max(p['mfe_r'], marked/p['initial_risk'])
        p['mae_r'] = min(p['mae_r'], marked/p['initial_risk'])
        stopped = executable <= p['stop'] if long else executable >= p['stop']
        target = executable >= p['target'] if long else executable <= p['target']
        session_clock = q['ts'] if p.get('observation_model') == 'recorded-quotes-v2' else observed
        reason = 'stop' if stopped else 'target' if target else 'session_flatten' if session_clock >= p['flatten_at'] else None
        if reason:
            # Never award a favorable gap beyond a resting target price.
            price = p['target'] if reason == 'target' else tick_price(
                executable+(-p['tick'] if long else p['tick']), p['tick'], not long)
            if p.get('fill_version') == FILL_VERSION:
                price = exit_price(p,q,reason)
            p['exit_observed_at'] = checked
            self.finish(c, p, observed, reason, price, q['ts'])
        else:
            p['replay_pending'] = False
            if not recorded:
                self.save(c, p)


def report_for(c, now):
    report = summaries(c, trials, now)
    records = c.execute(select(trials.c.payload).where(
        trials.c.started >= now-WINDOW_DAYS*86400, trials.c.started <= now)
        .order_by(trials.c.started.desc(), trials.c.id.desc()).limit(100)).scalars().all()
    return {**report, 'at':now, 'version':VERSION, 'records':records,
        'truncated':False, 'window_days':WINDOW_DAYS, 'limit':None,
        'coverage':'full_window', 'since':now-WINDOW_DAYS*86400,
        'records_limit':100, 'records_has_more':report['count']>len(records),
        'basis':'Independent one-unit experiments grouped by contract, strategy, direction, version and alert cohort; overlapping results are not portfolio returns'}


def snapshot(db, c, cfg, now):
    return {**db.get(c, 'setup_study:report', {'groups':[], 'records':[], 'count':0}),
        'futures_census':[v['snapshot'] for k,v in db.prefix(c,'futures_research:').items() if 'snapshot' in v],
        'enabled':cfg.setup_study, 'activation':db.get(c, 'setup_study:activation'),
        'model_activation':db.get(c, 'setup_study:model_activation:'+VERSION),
        'entry_variants_activation':db.get(c, futures_variants.PREFIX+'activation'),
        'feed_comparison_activation':db.get(c, futures_feed_study.KEY),
        'worker':db.get(c, 'setup_study:worker'), 'max_entries':cfg.max_entries}


def option_requests(db,c,now):
    """Request fresh quotes for every waiting 0DTE hypothesis; no account checks."""
    wanted=[]
    for pending in db.prefix(c,'pending_research_options:').values():
        if pending.get('status')!='waiting' or now>=pending['expires_at']:
            continue
        signal=pending['signal']
        chain=db.get(c,'chain:'+signal['symbol'],{})
        if not 0<=now-chain.get('asof',0)<=120:
            continue
        kind='call' if signal['side']=='long' else 'put'
        contracts=[o for o in chain.get('contracts',[]) if o.get('expiry')==day(now)
                   and o.get('type')==kind and o.get('multiplier')==100]
        contracts.sort(key=lambda o:abs(o['strike']-signal['signal_price']))
        wanted.extend(o['symbol'] for o in contracts[:8])
    return list(dict.fromkeys(wanted))
