import asyncio
import time
import uuid
from urllib.parse import urlparse
import httpx
from sqlalchemy import select
from .store import events

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
                    p=row["payload"]
                    content=f"[SIMULATED] {row['symbol']} | {p.get('status')}\n{p.get('strategy','')} {p.get('side','')}\n{p.get('reason',p.get('exit_reason',''))}"
                    for label in ["entry","stop","target","qty","exit","pnl"]:
                        if label in p: content+=f"\n{label}: {p[label]}"
                    content+=f"\nEvent #{row['id']} | "+time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime(row["ts"]))
                    r=await client.post(cfg.discord,json={"content":content[:1900],"allowed_mentions":{"parse":[]}})
                    if r.status_code==429:
                        await asyncio.sleep(min(30,float(r.json().get("retry_after",5))))
                        continue
                    if r.status_code not in {200,204}: raise RuntimeError("Webhook rejected")
                    with db.tx() as c: db.put(c,"outbox:discord",row["id"])
                    db.health("discord","delivered","At least once delivery; event IDs identify retries",time.time())
            except asyncio.CancelledError: raise
            except Exception as e:
                db.health("discord","error",type(e).__name__+"; alert remains queued")
                await asyncio.sleep(15)
            await asyncio.sleep(1)
