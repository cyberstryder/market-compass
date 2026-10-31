"""Publication policy only. Research admission and strategy exits are unchanged."""
from datetime import datetime
import math
from collections import Counter
from sqlalchemy import select
from .store import identity, state, discord_jobs
from .alert_format import OPTION, FUTURE, CT

VERSION = 'option-alert-ownership-v1'
LABELS = {'spy_morning':'SPY 0DTE', 'options_0dte':'OPTIONS 0DTE',
          'options_ideas':'OPTIONS INTRADAY', 'swing':'OPTIONS SWING',
          'smoothers':'SMOOTHERS'}
ENTRY = {'option_idea_new','swing_idea_new','option_setup_new','native_option_entry','entered','open'}
EXIT = {'option_idea_closed','swing_idea_closed','setup_result','native_option_exit','closed'}


def numeric(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)


def contract(row):
    p=row['payload']; raw=(p.get('contract') or {}).get('symbol') or row['symbol']
    m=OPTION.fullmatch(raw or '')
    if not m:return None
    try: expiry=datetime.strptime(m[2],'%y%m%d').date()
    except ValueError:return None
    return dict(symbol='O:'+m[0].removeprefix('O:'),underlying=m[1],expiration=expiry.isoformat(),
                type='CALL' if m[3]=='C' else 'PUT',strike=int(m[4])/1000)


def classification(row, o):
    p=row['payload']; at=p.get('opened_at') or p.get('started') or row['ts']
    dte=(datetime.fromisoformat(o['expiration']).date()-datetime.fromtimestamp(at,CT).date()).days
    horizon=p.get('holding_horizon') or ('swing' if p.get('track')=='swing' or row.get('source') in ('swing_ideas','smoothers') else 'intraday')
    if dte==0:horizon='intraday';category='spy_morning' if o['underlying']=='SPY' else 'options_0dte'
    elif p.get('project')=='smoothers' or row.get('source')=='smoothers':category='smoothers';horizon='swing'
    elif horizon=='swing':category='swing'
    else:category='options_ideas'
    return category,horizon


def quote_error(q, now):
    if not isinstance(q,dict):return 'No option quote'
    if not all(numeric(q.get(k)) for k in ('bid','ask','ts')):return 'Incomplete option quote'
    # 60s, not 10s: the Discord delivery tick does webhook verifications and sends
    # before it assesses, so a quote that was fresh at selection can legitimately be
    # tens of seconds old by assess time. The entry expiry remains the hard
    # actionability bound; this gate only rejects genuinely stale data.
    if not 0<=now-q['ts']<=60:return 'Stale or future option quote'
    if not 0<q['bid']<q['ask']:return 'Invalid or locked option quote'
    if (q['ask']-q['bid'])/((q['ask']+q['bid'])/2)>.25:return 'Option spread exceeds 25%'
    return None


def source_key(row):
    p=row['payload']
    # Producer id stays stable from entry through updates and exit.
    return identity(row.get('source'),p.get('id') or p.get('setup_trial_id') or p.get('source_id') or row['id'])


def read_trade(db,c,trade_id):
    path=db.get(c,'publication:index:'+trade_id)
    return (path,db.get(c,path)) if path else (None,None)


