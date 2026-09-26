"""Import/merge TradingView one-minute exports into the reusable local input store."""
from pathlib import Path
import argparse, os, json
import pandas as pd
from run import load_prices, digest, ROOT

def merge(old,new):
    overlap=old.index.intersection(new.index)
    cols=['open','high','low','close']
    if 'volume' in old and 'volume' in new:cols+=['volume']
    if len(overlap) and not old.loc[overlap,cols].equals(new.loc[overlap,cols]):
        # Numeric dtype differences alone must not make equal prices conflict.
        if not (old.loc[overlap,cols].fillna(-1).to_numpy()==new.loc[overlap,cols].fillna(-1).to_numpy()).all():
            raise ValueError('Conflicting historical bars. Check symbol, roll and adjustment settings; use --replace only for an intentional full replacement.')
    return pd.concat([old,new.loc[~new.index.isin(old.index)]]).sort_index()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('symbol');p.add_argument('file',nargs='?');p.add_argument('--workspace',default=os.environ.get('COMPASS_RESEARCH_HOME',str(ROOT/'workspace')));p.add_argument('--replace',action='store_true');a=p.parse_args()
    cfg=json.loads((ROOT/'config.json').read_text());symbol=a.symbol.upper()
    if symbol not in cfg['instruments']:p.error('Symbol is not in config.json')
    source=a.file
    if not source:
        import tkinter as tk
        from tkinter.filedialog import askopenfilename
        app=tk.Tk();app.withdraw();source=askopenfilename(title=f'Select {symbol} ONE-minute CSV',filetypes=[('CSV','*.csv')]);app.destroy()
        if not source:return
    new=load_prices(source);root=Path(a.workspace).expanduser();target=root/'input'/f'{symbol}.csv';target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        old=load_prices(target)
        if not a.replace:new=merge(old,new)
        archive=root/'input-backups';archive.mkdir(parents=True,exist_ok=True)
        backup=archive/f'{symbol}-{digest(target)[:16]}.csv'
        if not backup.exists():backup.write_bytes(target.read_bytes())
    new=new.copy();new['time']=new.index.asi8//10**9
    cols=['time','open','high','low','close']+(['volume'] if 'volume' in new else [])
    temp=target.with_suffix('.tmp');new[cols].to_csv(temp,index=False);temp.replace(target)
    print(f'Saved {len(new)} bars for {symbol}: {target}\nMake sure this file really represents {symbol}; ticker identity is not embedded in ordinary TradingView CSVs.')
if __name__=='__main__':main()
