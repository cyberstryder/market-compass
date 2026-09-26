"""Private dashboard APIs. Official activation is gated and never automatic."""
import time
from typing import Literal
from pydantic import BaseModel,Field,ConfigDict,StrictBool
from fastapi import HTTPException,Query
from fastapi.responses import JSONResponse
from . import native_config,native_routing,native_reports,native_handoff,smoothers_handoff


class ConfigChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:str=Field(min_length=64,max_length=64)
    configs:list[dict]|None=Field(default=None,max_length=500)
    restore_revision:str|None=Field(default=None,min_length=64,max_length=64)
    reason:str=Field(min_length=1,max_length=300)


class RouteChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:str|None=Field(default=None,min_length=64,max_length=64)
    action:Literal['prepare','schedule','cancel_future']
    mode:Literal['direct_shadow','relay_shadow']|None=None
    effective_session:str|None=Field(default=None,max_length=10)
    reason:str=Field(min_length=1,max_length=300)



class HandoffChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['prepare','activate','rollback']
    review_session:str=Field(default='',max_length=10)
    effective_session:str=Field(default='',max_length=10)
    plan_id:str=Field(default='',max_length=64)
    previous_sender_paused:bool=False
    operator_reviewed:bool=False


class SmoothersHandoffChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['prepare','activate','rollback']
    review_week:str=Field(default='',max_length=10)
    effective_week:str=Field(default='',max_length=10)
    plan_id:str=Field(default='',max_length=64)
    previous_sender_paused:StrictBool=False
    operator_reviewed:StrictBool=False
    original_alerts_reviewed:StrictBool=False


def install(app,db):
    @app.get("/api/strategy-readiness")
    def get_strategy_readiness():
        from .strategy_readiness import KEY
        with db.tx() as c:return db.get(c,KEY,{"status":"awaiting_report"})

    @app.get('/api/native/smoothers/handoff')
    def get_smoothers_handoff():
        with db.tx() as c:return smoothers_handoff.snapshot(db,c)

    @app.post('/api/native/smoothers/handoff')
    def set_smoothers_handoff(change:SmoothersHandoffChange):
        try:
            if change.action=='prepare':return smoothers_handoff.prepare(db,change.review_week,change.effective_week,time.time())
            if change.action=='activate':return smoothers_handoff.activate(db,change.plan_id,change.previous_sender_paused,change.operator_reviewed,change.original_alerts_reviewed,time.time())
            return smoothers_handoff.rollback(db,time.time())
        except native_config.RevisionConflict as e:raise HTTPException(409,str(e)) from None
        except ValueError as e:raise HTTPException(422,str(e)) from None

    @app.get('/api/native/morning/handoff')
    def get_handoff():
        with db.tx() as c:return native_handoff.snapshot(db,c)

    @app.post('/api/native/morning/handoff')
    def set_handoff(change:HandoffChange):
        try:
            if change.action=='prepare':return native_handoff.prepare(db,change.review_session,change.effective_session,time.time())
            if change.action=='activate':return native_handoff.activate(db,change.plan_id,change.previous_sender_paused,change.operator_reviewed,time.time())
            return native_handoff.rollback(db,time.time())
        except native_config.RevisionConflict as e:raise HTTPException(409,str(e)) from None
        except ValueError as e:raise HTTPException(422,str(e)) from None

    @app.get('/api/native/report')
    def get_report(limit:int=Query(100,ge=1,le=200),start:str=Query('',max_length=10),end:str=Query('',max_length=10),
            ticker:str=Query('',max_length=50,pattern=r'^[A-Za-z0-9_:!.\-]*$'),week:str=Query('',max_length=10),download:bool=False):
        try:
            with db.tx() as c:result=native_reports.report(db,c,time.time(),limit,start,end,ticker.upper(),week)
        except ValueError as e:raise HTTPException(422,str(e)) from None
        return JSONResponse(result,headers={'Content-Disposition':'attachment; filename="native-program-report.json"'} if download else {})

    @app.get('/api/native/smoothers/config')
    def get_config():
        with db.tx() as c:return native_config.snapshot(db,c)

    @app.post('/api/native/smoothers/config')
    def set_config(change:ConfigChange):
        if (change.configs is None)==(change.restore_revision is None):raise HTTPException(422,'Provide configurations or a revision to restore')
        try:return native_config.revise(db,change.expected_revision,change.configs,change.reason,time.time(),change.restore_revision)
        except native_config.RevisionConflict as e:raise HTTPException(409,str(e)) from None
        except ValueError as e:raise HTTPException(422,str(e)) from None

    @app.get('/api/native/morning/routing')
    def get_routing():
        with db.tx() as c:return native_routing.snapshot(db,c,time.time())

    @app.post('/api/native/morning/routing')
    def set_routing(change:RouteChange):
        try:return native_routing.change(db,change.expected_revision,change.action,time.time(),change.mode,change.effective_session,change.reason)
        except native_config.RevisionConflict as e:raise HTTPException(409,str(e)) from None
        except ValueError as e:raise HTTPException(422,str(e)) from None
