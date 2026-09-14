"""Completed daily/weekly structure and attributed, filtered swing-flow evidence."""
from collections import defaultdict
import re
from datetime import date, timedelta
from functools import lru_cache
from .market import day, session, number, calendar


@lru_cache(maxsize=512)
def sessions_between(first, last):
    if first > last:
        return ()
    return tuple(str(t.date()) for t in calendar('XNYS').sessions_in_range(first, last))


def trading_seconds(first, last):
    """Elapsed observable cash-session time; nights, holidays and weekends add zero."""
    if last <= first:
        return 0
    # A months-long interruption cannot be a valid swing observation path.
    if last-first > 120*86400:
        return float('inf')
    return sum(max(0, min(last, session(d)[1])-max(first, session(d)[0]))
               for d in sessions_between(day(first), day(last)))


def hold_deadline(now, expiry, max_sessions):
    dates = sessions_between(day(now), expiry)
    # Always exit before the listed expiry date, including holiday schedules.
    dates = [d for d in dates if d < expiry]
    return session(dates[min(max_sessions, len(dates))-1])[1]-900 if dates else None


def daily_context(rows, now):
    today = date.fromisoformat(day(now))
    expected = sessions_between((today-timedelta(days=160)).isoformat(),
                                (today-timedelta(days=1)).isoformat())
    latest = {}
    for row in sorted(rows, key=lambda r:r.get('id', 0)):
        d = day(row['ts'])
        if d < today.isoformat() and d in expected:
            p = row['payload']
            vals = [number(p.get(k)) for k in ('o','h','l','c','v')]
            if (all(v is not None for v in vals) and min(vals[:4]) > 0 and vals[4] >= 0
                    and vals[1] >= max(vals[0], vals[2], vals[3])
                    and vals[2] <= min(vals[0], vals[3])):
                latest[d] = dict(zip(('o','h','l','c','v'), vals))
    result = dict(day=today.isoformat(), computed_at=now, status='warming_up',
                  reason='Sixty consecutive completed daily sessions and ten completed weeks required')
    if len(expected) < 60 or any(d not in latest for d in expected[-60:]):
        return result
    # Discard any old bars before a gap. Indicators share a continuous price basis.
    dates = []
    for d in reversed(expected):
        if d not in latest:
            break
        dates.insert(0, d)
    bars = [latest[d] for d in dates]
    weeks = defaultdict(list)
    this_week = today-timedelta(days=today.weekday())
    for d in expected:
        dt = date.fromisoformat(d)
        monday = dt-timedelta(days=dt.weekday())
        if monday < this_week:
            weeks[monday].append(d)
    complete_weeks = sorted(weeks)[-10:]
    if len(complete_weeks) < 10 or any(d not in latest for w in complete_weeks for d in weeks[w]):
        return result
    weekly = [latest[weeks[w][-1]]['c'] for w in complete_weeks]
    closes = [b['c'] for b in bars]
    changes = [b-a for a,b in zip(closes, closes[1:])]
    gains, losses = [max(0,x) for x in changes], [max(0,-x) for x in changes]
    def wilder(values):
        value = sum(values[:14])/14
        for x in values[14:]:
            value = (value*13+x)/14
        return value
    gain, loss = wilder(gains), wilder(losses)
    rsi = 100-100/(1+gain/loss) if loss else 100 if gain else 50
    tr = [max(b['h']-b['l'], abs(b['h']-a['c']), abs(b['l']-a['c'])) for a,b in zip(bars,bars[1:])]
    sma20, sma50, weekly10 = sum(closes[-20:])/20, sum(closes[-50:])/50, sum(weekly)/10
    return dict(result, status='ready', reason=None, through=dates[-1], bars=len(bars),
        close=closes[-1], sma20=sma20, sma50=sma50, rsi14=rsi, atr14=wilder(tr),
        high20=max(b['h'] for b in bars[-20:]), low20=min(b['l'] for b in bars[-20:]),
        high5=max(b['h'] for b in bars[-5:]), low5=min(b['l'] for b in bars[-5:]),
        weekly_close=weekly[-1], weekly_sma10=weekly10, weekly_through=weeks[complete_weeks[-1]][-1],
        weekly_bias='bullish' if weekly[-1]>weekly10 else 'bearish' if weekly[-1]<weekly10 else 'neutral',
        basis='Alpaca split-adjusted daily bars; completed sessions and completed weeks only')


