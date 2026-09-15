"""Continuous, paper-only cross-source setup detection.

Trade candidates require an explicit price trigger and invalidation. Exposure,
flow and vendor screeners remain attributed evidence, never probability claims.
"""
import time
from collections import defaultdict
from .market import levels, fresh, day, number, session, dedup
from .futures import research_session, future_levels, futures_session, risk_day, prior_rth, active_selection
from .store import identity
from .instruments import future_root
from .alert_format import alert_context
from .research import FEEDS, catalog
from .vendor_freshness import confirmation, context_check
from .flow_recovery import freshness as flow_freshness
from .futures_variants import observe_bar

VERSION='compass-scanner-v2'


def ema(values, period):
    if not values:
        return []
    result=[values[0]]
    alpha=2/(period+1)
    for value in values[1:]:
        result.append(result[-1]+alpha*(value-result[-1]))
    return result


def bars_from_window(window):
    return [{'id':int(row[0]),'ts':row[0],
             'payload':dict(zip(('o','h','l','c','v'),row[1:6])) |
                       ({'vw':row[6]} if len(row)>6 and row[6] is not None else {})}
            for row in window]


def features(symbol, rows, now, previous=None,coverage=None):
    rows=dedup(rows,now)
    if len(rows)<30:
        return {'symbol':symbol,'status':'warming_up','asof':rows[-1]['ts']+60 if rows else None,'reason':'At least 30 completed minute bars required'}
    if rows[-1]['ts']-rows[-30]['ts']!=29*60:
        return {'symbol':symbol,'status':'warming_up','asof':rows[-1]['ts']+60,
            'reason':'Thirty consecutive completed minutes required; historical gaps cannot stand in for recent price structure'}
    future='@' in symbol
    context=future_levels(rows,now,coverage,symbol) if future else levels(rows,now)
    if previous and previous.get('day')==context.get('day') and previous.get('prior_complete') and not context.get('prior_complete'):
        context.update({key:previous[key] for key in ('prior_high','prior_low','prior_close','prior_complete') if key in previous})
    latest=rows[-1]
    if not 0<=now-latest['ts']-60<=90 or rows[-2]['ts']+60!=latest['ts']:
        return {'symbol':symbol,'status':'stale','asof':latest['ts']+60,'reason':'No consecutive fresh completed bars'}
    closes=[r['payload']['c'] for r in rows[-120:]]
    e9,e21=ema(closes,9),ema(closes,21)
    lookback=rows[-21:-1]
    average=sum(r['payload']['v'] for r in lookback)/20
    groups=defaultdict(list)
    for row in rows:
        groups[int(row['ts']//900)*900].append(row)
    higher=[]
    for start, group in sorted(groups.items()):
        if start+900<=now and {r['ts'] for r in group}==set(range(start,start+900,60)):
            higher.append(group[-1]['payload']['c'])
    ht=ema(higher[-60:],21)
    bias=(1 if higher[-1]>ht[-1] and ht[-1]>ht[-2] else -1 if higher[-1]<ht[-1] and ht[-1]<ht[-2] else 0) if len(ht)>=21 else None
    volume=latest['payload']['v']
    prior5=rows[-6:-1]
    prior5_high=max(r['payload']['h'] for r in prior5)
    prior5_low=min(r['payload']['l'] for r in prior5)
    return {**context,'symbol':symbol,'status':'ready','asof':latest['ts']+60,
        'bar':latest['payload'],'previous_bar':rows[-2]['payload'],'bar_start':latest['ts'],
        'ema9':e9[-1],'ema21':e21[-1],'ema21_previous':e21[-2],
        'htf15_bias':bias,'htf15_completed_bars':len(ht),
        'prior5_high':prior5_high, 'prior5_low':prior5_low,
        'prior5_range_atr':(prior5_high-prior5_low)/context['atr14'] if context.get('atr14') else None,
        'rvol20':volume/average if average>0 else None,
        'rvol_method':'Current minute volume / preceding 20 observed minute volumes',
        'high20':max(r['payload']['h'] for r in lookback),
        'low20':min(r['payload']['l'] for r in lookback),
        'high5':max(r['payload']['h'] for r in rows[-5:]),
        'low5':min(r['payload']['l'] for r in rows[-5:])}


def session_open(symbol,now):
    if '@' in symbol:
        return futures_session(now,symbol)['entry_open']
    hours=session(day(now))
    return bool(hours and hours[0]<=now<hours[1]-1800)


def make_candidate(symbol,rule,side,price,stop,clock,reason,evidence=None):
    if not all(number(v) is not None and v>0 for v in (price,stop)):
        return None
    distance=price-stop if side=='long' else stop-price
    if distance<=0:
        return None
    return {'id':identity(VERSION,symbol,rule,side,clock),'symbol':symbol,'side':side,
        'strategy':VERSION+':'+rule,'rule':rule,'signal_time':clock,'signal_price':price,
        'invalidation':stop,'stop_distance':distance,'track':'intraday',
        'reason':reason,'evidence':evidence or [],'rule_version':VERSION}


def technical_candidates(f,arms,now,tick, research=False):
    """Only completed-bar patterns; arms are durable and session-specific."""
    result=[]
    arms=dict(arms)
    if f.get('status')!='ready' or not 0<=now-f.get('asof',0)<=90 or not f.get('atr14') or not (research_session(now,f['symbol'])['entry_open'] if research else session_open(f['symbol'],now)):
        return result,arms
    b,p=f['bar'],f['previous_bar']
    price,clock=b['c'],f['asof']
    atr=f['atr14']
    if arms.get('session')!=f.get('session_open'):
        arms={'session':f.get('session_open')}

    def add(rule,side,stop,reason,evidence=None):
        candidate=make_candidate(f['symbol'],rule,side,price,stop,clock,reason,evidence)
        if candidate:
            result.append(candidate)

    if f.get('or_complete'):
        for side,level in (('long',f['or_high']),('short',f['or_low'])):
            key='orb_'+side
            armed=arms.get(key)
            outside=price>level if side=='long' else price<level
            if armed and (not outside or clock-armed['at']>1800):
                arms.pop(key,None)
                armed=None
            if armed and clock>armed['at']:
                touched=b['l']<=level+tick if side=='long' else b['h']>=level-tick
                if touched and outside:
                    add('orb_retest',side,min(b['l'],level)-tick if side=='long' else max(b['h'],level)+tick,
                        'Opening-range breakout followed by a later retest and close outside',
                        [{'source':'price','kind':'opening_range','level':level,'source_ts':clock}])
                    arms.pop(key,None)
            crossed=p['c']<=level<price if side=='long' else p['c']>=level>price
            if crossed:
                arms[key]={'at':clock,'level':level}

    references=[]
    if f.get('prior_complete'):
        references.extend((('prior_high',f.get('prior_high'),'short'),('prior_low',f.get('prior_low'),'long')))
    if f.get('range_name','RTH')=='RTH':
        references.extend((('overnight_high',f.get('overnight_high'),'short'),('overnight_low',f.get('overnight_low'),'long')))
    for name,level,side in references:
        if not level:
            continue
        swept=b['l']<level-tick and price>level and price>p['h'] if side=='long' else b['h']>level+tick and price<level and price<p['l']
        if swept:
            add('session_sweep_reclaim',side,b['l']-tick if side=='long' else b['h']+tick,
                name.replace('_',' ')+' swept, reclaimed, and prior-bar structure broken',
                [{'source':'price','kind':name,'level':level,'source_ts':clock}])

    vwap=f.get('vwap')
    if vwap:
        up=price>vwap and f['ema9']>f['ema21']>f['ema21_previous'] and f['htf15_bias']==1
        down=price<vwap and f['ema9']<f['ema21']<f['ema21_previous'] and f['htf15_bias']==-1
        near=abs(p['l']-f['ema21'])<=atr*.35 or p['l']<=vwap<=p['h']
        if up and near and price>p['h'] and price>f['ema9']:
            add('trend_pullback','long',min(p['l'],b['l'])-tick,
                'EMA/VWAP pullback reclaimed with a higher close and aligned completed 15-minute bias')
        near=abs(p['h']-f['ema21'])<=atr*.35 or p['l']<=vwap<=p['h']
        if down and near and price<p['l'] and price<f['ema9']:
            add('trend_pullback','short',max(p['h'],b['h'])+tick,
                'EMA/VWAP pullback rejected with a lower close and aligned completed 15-minute bias')
    if (f.get('rvol20') or 0)>=1.5 and clock>=f.get('session_open',clock)+1800:
        if price>f['high20'] and f['htf15_bias']==1:
            add('volume_breakout','long',max(b['l']-tick,price-1.5*atr),
                '20-minute high broken on at least 1.5x recent minute volume; 15-minute bias agrees')
        elif price<f['low20'] and f['htf15_bias']==-1:
            add('volume_breakout','short',min(b['h']+tick,price+1.5*atr),
                '20-minute low broken on at least 1.5x recent minute volume; 15-minute bias agrees')
    return result,arms


def exposure_candidates(f,apex,now,tick):
    if not apex or f.get('status')!='ready' or not 0<=now-f.get('asof',0)<=90 or not f.get('atr14') or apex.get('vendor_stale'):
        return []
    stamp=apex.get('source_ts')
    if not (context_check(apex,now)['eligible_for_context'] if apex.get('cache_policy') else confirmation(apex,now,600,600)['eligible_for_live_confirmation']):
        return []
    b,p=f['bar'],f['previous_bar']
    result=[]
    for level in apex.get('levels',[]):
        if level['kind']=='apex' and (level.get('score') or 0)<70:
            continue
        # A newly moving/recomputed level cannot manufacture a historical cross.
        if level.get('known_at',apex['received'])>f['bar_start']:
            continue
        price=level['price']
        long=p['c']<=price<b['c']
        short=p['c']>=price>b['c']
        if not (long or short):
            continue
        side='long' if long else 'short'
        stop=min(price,b['l'])-tick if long else max(price,b['h'])+tick
        item=make_candidate(f['symbol'],'exposure_level_break',side,b['c'],stop,f['asof'],
            'Completed price close crossed a previously observed '+level['kind'].replace('_',' ')+' level',
            [{'source':'tradermatrix','kind':level['kind'],'level':price,'score':level.get('score'),'source_ts':stamp}])
        if item:
            result.append(item)
    return result


def flow_candidate(f,row,q,now,tick):
    stamp=row.get('source_ts')
    if f.get('status')!='ready' or not 0<=now-f.get('asof',0)<=90 or not f.get('atr14') or stamp is None or not 0<=now-stamp<=120:
        return None
    if not fresh(q,now) or q['ts']<stamp or (row.get('premium') or 0)<100000 or (row.get('score') or 0)<85:
        return None
    sentiment=str(row.get('sentiment','')).lower()
    side='long' if sentiment=='bullish' else 'short' if sentiment=='bearish' else None
    if not side or not f.get('vwap'):
        return None
    price=(q['bid']+q['ask'])/2
    if side=='long':
        passed=price>f['high5']+tick and price>f['vwap'] and f['ema9']>f['ema21']
        stop=max(f['bar']['l']-tick,price-f['atr14']*1.5)
    else:
        passed=price<f['low5']-tick and price<f['vwap'] and f['ema9']<f['ema21']
        stop=min(f['bar']['h']+tick,price+f['atr14']*1.5)
    if not passed:
        return None
    item=make_candidate(f['symbol'],'flow_price_breakout',side,price,stop,max(stamp,f['asof']),
        'Fresh high-score vendor flow plus a live price break of the last five completed minutes',
        [{'source':'tradermatrix','kind':'unusual_flow','vendor_id':row.get('vendor_id'),
          'premium':row['premium'],'score':row['score'],'sentiment':row.get('sentiment'),
          'classification':row.get('classification'),'source_ts':stamp}])
    if item:
        item['id']=identity(VERSION,f['symbol'],'flow_price_breakout',row.get('vendor_id'))
        item['confirmation']='Live quote trigger following completed-bar context'
    return item


def context_evidence(f,flow,apex,quotes,features_by_symbol,now):
    evidence=[{'source':'price','kind':'structure','source_ts':f.get('asof'),
        'vwap':f.get('vwap'),'ema9':f.get('ema9'),'ema21':f.get('ema21'),
        'rvol20':f.get('rvol20'),'htf15_bias':f.get('htf15_bias')}]
    symbol=f['symbol']
    root=future_root(symbol)
    pair={'MGC':('GC','MGC'),'GC':('GC','MGC'),'SIL':('SI','SIL'),'SI':('SI','SIL'),
          'MCL':('CL','MCL'),'CL':('CL','MCL')}.get(root)
    peers=[] if pair else list(('QQQ','SPY') if root not in ('MES','ES','YM','MYM') else ('SPY','QQQ'))
    families=(('NQ','MNQ'),('ES','MES'))+((('YM','MYM'),) if root in ('YM','MYM') else ())
    for roots in ((pair,) if pair else families):
        matches=[key for key in features_by_symbol if '@' in key and future_root(key) in roots]
        if matches:
            ordered=sorted(matches,key=lambda key:(key.startswith('M'),key))
            peers.extend(ordered if pair else ordered[:1])
    for peer in peers:
        other=features_by_symbol.get(peer)
        quote=quotes.get(peer)
        if peer==symbol or not other or other.get('status')!='ready' or not 0<=now-other.get('asof',0)<=90 or not fresh(quote,now):
            continue
        evidence.append({'source':quote.get('source','market_data'),'kind':'cross_market','symbol':peer,
            'source_ts':other.get('asof'),'quote_ts':quote['ts'],
            'htf15_bias':other.get('htf15_bias'),'above_vwap':other.get('price',0)>(other.get('vwap') or float('inf'))})
    recent=[r for r in flow if r.get('symbol')==symbol and r.get('source_ts') is not None and 0<=now-r['source_ts']<=120]
    for row in recent[:3]:
        evidence.append({'source':'tradermatrix','kind':'unusual_flow','source_ts':row['source_ts'],
            'premium':row.get('premium'),'score':row.get('score'),'sentiment':row.get('sentiment'),
            'vendor_id':row.get('vendor_id')})
    if apex and (context_check(apex,now)['eligible_for_context'] if apex.get('cache_policy') else confirmation(apex,now,600,600)['eligible_for_live_confirmation']):
        evidence.append({'source':'tradermatrix','kind':'apex_context','source_ts':apex['source_ts'], 'freshness':context_check(apex,now),
            'levels':apex.get('levels',[])[:8]})
    return evidence


class Scanner:
    def __init__(self,db,cfg):
        self.db,self.cfg=db,cfg

    def scan(self,c,now,engine):
        db,cfg=self.db,self.cfg
        quotes={key[6:]:value for key,value in db.prefix(c,'quote:').items()}
        latest=db.prefix(c,'latestbar:')
        facts={key[17:]:value for key,value in db.prefix(c,'scanner_features:').items()}
        flow_summary=db.get(c,'matrix:unusual_activity',{})
        flow=flow_summary.get('rows',[]) if flow_freshness(flow_summary,now)['eligible_for_live_confirmation'] else []
        apexes={key[5:]:value for key,value in db.prefix(c,'apex:').items()}
        concentrations={key[14:]:value for key,value in db.prefix(c,'matrix_levels:').items()}
        selected={item['raw_symbol'] for item in active_selection(db,c,cfg,now)}
        wanted=set(cfg.watch_symbols)|{symbol for symbol in quotes if '@' in symbol and symbol.split('@')[0] in selected}
        changed=[]
        for symbol in sorted(wanted):
            if not session_open(symbol,now):
                continue
            last=latest.get('latestbar:'+symbol)
            if last is None or last==db.get(c,'scanner_cursor:'+symbol):
                continue
            rows=bars_from_window(db.get(c,'bar_window:'+symbol,[]))
            if not rows:
                rows=db.recent(c,'bar',symbol,limit=1800)
            coverage=db.get(c,'historycoverage:'+symbol+':'+prior_rth(risk_day(now))[0]) if '@' in symbol else None
            f=features(symbol,rows,now,facts.get(symbol),coverage)
            facts[symbol]=f
            db.put(c,'scanner_features:'+symbol,f)
            db.put(c,'scanner_cursor:'+symbol,last)
            if cfg.setup_study and engine.specification(symbol)['asset'] == 'future':
                observe_bar(db,c,symbol,f,now)
            changed.append(symbol)
        candidates=[]
        for symbol in changed:
            f=facts[symbol]
            tick=engine.specification(symbol)['tick']
            items,arms=technical_candidates(f,db.get(c,'scanner_arms:'+symbol,{}),now,tick)
            db.put(c,'scanner_arms:'+symbol,arms)
            candidates.extend(items)
            if f.get('status')=='ready' and session_open(symbol,now):
                candidates.extend(exposure_candidates(f,apexes.get(symbol),now,tick))
                candidates.extend(exposure_candidates(f,concentrations.get(symbol),now,tick))
        for row in flow:
            symbol=row.get('symbol')
            if symbol not in wanted or not session_open(symbol,now) or symbol not in facts:
                continue
            item=flow_candidate(facts[symbol],row,quotes.get(symbol),now,engine.specification(symbol)['tick'])
            if item:
                candidates.append(item)
            # Interesting recent flow prioritizes refresh even before a price trigger.
            if row.get('source_ts') is not None and 0<=now-row['source_ts']<=120 and (row.get('score') or 0)>=85:
                db.put(c,'focus:'+symbol,{'symbol':symbol,'priority':80,'at':now,'reason':'Recent unusual flow'})
        grouped={}
        for item in candidates:
            group=(item['symbol'],item['side'])
            if group not in grouped:
                grouped[group]=item|{'matched_rules':[item['rule']],'candidate_ids':[item['id']]}
            else:
                grouped[group]['matched_rules'].append(item['rule'])
                grouped[group]['candidate_ids'].append(item['id'])
                grouped[group]['evidence']+=item['evidence']
        candidate_map={item['id']:dict(item) for item in candidates}
        for item in grouped.values():
            symbol=item['symbol']
            if db.get(c,'scanner_seen:'+item['id']):
                continue
            for candidate_id in item['candidate_ids']:
                db.put(c,'scanner_seen:'+candidate_id,{'at':now})
            f=facts[symbol]
            item.update(decided_at=now,expires_at=now+120,
                evidence=item['evidence']+context_evidence(f,flow,apexes.get(symbol),quotes,facts,now),
                context=f,mode='SIMULATED',status='triggered')
            matches=db.get(c,'research_matches:'+symbol,{})
            for match in sorted(matches.values(),key=lambda row:row.get('received',0),reverse=True)[:5]:
                if now-match.get('received',0)>3600:
                    continue
                stamp=match.get('source_ts')
                item['evidence'].append({'source':'tradermatrix','kind':'vendor_research_match',
                    'label':match['label'],'source_ts':stamp,'received':match['received'],
                    'usage':'dated_context' if confirmation(match,now,600,600)['eligible_for_live_confirmation'] else 'context_only_not_current_confirmation'})
            for evidence in item['evidence']:
                if evidence.get('kind')=='cross_market':
                    bias=evidence.get('htf15_bias')
                    evidence['agreement']='unknown' if bias in (None,0) else 'aligned' if bias==(1 if item['side']=='long' else -1) else 'conflicting'
            db.put(c,'focus:'+symbol,{'symbol':symbol,'priority':100,'at':now,'reason':item['rule']})
            cool=db.get(c,'scanner_cooldown:'+symbol+':'+item['side'],0)
            quote=quotes.get(symbol)
            blocked=(not fresh(quote,now) or quote['ts']<item['signal_time'])
            if blocked:
                item.update(status='blocked',blocked_reason='Fresh quote after trigger required')
            elif abs((quote['bid']+quote['ask'])/2-item['signal_price'])>max(f['atr14']*.5,engine.specification(symbol)['tick']*4):
                item.update(status='blocked',blocked_reason='Price moved too far from the observed trigger')
            elif now-cool<600:
                item.update(status='watch',blocked_reason='Same-direction alert cooldown; evidence recorded')
            if item['status'] in ('triggered','watch'):
                for candidate_id in item['candidate_ids']:
                    trial_signal={**candidate_map[candidate_id],'context':f}
                    trial_id=engine.study.start(c,trial_signal,now,engine.specification(symbol),
                        alerted=item['status']=='triggered',primary=candidate_id==item['id'])
                    if candidate_id==item['id']:
                        item['setup_trial_id']=trial_id
            if item['status']=='triggered':
                db.put(c,'scanner_cooldown:'+symbol+':'+item['side'],now)
                # Preserve price invalidation even if the executable quote moved.
                spec=engine.specification(symbol)
                entry=(quote['ask']+spec['tick'] if item['side']=='long' else max(spec['tick'],quote['bid']-spec['tick']))
                item['stop_distance']=abs(entry-item['invalidation'])
                item['entry']=entry
                item['stop']=item['invalidation']
                item['target']=entry+(1 if item['side']=='long' else -1)*item['stop_distance']*2
                if (item['side']=='long' and entry<=item['stop']) or (item['side']=='short' and entry>=item['stop']):
                    item.update(status='blocked',blocked_reason='Trigger already invalidated')
                else:
                    engine.ideas.queue(c,item,now)
                    if cfg.scanner_paper:
                        signal={k:v for k,v in item.items() if k not in ('entry','stop','target')}
                        filled=engine.enter(c,signal,now,quiet=True)
                        item['paper_status']='entered' if filled else 'risk_or_position_blocked'
                        if symbol in cfg.watch_symbols:
                            db.put(c,'pending_options:'+item['id'],{'signal':signal,'expires_at':item['expires_at'],'status':'waiting'})
                    db.append(c,'alert','scanner',symbol,now,{**item,'status':'setup_triggered'},'setup:'+item['id'])
            current=db.get(c,'opportunity:'+symbol+':'+item['side'],{})
            if not (current.get('status')=='triggered' and now<current.get('expires_at',0) and item['status'] in ('watch','blocked')):
                db.put(c,'opportunity:'+symbol+':'+item['side'],item)
            db.append(c,'opportunity','scanner',symbol,now,item,item['id'])
        self.manage_opportunities(c,quotes,now)
        self.retry_options(c,quotes,now,engine)
        db.put(c,'scanner:status',{'at':now,'mode':'SIMULATED','watch_symbols':len(cfg.watch_symbols),
            'features_ready':sum(f.get('status')=='ready' and 0<=now-f.get('asof',0)<=90 for f in facts.values()),
            'last_updated_symbols':len(changed),'rules_version':VERSION,
            'rules':['orb_retest','session_sweep_reclaim','trend_pullback','volume_breakout','exposure_level_break','flow_price_breakout'],
            'prop_account_rules':'Not configured: simulated risk limits do not model a prop-firm drawdown floor'})

    def retry_options(self,c,quotes,now,engine):
        for key,pending in self.db.prefix(c,'pending_options:').items():
            if pending.get('status')!='waiting':
                continue
            signal=pending['signal']
            symbol=signal['symbol']
            quote=quotes.get(symbol)
            reason=None
            if now>=pending['expires_at'] or not session_open(symbol,now):
                reason='Option selection window ended without an eligible same-day contract, fresh quote and available risk'
            elif fresh(quote,now):
                crossed=quote['bid']<=signal['invalidation'] if signal['side']=='long' else quote['ask']>=signal['invalidation']
                if crossed:
                    reason='Underlying setup invalidated before an option could be selected'
            if reason:
                pending.update(status='expired',reason=reason,updated_at=now)
                self.db.put(c,key,pending)
                engine.alert(c,symbol,{**alert_context(signal),'status':'options_skipped','reason':reason,'parent_signal':signal['id']},'pending-option-expired:'+signal['id'])
            elif fresh(quote,now) and abs((quote['bid']+quote['ask'])/2-signal['signal_price'])<=signal['context']['atr14']*.5:
                if engine.options(c,signal,now,quiet=True):
                    pending.update(status='entered',updated_at=now)
                    self.db.put(c,key,pending)

    def manage_opportunities(self,c,quotes,now):
        for key,item in self.db.prefix(c,'opportunity:').items():
            if item.get('status') not in ('triggered','watch','blocked'):
                continue
            q=quotes.get(item['symbol'])
            invalid=False
            if fresh(q,now):
                invalid=q['bid']<=item['invalidation'] if item['side']=='long' else q['ask']>=item['invalidation']
            status='invalidated' if invalid else 'expired' if now>=item['expires_at'] else None
            if status:
                item={**item,'status':status,'updated_at':now}
                self.db.put(c,key,item)
                self.db.append(c,'opportunity_update','scanner',item['symbol'],now,item,identity(item['id'],status))
                if status=='invalidated' and item.get('entry'):
                    self.db.append(c,'alert','scanner',item['symbol'],now,
                        {**alert_context(item),'status':'setup_invalidated',
                         'reason':'Underlying invalidation reached; see the separate paper-position ledger for fill/exit status',
                         'setup_id':item['id']},identity('invalidated',item['id']))


def snapshot(db,c,cfg,now):
    opportunities=sorted(db.prefix(c,'opportunity:').values(),key=lambda r:r['decided_at'],reverse=True)
    active=[]
    for row in opportunities:
        item={k:v for k,v in row.items() if k!='context'}
        if now>=item['expires_at'] and item['status'] in ('watch','blocked','triggered'):
            item['status']='expired'
        item['age']=now-item['decided_at']
        active.append(item)
    return {'status':db.get(c,'scanner:status',{'mode':'SIMULATED','watch_symbols':len(cfg.watch_symbols),'at':None}),
        'watchlist':list(cfg.watch_symbols),'opportunities':active[:100],
        'feeds':catalog(db,c,cfg,now),
        'focus':focus_for_display(db,c,now),
        'limits':{'price_quote_max_age_seconds':5,'completed_bar_max_age_seconds':90,
            'unusual_flow_max_age_seconds':120,'apex_max_age_seconds':600,
            'entry_window_seconds':120,'same_direction_cooldown_seconds':600},
        'coverage_note':'Live price scanning covers the configured watchlist. Vendor analytics refresh on independent schedules; missing or undated inputs cannot trigger exposure/flow trades.'}


def focus_for_display(db,c,now):
    return sorted((value for value in db.prefix(c,'focus:').values() if now-value.get('at',0)<=600),
        key=lambda row:(row.get('priority',0),row.get('at',0)),reverse=True)[:30]


def research_candidates(f,arms,now,tick):
    return technical_candidates(f,arms,now,tick,research=True)
