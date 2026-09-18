from datetime import datetime
import pytest
from compass import paper_risk
from compass.engine import Engine
from compass.config import Config
from compass.market import CT
from test_scanner import db, NOW, quote
from test_option_selection_audit import signal, seed, CONTRACT
from test_setup_reporting import report_db, pg


def legacy(db, c, asset='future', pnl=-426, entered=NOW-600, exited=NOW-60, id='legacy'):
    db.put(c,'trade:'+id,dict(id=id,asset=asset,status='closed',entered_at=entered,exited_at=exited,pnl=pnl))


def test_migration_conserves_loss_and_enables_only_independent_options(report_db):
    db=report_db
    cfg=Config(local=True,setup_study=False)
    with db.tx() as c:
        legacy(db,c)
        original=dict(realized=-426,entries=1)
        db.put(c,'risk:'+paper_risk.risk_day(NOW),original)
        before=db.prefix(c,'trade:')
        accounts=paper_risk.ledgers(db,c,NOW,persist=True)
        assert accounts['future']['realized']==-426 and accounts['future']['entries']==1
        assert accounts['option']['realized']==accounts['option']['entries']==0
        assert all(a['ready'] for a in accounts.values())
        seed(db,c)
        assert Engine(db,cfg).options(c,signal(),NOW)
        assert paper_risk.account(db,c,NOW,'option')['entries']==1
        assert paper_risk.account(db,c,NOW,'future')['realized']==-426
        assert db.get(c,'risk:'+paper_risk.risk_day(NOW))==original
        assert db.get(c,'trade:legacy')==before['trade:legacy']
        # Restart must not reseed/reset an account after new entries.
        assert paper_risk.ledgers(db,c,NOW+1,persist=True)['option']['entries']==1


@pytest.mark.parametrize('missing', ['trade','pnl','asset','partial'])
def test_incomplete_migration_fails_closed(report_db,missing):
    db=report_db
    with db.tx() as c:
        db.put(c,'risk:'+paper_risk.risk_day(NOW),dict(realized=-426,entries=1))
        if missing!='trade':legacy(db,c,asset='unknown' if missing=='asset' else 'future',pnl=None if missing=='pnl' else -426)
        if missing=='partial':db.put(c,paper_risk.key(NOW,'option'),dict(realized=0,entries=0,ready=True))
        assert not any(a['ready'] for a in paper_risk.ledgers(db,c,NOW,persist=True).values())
        seed(db,c)
        reason,_=Engine(db,Config(local=True)).entry_check(c,{**signal(),'symbol':CONTRACT},NOW)
        assert reason=='Paper risk migration requires reconciliation'


def test_each_account_has_its_own_capacity_and_loss_limit(report_db):
    db=report_db
    cfg=Config(local=True,setup_study=False)
    with db.tx() as c:
        paper_risk.ledgers(db,c,NOW,persist=True)
        for i in range(3):db.put(c,'position:future'+str(i),dict(status='open',asset='future'))
        seed(db,c)
        engine=Engine(db,cfg)
        option_signal={**signal(),'symbol':CONTRACT,'stop_distance':.6}
        assert engine.entry_check(c,option_signal,NOW)[0] is None
        for i in range(3):db.put(c,'position:option'+str(i),dict(status='open',asset='option'))
        assert engine.entry_check(c,option_signal,NOW)[0]=='Portfolio cap: three simultaneous simulated positions'
        for i in range(3):db.put(c,'position:option'+str(i),dict(status='closed',asset='option'))
        account=paper_risk.account(db,c,NOW,'option');account['realized']=-300
        db.put(c,paper_risk.key(NOW,'option'),account)
        assert engine.entry_check(c,option_signal,NOW)[0]=='Daily simulated loss limit'


def test_exit_updates_only_own_account_once_and_keeps_legacy_frozen(report_db):
    db=report_db
    cfg=Config(local=True,setup_study=False)
    with db.tx() as c:
        seed(db,c)
        engine=Engine(db,cfg)
        assert engine.options(c,signal(),NOW)
        db.put(c,'quote:'+CONTRACT,quote(NOW+1,1.3,1.31))
        engine.exits(c,NOW+1)
        ledger=paper_risk.account(db,c,NOW,'option')
        assert ledger['realized']<0 and ledger['entries']==1
        assert paper_risk.account(db,c,NOW,'future')['realized']==0
        assert not db.prefix(c,'risk:')
        engine.exits(c,NOW+2)
        assert paper_risk.account(db,c,NOW,'option')==ledger


def test_old_open_position_exit_carries_loss_to_new_day_without_new_entry(report_db):
    db=report_db
    cfg=Config(local=True,setup_study=False)
    with db.tx() as c:
        seed(db,c);engine=Engine(db,cfg)
        assert engine.options(c,signal(),NOW)
        p=db.get(c,'position:'+CONTRACT)
        # Model a position admitted before the policy change, still unresolved.
        p.pop('risk_policy');p.pop('paper_portfolio')
        db.put(c,'position:'+CONTRACT,p);db.put(c,'trade:'+p['id'],p)
        tomorrow=datetime(2026,9,15,9,15,tzinfo=CT).timestamp()
        db.put(c,'quote:'+CONTRACT,quote(tomorrow,1.3,1.31))
        engine.exits(c,tomorrow)
        risk=paper_risk.account(db,c,tomorrow,'option')
        assert risk['ready'] and risk['entries']==0 and risk['realized']<0
        assert db.get(c,'trade:'+p['id'])['exit_risk_policy']==paper_risk.VERSION


def test_read_only_preview_has_no_side_effects_and_includes_carryover(report_db):
    db=report_db
    with db.tx() as c:
        legacy(db,c);db.put(c,'risk:'+paper_risk.risk_day(NOW),dict(realized=-426,entries=1))
        before=db.prefix(c,'')
        view=paper_risk.snapshot(db,c,Config(local=True),NOW)
        assert view['accounts'][0]['daily_loss_locked']
        assert not view['accounts'][1]['daily_loss_locked']
        assert db.prefix(c,'')==before


def test_missing_all_account_states_never_resets_recorded_v2_activity(report_db):
    from sqlalchemy import delete
    from compass.store import state
    db=report_db
    with db.tx() as c:
        seed(db,c)
        assert Engine(db,Config(local=True,setup_study=False)).options(c,signal(),NOW)
        c.execute(delete(state).where(state.c.key.startswith('paper_risk:v2:')))
        accounts=paper_risk.ledgers(db,c,NOW,persist=True)
        assert not any(a['ready'] for a in accounts.values())
        assert all(a['migration']['problems'] for a in accounts.values())
