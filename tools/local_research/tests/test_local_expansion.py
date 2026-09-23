import io,json,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import gaps,engine,compass_sync,compass_report
from run import coverage,save

def prices(n=2500):
    idx=pd.date_range('2026-09-01 17:00',periods=n,freq='min',tz='America/Chicago')
    c=100+np.sin(np.arange(n)/19)
    return pd.DataFrame({'open':c,'high':c+.2,'low':c-.2,'close':c},index=idx)

class GapTests(unittest.TestCase):
    def test_only_scheduled_closures_bridge(self):
        t=lambda s:pd.Timestamp(s,tz='America/Chicago')
        self.assertTrue(gaps.scheduled(t('2026-09-01 15:59'),t('2026-09-01 17:00')))
        self.assertTrue(gaps.scheduled(t('2026-09-04 15:59'),t('2026-09-06 17:00')))
        self.assertFalse(gaps.scheduled(t('2026-09-04 15:59'),t('2026-09-07 17:00')))
        d=prices(1380).drop(prices(1380).index[100])
        self.assertEqual(len(coverage(d,set(),'segments')[1]),1)
        self.assertEqual(len(coverage(d,set(),'strict')[1]),0)
        self.assertEqual(len(coverage(d.iloc[1:],set(),'segments')[1]),0)
    def test_features_reset_and_are_causal(self):
        raw=prices().drop(prices().index[1000])
        whole=gaps.calculate(raw,3);later=gaps.calculate(raw.iloc[1000:],3)
        self.assertTrue(whole[-1].any())
        self.assertFalse(later[-1][:240].any())
        for pos in (1,2,3,5,6):
            np.testing.assert_allclose(np.asarray(whole[pos])[-len(later[0]):],np.asarray(later[pos]),equal_nan=True)
        shorter=gaps.calculate(raw.iloc[:2100],3)
        loc=whole[0].index.get_indexer(shorter[0].index)
        for key,value in shorter[4].items():np.testing.assert_array_equal(value,whole[4][key][loc])
    def test_unknown_exit_blocks_rest_of_session(self):
        raw=prices(600);raw.loc[:,['open','high','low','close']]=[100,100.2,99.8,100]
        raw=raw.drop(raw.index[205]);sig=np.zeros(len(raw),int);sig[201]=sig[300]=1
        trades,unknown=engine.simulate(raw,raw,1,sig,pd.Series(1.,index=raw.index),np.full(len(raw),50),'test',{'2026-09-02'})
        self.assertEqual(len(trades),0);self.assertEqual(len(unknown),1)
        self.assertEqual(unknown[0]['entry'],100)

class CompassTests(unittest.TestCase):
    def test_local_report_replaces_snapshot_and_keeps_unknowns(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);day='2026-09-22'
            group={'symbol':'MNQ','strategy':'test','total':5,'wins':1,'losses':1,'breakeven':0,'net_pnl':10,'pending':2,'missing_pnl':1}
            for number in (1,2):
                folder=root/'downloads'/str(number)
                save(folder/f'daily-{day}.json',{'day':day,'asof':number,'futures':{'groups':[group],'totals':group,'counts_complete':True},'smoothers_daily':{'marker':number}})
                save(folder/'manifest.json',{'reports':[{'file':f'daily-{day}.json','state':'downloaded'}]})
            save(root/'downloads'/'3'/'manifest.json',{'reports':[{'file':f'daily-{day}.json','state':'blocked','reason':'HTTP 503'}]})
            compass_report.analyze(root)
            result=json.loads((root/'analysis'/'evidence.json').read_text())
            self.assertEqual(len(result['cohort_groups']),1)
            self.assertEqual(result['cohort_groups'][0]['win_rate'],.5)
            self.assertEqual(result['latest_daily_smoothers']['marker'],2)
            self.assertEqual(len(result['download_failures']),1)
            self.assertTrue((root/'analysis'/'compass-review.zip').exists())
            self.assertTrue(result['warnings'])
    def test_truncation_flags(self):
        self.assertEqual(compass_report.scan_warnings({'truncated':{'signals':False}}),[])
        self.assertTrue(compass_report.scan_warnings({'truncated':{'signals':True}}))
    def test_login_post_and_reports_get_only(self):
        seen=[]
        class FakeOpener:
            def open(self,req,timeout):
                seen.append(req)
                return io.BytesIO(b'{"ok":true}')
        compass_sync.request(FakeOpener(),'/login',{'password':'test-secret'})
        compass_sync.request(FakeOpener(),'/api/daily-results?session_day=2026-09-22')
        self.assertEqual([r.get_method() for r in seen],['POST','GET'])
        self.assertIsNone(seen[1].data)
        self.assertIsNone(compass_sync.NoRedirect().redirect_request(None,None,None,None,None,None))
    def test_sync_keeps_partial_results_without_secrets(self):
        calls=[]
        def fake(opener,path,payload=None):
            calls.append((path,payload))
            if path=='/login':return {'ok':True}
            if '/native/' in path:raise urllib.error.HTTPError(path,503,'Unavailable',{},None)
            if '/daily-results' in path:return {'day':'2026-09-22','asof':1}
            return {'truncated':False}
        with tempfile.TemporaryDirectory() as temp,patch.object(compass_sync,'request',side_effect=fake),patch.object(compass_sync.getpass,'getpass',return_value='private-test-password'),patch('sys.argv',['compass_sync.py','--workspace',temp,'--end','2026-09-22']):
            compass_sync.main()
            self.assertEqual(len(calls),4)
            self.assertTrue(all(payload is None for _,payload in calls[1:]))
            for path in Path(temp).rglob('*.json'):self.assertNotIn('private-test-password',path.read_text())
            result=json.loads((Path(temp)/'compass'/'analysis'/'evidence.json').read_text())
            self.assertEqual(result['download_failures'][0]['reason'],'HTTP 503')

if __name__=='__main__':unittest.main()
