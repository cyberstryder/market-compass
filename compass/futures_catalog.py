"""Additional completed-bar hypotheses, independently measured without alerts."""
from .scanner import make_candidate
from .market import number

VERSION = 'futures-catalog-v1'
RULES = ('momentum_continuation', 'failed_breakout', 'range_reversal', 'compression_breakout')


def candidates(f, now, tick):
    if f.get('status') != 'ready' or not 0 <= now-f.get('asof',0) <= 90:
        return []
    atr = number(f.get('atr14'))
    if atr is None or atr <= 0:
        return []
    b,p=f['bar'],f['previous_bar']
    hi,lo=f['high20'],f['low20']
    result=[]
    def add(rule,side,stop):
        item=make_candidate(f['symbol'],rule,side,b['c'],stop,f['asof'],
                            'Forward candidate: '+rule.replace('_',' '))
        if item:
            from .store import identity
            item.update(strategy=VERSION+':'+rule,rule_version=VERSION,
                        id=identity(VERSION,f['symbol'],rule,side,f['asof']))
            result.append(item)
    for side,d in (('long',1),('short',-1)):
        if f.get('htf15_bias')==d and (b['c']-p['c'])*d>0 and (p['c']-p['o'])*d>0:
            if (b['c']-f['ema9'])*d>0 and (f['ema9']-f['ema21'])*d>0 and (f.get('rvol20') or 0)>=1:
                add('momentum_continuation',side,min(b['l'],p['l'])-tick if d==1 else max(b['h'],p['h'])+tick)
        failed=b['l']<lo-tick and b['c']>lo+tick if d==1 else b['h']>hi+tick and b['c']<hi-tick
        if failed:
            add('failed_breakout',side,b['l']-tick if d==1 else b['h']+tick)
        flat=abs(f['ema9']-f['ema21'])<=.15*atr and abs(f['ema21']-f['ema21_previous'])<=.05*atr
        reversal=b['l']<=lo+.1*atr and b['c']>p['h'] if d==1 else b['h']>=hi-.1*atr and b['c']<p['l']
        if flat and hi-lo>=2*atr and reversal:
            add('range_reversal',side,b['l']-tick if d==1 else b['h']+tick)
        compressed=f.get('prior5_range_atr') is not None and f['prior5_range_atr']<=1.5
        breakout=b['c']>f.get('prior5_high',float('inf')) if d==1 else b['c']<f.get('prior5_low',-float('inf'))
        if compressed and breakout and (f.get('rvol20') or 0)>=1.5:
            add('compression_breakout',side,f['prior5_low']-tick if d==1 else f['prior5_high']+tick)
    return result
