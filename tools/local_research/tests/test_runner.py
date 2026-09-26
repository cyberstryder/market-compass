import unittest,tempfile,json
from pathlib import Path
import pandas as pd
import numpy as np
from run import load_prices,coverage,fingerprint,validate_config,ROOT,make_review,selected_symbols
class RunnerTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def test_symbol_selection_defaults_overrides_and_legacy(self):
  cfg=json.loads((ROOT/'config.json').read_text())
  self.assertEqual(selected_symbols(cfg),['MES'])
  self.assertEqual(selected_symbols(cfg,['NQ']),['NQ'])
  with self.assertRaises(ValueError):selected_symbols(cfg,['UNKNOWN'])
  cfg['default_symbols']=[]
  with self.assertRaises(ValueError):validate_config(cfg)
  del cfg['default_symbols']
  self.assertEqual(selected_symbols(cfg),list(cfg['instruments']))
 def test_bad_prices_and_duplicates_rejected(self):
  p=self.root/'x.csv';p.write_text('time,open,high,low,close\n1787587200,100,101,99,100\n1787587200,100,101,99,100\n')
  with self.assertRaisesRegex(ValueError,'Duplicate'):load_prices(p)
  p.write_text('time,open,high,low,close\n1787587200,100,99,98,100\n')
  with self.assertRaisesRegex(ValueError,'geometry'):load_prices(p)
 def test_naive_timestamp_rejected(self):
  p=self.root/'x.csv';p.write_text('time,open,high,low,close\n2026-09-01 10:00,100,101,99,100\n')
  with self.assertRaisesRegex(ValueError,'offset'):load_prices(p)
 def test_missing_bar_cannot_be_complete_session(self):
  idx=pd.date_range('2026-09-01 17:00',periods=1380,freq='min',tz='America/Chicago')
  df=pd.DataFrame({'close':1},index=idx);inv,days=coverage(df,set());self.assertEqual(days,['2026-09-02'])
  inv,days=coverage(df.drop(idx[100]),set());self.assertEqual(days,[]);self.assertEqual(inv[0]['missing_minutes'],1)
 def test_cache_invalidated_by_input_and_config(self):
  p=self.root/'x';p.write_text('a');a,_=fingerprint(p,{'test':1},'MNQ')
  p.write_text('b');b,_=fingerprint(p,{'test':1},'MNQ');self.assertNotEqual(a,b)
  c,_=fingerprint(p,{'test':2},'MNQ');self.assertNotEqual(b,c)
 def test_config_and_review_missing_data(self):
  cfg=json.loads((ROOT/'config.json').read_text());validate_config(cfg)
  make_review({'MES':{'state':'pending','error':'Missing input/MES.csv'}},cfg,self.root)
  self.assertIn('pending',(self.root/'summary.md').read_text());self.assertTrue((self.root/'review.zip').exists())
  cfg['validation_end']=cfg['development_end']
  with self.assertRaisesRegex(ValueError,'increasing'):validate_config(cfg)
if __name__=='__main__':unittest.main()

class ImportTests(unittest.TestCase):
 def test_matching_overlap_merges_and_conflict_rejects(self):
  from import_csv import merge
  idx=pd.date_range('2026-09-01',periods=3,freq='min',tz='UTC')
  x=pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.},index=idx)
  self.assertEqual(len(merge(x.iloc[:2],x.iloc[1:])),3)
  bad=x.copy();bad.loc[idx[1],'close']=100.5
  with self.assertRaisesRegex(ValueError,'Conflicting'):merge(x,bad)
