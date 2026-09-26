"""Prospective weekday candidate board. No orders, alerts, or new entry rules."""
from sqlalchemy import select, func
from .store import state, identity
from .market import day, session, number
from .vendor_freshness import confirmation
from .universe import EXCLUDED_STOCKS

VERSION = 'weekday-candidates-v1'
PREFIX = VERSION + ':candidate:'
GLOBAL = ('extra_accumulation','screener_momentum','screener_bullish-pullback',
          'screener_volatility-squeeze','repeat_flow','earnings','economic_calendar',
          'sector_dashboard','sector_rotation','market_stats','vix')
DETAIL = ('extra_repeat_detail_','extra_ticker_flow_','extra_earnings_detail_',
          'extra_quality_detail_','extra_apex_history_','extra_apex_trend_')
MACRO = {'economic_calendar','sector_dashboard','sector_rotation','market_stats','vix'}


def compact(value,depth=0):
    if depth>=3:return '[additional detail omitted]'
    if isinstance(value,dict):return {k:compact(v,depth+1) for k,v in list(value.items())[:8]}
    if isinstance(value,list):return [compact(v,depth+1) for v in value[:5]]
    if isinstance(value,str):return value[:200]
    return value


def evidence(db,c,symbol,now):
    result=[]
    for key in GLOBAL + tuple(k+symbol for k in DETAIL):
        item=db.get(c,'research:'+key,{})
        received=number(item.get('received'))
        if received is None or received>now:
            result.append(dict(key=key,label=key,status='unavailable',scope='unknown',rows=[]))
            continue
        rows=[r for r in item.get('items',[]) if r.get('symbol')==symbol]
        scope='symbol_match' if rows else 'market_context' if key in MACRO else 'ticker_detail' if key.endswith('_'+symbol) else 'no_symbol_match'
        check=confirmation(item,now,1800,1800)
        # Item-level source clocks are required for a matching row's freshness.
        matches=[]
        for r in rows[:10]:
            check_row=confirmation({**item,'source_ts':r.get('source_ts')},now,1800,1800)
            matches.append(dict(data=compact(r.get('data')),source_ts=r.get('source_ts'),status=check_row['status']))
        result.append(dict(key=key,label=item.get('label',key),scope=scope,
            status=check['status'],source_ts=item.get('source_ts'),received=received,
            rows=matches,data=compact(item.get('data')) if scope in ('market_context','ticker_detail') else None,
            use='Partial frozen context; full response retained in source archive. Not a directional confirmation'))
    return result


def family(rule):
    if any(k in rule for k in ('reversal','sweep','pullback','bounce','range_rejection')):
        return 'Pullback / reversal'
    return 'Momentum / breakout'


def capture(db,c,signal,now,trial_id):
    symbol=signal['symbol'];hours=session(day(now))
    if symbol in EXCLUDED_STOCKS or signal.get('track')=='swing' or not hours or not hours[0]<=now<hours[1]:
        return
    rule=signal.get('rule') or signal.get('strategy','unknown')
    key=PREFIX+identity(VERSION,symbol,signal['side'],rule,day(now))
    if db.get(c,key):return
    frozen=evidence(db,c,symbol,now)
    matched=[e['key'] for e in frozen if e['scope']=='symbol_match' and any(r['status']=='current' for r in e['rows'])]
    from .option_ideas import VERSION as option_version
    p=dict(version=VERSION,symbol=symbol,side=signal['side'],rule=rule,family=family(rule),
        day=day(now),captured_at=now,signal_time=signal.get('signal_time'),
        trigger=signal.get('reason') or rule,signal_price=signal.get('signal_price'),
        invalidation=signal.get('invalidation'),setup_trial_id=trial_id,
        option_id=identity(option_version,signal['id']),source_id=signal['id'],
        matched_sources=matched,context=frozen,mode='RESEARCH ONLY',
        comparison_group='current_vendor_match' if matched else 'no_current_vendor_match')
    c.execute(db.insert(state).values(key=key,value=p,updated=now).on_conflict_do_nothing(index_elements=['key']))


def watchlist(db,c,now):
    """A current vendor shortlist is distinct from frozen triggered observations."""
    rows=[]
    for key in ('extra_accumulation','screener_momentum','screener_bullish-pullback','screener_volatility-squeeze'):
        item=db.get(c,'research:'+key,{})
        received=number(item.get('received'))
        if received is None or not 0<=now-received<=86400:continue
        for row in item.get('items',[]):
            symbol=row.get('symbol')
            if not symbol or symbol in EXCLUDED_STOCKS:continue
            check=confirmation({**item,'source_ts':row.get('source_ts')},now,1800,1800)
            rows.append(dict(symbol=symbol,source=item.get('label',key),status='Awaiting price trigger',
                freshness=check['status'],received=received,source_ts=row.get('source_ts'),
                direction='Unconfirmed',data=compact(row.get('data'))))
    return dict(rows=rows[:100],total=len(rows),truncated=len(rows)>100,
        note='Vendor candidates only; no entry, option selection, or automatic admission. Scope is received screener rows.')


def report(db,c,now):
    from .setup_study import trials
    from .option_ideas import ideas
    conditions=(state.c.key.like(PREFIX+'%'),state.c.updated>=now-30*86400)
    total=c.execute(select(func.count()).select_from(state).where(*conditions)).scalar_one()
    entries=list(c.execute(select(state.c.value).where(*conditions).order_by(state.c.updated.desc()).limit(100)).scalars())
    trial_ids=[p['setup_trial_id'] for p in entries if p.get('setup_trial_id')]
    option_ids=[p['option_id'] for p in entries]
    stock={r.id:r.payload for r in c.execute(select(trials.c.id,trials.c.payload).where(trials.c.id.in_(trial_ids)))} if trial_ids else {}
    options={r.id:r.payload for r in c.execute(select(ideas.c.id,ideas.c.payload).where(ideas.c.id.in_(option_ids)))} if option_ids else {}
    for p in entries:
        t=stock.get(p.get('setup_trial_id'));o=options.get(p['option_id'])
        p['stock']=({k:t.get(k) for k in ('status','reason','entry','stop','target','exit','pnl','outcome','exit_reason')} if t else dict(status='unavailable',reason='No linked setup observation'))
        p['option']=({k:o.get(k) for k in ('status','contract','entry','exit','pnl','outcome','exit_reason','waiting_reason','basis','best_pct','worst_pct')} if o else dict(status='unavailable',waiting_reason='No linked option observation'))
    return dict(version=VERSION,at=now,total=total,records=entries,truncated=total>len(entries),
        watchlist=watchlist(db,c,now),
        note='First setup per ticker/direction/rule/session, captured prospectively. Latest 100 shown; 30-day total. Stock and one-contract option simulations are separate. All losses and unavailable observations remain visible. Correlated candidates are not a portfolio or proof of vendor contribution.')
