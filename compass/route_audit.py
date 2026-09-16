"""Read-only extraction for the first-15-minute versus later-confirmation study."""
import hashlib
import json
import os
import time
from sqlalchemy import text
from .store import Store

OPTIONS_KEY = 'spy-0dte-timing-v1:run:cce4690114ab5bd91cc32b010ea548204cd759f5d3e9821d3a35013372fe0ea5'
UNDERLYING_KEY = 'spy-opening-wait-v1:run:bdbe3c7f045115d88057d42470a14600612b5f4015f8bf380024f2d6e26936d4'
PROTOCOL = {
    'question': 'When the first 15m candle stays within the premarket range, skip that day or wait for a later completed 15m breakout?',
    'candidate_checks_minutes_after_open': [30,45,60],
    'baseline': 'No trade if the first 15m does not confirm',
    'candidate': 'First later qualifying 15m close, at most one trade per day',
    'primary_cohort': 'Original common cohort with evaluable evidence in all six candle windows',
    'sensitivity_cohort': 'All dates with evaluable 15m evidence',
    'validation_start': '2026-03-16',
    'costs': 'Existing ask entry, bid exit, $0.65 per contract per side, one contract',
    'exits': ['primary_exit','fixed_exit'],
    'statistics': 'Net dollars, win rate, average trade, profit factor, maximum drawdown; paired weekly bootstrap versus zero',
    'status': 'Exploratory post-hoc subgroup analysis, not a fresh holdout or live setting change',
    'external_requests': 0,
}

def main():
    db = Store(os.environ['DATABASE_URL'])
    with db.tx() as c:
        c.execute(text('SET TRANSACTION READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        report = db.get(c, OPTIONS_KEY)
        records = db.get(c, OPTIONS_KEY+':records')
        signals = db.get(c, UNDERLYING_KEY+':observations')
        if not report or report.get('status') != 'complete' or not records or not signals:
            raise ValueError('completed_frozen_research_evidence_required')
        by_day = {s['date']: s for s in signals if s['policy']=='candle_confirmation' and s['wait']==15}
        bad = {r['date'] for r in records if r['status'] not in ('traded','no_signal','entry_liquidity_filter')}
        selected = sorted((r for r in records if r['wait']==15),key=lambda r:r['date'])
        output = []
        for r in selected:
            s = by_day[r['date']]
            minute = s.get('entry_minute')
            if s['traded'] and minute not in (15,30,45,60):
                raise ValueError('unexpected_signal_boundary')
            if not s['traded'] and r['status'] != 'no_signal':
                raise ValueError('signal_record_mismatch')
            out = {k:r.get(k) for k in ('date','status','side','entry_at','entry','primary_exit','fixed_exit','primary_exit_at','exit_reason')}
            out.update(common_eligible=r['date'] not in bad, signal_traded=s['traded'],
                entry_minute=minute, entry_reference=s.get('entry_reference'),
                risk=s.get('risk_dollars_per_share'),trigger=s.get('trigger'),
                contract=r.get('contract',{}).get('ticker'))
            output.append(out)
        read_only = c.execute(text('SHOW transaction_read_only')).scalar_one()
    db.engine.dispose()
    packed = json.dumps(output,sort_keys=True,separators=(',',':'),allow_nan=False)
    meta = dict(protocol=PROTOCOL, options_key=OPTIONS_KEY, underlying_key=UNDERLYING_KEY,
        source_record_count=len(records), selected_count=len(output), common_exclusions=sorted(bad),
        original_validation_15m=report['results']['validation'].get('15',report['results']['validation'].get(15)),
        source_records_sha256=hashlib.sha256(packed.encode()).hexdigest(),
        database_read_only=read_only, code=os.getenv('RAILWAY_GIT_COMMIT_SHA'), at=time.time())
    print('SPY_LATE15_META '+json.dumps(meta,sort_keys=True,separators=(',',':')),flush=True)
    for start in range(0,len(output),5):
        print('SPY_LATE15_ROWS '+json.dumps({'offset':start,'rows':output[start:start+5]},sort_keys=True,separators=(',',':')),flush=True)

if __name__ == '__main__':
    main()