def assess(db,c,row,now):
    """Idempotent admission; called under one global publication transaction lock."""
    key='publication:event:'+str(row['id'])
    prior=db.get(c,key)
    if prior:return prior
    db.locked_get(c,'publication:lock',{})
    prior=db.get(c,key)
    if prior:return prior
    p=row['payload']; status=p.get('status',''); o=contract(row)
    def result(action,reason='',**extra):
        value=dict(action=action,reason=reason,at=now,**extra)
        db.put(c,key,value)
        if action in ('research','suppressed'):
            day=datetime.fromtimestamp(now,CT).date().isoformat()
            db.put(c,'publication:decision:'+day+':'+str(row['id']),dict(action=action,reason=reason,symbol=row['symbol'],at=now,**{k:v for k,v in extra.items() if k in ('trade_id','conflict_trade_id')}))
        return value
    if status in ('notification_test','spy_morning_brief','spy_chart_prompt') or p.get('asset')=='future' or FUTURE.fullmatch(row['symbol']):
        return result('passthrough')
    if status in ('entered','open','closed') and row.get('source')=='engine':
        return result('research','Paper accounting is separate from option research alerts')
    if p.get('research_only') and status=='native_option_entry':
        return result('research','Morning observations have no selected trade exit; retained for research')
    if status in ('secondary_review','project_observation'):
        return result('research','Context/source mirror, not a new option trade')
    if not o:
        return result('research','Underlying-only setup: no selected option contract')
    category,horizon=classification(row,o)
    source=source_key(row); link=db.get(c,'publication:source:'+source)
    exit_admitted=False
    if not link and status=='native_option_exit' and p.get('project')=='smoothers':
        # Josh 2026-10-26: Smoothers exits alert for every tracked position,
        # even when the entry never had a detailed publication (compact-batch
        # only, or no detailed entry intent at all). Admit the exit by opening
        # its publication trade record here, then let the normal linked-exit
        # path below run. Entry publication is unchanged, other programs keep
        # the published-entry requirement, and a position whose detailed entry
        # WAS admitted keeps the existing entry-delivered gates (its link is
        # found above, so this branch never fires for it). Category/horizon
        # are the program's own: classification() at exit time would call a
        # Friday-expiring contract intraday/0DTE and misroute the alert.
        trade_id=identity(VERSION,row['id']); day=datetime.fromtimestamp(now,CT).date().isoformat()
        path='publication:trade:'+day+':'+trade_id
        t=dict(id=trade_id,category='smoothers',horizon='swing',contract=o,state='open',created=now,
               entry_event_id=str(row['id']),primary_source=row.get('source'),source_id=p.get('id'),
               supporting_sources=[],entry=p.get('entry'),entry_quote=p.get('last_quote') or p.get('quote'),pnl=None,outcome=None)
        db.put(c,path,t);db.put(c,'publication:index:'+trade_id,path)
        db.put(c,'publication:source:'+source,dict(trade_id=trade_id,supporting=False))
        link=dict(trade_id=trade_id,supporting=False); exit_admitted=True
    if status not in ENTRY and not link:
        return result('research','No published entry for this lifecycle')
    if link:
        path,t=read_trade(db,c,link['trade_id'])
        if not t:return result('research','Publication record unavailable')
        if link.get('supporting'):
            return result('suppressed','Supporting study cannot manage the primary trade',trade_id=t['id'])
        if status in ENTRY:return result('suppressed','Initial alert already recorded',trade_id=t['id'])
        if t.get('publication_block') or t['state']!='open':return result('suppressed','Trade lifecycle already ended',trade_id=t['id'])
        event='EXIT' if status in EXIT else 'DATA GAP' if 'unresolved' in status else 'UPDATE'
        material={k:p.get(k) for k in ('premium_stop','premium_target','stop','target','exit','exit_reason','idea_status','study_status')}
        # Premium milestone alerts are material; quote fluctuations alone are not.
        if '_update_' in status:material['milestone']=status.split('_update_',1)[1]
        signature=identity(event,material)
        if signature==t.get('last_update'):return result('suppressed','Unchanged update',trade_id=t['id'])
        t['last_update']=signature
        if event in ('EXIT','DATA GAP'):
            t.update(state='unresolved' if event=='DATA GAP' or p.get('outcome')=='unresolved' else 'closed',finished=now,
                     pnl=p.get('pnl'),outcome=p.get('outcome'))
        db.put(c,path,t)
        return result('publish',trade_id=t['id'],category=t['category'],event=event,contract=o,
                      **(dict(exit_admitted_without_entry=True) if exit_admitted else {}),
                      payload={**p,'publication':dict(trade_id=t['id'],category=t['category'],event=event,contract=o,horizon=t['horizon'])})
    q=p.get('last_quote') or p.get('quote')
    reason=quote_error(q,now)
    if reason:return result('research',reason)
    if now-row['ts']>120 or now-row['ts']<0:return result('research','Expired entry notification')
    if p.get('expires_at') is not None and now>=p['expires_at']:return result('research','Entry deadline passed')
    if datetime.fromisoformat(o['expiration']).date()<datetime.fromtimestamp(now,CT).date():return result('research','Expired contract')
    # One active idea per underlying, direction and category; alternate contracts are supporting research.
    scope=identity(o['underlying'],o['type'],category,horizon)
    existing=db.get(c,'publication:active:'+scope)
    if existing:
        _,t=read_trade(db,c,existing)
        if t and t['state']=='open':
            t['supporting_sources']=list(dict.fromkeys(t.get('supporting_sources',[])+[str(p.get('strategy') or row.get('source'))]))
            path,_=read_trade(db,c,existing);db.put(c,path,t)
            db.put(c,'publication:source:'+source,dict(trade_id=existing,supporting=True))
            return result('suppressed','Same underlying, direction and alert category already open',trade_id=existing)
    direction_scope=identity(o['underlying'],horizon)
    active=db.get(c,'publication:direction:'+direction_scope,[])
    still_open=[]
    for active_id in active:
        _,other=read_trade(db,c,active_id)
        if other and other['state']=='open':still_open.append(active_id)
        if other and other['state']=='open' and other['contract']['type']!=o['type']:
            return result('research','Opposing active idea requires review',conflict_trade_id=other['id'])
    trade_id=identity(VERSION,row['id']); day=datetime.fromtimestamp(now,CT).date().isoformat()
    path='publication:trade:'+day+':'+trade_id
    t=dict(id=trade_id,category=category,horizon=horizon,contract=o,state='open',created=now,
           entry_event_id=str(row['id']),primary_source=row.get('source'),source_id=p.get('id'),
           supporting_sources=[],entry=p.get('entry'),entry_quote=q,pnl=None,outcome=None)
    db.put(c,path,t);db.put(c,'publication:index:'+trade_id,path)
    db.put(c,'publication:active:'+scope,trade_id);db.put(c,'publication:direction:'+direction_scope,still_open+[trade_id])
    db.put(c,'publication:source:'+source,dict(trade_id=trade_id,supporting=False))
    pub=dict(trade_id=trade_id,category=category,event='ENTRY',contract=o,horizon=horizon)
    return result('publish',trade_id=trade_id,category=category,event='ENTRY',contract=o,payload={**p,'publication':pub})


