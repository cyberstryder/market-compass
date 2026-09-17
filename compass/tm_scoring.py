"""Frozen, explanatory research rubric; neither TM's formula nor a probability."""
from datetime import date, timedelta
from .market import number, day, session, fresh

VERSION='tm-receipt-study-v1'
HORIZONS=('15m','60m','close','1session','3session','5session')
TM_THRESHOLD=85
COMPASS_THRESHOLD=70
PROTOCOL=dict(version=VERSION,primary_horizon='60m',tm_threshold=TM_THRESHOLD,
    compass_threshold=COMPASS_THRESHOLD,horizons=list(HORIZONS),
    basis='First receipt processing; at most 30 seconds after receipt. Frozen source score and observed price context.',
    outcome='Underlying midpoint change, not option returns, executable fills or profit.',
    comparison='TM-only, Compass-only and both on a common cohort with both numeric scores and the same checkpoint.',
    evaluation='Provisional fixed rubric; no training on these outcomes. Review after later sessions; keep inconclusive results.')


def age_bucket(source,receipt):
    age=receipt-source if number(source) is not None else None
    return ('unknown' if age is None else 'clock_error' if age<0 else '0–2m' if age<=120
        else '2–15m' if age<=900 else '15–60m' if age<=3600 else '>60m')


def dte_bucket(expiry,at):
    try:dte=(date.fromisoformat(expiry)-date.fromisoformat(day(at))).days
    except (ValueError,TypeError):return 'unknown'
    return 'expired' if dte<0 else '0DTE' if dte==0 else '1–7DTE' if dte<=7 else '8–30DTE' if dte<=30 else '>30DTE'


def score(row,q,f,at):
    """TM score never enters the Compass points. Missing inputs remain unknown."""
    sentiment=str(row.get('sentiment','')).lower()
    direction='long' if sentiment=='bullish' else 'short' if sentiment=='bearish' else 'unknown'
    sign=1 if direction=='long' else -1
    quote_ok=bool(fresh(q,at) and 0<=at-q['ts']<=5 and q.get('received',at)<=at)
    context_ok=f.get('status')=='ready' and 0<=at-f.get('asof',0)<=90
    price=(q['bid']+q['ask'])/2 if quote_ok else None
    directional=quote_ok and context_ok and direction!='unknown'
    components=[]
    def add(name,weight,value,evidence):
        components.append(dict(name=name,weight=weight,points=weight if value else 0 if value is False else None,evidence=evidence))
    def relation(a,b):
        return sign*(a-b)>0 if directional and number(a) is not None and number(b) is not None else None
    add('EMA9 / EMA21 alignment',20,relation(f.get('ema9'),f.get('ema21')),
        dict(ema9=f.get('ema9'),ema21=f.get('ema21')))
    add('Price / observed-session VWAP alignment',20,relation(price,f.get('vwap')),dict(price=price,vwap=f.get('vwap')))
    bias=number(f.get('htf15_bias'))
    add('Completed 15-minute trend alignment',20,bias==sign if directional and bias is not None else None,dict(bias=bias))
    boundary=f.get('high5' if direction=='long' else 'low5')
    add('Five-minute price breakout',10,relation(price,boundary),dict(price=price,boundary=boundary))
    rvol=number(f.get('rvol20'))
    add('Relative volume at least 1.25',10,rvol>=1.25 if context_ok and rvol is not None else None,dict(rvol20=rvol))
    volume,oi=number(row.get('volume')),number(row.get('open_interest'))
    add('Volume/OI at least 2 with OI at least 100',10,
        oi>=100 and volume/oi>=2 if volume is not None and oi is not None and oi>0 else False if oi==0 else None,
        dict(volume=volume,open_interest=oi))
    premium=number(row.get('premium'))
    add('Premium at least $100,000',10,premium>=100000 if premium is not None else None,dict(premium=premium))
    lower=sum(p['points'] or 0 for p in components)
    missing=sum(p['weight'] for p in components if p['points'] is None)
    complete=not missing and directional
    value=lower if complete else None
    tm=number(row.get('score'))
    tm=tm if tm is not None and 0<=tm<=100 else None
    hours=session(day(at))
    return dict(direction=direction,tm_score=tm,compass_score=value,
        score_status='scored' if complete else 'insufficient_data',
        score_range=[lower,lower+missing],components=components,
        tm_selected=tm>=TM_THRESHOLD if tm is not None else None,
        compass_selected=value>=COMPASS_THRESHOLD if value is not None else None,
        quote_ready=quote_ok,context_ready=context_ok,entry_mid=price,
        entry_quote=dict(q) if quote_ok else None,
        entry_spread_bps=(q['ask']-q['bid'])/price*10000 if quote_ok else None,
        receipt_session='regular' if hours and hours[0]<=at<hours[1] else 'extended_or_closed',
        feature_asof=f.get('asof'),vendor_classification=row.get('classification'),
        note='Fixed research points; not a calibrated probability. VWAP uses the stored observed session. Thin OI does not earn volume/OI points.')


def checkpoint_times(at):
    """Minutes mean exchange trading time; session closes respect holidays."""
    start=date.fromisoformat(day(at))
    sessions=[]
    for offset in range(30):
        hours=session((start+timedelta(days=offset)).isoformat())
        if hours and hours[1]>at:sessions.append(hours)
        if len(sessions)>=6:break
    if len(sessions)<6:raise ValueError('Insufficient exchange calendar coverage')
    def advance(seconds):
        for opening,close in sessions:
            begin=max(at,opening)
            if begin>=close:continue
            if seconds<=close-begin:return min(begin+seconds,close-.001)
            seconds-=close-begin
        raise ValueError('Insufficient exchange calendar coverage')
    return dict(zip(HORIZONS,[advance(900),advance(3600),sessions[0][1]-.001,
        sessions[1][1]-.001,sessions[3][1]-.001,sessions[5][1]-.001]))
