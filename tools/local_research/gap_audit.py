"""Inventory exact missing futures minutes without changing prices or test gates."""
import argparse,json,os
from pathlib import Path
from datetime import timedelta
import pandas as pd
from run import ROOT,load_prices,coverage,save,digest

def inventory(df,excluded):
    sessions,_=coverage(df,excluded,'segments');out=[]
    if not sessions:raise ValueError('No rows inside the configured study period')
    known={r['day'] for r in sessions}
    for d in pd.date_range(sessions[0]['day'],sessions[-1]['day'],freq='B'):
        label=d.date().isoformat()
        if label not in known:sessions.append(dict(day=label,excluded_by_config=label in excluded))
    sessions.sort(key=lambda r:r['day'])
    for item in sessions:
        if item['excluded_by_config'] or pd.Timestamp(item['day']).weekday()>4:continue
        end=pd.Timestamp(item['day']+' 16:00',tz='America/Chicago')
        start=pd.Timestamp(str(pd.Timestamp(item['day']).date()-timedelta(days=1))+' 17:00',tz='America/Chicago')
        missing=pd.date_range(start,end,freq='min',inclusive='left').difference(df.index)
        ranges=[]
        for stamp in missing:
            if ranges and stamp==ranges[-1][1]+pd.Timedelta(minutes=1):ranges[-1][1]=stamp
            else:ranges.append([stamp,stamp])
        if ranges:out.append(dict(day=item['day'],missing_minutes=len(missing),ranges=[dict(start=str(a),end_exclusive=str(b+pd.Timedelta(minutes=1)),minutes=int((b-a).total_seconds()/60)+1) for a,b in ranges]))
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',default=os.environ.get('COMPASS_RESEARCH_HOME',str(ROOT/'workspace')));p.add_argument('--config',default=str(ROOT/'config.json'));a=p.parse_args()
    root=Path(a.workspace);cfg=json.loads(Path(a.config).read_text(encoding='utf-8-sig'));result={}
    for symbol in cfg['instruments']:
        path=root/'input'/f'{symbol}.csv'
        if not path.exists():result[symbol]={'state':'missing_input'};continue
        try:
            df=load_prices(path);labels=(df.index.tz_localize(None)+pd.Timedelta(hours=7)).strftime('%Y-%m-%d');df=df[labels<=cfg['later_end']]
            rows=inventory(df,set(cfg.get('exclude_sessions',[])))
            result[symbol]=dict(state='needs_source_verification' if rows else 'no_gaps_in_observed_sessions',sha256=digest(path),sessions=rows,missing_minutes=sum(r['missing_minutes'] for r in rows))
            print(symbol+': '+str(result[symbol]['missing_minutes'])+' missing standard-session minutes')
        except (ValueError,KeyError) as e:result[symbol]=dict(state='blocked',reason=str(e))
    save(root/'gap-audit.json',dict(instruments=result,basis='Standard-session expected-minute inventory, not proof of a feed outage. No-trade minutes, shortened sessions and missing history require source verification. Entirely absent sessions before the first observed date are not inferred. Never mix continuous/dated or differently adjusted series; import matching source history only.'))
    print('Saved gap-audit.json; the next Compass report includes it. Prices and prior results are unchanged.')
if __name__=='__main__':main()
