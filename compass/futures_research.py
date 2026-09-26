"""Full-session census and additional shadow trials; no orders or alerts."""
from .futures import research_session
from .futures_assessment import assess
from .futures_catalog import candidates, VERSION
from .store import identity
from .market import fresh


def observe(db,c,symbol,f,now,study,spec,orb_enabled=True):
    h=research_session(now,symbol)
    if not h['is_open']:
        return
    stamp=f.get('asof')
    if stamp is None or not 0<=now-stamp<=90:
        return
    key='futures_research:'+symbol
    old=db.get(c,key,{})
    if stamp<=old.get('bar_at',0):
        return
    items=candidates(f,now,spec['tick']) if h['entry_open'] else []
    # Existing scanner hypotheses also run through the final account-restricted
    # period, with separate session policy/version identities for comparisons.
    from .scanner import session_open
    from .scanner import research_candidates
    extra,arms=research_candidates(f,db.get(c,key+':arms',{}),now,spec['tick'],orb_enabled=orb_enabled)
    db.put(c,key+':arms',arms)
    if not session_open(symbol,now) and h['entry_open']:
        for item in extra:
            item.update(id=identity(VERSION,'extended',item['id']),strategy=VERSION+':'+item['rule'])
        items+=extra
    q=db.get(c,'quote:'+symbol)
    snapshot=dict(assess(f,now),symbol=symbol,session=h['day'],policy=h['policy'],
                  quote_fresh=fresh(q,now),candidate_rules=[s['rule'] for s in items],
                  basis='Additional shadow candidates; original scanner trials remain separate')
    db.append(c,'futures_assessment',VERSION,symbol,stamp,snapshot,identity(VERSION,symbol,stamp))
    db.put(c,key,dict(bar_at=stamp,observed_at=now,**{'snapshot':snapshot}))
    for item in items:
        study.start(c,item,now,spec,alerted=False,primary=False)