def _smoothers_dte(o,week):
    """Inclusive Monday-to-expiry DTE, same convention as smoothers_messages.entry."""
    try:
        return (datetime.fromisoformat(o['expiration']).date()-datetime.fromisoformat(week).date()).days+1
    except (TypeError,ValueError):
        return None


def _smoothers_board_line(p):
    """Shadow-board cost segment (2026-10-31), present only when the
    publication carries generation-time cost fields (older publications
    render exactly as before)."""
    rank=p.get('cost_rank');spread=p.get('entry_spread_pct');rent=p.get('entry_rent_pct_day');key=p.get('cost_key')
    if not (numeric(rank) and numeric(spread) and numeric(rent) and numeric(key)):return None
    line=f"Board cost #{int(rank)} · Spread {spread:.1f}% · Rent {rent:.1f}%/day · Cost {key:.1f}"
    if p.get('friction'):line+=f" · ⚠ FRICTION — {p.get('friction_reason') or 'wide spread'}"
    return line


def _smoothers_horizon_line(p,reason):
    """Wednesday-horizon outcome for an exit publication that carries
    one; None for publications from before the horizon existed."""
    h=p.get('horizon')
    if not isinstance(h,dict) or not h:return None
    if h.get('status')=='scored':
        line=(f"Wednesday horizon: scored out {h.get('date')} at chain bid ${h['chain_bid']:,.2f} "
              f"· premium {h['premium_pct']:+.1f}% (net ${h['premium_net']:+,.2f})")
        if reason=='target':line+=' · this touch is a statistic only'
        return line
    return f"Wednesday horizon: chain bid unavailable — premium unmeasurable ({h.get('reason') or 'no_chain_data'})"


