"""Pure Discord payloads for the native weekly strategy, usable as previews."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .market import number,ts

VERSION='smoothers-alerts-v1'
TIERS=('A+','A','B','WATCH')
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


def envelope(p,event,description,fields,color):
    return {'username':'Smoothers','allowed_mentions':{'parse':[]},'embeds':[{
        'title':f"SMOOTHERS | {event} | {p['ticker']} · {p.get('direction','Unavailable')}",
        'description':description,'color':color,'fields':fields,
        'footer':{'text':f"Smoothers · Weekly swing · Week {p['week']} · Signal {p['id']}"}}]}


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
    fields=[field('Underlying Entry',dollar(p.get('entry_price'))),field('Underlying Target',dollar(p.get('target_price'))),
        *contract_fields(p),field('Option Reference Premium',dollar(p.get('entry_premium'))),
        field('DTE at Entry',f'{dte} days' if dte is not None else 'Unavailable'),
        field('Est return',pct(p.get('est_return_pct'),0)),
        field('Quality Tier',f"{p.get('quality_tier','WATCH')} • Featured #{p.get('featured_rank') or 0:02d} • Overall #{p.get('quality_rank') or 0:02d} • Score {p.get('quality_score') or 0:.1f}/100",False)]
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
    return envelope(p,'ENTRY SIGNAL','Signal only · Weekly swing. Underlying levels and option quotes are references, not broker fills. Featured rank uses target / ATR; the score is not a win probability.',fields,3066993 if p.get('direction')=='CALL' else 15158332)


def target(p):
    bar=p.get('touch_bar') or {};q=p.get('exit_quote') or {};mid=number(q.get('midpoint'))
    valid=q.get('status') in ('available','wide_spread') and mid is not None
    fields=[*contract_fields(p),field('Underlying Entry',dollar(p.get('entry_price'))),field('Target Level',dollar(p.get('target_price'))),
        *[field('1m '+k.title(),dollar(bar.get(k))) for k in ('high','low','close')],
        field('Target Minute',clock(p.get('resolution_time'))),field('Observed At',clock(p.get('touch_detected_at'))),
        field('Option Entry Reference',dollar(p.get('entry_premium')))]
    if valid:
        fields += [field('Option Bid / Ask',dollar(q.get('bid'))+' / '+dollar(q.get('ask'))),field('Option Mid at Observation',dollar(mid)),
            field('Quote Time',clock(q.get('quote_at_ms'))),field('Quote Status',q['status'])]
        if number(p.get('entry_premium')) and p['entry_premium']>0:fields.append(field('Option Return vs Entry',pct((mid/p['entry_premium']-1)*100)))
    else:fields.append(field('Option Quote','Unavailable — underlying target observation retained',False))
    if p.get('coverage_missing'):fields.append(field('Coverage','Missing minutes; an earlier target touch cannot be excluded',False))
    return envelope(p,'TARGET HIT','Exit signal · Weekly swing · Completed 1-minute bar. Target Level is the strategy trigger; High/Low/Close are observed prices. The option snapshot is a reference, not a verified fill or realized profit.',fields,2067276)


def close(p):
    incomplete=p.get('status')=='UNRESOLVED'
    return envelope(p,'WEEK CLOSED',
        'Observation incomplete; a target hit cannot be ruled out.' if incomplete else 'Target not observed by the weekly deadline. This strategy outcome is not a verified option loss.',
        [*contract_fields(p),field('Underlying Entry',dollar(p.get('entry_price'))),field('Underlying Target',dollar(p.get('target_price'))),
         field('Final 1m Close',dollar(p.get('exit_underlying'))),field('Closed At',clock(p.get('resolution_time'))),
         field('Result','Unresolved — missing observations' if incomplete else 'Target not hit',False)],10038562)


def roster(signals,week,now,errors=0):
    calls=sum(p['direction']=='CALL' for p in signals);puts=len(signals)-calls
    blocks=[f'Week {week} · {clock(now)}\n{len(signals)} observations | PUTs: {puts} CALLs: {calls} | Processing errors: {errors}\n'
        'A+ = top 10 eligible signals by target / ATR. WATCH = research only. Cost and return are option estimates; stock levels are references. Scores and recorded win rates are not win probabilities.']
    if not signals:blocks.append('No signals selected.' if not errors else 'Selection incomplete; review processing errors.')
    for tier in TIERS:
        rows=sorted((p for p in signals if p.get('quality_tier','WATCH')==tier),key=lambda p:p.get('featured_rank' if tier=='A+' else 'quality_rank') or 9999)
        if not rows:continue
        blocks.append(f'**{tier} ({len(rows)})**')
        for p in rows:
            config=p.get('config') or {};c=p.get('contract') or {}
            # Est return is only meaningful against the displayed quote; a
            # stale modeled return with no usable quote renders Unavailable.
            ret=p.get('est_return_pct') if quote_ok(p) else None
            blocks.append(f"**{p['ticker']} {p['direction']} · {tier} · "+(f"F{p['featured_rank']:02d}" if p.get('featured_rank') else f"Q{p.get('quality_rank','—')}")+f"**\nStock {dollar(p.get('entry_price'))} → target {dollar(p.get('target_price'))} | Strike {dollar(c.get('strike'))}\n"
                f"{quote_line(p)} | Est return {pct(ret,0)} | ATR {p.get('target_atr_mult','Unavailable')} | Score {p.get('quality_score','Unavailable')} | Backtest {pct(config['backtest_wr']*100,0) if config.get('backtest_wr') is not None else 'Unavailable'} | Recorded {record(p.get('stats_at_entry'))}")
    chunks=[];header=f'**SMOOTHERS | WEEKLY ROSTER | {week}**';current=header
    for block in blocks:
        if len(current)+len(block)+2>1900:chunks.append(current);current=header+' | CONTINUED'
        current+='\n\n'+block
    chunks.append(current)
    return [{'username':'Smoothers','content':x,'allowed_mentions':{'parse':[]}} for x in chunks]
