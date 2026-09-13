import asyncio
import time
import uuid
from urllib.parse import urlparse,parse_qsl,urlencode
import httpx
from sqlalchemy import select,func
from .store import events

class DeliveryError(Exception):
    def __init__(self,detail,retry=15):
        super().__init__(detail)
        self.retry=retry


def confirmed_url(webhook):
    url=urlparse(webhook)
    params=[(k,v) for k,v in parse_qsl(url.query,keep_blank_values=True) if k!="wait"]
    return url._replace(query=urlencode(params+[("wait","true")]),fragment="").geturl()


def outbox_status(db,c,now):
    cursor=db.get(c,"outbox:discord",0)
    count,oldest=c.execute(select(func.count(),func.min(events.c.ts)).where(events.c.kind=="alert",events.c.id>cursor)).one()
    return {"pending":count,"oldest_age":round(now-oldest,1) if oldest is not None else None,
        "last_acknowledged_event":cursor or None,"last_confirmation":db.get(c,"outbox:discord:confirmation")}


async def dispatch(client,db,webhook,row):
    p=row["payload"]
    content=f"[SIMULATED] {row['symbol']} | {p.get('status')}\n{p.get('strategy','')} {p.get('side','')}\n{p.get('reason',p.get('exit_reason',''))}"
    for label in ["entry","stop","target","qty","exit","pnl"]:
        if label in p: content+=f"\n{label}: {p[label]}"
    content+=f"\nEvent #{row['id']} | "+time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime(row["ts"]))
    response=await client.post(confirmed_url(webhook),json={"content":content[:1900],"allowed_mentions":{"parse":[]}})
    if response.status_code==429:
        try: retry=max(1,min(60,float(response.json().get("retry_after",5))))
        except (ValueError,TypeError): retry=5
        raise DeliveryError("Discord rate limit; alert remains queued",retry)
    if response.status_code!=200:
        raise DeliveryError(f"Discord HTTP {response.status_code}; message not confirmed")
    try: message=response.json()
    except ValueError: raise DeliveryError("Discord returned no message confirmation") from None
    message_id=message.get("id") if isinstance(message,dict) else None
    if not isinstance(message_id,str) or not message_id.isdigit():
        raise DeliveryError("Discord returned no message ID; alert remains queued")
    now=time.time()
    with db.tx() as c:
        db.put(c,"outbox:discord",max(row["id"],db.get(c,"outbox:discord",0)))
        confirmation={"event_id":row["id"],"message_id":message_id,"at":now}
        db.put(c,"outbox:discord:confirmation",confirmation)
        db.append(c,"alert_delivery","discord",row["symbol"],now,confirmation,key="discord:"+message_id)
    db.health("discord","delivered","Discord confirmed a saved message; event IDs identify possible retries",now)


async def deliver(db,cfg):
    owner=uuid.uuid4().hex
    if not cfg.discord:
        db.health("discord","not_configured","Dashboard alerts active; add a webhook for phone delivery")
        return
    url=urlparse(cfg.discord)
    if url.scheme!="https" or url.hostname not in {"discord.com","discordapp.com"} or not url.path.startswith("/api/webhooks/"):
        db.health("discord","error","Invalid Discord webhook URL")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        verified=False
        while True:
            try:
                if not verified:
                    # Read-only credential check: never broadcast a synthetic trade.
                    check=await client.get(url._replace(query="",fragment="").geturl())
                    if check.status_code!=200:
                        db.health("discord","error",f"Webhook verification HTTP {check.status_code}; check destination and token")
                        await asyncio.sleep(30)
                        continue
                    db.health("discord","connected","Webhook verified; awaiting simulated alerts")
                    verified=True
                with db.tx() as c:
                    rows=[]
                    if db.lease(c,"discord",owner,45):
                        cursor=db.get(c,"outbox:discord",0)
                        rows=list(c.execute(select(events).where(events.c.kind=="alert",events.c.id>cursor).order_by(events.c.id).limit(1)).mappings())
                for row in rows:
                    await dispatch(client,db,cfg.discord,row)
            except asyncio.CancelledError: raise
            except DeliveryError as e:
                db.health("discord","error",str(e))
                await asyncio.sleep(e.retry)
            except Exception as e:
                db.health("discord","error",type(e).__name__+"; alert remains queued")
                await asyncio.sleep(15)
            await asyncio.sleep(1)
