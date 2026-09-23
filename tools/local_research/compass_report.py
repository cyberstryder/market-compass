"""Analyze locally downloaded report evidence; never infer unobserved option P&L."""
from pathlib import Path
import argparse,csv,json,os,zipfile
from run import ROOT,save
COHORTS={'futures':'Futures','zero_dte_setups':'0DTE setups','options':'Options ideas','swing_options':'Swing options','stock_setups':'Intraday stocks'}

def scan_warnings(obj,path=''):
    out=[]
    if isinstance(obj,dict):
        for k,v in obj.items():
            full=path+'.'+k
            if 'truncat' in full.lower() and not isinstance(v,(dict,list)) and v:out.append(full+': '+str(v))
            elif k in ('counts_complete','complete') and v is False:out.append(full+': false')
            if isinstance(v,(dict,list)):out+=scan_warnings(v,full)
    elif isinstance(obj,list):
        for i,v in enumerate(obj):out+=scan_warnings(v,f'{path}[{i}]')
    return out

def analyze(root):
    root=Path(root);output=root/'analysis';output.mkdir(parents=True,exist_ok=True)
    # Pick the most recently downloaded evidence for each creation-session date.
    latest={};native={};failures=[]
    for manifest_path in sorted((root/'downloads').glob('*/manifest.json')):
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        for record in manifest.get('reports',[]):
            name=record['file'];path=manifest_path.parent/name
            if record['state']!='downloaded':failures.append({'download':manifest_path.parent.name,**record});continue
            if name.startswith('daily-'):latest[name]=path
            else:native[name]=path
    rows=[];programs=[];warnings=[];supplemental={};daily_saved={}
    for filename,path in sorted(latest.items()):
        d=json.loads(path.read_text(encoding='utf-8'));day=d.get('day')
        if not day or filename!=f'daily-{day}.json':warnings.append(filename+': date mismatch; excluded');continue
        daily_saved[day]=d
        warnings+=scan_warnings(d,day)
        for key,label in COHORTS.items():
            cohort=d.get(key)
            if not isinstance(cohort,dict):warnings.append(day+': missing '+key);continue
            for g in cohort.get('groups',[]):
                measured=sum(g.get(k,0) or 0 for k in ('wins','losses','breakeven'))
                rows.append({'day':day,'category':label,'symbol':g.get('symbol'),'strategy':g.get('strategy'),'version':g.get('version'),
                   'total':g.get('total'),'measured':measured,'wins':g.get('wins'),'losses':g.get('losses'),'breakeven':g.get('breakeven'),
                   'win_rate':(g.get('wins',0) or 0)/measured if measured else None,'net_observed_pnl':g.get('net_pnl') if measured else None,
                   'missing_pnl':g.get('missing_pnl'),'pending':g.get('pending'),'open':g.get('open'),'unresolved':g.get('unresolved'),'excluded':g.get('excluded'),
                   'asof':d.get('asof'),'counts_complete':cohort.get('counts_complete')})
        # Preserve overlapping horizons/cohorts independently, never sum into account equity.
        for item in d.get('research',[]):programs.append({'day':day,**item})
        supplemental[day]={k:d.get(k) for k in ('morning','smoothers','spy_options','post_lock','zero_dte_selection','quote_reliability','verification')}
    # Daily Smoothers is a cumulative/current snapshot embedded in daily reports.
    # Keep just the latest asof, rather than adding repeated snapshots across days.
    smooth=max(daily_saved.values(),key=lambda d:d.get('asof',0) or 0).get('smoothers_daily') if daily_saved else None
    reports={k:json.loads(v.read_text(encoding='utf-8')) for k,v in native.items()}
    for k,v in reports.items():warnings+=scan_warnings(v,k)
    save(output/'evidence.json',{'daily_reports':daily_saved,'cohort_groups':rows,'other_program_coverage':programs,'daily_supplemental':supplemental,'latest_daily_smoothers':smooth,'latest_native_reports':reports,'download_failures':failures,'warnings':warnings})
    if rows:
        with (output/'cohort_groups.csv').open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    elif (output/'cohort_groups.csv').exists():(output/'cohort_groups.csv').unlink()
    lines=['# Compass local evidence review','',
      'This analyzes saved dashboard reports locally. It does not contain the full raw quote/candle warehouse, reconstruct missing fills, or provide a new backtest.',
      'Current saved outcomes of creation cohorts are not historical point-in-time account returns. The newest successful download per session is used; failed refreshes leave older evidence visible.',
      '',f'Sessions loaded: {len(daily_saved)}. Cohort groups: {len(rows)}. Recorded download failures: {len(failures)}.',
      '', '| Day | Category | Candidates | Measured | Missing closed P&L | Open/pending/unresolved |','|---|---|---:|---:|---:|---:|']
    for day,d in sorted(daily_saved.items()):
        for key,label in COHORTS.items():
            t=(d.get(key) or {}).get('totals',{})
            if not t:
                lines.append(f'| {day} | {label} | unavailable | unavailable | unavailable | unavailable |');continue
            measured=sum(t.get(k,0) or 0 for k in ('wins','losses','breakeven'))
            unknown=sum(t.get(k,0) or 0 for k in ('open','pending','unresolved'))
            lines.append(f"| {day} | {label} | {t.get('total','unknown')} | {measured} | {t.get('missing_pnl','unknown')} | {unknown} |")
    lines+=['','## Exclusions and unresolved reasons']
    for day,d in sorted(daily_saved.items()):
        for key,label in COHORTS.items():
            for r in (d.get(key) or {}).get('reasons',[]):
                lines.append(f"- {day} / {label} / {r.get('status','unknown')}: {r.get('reason','unknown')} — {r.get('count','unknown')}")
    lines+=['','## Other research',
      '- Morning: stock coverage and option measurements retained separately by contract variant and horizon.',
      '- Weekly Smoothers: weekly states retained per snapshot; not added across days or treated as option returns.',
      '- Daily Smoothers: only the latest cumulative overlap/timing/weekday snapshot is retained.',
      '- TraderMatrix, swing flow and discovery: cohort/checkpoint completeness retained; counts do not imply profitability.',
      '- SPY observations, post-lock futures research and quote-reliability evidence retained independently.',
      '- Native/Morning detail endpoints have 200-row limits. Truncation remains visible; they are not full raw-data exports.',
      '- No combined account equity, options replay or strategy optimization is calculated from these aggregate reports.',
      '', '## Coverage warnings']
    lines += ['- '+x for x in warnings] or ['- No explicit truncation flag reported; that is not proof of complete raw data.']
    if smooth:
        first=(smooth.get('activation') or {}).get('sessions',[{}])[0]
        state='awaiting_first_session' if first.get('open',0)>(smooth.get('asof') or 0) else smooth.get('state','unknown')
        lines+=['',f"Daily Smoothers: {state}; first full session {first.get('day','unknown')}; records {smooth.get('records','unknown')}."]
    lines+=['','## Download failures']+['- '+x['download']+' / '+x['file']+': '+x.get('reason','unknown') for x in failures]
    lines+=['','## Snapshot times']+[f"- {day}: source asof {d.get('asof','unknown')}" for day,d in sorted(daily_saved.items())]
    (output/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    with zipfile.ZipFile(output/'compass-review.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in ('summary.md','cohort_groups.csv','evidence.json'):
            if (output/name).exists():z.write(output/name,name)
        if (root.parent/'gap-audit.json').exists():z.write(root.parent/'gap-audit.json','gap-audit.json')
    print(f'Compass report: {output/"summary.md"}\nShare: {output/"compass-review.zip"}')

def main():
    p=argparse.ArgumentParser();p.add_argument('--workspace',default=os.environ.get('COMPASS_RESEARCH_HOME',str(ROOT/'workspace')));a=p.parse_args();analyze(Path(a.workspace).expanduser()/'compass')
if __name__=='__main__':main()
