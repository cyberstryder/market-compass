"""Connection-scoped subscription evidence; acknowledgements are not fresh quotes."""
from collections import deque
import logging
import re
import uuid
from .diagnostics import redacted_detail


class SubscriptionTrace:
    def __init__(self, now, secret='', capacity=4096):
        self.connection_id=uuid.uuid4().hex
        self.secret=secret
        self.capacity=capacity
        self.queue=deque()
        self.channels={}
        self.sequence=0
        self.dropped=0
        self.persistence_error=None
        self.emit('connected','*',now)

    def emit(self,event,symbol,now,**details):
        self.sequence+=1
        value=dict(connection_id=self.connection_id,sequence=self.sequence,event=event,
                   symbol=symbol,at=now,**details)
        if len(self.queue)>=self.capacity:
            self.dropped+=1
            if self.dropped==1:
                logging.getLogger('uvicorn.error').warning('Option subscription trace queue full; evidence will be incomplete')
            return
        self.queue.append(value)

    def request(self,action,symbols,now):
        for symbol in sorted(symbols):
            for kind in ('Q','T'):
                channel=kind+'.'+symbol
                self.channels[channel]=dict(action=action,requested_at=now,sent_at=None,
                    acknowledged_at=None,first_event_at=None,first_source_ts=None)
                self.emit('requested',symbol,now,channel=channel,action=action)

    def sent(self,action,symbols,now):
        for symbol in sorted(symbols):
            for kind in ('Q','T'):
                channel=kind+'.'+symbol
                self.channels[channel]['sent_at']=now
                self.emit('sent',symbol,now,channel=channel,action=action)

    def status(self,item,now):
        message=redacted_detail(item.get('message',''),(self.secret,),1000)
        status=redacted_detail(item.get('status',''),(self.secret,),80)
        self.emit('provider_status','*',now,status=status,message=message)
        # Generic success/authentication is not a per-contract acknowledgement.
        match=re.fullmatch(r'(subscribed to|unsubscribed from|unsubscribed to):?\s+(.+)',
                           str(item.get('message','')),re.IGNORECASE)
        if status!='success' or not match:return
        action='subscribe' if match[1].lower()=='subscribed to' else 'unsubscribe'
        for channel in dict.fromkeys(re.findall(r'\b[QT]\.O:[A-Z0-9.]+',match[2])):
            saved=self.channels.get(channel)
            matched=bool(saved and saved['action']==action)
            if matched:saved['acknowledged_at']=now
            self.emit('acknowledged',channel[2:],now,channel=channel,action=action,
                      matches_current_request=matched)

    def data(self,kind,symbol,now,source_ts):
        saved=self.channels.get(kind+'.'+symbol)
        if saved and saved['action']=='subscribe' and saved['first_event_at'] is None:
            saved.update(first_event_at=now,first_source_ts=source_ts)
            self.emit('first_event',symbol,now,channel=kind+'.'+symbol,source_ts=source_ts)

    def snapshot(self,symbol):
        return {kind:dict(self.channels.get(kind+'.'+symbol,{})) for kind in ('Q','T')}

    def prune(self,wanted):
        # Historical requests/acks remain in the event journal. Inactive state
        # need not grow for the lifetime of a busy socket.
        for channel in list(self.channels):
            if channel[2:] not in wanted:del self.channels[channel]
