"""Review pre-evaluation, non-ORB futures only; never read held-out returns."""
import argparse,csv,io,json,zipfile
from collections import defaultdict,Counter
from datetime import datetime,date
from pathlib import Path
from zoneinfo import ZoneInfo

def run(archive,destination):
    groups=defaultdict(list);sessions=set();skip=0
    with zipfile.ZipFile(archive) as z:
        manifest=json.loads(z.read('compass_snapshot_manifest.json'))
        with z.open('csv/compass/setup_trials_v1.csv') as f:
            for row in csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig')):
                stamp=datetime.fromtimestamp(float(row['started']),ZoneInfo('America/Chicago'))
                # Check the entry clock BEFORE parsing any outcome payload.
                if stamp.date()>=date(2026,9,18):skip+=1;continue
                if 'orb' in row['strategy'].lower():continue
                p=json.loads(row['payload'])
                if p.get('asset')!='future':continue
                clock=stamp.hour*60+stamp.minute
                period='cash_overlap' if 510<=clock<900 else 'post_cash' if 900<=clock<960 else 'overnight'
                key=(row['symbol'],p.get('rule',row['strategy']),row['version'],period)
                risk=abs((p.get('entry') or 0)-(p.get('stop') or 0))*(p.get('multiplier') or 0)
                pnl=p.get('pnl');measured=p.get('status')=='closed' and isinstance(pnl,(float,int)) and risk>0
                groups[key].append(dict(status=p.get('status'),day=str(stamp.date()),
                    pnl=pnl if measured else None,net_r=pnl/risk if measured else None,
                    giveback=bool(measured and pnl<0 and (p.get('mfe_r') or 0)>0)))
                sessions.add(str(stamp.date()))
    output=[]
    for key,rows in sorted(groups.items()):
        closed=[r for r in rows if r['net_r'] is not None];counts=Counter(r['status'] for r in rows)
        output.append(dict(zip(('contract','rule','version','period'),key))|dict(
            observations=len(rows),resolved=len(closed),excluded=counts['excluded'],unresolved=counts['unresolved'],
            entry_days=len({r['day'] for r in rows}),wins=sum(r['pnl']>0 for r in closed),
            mean_net_r=sum(r['net_r'] for r in closed)/len(closed) if closed else None,
            favorable_then_loss=sum(r['giveback'] for r in closed)))
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    if output:
        with (destination/'non_orb_development.csv').open('w') as f:
            writer=csv.DictWriter(f,fieldnames=list(output[0]));writer.writeheader();writer.writerows(output)
    result=dict(snapshot=manifest['snapshot_started_at_utc'],entry_dates=sorted(sessions),
        observations=sum(r['observations'] for r in output),groups=len(output),
        evaluation_rows_not_parsed=skip,rows=output,
        limits='Development only, before September 18. Independent correlated one-contract trials, not a portfolio. No promotion or retuning. Missing paths are not losses.')
    (destination/'development_summary.json').write_text(json.dumps(result,indent=2))
    return {k:v for k,v in result.items() if k!='rows'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('archive');parser.add_argument('destination')
    args=parser.parse_args();print(json.dumps(run(args.archive,args.destination),indent=2))