def _format_smoothers_entry(p,pub,o):
    """Compact human-readable Smoothers entry alert.

    Internal bookkeeping (trade IDs, event IDs, quote-source diagnostics)
    stays in the database; the Discord message carries only what a human
    needs to evaluate the entry.
    """
    lines=[f"{LABELS['smoothers']} | ENTRY | {o['underlying']} {o['strike']:g} {o['type']} · {o['expiration']}"]
    stock=[];entry_px=p.get('entry_price');target_px=p.get('target_price') or p.get('underlying_target')
    if numeric(entry_px) and numeric(target_px):
        # entry_price is the frozen first-hour model reference, never a live
        # price — label it with its freeze time (2026-10-26 roster lesson).
        met=p.get('model_entry_time');label='(entry ref)'
        if numeric(met) and met>0:
            try:label=f"(ref {datetime.fromtimestamp(met,CT).strftime('%H:%M CT')})"
            except (OverflowError,OSError,ValueError):pass
        stock.append(f"Stock ${entry_px:,.2f} {label} → target ${target_px:,.2f}")
    elif numeric(target_px):
        stock.append(f"Target ${target_px:,.2f}")
    # Live underlying captured in the publication batch, stamped with the
    # trade's own time; absent whenever the batch read was unavailable,
    # stale, or carries no trustworthy time — the labeled reference then
    # stands alone.
    live=p.get('live_underlying') or {}
    if live.get('status')=='available' and numeric(live.get('price')) \
            and numeric(live.get('ts')) and live['ts']>0:
        seg=(f"live ${live['price']:,.2f} @ "
             +datetime.fromtimestamp(live['ts'],CT).strftime('%H:%M CT'))
        stock.append(seg)
    est=p.get('est_return_pct')
    if numeric(est):
        stock.append(f"Est return {est:+.0f}%")
    if stock:lines.append(' · '.join(stock))
    board=_smoothers_board_line(p)
    if board:lines.append(board)
    q=p.get('last_quote') or p.get('quote') or {}
    opt=[f"${q['bid']:.2f}/${q['ask']:.2f}" if q and all(numeric(q.get(k)) for k in ('bid','ask')) else 'quote unavailable',
         f"Strike ${o['strike']:g}"]
    dte=_smoothers_dte(o,p.get('week'))
    if dte is not None:opt.append(f"{dte} DTE")
    if p.get('expires_at'):opt.append('enter by '+datetime.fromtimestamp(p['expires_at'],CT).strftime('%H:%M CT'))
    lines.append('Option '+' · '.join(opt))
    lines.append(f"Exit: {p.get('exit_rule') or 'Underlying target, otherwise Friday close'}")
    return '\n'.join(lines)[:1950]


def _format_smoothers_exit(p,pub,o):
    """Compact human-readable Smoothers exit alert.

    Covers underlying-target hits and Friday week-closes. Internal
    bookkeeping (trade IDs, event IDs, quote-source diagnostics) stays in
    the database; the Discord message carries only the outcome.
    """
    title=f"{LABELS['smoothers']} | EXIT | {o['underlying']} {o['strike']:g} {o['type']} · {o['expiration']}"
    reason=p.get('exit_reason')
    tgt=p.get('underlying_target')
    q=p.get('last_quote') or p.get('quote') or {}
    bid,ask,ts=q.get('bid'),q.get('ask'),q.get('ts')
    quote_ok=all(numeric(v) for v in (bid,ask,ts))
    entry=p.get('entry')
    if reason=='target':
        line2=f"Target hit · underlying reached ${tgt:,.2f}" if numeric(tgt) else "Target hit"
        if quote_ok:
            stamp=datetime.fromtimestamp(ts,CT).strftime('%H:%M CT')
            line3=f"Option ${bid:.2f}/${ask:.2f} @ {stamp}"
            if numeric(entry) and entry>0:
                ret=((bid+ask)/2/entry-1)*100
                line3+=f" · modeled entry ${entry:.2f} ({ret:+.0f}% ref)"
        else:
            line3="Option quote unavailable"
    else:
        status=p.get('signal_status')
        if status=='UNRESOLVED':
            line2="Week closed · outcome unresolved (missing observations)"
        elif numeric(tgt):
            line2=f"Week closed · target ${tgt:,.2f} not hit"
        else:
            line2="Week closed · target not hit"
        final=p.get('exit_underlying')
        line3=f"Underlying ${final:,.2f} at close" if numeric(final) else None
    lines=[title,line2]
    if line3:lines.append(line3)
    horizon=_smoothers_horizon_line(p,reason)
    if horizon:lines.append(horizon)
    return '\n'.join(lines)[:1950]


