"""Download existing Compass reports. Password/cookies stay in process memory."""
from pathlib import Path
from datetime import datetime,timedelta,date
from zoneinfo import ZoneInfo
import argparse,getpass,http.cookiejar,json,os,urllib.request,urllib.parse,urllib.error
from run import ROOT,save,digest
BASE='https://dashboard-production-c3a7.up.railway.app'
LIMIT=15*1024*1024
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def request(opener,path,payload=None):
    if not path.startswith(('/api/','/login')):raise ValueError('Unexpected endpoint')
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(BASE+path,data=body,headers={'Content-Type':'application/json','Accept':'application/json'},method='POST' if payload is not None else 'GET')
    with opener.open(req,timeout=60) as response:
        content=response.read(LIMIT+1)
        if len(content)>LIMIT:raise ValueError('Report exceeds 15 MB limit')
        result=json.loads(content)
        if not isinstance(result,dict):raise ValueError('Unexpected report format')
        return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--days',type=int,default=1);p.add_argument('--end',default=(datetime.now(ZoneInfo('America/Chicago')).date()-timedelta(days=1)).isoformat());p.add_argument('--workspace',default=os.environ.get('COMPASS_RESEARCH_HOME',str(ROOT/'workspace')));a=p.parse_args()
    if not 1<=a.days<=7:p.error('--days must be 1 through 7 per download')
    end=date.fromisoformat(a.end);requested=[end-timedelta(days=i) for i in reversed(range(a.days))]
    days=[d.isoformat() for d in requested if d.weekday()<5]
    if not days:raise SystemExit('No weekday session dates in this range; choose a trading date with --end YYYY-MM-DD.')
    root=Path(a.workspace).expanduser()/'compass';stamp=datetime.now().strftime('%Y%m%dT%H%M%S%f');dest=root/'downloads'/stamp;dest.mkdir(parents=True,exist_ok=False)
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    password=getpass.getpass('Compass dashboard password (not saved): ')
    try:request(opener,'/login',{'password':password})
    except (urllib.error.HTTPError,urllib.error.URLError,ValueError,TimeoutError) as e:
        reason=f'HTTP {e.code}' if isinstance(e,urllib.error.HTTPError) else type(e).__name__
        raise SystemExit(f'Login failed: {reason}. No password or cookie was saved.')
    finally:password=None
    manifest={'version':1,'downloaded_at':datetime.now().astimezone().isoformat(),'start':days[0],'end':days[-1],'skipped_non_session_dates':[d.isoformat() for d in requested if d.weekday()>=5],'reports':[]}
    endpoints=[(f'daily-{day}.json','/api/daily-results?'+urllib.parse.urlencode({'session_day':day})) for day in days]
    params=urllib.parse.urlencode({'start':days[0],'end':days[-1],'limit':200})
    endpoints += [('morning.json','/api/projects/morning/report?'+params),('native.json','/api/native/report?'+params)]
    for filename,url in endpoints:
        try:
            data=request(opener,url);save(dest/filename,data)
            manifest['reports'].append({'file':filename,'state':'downloaded','sha256':digest(dest/filename)})
            print('Downloaded '+filename,flush=True)
        except (urllib.error.HTTPError,urllib.error.URLError,ValueError,TimeoutError) as e:
            reason=f'HTTP {e.code}' if isinstance(e,urllib.error.HTTPError) else type(e).__name__
            manifest['reports'].append({'file':filename,'state':'blocked','reason':reason});print(filename+': '+reason)
    save(dest/'manifest.json',manifest)
    save(root/'latest_download.json',{'folder':str(dest.resolve())})
    from compass_report import analyze
    analyze(root)
    print('Finished. Dashboard report limits and missing observations remain explicit.')
    return 2 if any(r['state']=='blocked' for r in manifest['reports']) else 0
if __name__=='__main__':raise SystemExit(main())