def technical_setups(symbol, daily, minute, now):
    if (daily.get('status') != 'ready' or daily.get('day') != day(now)
            or minute.get('status') != 'ready' or not 0 <= now-minute.get('asof',0) <= 90):
        return []
    hours = session(day(now))
    if not hours or not hours[0] <= minute.get('bar_start',0) < now < hours[1]-1800:
        return []
    b, p = minute['bar'], minute['previous_bar']
    price, atr = number(b.get('c')), number(daily.get('atr14'))
    if not price or not atr or atr <= 0:
        return []
    result = []
    for side, sign in (('long',1), ('short',-1)):
        aligned = daily['weekly_bias'] == ('bullish' if sign == 1 else 'bearish')
        trend = sign*(price-daily['sma20']) > 0 and sign*(daily['sma20']-daily['sma50']) >= 0
        def crossed(level):
            return sign*(p['c']-level) <= 0 < sign*(price-level)
        tests = []
        extreme = daily['high20'] if sign == 1 else daily['low20']
        if aligned and trend and crossed(extreme):
            tests.append(('daily_breakout', extreme-sign*.5*atr, '20-session range breakout; daily and weekly trend aligned'))
        if aligned and trend and crossed(daily['sma20']):
            stop = daily['low5']-.1*atr if sign == 1 else daily['high5']+.1*atr
            tests.append(('pullback_reclaim', stop, 'Daily SMA20 reclaim in the weekly trend'))
        reversing = sign*(daily['close']-daily['sma20']) < 0 and sign*(daily['sma20']-daily['sma50']) < 0
        rsi_ok = 25 <= daily['rsi14'] <= 55 if sign == 1 else 45 <= daily['rsi14'] <= 75
        if reversing and rsi_ok and crossed(daily['sma20']):
            stop = daily['low5']-.1*atr if sign == 1 else daily['high5']+.1*atr
            tests.append(('daily_reversal', stop, 'SMA20 reversal from the prior daily trend; weekly alignment reported separately'))
        for rule, stop, reason in tests:
            risk = sign*(price-stop)
            if 0 < risk <= 3*atr and stop > 0 and price+sign*2*risk > 0:
                result.append(dict(symbol=symbol, side=side, rule=rule, strategy='swing-ideas-v1:'+rule,
                    signal_time=minute['asof'], signal_price=price, stop=stop, target=price+sign*2*risk,
                    reason=reason, daily=daily, weekly_aligned=aligned))
    return result


def summarize_flow(rows, now, min_dte=14, max_dte=60):
    """Call/put totals are context; explicit vendor sentiment confirms direction."""
    result = {}
    seen = set()
    for r in rows:
        p = r.get('payload', r)
        ident = (p.get('vendor_id'), p.get('symbol'))
        stamp, premium = number(p.get('source_ts')), number(p.get('premium'))
        if (not ident[0] or ident in seen or stamp is None or premium is None or premium <= 0
                or not 0 <= now-stamp <= 1800 or r.get('first_seen',now)>now):
            continue
        seen.add(ident)
        symbol = str(p.get('symbol','')).upper()
        f = result.setdefault(symbol, dict(call_premium=0, put_premium=0, bullish_premium=0,
            bearish_premium=0, bullish_ids=[], bearish_ids=[], bullish_latest=None, bearish_latest=None,
            latest=None, prints=0, window_seconds=1800,
            basis='Observed TraderMatrix unusual activity filtered at $50k minimum; not all market flow. Sentiment is vendor classified.'))
        kind = str(p.get('option_type','')).lower()
        if kind in ('c','call','calls'):
            f['call_premium'] += premium
        elif kind in ('p','put','puts'):
            f['put_premium'] += premium
        f['latest'] = max(f['latest'] or 0, stamp)
        f['prints'] += 1
        try:
            raw_expiry = str(p.get('expiry','')).strip()
            # TraderMatrix's live feed uses MM/DD/YY; archived rows may use ISO.
            us = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})',raw_expiry)
            expiry = (date(int(us[3])+(2000 if len(us[3])==2 else 0),int(us[1]),int(us[2]))
                      if us else date.fromisoformat(raw_expiry))
            dte = (expiry-date.fromisoformat(day(now))).days
        except (ValueError,TypeError):
            continue
        sentiment = str(p.get('sentiment','')).strip().lower()
        if sentiment in ('bullish','bearish') and min_dte <= dte <= max_dte:
            f[sentiment+'_premium'] += premium
            f[sentiment+'_ids'].append(str(ident[0]))
            f[sentiment+'_latest'] = max(f[sentiment+'_latest'] or 0, stamp)
    return result


def confirms(flow, side, now):
    direction, opposite = ('bullish','bearish') if side == 'long' else ('bearish','bullish')
    stamp = flow.get(direction+'_latest')
    amount = flow.get(direction+'_premium',0)
    return bool(stamp is not None and 0 <= now-stamp <= 300 and amount >= 100000
                and (len(flow.get(direction+'_ids',[])) >= 2 or amount >= 250000)
                and amount >= 2*flow.get(opposite+'_premium',0))