def format_message(row):
    p=row['payload']; pub=p['publication']; o=pub['contract']
    if pub.get('category')=='smoothers' and pub.get('event')=='ENTRY':
        return _format_smoothers_entry(p,pub,o)
    if pub.get('category')=='smoothers' and pub.get('event')=='EXIT':
        return _format_smoothers_exit(p,pub,o)
    q=p.get('last_quote') or p.get('quote') or {}
    lines=[f"{LABELS[pub['category']]} | {pub['event']} | {o['underlying']} {o['strike']:g} {o['type']} · {o['expiration']}",
           '[RESEARCH SIGNAL] — no broker order',f"Trade ID: {pub['trade_id']}",f"Holding plan: {pub['horizon']}"]
    if q and all(numeric(q.get(k)) for k in ('bid','ask','ts')):
        lines.append(f"Observed bid / ask: ${q['bid']:.2f} / ${q['ask']:.2f} · {datetime.fromtimestamp(q['ts'],CT).strftime('%H:%M:%S CT')}")
        lines.append('Quote source: '+str(q.get('source') or 'not supplied'))
        lines.append(f"Spread: ${q['ask']-q['bid']:.2f} · Ask cost per standard contract: ${q['ask']*100:.2f}")
    for key,label in [('entry','Modeled entry premium'),('premium_stop','Premium stop'),('premium_target','Premium target'),('underlying_stop','Underlying invalidation'),('underlying_invalidation','Underlying invalidation'),('underlying_target','Underlying target'),('exit','Modeled exit premium')]:
        if numeric(p.get(key)):lines.append(f"{label}: ${p[key]:.2f}")
    # Compass evidence block (audit #8): appended to the verbose 0DTE alert,
    # which stays as the feed canary per Josh's 2026-10-09 decision.
    if pub.get('category')=='options_0dte' and isinstance(p.get('evidence_snapshot'),dict):
        try:
            from .entry_evidence import alert_lines
            lines.extend(alert_lines(p['evidence_snapshot']))
        except Exception:
            pass
    if not p.get('premium_stop') and numeric(p.get('stop')):lines.append(f"Premium stop: ${p['stop']:.2f}")
    if not p.get('premium_target') and numeric(p.get('target')):lines.append(f"Premium target: ${p['target']:.2f}")
    deadline=p.get('exit_deadline') or p.get('flatten_at')
    if deadline:lines.append('Exit deadline: '+datetime.fromtimestamp(deadline,CT).strftime('%Y-%m-%d %H:%M CT'))
    if p.get('expires_at'):lines.append('Entry deadline: '+datetime.fromtimestamp(p['expires_at'],CT).strftime('%Y-%m-%d %H:%M:%S CT'))
    if p.get('exit_rule'):lines.append('Exit rule: '+p['exit_rule'])
    lines.append('Source: '+str(p.get('strategy') or row.get('source')))
    if p.get('exit_reason') or p.get('reason'):lines.append('Reason: '+str(p.get('exit_reason') or p.get('reason')))
    lines.append(f"Event #{row['id']} | "+datetime.fromtimestamp(row['ts'],CT).strftime('%Y-%m-%d %H:%M:%S CT'))
    return '\n'.join(lines)[:1950]


