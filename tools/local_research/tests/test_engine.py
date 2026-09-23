import unittest
import numpy as np
import pandas as pd
from engine import barrier,features,make_bars,pivots,simulate

class CausalScreenTests(unittest.TestCase):
 def test_stop_wins_ambiguous_minute(self):
  self.assertEqual(barrier(100,103,97,98,102,1),(98,'stop',True))
 def test_stop_gap_is_not_filled_at_stale_stop(self):
  self.assertEqual(barrier(95,99,94,98,102,1),(95,'stop',False))
  self.assertEqual(barrier(105,106,101,102,98,-1),(105,'stop',False))
 def test_pivot_only_at_confirmation(self):
  d=pd.DataFrame({'high':[1,2,7,3,2,1], 'low':[0,1,2,1,0,-1]})
  ph,_=pivots(d,2);self.assertTrue(np.isnan(ph.iloc[2]));self.assertEqual(ph.iloc[4],7)
 def test_future_prices_do_not_change_existing_features(self):
  rng=np.random.default_rng(91)
  c=100+np.cumsum(rng.normal(0,.1,6000))
  o=np.r_[c[0],c[:-1]]
  r=pd.DataFrame({'open':o,'high':np.maximum(o,c)+.2,'low':np.minimum(o,c)-.2,'close':c},index=pd.date_range('2026-08-24 17:00',periods=6000,freq='min',tz='America/Chicago'))
  short=r.iloc[:4500];full=r.iloc[:6000]
  for n in (1,3,5):
   ds=make_bars(short,n);df=make_bars(full,n)
   a,b,e,s,k=features(ds,short,n);aa,bb,ee,ss,kk=features(df,full,n)
   inds=df.index.get_indexer(ds.index)
   for x,y in [(a.to_numpy(),aa.to_numpy()),(b,bb),(e,ee),(k,kk)]:np.testing.assert_allclose(x,y[inds],equal_nan=True)
   for family in s:np.testing.assert_array_equal(s[family],ss[family][inds])
 def test_next_open_entry_and_first_minute_protection(self):
  ts=pd.date_range('2026-09-01 09:00',periods=210,freq='min',tz='America/Chicago')
  r=pd.DataFrame({'open':100.,'high':100.5,'low':99.5,'close':100.},index=ts)
  r.iloc[202,r.columns.get_loc('low')]=97
  sig=np.zeros(len(r),int);sig[201]=1
  t,c=simulate(r,r,1,sig,pd.Series(1.,index=ts),np.full(len(r),50.),'test',{'2026-09-01'})
  self.assertEqual(len(t),1);self.assertEqual(t.entry_at.iloc[0],str(ts[202]));self.assertEqual(t.exit.iloc[0],98.5);self.assertEqual(t.reason.iloc[0],'stop')
 def test_missing_exit_path_is_censored(self):
  ts=pd.date_range('2026-09-01 09:00',periods=220,freq='min',tz='America/Chicago')
  r=pd.DataFrame({'open':100.,'high':100.5,'low':99.5,'close':100.},index=ts).drop(ts[205])
  sig=np.zeros(len(r),int);sig[201]=1
  t,c=simulate(r,r,1,sig,pd.Series(1.,index=r.index),np.full(len(r),50.),'test',{'2026-09-01'})
  self.assertEqual(len(t),0);self.assertEqual(len(c),1)
 def test_fractal_waits_for_htf_closure(self):
  from fractal import fractal_signals
  ts=pd.date_range('2026-09-01 08:00',periods=40,freq='5min',tz='America/Chicago')
  d=pd.DataFrame({'open':101.,'high':110.,'low':90.,'close':100.},index=ts)
  d.iloc[24:]=[102.,104.,99.,102.]
  d.iloc[24]=[100.,103.,89.,102.]
  d.iloc[36:]=[102.,103.,101.,102.]
  # Expanded minute OHLC produces the same completed hourly boundaries.
  raw=d.reindex(pd.date_range(ts[0],periods=200,freq='min'),method='ffill')
  sig,audit=fractal_signals(d,raw,5,np.full(len(d),20.))
  self.assertEqual(sig[24:36].sum(),0)
  self.assertEqual(sig[36],1)
  self.assertEqual(audit[0]['closure'],2)
  # A two-sided sweep must not become a reviewed entry.
  d.iloc[24,d.columns.get_loc('high')]=111
  raw=d.reindex(pd.date_range(ts[0],periods=200,freq='min'),method='ffill')
  sig,_=fractal_signals(d,raw,5,np.full(len(d),20.))
  self.assertEqual(sig[36],0)
if __name__=='__main__':unittest.main()
