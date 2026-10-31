"""Pure Discord payloads for the native weekly strategy, usable as previews."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .market import number,ts

VERSION='smoothers-alerts-v3'
TIERS=('A+','A','B','WATCH')
# Shadow-board presentation (Josh-approved Smoothers plan, 2026-10-30):
# the board is a scored scorecard, not a buy list. Rows are ordered by
# the executable-cost key (compass/smoothers_cost.py), quality tiers are
# stored on payloads for audit but never presented as a ranking, and
# premium scoring closes at the Wednesday horizon.
BOARD_HEADER='SMOOTHERS — SHADOW BOARD'
BOARD_TERMS='scored only · not a buy list · Horizon: Wednesday close · ranked by executable cost'
CT=ZoneInfo('America/Chicago')


def dollar(value):
    n=number(value)
    return f'${n:,.2f}' if n is not None else 'Unavailable'


def pct(value,digits=1):
    n=number(value)
    return f'{n:+.{digits}f}%' if n is not None else 'Unavailable'


def clock(value):
    try:return datetime.fromtimestamp(ts(value),CT).strftime('%a %b %d %H:%M:%S CT')
    except (TypeError,ValueError,OverflowError):return 'Unavailable'


def record(stats):
    if not stats or 'wins' not in stats or 'losses' not in stats:return 'Unavailable'
    w,l=stats['wins'],stats['losses'];n=w+l
    return f'{w}W/{l}L ('+(f'{w/n*100:.0f}%' if n else 'no resolved trades')+')'


def field(name,value,inline=True):return dict(name=name,value=str(value),inline=inline)


def quote_stamp(value):
    try:return datetime.fromtimestamp(ts(value),CT).strftime('%H:%M CT')
    except (TypeError,ValueError,OverflowError):return 'time unavailable'


def quote_ok(p):
    """A roster row may show option pricing only when the publication-batch
    quote is usable; otherwise any modeled number is stale."""
    q=p.get('quote') or {}
    return (q.get('status') in ('available','wide_spread')
            and number(q.get('bid')) is not None and number(q.get('ask')) is not None)


def quote_line(p):
    """Publication-batch option quote for a roster row, with its timestamp.

    Uses only the batch-refreshed quote from refresh_entry_quotes(); a stale
    modeled premium is never shown as a price. No fresh quote renders an
    honest 'unavailable' instead of a stale number."""
    q=p.get('quote') or {}
    if quote_ok(p):
        mid=number(q.get('midpoint'))
        return (f"Option {dollar(q['bid'])}/{dollar(q['ask'])}"
                +(f' (mid {dollar(mid)})' if mid is not None else '')
                +f" @ {quote_stamp(q.get('quote_at_ms'))}")
    return 'Option quote unavailable'


def stock_text(p):
    """Stock segment for entry alerts: the entry reference, labeled with its
    freeze time, plus the live underlying captured in the publication batch.

    entry_price is the strategy's model reference — the close of the first
    session hour, frozen at generation (model_entry_time) — and is never a
    live price; printing it bare beside fresh option quotes made it read as
    one (2026-10-26: META printed 636.14 with the market at 643.53). The
    live segment appears only when refresh_entry_quotes() captured a fresh
    snapshot (gated at fetch, same 30s convention as option quotes) and is
    always stamped with the trade's own time; otherwise the labeled
    reference stands alone."""
    stamp=quote_stamp(p.get('model_entry_time'))
    label=f'(ref {stamp})' if stamp!='time unavailable' else '(entry ref)'
    text=f"{dollar(p.get('entry_price'))} {label}"
    live=p.get('live_underlying') or {}
    price=number(live.get('price'))
    live_stamp=quote_stamp(live.get('trade_at_ms'))
    # A live price without a trustworthy time is the failure mode being
    # fixed; it does not print.
    if live.get('status')=='available' and price is not None and live_stamp!='time unavailable':
        text+=f" · live {dollar(price)} @ {live_stamp}"
    return text


def cost_text(p):
    """Board cost segment for one pick: 'Spread x% · Rent y%/day · Cost z',
    from the generation-time fields smoothers_cost.annotate() stored.
    Unrankable picks (no usable entry quote) say so instead of a number."""
    spread=number(p.get('entry_spread_pct'));rent=number(p.get('entry_rent_pct_day'));key=number(p.get('cost_key'))
    if spread is None or rent is None or key is None:return 'Cost unavailable — entry quote missing'
    return f'Spread {spread:.1f}% · Rent {rent:.1f}%/day · Cost {key:.1f}'


def friction_text(p):
    if p.get('friction'):return f" ⚠ FRICTION — {p.get('friction_reason') or 'wide spread'}"
    return ''


def horizon_field(p):
    """Wednesday-horizon scoring outcome, when the pick was scored out
    before its underlying outcome resolved (or before Friday close)."""
    h=p.get('horizon') or {}
    if not h:return None
    if h.get('status')=='scored':
        return field('Wednesday Horizon',
            f"Premium scored out Wed {h.get('date')} at chain bid {dollar(h.get('chain_bid'))} "
            f"({pct(h.get('premium_pct'))} vs entry ask; net {dollar(h.get('premium_net'))}). "
            'Later touches are tracked as statistics only.',False)
    return field('Wednesday Horizon',
        f"Wednesday chain bid unavailable — premium outcome unmeasurable ({h.get('reason') or 'no_chain_data'}).",False)


def envelope(p,event,description,fields,color):
    # Shadow board (2026-10-31): no tier is presented as a recommendation,
    # so the old 'A+ Featured' entry title is gone; every event uses the
    # plain scored-notice title.
    title = f"SMOOTHERS | {event} | {p['ticker']} · {p.get('direction','Unavailable')}"
    return {'username':'Smoothers','allowed_mentions':{'parse':[]},'embeds':[{
        'title':title,
        'description':description,'color':color,'fields':fields,
        'footer':{'text':f"Smoothers · Shadow board · Week {p['week']} · Signal {p['id']}"}}]}


def contract_fields(p):
    c=p.get('contract') or {}
    return [field('Contract',c.get('symbol') or 'Unavailable'),field('Strike',dollar(c.get('strike'))),
            field('Expiration',c.get('expiration') or 'Unavailable')]


def entry(p):
    config=p.get('config') or {};contract=p.get('contract') or {};q=p.get('quote') or {}
    dte=p.get('dte_days')
    if dte is None and contract.get('expiration'):
        # Preserve original inclusive Monday-to-expiry convention.
        dte=(datetime.fromisoformat(contract['expiration']).date()-datetime.fromisoformat(p['week']).date()).days+1
    # Shadow board header (2026-10-31): the board rank is executable cost,
    # not the quality tier. The tier/score stay on the payload for audit;
    # they are not presented as a ranking anywhere in the alert.
    rank=p.get('cost_rank')
    board_line = (f"Cost #{rank} · "+cost_text(p)+friction_text(p)) if rank else cost_text(p)+friction_text(p)
    fields=[field('Board Cost', board_line, False),
        field('Ranking', 'Executable cost at entry: spread % + rent %/day × 1.908 expected days. Cheapest to trade first; all expiries compete head-to-head.', False),
        field('Underlying Entry',stock_text(p)),field('Underlying Target',dollar(p.get('target_price'))),
        *contract_fields(p),field('Option Reference Premium',dollar(p.get('entry_premium'))),
        field('DTE at Entry',f'{dte} days' if dte is not None else 'Unavailable'),
        field('Est return',pct(p.get('est_return_pct'),0))]
    # Historical conviction data (5w) — added 2026-10-03
    hist=p.get('historical_stats') or {}
    if hist.get('total',0)>0:
        fields.append(field('Historical Edge (5w)',
            f"Win rate: {hist.get('win_rate',0):.0f}% ({hist.get('wins',0)}/{hist.get('total',0)}) • "
            f"Avg return: {hist.get('avg_return_pct',0):+.1f}% • "
            f"Avg premium: ${hist.get('avg_premium',0):.2f}",False))
        fields.append(field('Avg Days to Target',f"{hist.get('avg_days_to_target',0):.1f} days",True))
    fields+=[
        field('Target / ATR',f"{p['target_atr_mult']:.3f}" if number(p.get('target_atr_mult')) is not None else 'Unavailable'),
        field('ATR component',f"{p.get('quality_atr_score') or 0:.1f}/100"),
        field('Reliability',f"{p.get('quality_reliability_score') or 0:.1f}/100"),
        field('Live WR',record(p.get('stats_at_entry'))),field('WR (last 4)',record(p.get('rolling_at_entry'))),
        field('Backtest WR',pct(config['backtest_wr']*100,0) if config.get('backtest_wr') is not None else 'Unavailable'),
        field('Quote Time',clock(q.get('quote_at_ms'))),field('Quote Status',q.get('status','unavailable'))]
    if config.get('min_fold_wr') is not None:fields.append(field('Min-fold WR',pct(config['min_fold_wr']*100,0)))
    return envelope(p,'SCORED ENTRY','Shadow board · scored only — not a buy list. Underlying levels and option quotes are references, not broker fills. Board order is executable cost (spread + rent), not a quality ranking.',fields,3066993 if p.get('direction')=='CALL' else 15158332)


def target(p):
    bar=p.get('touch_bar') or {};q=p.get('exit_quote') or {};mid=number(q.get('midpoint'))
    valid=q.get('status') in ('available','wide_spread') and mid is not None
    fields=[*contract_fields(p),field('Underlying Entry',stock_text(p)),field('Target Level',dollar(p.get('target_price'))),
        *[field('1m '+k.title(),dollar(bar.get(k))) for k in ('high','low','close')],
        field('Target Minute',clock(p.get('resolution_time'))),field('Observed At',clock(p.get('touch_detected_at'))),
        field('Option Entry Reference',dollar(p.get('entry_premium')))]
    if valid:
        fields += [field('Option Bid / Ask',dollar(q.get('bid'))+' / '+dollar(q.get('ask'))),field('Option Mid at Observation',dollar(mid)),
            field('Quote Time',clock(q.get('quote_at_ms'))),field('Quote Status',q['status'])]
        if number(p.get('entry_premium')) and p['entry_premium']>0:fields.append(field('Option Return vs Entry',pct((mid/p['entry_premium']-1)*100)))
    else:fields.append(field('Option Quote','Unavailable — underlying target observation retained',False))
    if p.get('coverage_missing'):fields.append(field('Coverage','Missing minutes; an earlier target touch cannot be excluded',False))
    hf=horizon_field(p)
    if hf:fields.append(hf)
    return envelope(p,'SCORED HIT','Scored hit · Shadow board · Completed 1-minute bar. Target Level is the strategy trigger; High/Low/Close are observed prices. The option snapshot is a reference, not a verified fill or realized profit.',fields,2067276)


def close(p):
    incomplete=p.get('status')=='UNRESOLVED'
    fields=[*contract_fields(p),field('Underlying Entry',stock_text(p)),field('Underlying Target',dollar(p.get('target_price'))),
         field('Final 1m Close',dollar(p.get('exit_underlying'))),field('Closed At',clock(p.get('resolution_time'))),
         field('Result','Unresolved — missing observations' if incomplete else 'Target not hit',False)]
    hf=horizon_field(p)
    if hf:fields.append(hf)
    return envelope(p,'WEEK CLOSED',
        'Observation incomplete; a target hit cannot be ruled out.' if incomplete else 'Target not observed by the weekly deadline. This strategy outcome is not a verified option loss.',
        fields,10038562)


def horizon(p):
    """Wednesday-horizon scoring notice (plain content payload, like the
    roster): a still-open pick is premium-scored out at Wednesday's chain
    bid; Thu/Fri touches remain tracked as statistics only."""
    c=p.get('contract') or {};h=p.get('horizon') or {}
    strike=c.get('strike');side='C' if p.get('direction')=='CALL' else 'P' if p.get('direction')=='PUT' else ''
    head=f"WEDNESDAY HORIZON — {p['ticker']} {strike:g}{side}" if isinstance(strike,(int,float)) else f"WEDNESDAY HORIZON — {p['ticker']}"
    if h.get('status')=='scored':
        body=(f"Still open at Wed close · Scored out at Wednesday chain bid {dollar(h.get('chain_bid'))} "
              f"· Premium {pct(h.get('premium_pct'))} vs entry ask (net {dollar(h.get('premium_net'))})\n"
              'Thu/Fri touches still tracked for stats, but no longer count as premium outcomes')
    else:
        body=(f"Still open at Wed close · Wednesday chain bid unavailable — premium outcome "
              f"unmeasurable ({h.get('reason') or 'no_chain_data'})\n"
              'Thu/Fri touches still tracked for stats, but no longer count as premium outcomes')
    return {'username':'Smoothers','content':head+'\n'+body,'allowed_mentions':{'parse':[]}}


def roster(signals,week,now,errors=0):
    """The weekly SHADOW BOARD (2026-10-31): every pick, ordered by
    executable cost, labeled scored-only. Quality tiers are not shown —
    they are stored on the payloads for audit, and on the graded record
    they ranked the book backwards (A+ -$26.49/pick vs WATCH +$75.85)."""
    calls=sum(p['direction']=='CALL' for p in signals);puts=len(signals)-calls
    blocks=[f'**{BOARD_HEADER}**\n{BOARD_TERMS}\n'
        f'Week {week} · {clock(now)}\n{len(signals)} observations | PUTs: {puts} CALLs: {calls} | Processing errors: {errors}\n'
        'Cost = entry spread % + rent %/day × 1.908 expected days, from each pick\'s own entry quote. '
        'Premium is scored at executable bid/ask and closes at the Wednesday horizon. Stock levels are references, not fills.']
    if not signals:blocks.append('No signals selected.' if not errors else 'Selection incomplete; review processing errors.')
    ranked=sorted((p for p in signals if p.get('cost_rank')),key=lambda p:p['cost_rank'])
    unranked=sorted((p for p in signals if not p.get('cost_rank')),key=lambda p:(str(p.get('ticker') or ''),str(p.get('direction') or '')))
    def row(p):
        c=p.get('contract') or {}
        # Est return is only meaningful against the displayed quote; a
        # stale modeled return with no usable quote renders Unavailable.
        ret=p.get('est_return_pct') if quote_ok(p) else None
        rank=f"#{p['cost_rank']}" if p.get('cost_rank') else 'unranked'
        return (f"**{p['ticker']} {p['direction']} · {rank}** · Strike {dollar(c.get('strike'))} · exp {c.get('expiration') or 'Unavailable'}\n"
            f"Stock {stock_text(p)} → target {dollar(p.get('target_price'))} | {cost_text(p)}{friction_text(p)}\n"
            f"{quote_line(p)} | Est return {pct(ret,0)}")
    top=[p for p in ranked if p['cost_rank']<=10];rest=[p for p in ranked if p['cost_rank']>10]
    if top:
        blocks.append(f'**TOP — cheapest to trade ({len(top)})**')
        blocks.extend(row(p) for p in top)
    if rest:
        blocks.append(f'**REST OF BOARD — by executable cost ({len(rest)})**')
        blocks.extend(row(p) for p in rest)
    if unranked:
        blocks.append(f'**UNRANKED — entry quote missing ({len(unranked)})**')
        blocks.extend(row(p) for p in unranked)
    chunks=[];header=f'**{BOARD_HEADER} | {week}**';current=header
    for block in blocks:
        if len(current)+len(block)+2>1900:chunks.append(current);current=header+' | CONTINUED'
        current+='\n\n'+block
    chunks.append(current)
    return [{'username':'Smoothers','content':x,'allowed_mentions':{'parse':[]}} for x in chunks]
