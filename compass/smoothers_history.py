"""Historical ticker stats for Smoothers conviction data in alerts."""
from sqlalchemy import select
from .store import meta
from .projects import records

# Cache per week to avoid repeated DB queries
_cache={}

def get_ticker_stats(db, ticker, week, owned_since=float('inf')):
    """
    Returns historical stats for a ticker from project_records_v1:
    {wins, losses, total, win_rate, avg_return_pct, avg_premium, avg_days_to_target}
    Uses last 5 weeks of resolved signals before the given week.
    """
    cache_key=(ticker,week)
    if cache_key in _cache:
        return _cache[cache_key]
    
    stats={'wins':0,'losses':0,'total':0,'win_rate':0,'avg_return_pct':0,'avg_premium':0,'avg_days_to_target':0}
    
    try:
        with db.tx() as c:
            rows=c.execute(
                select(records.c.payload)
                .where(records.c.project=='smoothers', records.c.symbol==ticker)
            ).scalars().all()
        
        resolved=[]
        for p in rows:
            original=p.get('original') or {}
            cohort=str(original.get('monday_date') or '')[:10]
            status=original.get('status')
            if not cohort or cohort>=week or status not in ('WIN','LOSS'):
                continue
            if p.get('source_ts', float('inf')) >= owned_since:
                continue  # Only use pre-ownership source history
            
            entry_premium=float(original.get('entry_premium') or 0)
            if entry_premium<=0:
                continue
                
            if status=='WIN':
                value=float(original.get('value_at_target') or 0)
                ret=(value-entry_premium)/entry_premium*100 if value>0 else 0
                # Days to target
                try:
                    from datetime import datetime
                    res_dt=datetime.fromisoformat(original['resolution_time'].replace('Z','+00:00'))
                    entry_dt=datetime.fromisoformat(original['model_entry_time'].replace('Z','+00:00'))
                    days=(res_dt-entry_dt).total_seconds()/86400
                except:
                    days=None
            else:
                value=float(original.get('last_premium') or 0)
                ret=(value-entry_premium)/entry_premium*100 if value>=0 else -100
                days=None
            
            resolved.append({
                'status':status,
                'return_pct':ret,
                'premium':entry_premium,
                'days':days,
                'cohort':cohort
            })
        
        # Take last 5 weeks
        resolved=sorted(resolved, key=lambda x: x['cohort'], reverse=True)[:5]
        
        if resolved:
            wins=sum(1 for r in resolved if r['status']=='WIN')
            total=len(resolved)
            avg_ret=sum(r['return_pct'] for r in resolved)/total
            avg_prem=sum(r['premium'] for r in resolved)/total
            days_list=[r['days'] for r in resolved if r['days'] is not None]
            avg_days=sum(days_list)/len(days_list) if days_list else 0
            
            stats={
                'wins':wins,
                'losses':total-wins,
                'total':total,
                'win_rate':wins/total*100 if total>0 else 0,
                'avg_return_pct':round(avg_ret,1),
                'avg_premium':round(avg_prem,2),
                'avg_days_to_target':round(avg_days,1)
            }
        
        _cache[cache_key]=stats
        return stats
        
    except Exception:
        return stats

def clear_cache():
    _cache.clear()