def _embed_field(name,value,inline=True):return dict(name=name,value=str(value),inline=inline)


def _embed_smoothers_entry(p,pub,o):
    """Rich-embed Smoothers entry (Josh 2026-10-26): the pre-consolidation
    layout, carrying exactly the fields _format_smoothers_entry shows —
    no legacy-only fields (quality/history blocks, signal numbers)."""
    fields=[]
    entry_px=p.get('entry_price');target_px=p.get('target_price') or p.get('underlying_target')
    if numeric(entry_px) and numeric(target_px):
        label='(entry ref)'
        met=p.get('model_entry_time')
        if numeric(met) and met>0:
            try:label=f"(ref {datetime.fromtimestamp(met,CT).strftime('%H:%M CT')})"
            except (OverflowError,OSError,ValueError):pass
        fields.append(_embed_field('Underlying Entry',f"${entry_px:,.2f} {label}"))
    if numeric(target_px):fields.append(_embed_field('Underlying Target',f"${target_px:,.2f}"))
    live=p.get('live_underlying') or {}
    if live.get('status')=='available' and numeric(live.get('price')) \
            and numeric(live.get('ts')) and live['ts']>0:
        fields.append(_embed_field('Live Underlying',f"${live['price']:,.2f} @ "
            +datetime.fromtimestamp(live['ts'],CT).strftime('%H:%M CT')))
    est=p.get('est_return_pct')
    if numeric(est):fields.append(_embed_field('Est Return',f"{est:+.0f}%"))
    board=_smoothers_board_line(p)
    if board:fields.append(_embed_field('Board Cost',board))
    q=p.get('last_quote') or p.get('quote') or {}
    fields.append(_embed_field('Option Quote',
        f"${q['bid']:.2f}/${q['ask']:.2f}" if q and all(numeric(q.get(k)) for k in ('bid','ask')) else 'quote unavailable'))
    fields.append(_embed_field('Strike',f"${o['strike']:g}"))
    dte=_smoothers_dte(o,p.get('week'))
    if dte is not None:fields.append(_embed_field('DTE',f"{dte}"))
    if p.get('expires_at'):fields.append(_embed_field('Enter By',datetime.fromtimestamp(p['expires_at'],CT).strftime('%H:%M CT')))
    fields.append(_embed_field('Exit',p.get('exit_rule') or 'Underlying target, otherwise Friday close',False))
    return dict(title=f"{LABELS['smoothers']} | ENTRY | {o['underlying']} {o['strike']:g} {o['type']} · {o['expiration']}",
                color=3066993 if o['type']=='CALL' else 15158332,fields=fields)


def _embed_smoothers_exit(p,pub,o):
    """Rich-embed Smoothers exit: same layout rule as the entry embed —
    exactly the fields _format_smoothers_exit shows, nothing legacy-only."""
    reason=p.get('exit_reason'); tgt=p.get('underlying_target')
    q=p.get('last_quote') or p.get('quote') or {}
    bid,ask,ts=q.get('bid'),q.get('ask'),q.get('ts')
    quote_ok=all(numeric(v) for v in (bid,ask,ts))
    entry=p.get('entry'); fields=[]
    if reason=='target':
        result=f"Target hit · underlying reached ${tgt:,.2f}" if numeric(tgt) else "Target hit"
        fields.append(_embed_field('Result',result,False))
        if quote_ok:
            fields.append(_embed_field('Option Quote',f"${bid:.2f}/${ask:.2f} @ "
                +datetime.fromtimestamp(ts,CT).strftime('%H:%M CT')))
            if numeric(entry) and entry>0:
                ret=((bid+ask)/2/entry-1)*100
                fields.append(_embed_field('Modeled Entry',f"${entry:.2f} ({ret:+.0f}% ref)"))
        else:fields.append(_embed_field('Option Quote','unavailable'))
        color=2067276
    else:
        status=p.get('signal_status')
        if status=='UNRESOLVED':result="Week closed · outcome unresolved (missing observations)"
        elif numeric(tgt):result=f"Week closed · target ${tgt:,.2f} not hit"
        else:result="Week closed · target not hit"
        fields.append(_embed_field('Result',result,False))
        final=p.get('exit_underlying')
        if numeric(final):fields.append(_embed_field('Underlying at Close',f"${final:,.2f}"))
        color=10038562
    horizon=_smoothers_horizon_line(p,reason)
    if horizon:fields.append(_embed_field('Wednesday Horizon',horizon,False))
    return dict(title=f"{LABELS['smoothers']} | EXIT | {o['underlying']} {o['strike']:g} {o['type']} · {o['expiration']}",
                color=color,fields=fields)


