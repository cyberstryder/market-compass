"""Offline research runner. No broker, provider, credential, or model access."""
from pathlib import Path
from datetime import timedelta, datetime, timezone
import argparse, hashlib, json, platform, sys, zipfile, os
import numpy as np
import pandas as pd
import engine

ROOT=Path(__file__).resolve().parent
VERSION='local-research-v1'
FAMILIES=['Trend breakout','Trend pullback','Band reentry','Failed breakout','Stochastic Pop','Opening Range Desk','Location Desk','Signal Desk','Fractal Model']

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def save(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,indent=2,allow_nan=False),encoding='utf-8');tmp.replace(path)

def load_prices(path):
    # Read only explicitly supported raw columns; indicator plots are not volume.
    df=pd.read_csv(path,usecols=lambda c:c.lower() in ('time','open','high','low','close','volume'))
    df.columns=df.columns.str.lower()
    if not {'time','open','high','low','close'}<=set(df):raise ValueError('CSV requires time,open,high,low,close')
    if len(df)==0:raise ValueError('CSV is empty')
    numeric=pd.to_numeric(df.time,errors='coerce')
    if numeric.notna().all():
        if numeric.abs().max()>1e11:raise ValueError('Numeric time must be Unix SECONDS, not milliseconds')
        times=pd.to_datetime(numeric,unit='s',utc=True)
    else:
        if not df.time.astype(str).str.contains(r'(?:Z|[+-]\d\d:\d\d)$',regex=True).all():raise ValueError('Text timestamps must include UTC Z or an explicit offset')
        times=pd.to_datetime(df.time,utc=True,format='mixed')
    df.index=pd.DatetimeIndex(times).tz_convert('America/Chicago')
    if not df.index.is_unique:raise ValueError('Duplicate timestamps; resolve source overlaps explicitly')
    if not df.index.is_monotonic_increasing:raise ValueError('Timestamps are not ascending')
    if ((df.index.asi8//10**9)%60!=0).any():raise ValueError('Bar timestamps must be minute-aligned opening times')
    if len(df)>1 and not np.any(np.diff(df.index.asi8)==60*10**9):raise ValueError('Expected ONE-minute bars; coarser bars cannot simulate minute fills')
    for k in ('open','high','low','close'):
        df[k]=pd.to_numeric(df[k],errors='raise')
        if not np.isfinite(df[k]).all() or (df[k]<=0).any():raise ValueError('Nonpositive or missing OHLC; unsupported input')
    if not ((df.high>=df[['open','close']].max(axis=1))&(df.low<=df[['open','close']].min(axis=1))&(df.high>=df.low)).all():raise ValueError('Invalid OHLC geometry')
    return df

def coverage(df,excluded):
    labels=(df.index.tz_localize(None)+pd.Timedelta(hours=7)).strftime('%Y-%m-%d')
    inventory=[];eligible=[]
    for day,g in df.groupby(labels):
        date=pd.Timestamp(day).date()
        start=pd.Timestamp(str(date-timedelta(days=1))+' 17:00',tz='America/Chicago')
        end=pd.Timestamp(day+' 16:00',tz='America/Chicago')
        expect=pd.date_range(start,end,freq='min',inclusive='left')
        missing=len(expect.difference(g.index));extra=len(g.index.difference(expect))
        ok=missing==0 and extra==0 and date.weekday()<5 and day not in excluded
        inventory.append(dict(day=day,rows=len(g),missing_minutes=missing,extra_minutes=extra,eligible=ok,excluded_by_config=day in excluded))
        if ok:eligible.append(day)
    return inventory,eligible

def validate_config(cfg):
    if cfg.get('version')!=1:raise ValueError('Expected config version 1')
    for k in ('development_end','validation_end','later_end'):pd.Timestamp(cfg[k])
    if not cfg['development_end']<cfg['validation_end']<cfg['later_end']:raise ValueError('Split dates must be strictly increasing')
    if not cfg.get('intervals') or any(n not in (1,3,5) for n in cfg['intervals']):raise ValueError('Supported intervals: 1, 3, 5')
    if not set(cfg.get('families',FAMILIES))<=set(FAMILIES):raise ValueError('Unknown strategy family')
    for symbol,spec in cfg['instruments'].items():
        if not symbol.isalnum():raise ValueError('Instrument name must be alphanumeric')
        for field in ('tick','point_value'):
            if not isinstance(spec[field],(int,float)) or not np.isfinite(spec[field]) or spec[field]<=0:raise ValueError('Invalid '+field)
        if not np.isfinite(spec['fee_round_trip']) or spec['fee_round_trip']<0:raise ValueError('Invalid fee')
    for field in ('base_slippage_ticks_per_side','stress_slippage_ticks_per_side'):
        if not np.isfinite(cfg[field]) or cfg[field]<0:raise ValueError('Invalid slippage')

def fingerprint(path,cfg,symbol):
    code={p.name:digest(p) for p in sorted(ROOT.glob('*.py'))}
    payload=dict(source=digest(path),config=cfg,symbol=symbol,code=code,python=platform.python_version(),numpy=np.__version__,pandas=pd.__version__,version=VERSION)
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),payload

def run_instrument(path,symbol,spec,cfg,out):
    df=load_prices(path)
    # Prevent observations after the declared last session from entering this study.
    labels=(df.index.tz_localize(None)+pd.Timedelta(hours=7)).strftime('%Y-%m-%d')
    df=df[labels<=cfg['later_end']]
    if df.empty:raise ValueError('No rows on or before later_end')
    inv,days=coverage(df,set(cfg.get('exclude_sessions',[])))
    splits={'development':[x for x in days if x<=cfg['development_end']],
            'validation':[x for x in days if cfg['development_end']<x<=cfg['validation_end']],
            'later':[x for x in days if cfg['validation_end']<x<=cfg['later_end']]}
    save(out/'coverage.json',dict(sessions=inv,splits=splits,raw_volume_present='volume' in df,rows=len(df)))
    if any(len(v)<cfg.get('minimum_sessions_per_phase',5) for v in splits.values()):
        raise ValueError('Insufficient complete sessions in split: '+str({k:len(v) for k,v in splits.items()}))
    engine.TICK=spec['tick'];engine.POINT=spec['point_value']
    base=spec['fee_round_trip']+2*spec['tick']*spec['point_value']*cfg['base_slippage_ticks_per_side']
    stress=spec['fee_round_trip']+2*spec['tick']*spec['point_value']*cfg['stress_slippage_ticks_per_side']
    records=[];trades=[];censored=[]
    for n in cfg['intervals']:
        d=engine.make_bars(df,n);a,bias,eff,sigs,ke=engine.features(d,df,n)
        for family in cfg.get('families',FAMILIES):
            sig=sigs[family]
            for combo in ('standalone','trend agreement','trend + efficiency'):
                mask=np.ones(len(d),bool) if combo=='standalone' else bias==sig
                if combo=='trend + efficiency':mask &= eff>=.2
                t,missing=engine.simulate(df,d,n,np.where(mask,sig,0),a,ke,family,set(days),base,stress)
                t['phase']=t.day.map({day:phase for phase,items in splits.items() for day in items})
                t['family']=family;t['minutes']=n;t['combination']=combo;t['symbol']=symbol
                if len(t):trades.append(t)
                censored.extend(dict(x,family=family,minutes=n,combination=combo) for x in missing)
                row=dict(symbol=symbol,family=family,minutes=n,combination=combo,censored=len(missing))
                for phase,items in splits.items():
                    sample=t[t.phase==phase]
                    row[phase]=engine.metrics(sample,items)
                    row[phase+'_stress']=engine.metrics(sample,items,'stress_net')
                v=row['validation_stress'];l=row['later_stress']
                row['preliminary_gate']=bool(not missing and v['net']>0 and l['net']>0 and l['trades']>=50 and l['daily_ci_low']>0 and l['without_best_day']>0)
                records.append(row)
        print(f'  {symbol}: {n}-minute interval complete',flush=True)
    if trades:pd.concat(trades,ignore_index=True).to_csv(out/'trades.csv',index=False)
    save(out/'results.json',records);save(out/'censored.json',censored)
    return dict(state='complete',configurations=len(records),preliminary_passes=sum(x['preliminary_gate'] for x in records),sessions={k:len(v) for k,v in splits.items()},censored=len(censored),note='Exploratory common-exit screen; no automatic strategy promotion')

def make_review(results,cfg,root):
    lines=['# Local Compass research review','',f'Generated {datetime.now(timezone.utc).isoformat()}',
      '', '**Exploratory signal screen, not native Pine parity or proof of reliability.**',
      'No broker actions, paid downloads or AI calls. Later results are retrospective; repeated runs do not create fresh holdouts.',
      '', '| Instrument | State | Detail |','|---|---|---|']
    for symbol,item in results.items():
        detail=item.get('error') or f"{item.get('configurations',0)} configurations; {item.get('preliminary_passes',0)} preliminary passes"
        lines.append(f"| {symbol} | {item['state']} | {str(detail).replace('|','/')} |")
    lines+=['','## Three-minute results (stressed costs, one contract)', '', '| Instrument | Rule | Combination | Development net | Validation net | Later net | Later trades |', '|---|---|---|---:|---:|---:|---:|']
    for symbol,item in results.items():
        if item['state']!='complete':continue
        rows=json.loads((root/item['run']/'results.json').read_text())
        # Sort by name, not final-period returns. Include all primary configurations.
        for r in sorted(rows,key=lambda x:(x['family'],x['combination'])):
            if r['minutes']!=3:continue
            values=[r[k+'_stress']['net'] for k in ('development','validation','later')]
            lines.append(f"| {symbol} | {r['family']} | {r['combination']} | "+' | '.join(f'{x:.2f}' for x in values)+f" | {r['later']['trades']} |")
    lines+=['','## Fixed limitations',
      '- Drift and Camarilla are not implemented in this runner; do not infer their results from other families.',
      '- Pine-native exits, order fills, sizing, FVG-retest mode and manually assessed POI are not reproduced.',
      '- Only complete standard 17:00–16:00 CT sessions are traded; missing/shortened sessions are excluded, never filled with invented bars.',
      '- Holiday-aware trading on shortened sessions is not implemented. Contract roll/adjustment settings must be checked at the source.',
      '- Results across contracts use different dollar multipliers. No correlated portfolio or prop-account pass probability is estimated.',
      '- Preliminary gates do not correct for multiple testing and need longer independent and prospective validation.',
      '', 'Upload review.zip for interpretation. Full trades remain in the local runs folder.']
    (root/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    save(root/'latest.json',dict(version=VERSION,config=cfg,instruments=results))
    tmp=root/'review.tmp.zip'
    with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
        for name in ('summary.md','latest.json'):z.write(root/name,name)
        for symbol,item in results.items():
            if not item.get('run'):continue
            for name in ('results.json','coverage.json','censored.json','manifest.json','status.json'):
                path=root/item['run']/name
                if path.exists():z.write(path,f'{symbol}/{name}')
    tmp.replace(root/'review.zip')

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',default=str(ROOT/'config.json'));p.add_argument('--workspace',default=os.environ.get('COMPASS_RESEARCH_HOME',str(ROOT/'workspace')));p.add_argument('--symbols',nargs='+');p.add_argument('--check',action='store_true');args=p.parse_args(argv)
    cfg=json.loads(Path(args.config).read_text(encoding='utf-8-sig'));validate_config(cfg)
    root=Path(args.workspace).expanduser().resolve();(root/'input').mkdir(parents=True,exist_ok=True)
    names=args.symbols or list(cfg['instruments']);unknown=set(names)-set(cfg['instruments'])
    if unknown:raise ValueError('Unconfigured symbols: '+','.join(sorted(unknown)))
    # Separate per-instrument content hashes permit safe result reuse between PCs.
    results={}
    for symbol in names:
        path=root/'input'/f'{symbol}.csv'
        if not path.exists():results[symbol]=dict(state='pending',error=f'Missing input/{symbol}.csv');print(f'{symbol}: missing CSV');continue
        run=None
        try:
            if args.check:
                df=load_prices(path);inv,days=coverage(df,set(cfg.get('exclude_sessions',[])))
                print(f'{symbol}: {len(df)} minute rows, {len(days)} complete standard sessions; check split dates before running');continue
            key,manifest=fingerprint(path,cfg,symbol);run=Path('runs')/symbol/key;out=root/run
            status=out/'status.json'
            if status.exists() and json.loads(status.read_text()).get('state')=='complete':
                item=json.loads(status.read_text());print(f'{symbol}: cached result reused')
            else:
                out.mkdir(parents=True,exist_ok=True);save(out/'manifest.json',manifest)
                print(f'{symbol}: computing locally',flush=True)
                item=run_instrument(path,symbol,cfg['instruments'][symbol],cfg,out)
                if digest(path)!=manifest['source']:raise ValueError('Input changed during run; wait for sync, then retry')
                save(status,item)
            results[symbol]=dict(item,run=run.as_posix())
        except Exception as e:
            # Local paths are not included in review packages.
            message=str(e).replace(str(root),'<workspace>')
            results[symbol]=dict(state='blocked',error=message)
            if run:
                results[symbol]['run']=run.as_posix();save(root/run/'status.json',dict(state='blocked',error=message))
            print(f'{symbol}: blocked: {message}',flush=True)
    if not args.check:
        make_review(results,cfg,root);print(f'Read: {root / "summary.md"}\nShare: {root / "review.zip"}')
    return 2 if any(x['state']=='blocked' for x in results.values()) else 0

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,KeyError,OSError) as e:print(f'Configuration/input error: {e}',file=sys.stderr);sys.exit(2)
