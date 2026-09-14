"""Independent forward trials for intraday stock/futures setups, one unit each.

Trials never read or write portfolio risk/positions. Overlapping setups are
separate experiments, not a realizable account equity curve. Only observations
received after activation may open a trial; missing paths are not reconstructed.
"""
from collections import defaultdict
import logging
from sqlalchemy import Table, Column, String, Float, JSON, Index, select, update
from .store import meta, identity
from .market import fresh, number, session, day
from .futures import futures_session
from .instruments import tick_price
from .simulation import bracket, exit_price, FILL_VERSION, FILL_DESCRIPTION
from .quote_path import recorded_path
from . import futures_variants

VERSION = 'setup-outcomes-v2'
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

    def start(self, c, signal, now, spec, alerted=True, primary=True):
        if not self.cfg.setup_study or spec['asset'] not in ('future', 'stock') or signal.get('track') == 'swing':
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
        hours = futures_session(now, signal['symbol']) if future else None
        cash = session(day(now)) if not future else None
        deadline = hours['flatten_at'] if future else (cash[1]-900 if cash else now)
        opened = hours['entry_open'] if future else bool(cash and cash[0] <= now < cash[1]-1800)
        cause = None
        stamp = number(signal.get('signal_time'))
        if stamp is None or not 0 <= now-stamp <= 90:
            cause = 'Signal is not a fresh completed-bar candidate'
        elif not opened:
            cause = 'Outside the configured research entry session'
        elif not fresh(q, now) or not stamp <= q['ts'] <= now:
            cause = 'Fresh two-sided quote after the signal is required'
        elif q['ask']-q['bid'] > max(spec['tick']*8, (q['ask']+q['bid'])/2*.002):
            cause = 'Spread exceeds the research liquidity limit'
        long = signal['side'] == 'long'
        entry = stop = target = risk = None
        if cause is None:
            try:
                prices = bracket(signal, q, spec)
                entry,stop,target = (prices[k] for k in ('entry','stop','target'))
                risk = prices['distance']*spec['multiplier']+2*spec['fee']
            except ValueError as error:
                cause = str(error)
        p = {**{k:signal[k] for k in ('id','symbol','side','strategy','rule','signal_time','signal_price','track') if k in signal},
            **spec, 'id':key, 'source_id':signal['id'], 'version':VERSION, 'qty':1,
            'status':'excluded' if cause else 'open', 'reason':cause,
            'started':now, 'finished':now if cause else None, 'entry':entry, 'stop':stop, 'target':target,
            'initial_risk':risk, 'flatten_at':deadline, 'alerted':alerted, 'primary':primary,
            'fill_version':FILL_VERSION, 'fill_model':FILL_DESCRIPTION, 'observation_model':'recorded-quotes-v2',
            'replayed_samples':0,
            'last_quote_ts':q['ts'] if fresh(q, now) else None, 'samples':0, 'max_gap_seconds':0,
            'mfe_r':0, 'mae_r':0, 'pnl':None, 'r_multiple':None,
            'basis':'Independent one-unit trial; sampled executable quotes, one adverse entry/stop tick, illustrative fees; not account P&L'}
        if future:
            p['entry_variants'] = futures_variants.classify(self.db, c, signal, now, cause is None)
        c.execute(self.db.insert(trials).values(id=key, source_id=signal['id'], symbol=p['symbol'],
            strategy=p['strategy'], side=p['side'], version=VERSION, status=p['status'], started=now,
            finished=p['finished'], payload=p).on_conflict_do_nothing(index_elements=['id']))
        return key

    def save(self, c, p):
        c.execute(update(trials).where(trials.c.id == p['id']).values(
            status=p['status'], finished=p.get('finished'), payload=p))

    def finish(self, c, p, now, reason, price=None, quote_ts=None):
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
                if truncated and not path:
                    p['gap_detail'] = {'reason':'recorded_batch_contains_no_usable_quotes','checked_at':observed}
                    self.finish(c,p,observed,'observation_gap')
                    continue
                if self.clock:
                    observed = self.clock()
                for recorded in path:
                    if recorded['ts'] <= p['last_quote_ts']:
                        continue
                    p['replayed_samples'] += 1
                    self.observe(c,p,recorded,observed,recorded=True)
                    if p['status'] != 'open':
                        break
                if p['status'] == 'open' and not truncated:
                    self.observe(c,p,q,observed)
                elif p['status'] == 'open':
                    p['replay_pending'] = True
                if p['status'] == 'open':
                    self.save(c,p)
                continue
            self.observe(c,p,q,observed)
        report = self.db.get(c, 'setup_study:report', {})
        if now-report.get('at', 0) >= 30:
            report = report_for(c, now)
            self.db.put(c, 'setup_study:report', report)
            groups = report['entry_variants']['groups']
            logging.getLogger('uvicorn.error').info(
                'Futures entry variants: version=%s cohorts=%s selected=%s unknown=%s unresolved=%s',
                futures_variants.VERSION, len(groups),
                {name:sum(g['selected'] for g in groups if g['variant']==name) for name in futures_variants.NAMES},
                sum(g['unknown'] for g in groups if g['variant']=='first_per_trend'),
                sum(g['unresolved'] for g in groups if g['variant']=='repeated'))
        self.db.put(c, 'setup_study:worker', {'at':now, 'version':VERSION})

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
    rows = c.execute(select(trials.c.payload).where(trials.c.started >= now-30*86400)
        .order_by(trials.c.started.desc()).limit(10001)).scalars().all()
    truncated = len(rows)>10000
    rows = rows[:10000]
    grouped = defaultdict(list)
    for p in rows:
        grouped[(p['symbol'], p['strategy'], p['side'], p['version'], p['alerted'])].append(p)
    groups = []
    for (symbol, strategy, side, version, alerted), members in grouped.items():
        closed = [p for p in members if p['status']=='closed']
        wins = sum(p['pnl']>0 for p in closed)
        groups.append(dict(symbol=symbol, strategy=strategy, side=side, version=version, alerted=alerted,
            total=len(members), closed=len(closed), wins=wins, losses=sum(p['pnl']<0 for p in closed),
            breakeven=sum(p['pnl']==0 for p in closed),
            targets=sum(p.get('exit_reason')=='target' for p in closed), stops=sum(p.get('exit_reason')=='stop' for p in closed),
            open=sum(p['status']=='open' for p in members), unresolved=sum(p['status']=='unresolved' for p in members),
            excluded=sum(p['status']=='excluded' for p in members), win_rate=wins/len(closed) if closed else None,
            mean_r=sum(p['r_multiple'] for p in closed)/len(closed) if closed else None,
            mean_seconds=sum(p['elapsed_seconds'] for p in closed)/len(closed) if closed else None))
    return {'at':now, 'version':VERSION, 'groups':groups, 'records':rows[:100], 'count':len(rows),
        'truncated':truncated, 'window_days':30, 'limit':10000, 'entry_variants':futures_variants.report(rows),
        'basis':'Independent one-unit experiments grouped by contract, strategy, direction, version and alert cohort; overlapping results are not portfolio returns'}


def snapshot(db, c, cfg, now):
    return {**db.get(c, 'setup_study:report', {'groups':[], 'records':[], 'count':0}),
        'enabled':cfg.setup_study, 'activation':db.get(c, 'setup_study:activation'),
        'model_activation':db.get(c, 'setup_study:model_activation:'+VERSION),
        'entry_variants_activation':db.get(c, futures_variants.PREFIX+'activation'),
        'worker':db.get(c, 'setup_study:worker'), 'max_entries':cfg.max_entries}