def format_payload(row):
    """Discord webhook body for an admitted publication.

    Smoothers ENTRY/EXIT publications render as rich embeds (username
    'Smoothers', the pre-consolidation layout) with exactly today's field
    set; every other publication keeps the compact text body. Used by the
    native outbox sender only — the legacy delivery path keeps
    format_message text for all categories.
    """
    p=row['payload']; pub=p['publication']
    if pub.get('category')=='smoothers' and pub.get('event') in ('ENTRY','EXIT'):
        embed=_embed_smoothers_entry(p,pub,pub['contract']) if pub['event']=='ENTRY' else _embed_smoothers_exit(p,pub,pub['contract'])
        return {'username':'Smoothers','allowed_mentions':{'parse':[]},'embeds':[embed]}
    return {'content':format_message(row),'username':'Market Compass','allowed_mentions':{'parse':[]}}


def entry_delivered(c,t):
    if t.get('native_delivered'):return True
    event=t['entry_event_id']
    return bool(event.isdigit() and c.execute(select(discord_jobs.c.event_id).where(
        discord_jobs.c.event_id==int(event),discord_jobs.c.status=='sent')).first())


def block_entry(db,c,trade_id,reason,now):
    path,t=read_trade(db,c,trade_id)
    if t:
        t.update(publication_block=reason,finished=now)
        if t['state']=='open':t['state']='unpublished'
        db.put(c,path,t)


def report(db,c,day):
    records=list(db.prefix(c,'publication:trade:'+day+':').values())
    counts=Counter(t['category'] for t in records)
    rows=[]
    for category in LABELS:
        ts=[t for t in records if t['category']==category]
        ids=[int(t['entry_event_id']) for t in ts if t['entry_event_id'].isdigit()]
        delivered=c.execute(select(discord_jobs.c.event_id).where(discord_jobs.c.event_id.in_(ids),discord_jobs.c.status=='sent')).scalars().all() if ids else []
        confirmed=[t for t in ts if entry_delivered(c,t)]
        resolved=[t for t in confirmed if t['state']=='closed' and numeric(t.get('pnl'))]
        rows.append(dict(category=category,ideas=counts[category],delivered_entries=len(delivered)+sum(bool(t.get('native_delivered')) for t in ts),
            open=sum(t['state']=='open' for t in ts),closed=sum(t['state']=='closed' for t in ts),
            unresolved=sum(t['state']=='unresolved' for t in ts),
            unpublished=sum(bool(t.get('publication_block')) for t in ts),
            resolved_delivered=len(resolved),net_pnl=sum(t['pnl'] for t in resolved) if resolved else None))
    decisions=list(db.prefix(c,'publication:decision:'+day+':').values())
    reasons=Counter(d['reason'] for d in decisions)
    return dict(version=VERSION,groups=rows,records=records,withheld=[dict(reason=k,count=v) for k,v in reasons.most_common()],
        basis='Unique admitted option ideas from activation onward. Confirmed delivery is separate from admission. Primary-source outcomes only; supporting experiments never add returns. Legacy history is not reclassified. Original senders outside this outbox remain separately reported.')
