"""Bounded Alpaca adapter for native weekly workflows; no broker orders."""
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import re
import time
import pandas as pd
from .native_morning.options import AlpacaQuotes,OptionsConfig

ET=ZoneInfo('America/New_York')


def iso(stamp): return datetime.fromtimestamp(stamp,timezone.utc).isoformat()


class Data:
    def __init__(self,cfg,client):
        self.cfg,self.client=cfg,client
        self.deadline=None
        self.history_pages=[]
        self.headers={'APCA-API-KEY-ID':cfg.alpaca_key,'APCA-API-SECRET-KEY':cfg.alpaca_secret}
        self.options=AlpacaQuotes(OptionsConfig(True,cfg.alpaca_key,cfg.alpaca_secret),client)

    def get(self,path,params):
        if self.deadline is not None and time.monotonic()>self.deadline:raise ValueError('Provider work deadline exceeded')
        response=self.client.get(path,params=params,headers=self.headers)
        if response.status_code!=200: raise ValueError('Provider HTTP '+str(response.status_code))
        if len(response.content)>16*1024*1024: raise ValueError('Provider response too large')
        return response.json()

    def calendar(self,monday):
        rows=self.get('https://paper-api.alpaca.markets/v2/calendar',{'start':monday.isoformat(),'end':(monday+timedelta(days=4)).isoformat()})
        if not isinstance(rows,list) or len(rows)>5: raise ValueError('Invalid calendar')
        result=[]
        for row in rows:
            day=datetime.fromisoformat(row['date']).date()
            if not monday<=day<=monday+timedelta(days=4): raise ValueError('Calendar outside requested week')
            start=datetime.fromisoformat(row['date']+'T'+row['open']).replace(tzinfo=ET).timestamp()
            end=datetime.fromisoformat(row['date']+'T'+row['close']).replace(tzinfo=ET).timestamp()
            if end<=start: raise ValueError('Invalid session')
            result.append({'date':row['date'],'open':start,'close':end})
        if len({r['date'] for r in result})!=len(result): raise ValueError('Duplicate session')
        return sorted(result,key=lambda r:r['date'])

    def bars(self,symbol,frame,start,end):
        return self.bar_batch([symbol],frame,start,end)[symbol]

    def bar_batch(self,symbols,frame,start,end):
        if not 1<=len(symbols)<=100:raise ValueError('Invalid bar batch')
        params={'symbols':','.join(symbols),'timeframe':frame,'start':iso(start),'end':iso(end),
                'adjustment':'split','feed':'sip','sort':'asc','limit':10000}
        all_rows={symbol:[] for symbol in symbols};seen=set()
        self.history_pages=[]
        for _ in range(100):
            data=self.get('https://data.alpaca.markets/v2/stocks/bars',params)
            self.history_pages.append(sum(len(data.get('bars',{}).get(s,[])) for s in symbols))
            if sum(self.history_pages)>150000:raise ValueError('Bar history row limit exceeded')
            for symbol in symbols:
                rows=data.get('bars',{}).get(symbol,[])
                if not isinstance(rows,list): raise ValueError('Invalid bars')
                all_rows[symbol].extend(rows)
            token=data.get('next_page_token')
            if not token: break
            if token in seen: raise ValueError('Repeated pagination')
            seen.add(token);params['page_token']=token
        else: raise ValueError('Incomplete bar history')
        return {symbol:self.normalize(rows) for symbol,rows in all_rows.items()}

    @staticmethod
    def normalize(rows):
        if not rows:return pd.DataFrame()
        df=pd.DataFrame(rows).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
        df.index=pd.to_datetime(df.pop('t'),utc=True)
        if df.index.has_duplicates:
            for _,group in df[df.index.duplicated(keep=False)].groupby(level=0):
                if len(group.drop_duplicates())!=1:raise ValueError('Conflicting duplicate bars')
        for column in ('open','high','low','close','volume'):
            df[column]=pd.to_numeric(df[column],errors='raise')
        import numpy as np
        if not np.isfinite(df[['open','high','low','close','volume']].to_numpy()).all():raise ValueError('Nonfinite bars')
        if (df['low']<=0).any() or (df['high']<df['low']).any() or (df['volume']<0).any() or any(((df[k]<df['low'])|(df[k]>df['high'])).any() for k in ('open','close')):raise ValueError('Invalid bar prices')
        return df[~df.index.duplicated()].sort_index()

    def hourly(self,symbol,asof):
        bars=self.bars(symbol,'30Min',asof-395*86400,asof)
        if bars.empty:return bars
        bars=bars[(bars.index+pd.Timedelta(minutes=30))<=pd.Timestamp(asof,unit='s',tz='UTC')]
        local=bars.index.tz_convert(ET)
        keep=((local.hour==9)&(local.minute>=30))|((local.hour>=10)&(local.hour<16))
        bars=bars[keep];local=local[keep]
        labels=local.normalize()+pd.to_timedelta(local.hour-(local.minute==0).astype(int),unit='h')+pd.Timedelta(minutes=30)
        bars=bars.assign(bucket=labels)
        counts=bars.groupby('bucket').size()
        out=bars.groupby('bucket').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
        out['parts']=counts
        out.index=pd.DatetimeIndex(out.index).tz_convert('UTC')
        return out.sort_index()

    def daily(self,symbol,asof):
        bars=self.bars(symbol,'1Day',asof-460*86400,asof)
        if not bars.empty:
            bars.index=bars.index.tz_convert(ET).normalize().tz_localize(None)
        return bars

    def contract(self,symbol,friday,direction,price):
        params={'expiration_date_gte':(friday-timedelta(days=2)).isoformat(),
                'expiration_date_lte':friday.isoformat(),'type':'call' if direction=='CALL' else 'put',
                'feed':'opra','limit':1000}
        contracts=[];seen=set()
        for _ in range(10):
            data=self.get('https://data.alpaca.markets/v1beta1/options/snapshots/'+symbol,params)
            for occ in data.get('snapshots',{}):
                match=re.fullmatch(re.escape(symbol)+r'(\d{6})([CP])(\d{8})',occ)
                if not match or match[2]!=('C' if direction=='CALL' else 'P'): continue
                expiry=datetime.strptime(match[1],'%y%m%d').date()
                if friday-timedelta(days=2)<=expiry<=friday:
                    contracts.append({'symbol':occ,'expiration':expiry.isoformat(),'strike':int(match[3])/1000,
                                      'underlying':symbol,'multiplier':100,'type':direction})
            token=data.get('next_page_token')
            if not token:break
            if token in seen:raise ValueError('Repeated option page')
            seen.add(token);params['page_token']=token
        else:raise ValueError('Incomplete option chain')
        if not contracts:return None
        latest=max(c['expiration'] for c in contracts)
        # Source's unspecified tie behavior is made deterministic and visible.
        return min((c for c in contracts if c['expiration']==latest),key=lambda c:(abs(c['strike']-price),c['strike']))

    def quote(self,contract):
        return self.options.snapshots([contract])[contract['symbol']]

    def quotes(self,contracts):
        result={}
        for offset in range(0,len(contracts),100):
            result.update(self.options.snapshots(contracts[offset:offset+100]))
        return result
