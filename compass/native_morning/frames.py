"""Bounded Pine v1.3 frames. Expand compact arrays into the existing strict models.

All effects commit atomically. A replay can recover a lost entry/measurement,
but later stock frames cannot fabricate an initial delivery or a Discord entry.
"""
from contextlib import nullcontext
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, select

from .candidate_models import ResearchBatch, ResearchSession
from .candidate_store import accept_research
from .discord import signal_message
from .models import Event, Signal, StrictModel
from .store import (PayloadConflict, accept_event, canonical, digest, insert_many_once,
                    insert_once, metadata, signals, stock_bars)

FEATURES = ('rvol', 'volume', 'vwap', 'ema_fast', 'ema_slow', 'orb_high', 'orb_low', 'bar_count')
FILTERS = ('trade_window', 'above_orb', 'above_vwap', 'ema_trend', 'volume_ok', 'green',
           'vwap_cross', 'ema_cross', 'ema_rising', 'fast_window', 'already_triggered')
BAR = ('open_at_ms', 'open', 'high', 'low', 'close', 'volume')
RECORD = ('kind', 'at_ms', 'price', 'previous_close', 'features', 'filters',
          'anchor_at_ms', 'anchor_price', 'source_signal_id', 'baseline_setup')
OBSERVATION = ('horizon_min', 'observed_at_ms', 'price', 'window_high', 'window_low',
               'peak_at_ms', 'dip_at_ms', 'hit_1_ms', 'hit_2_ms', 'hit_3_ms', 'bars_observed', 'continuous')
SOURCE = ('signal_id', 'setup', 'signal_at_ms', 'bar_open_ms', 'price', 'features')
FRAME_LIMIT = 3800

frames = Table('compass_native_morning_frames_v4', metadata,
    Column('event_id', String(550), primary_key=True),
    Column('session_id', String(500), nullable=False, index=True),
    Column('observed_at_ms', BigInteger, nullable=False),
    Column('received_at_ms', BigInteger, nullable=False),
    Column('payload_hash', String(64), nullable=False),
    Column('frame_json', Text, nullable=False),
    Column('body_bytes', Integer, nullable=False))


def expand(values, keys):
    if not isinstance(values, list) or len(values) != len(keys):
        raise ValueError('Compact array has incorrect length')
    return dict(zip(keys, values))


class Frame(StrictModel):
    schema_version: Literal[4]
    event_type: Literal['frame']
    observed_at_ms: int = Field(gt=0)
    part: int = Field(ge=1, le=2)
    session: ResearchSession
    research_enabled: bool
    until_ms: int = Field(gt=0)
    bars: list[list] = Field(min_length=1, max_length=2)
    records: list[list] = Field(max_length=6)
    source: list | None = None
    entry: bool
    checkpoints: list[list] = Field(max_length=4)

    @property
    def event_id(self):
        return f'{self.session.session_id}-frame-{self.observed_at_ms}-{self.part}'

    def research(self):
        records = []
        for compact in self.records:
            r = expand(compact, RECORD)
            r['features'] = expand(r['features'], FEATURES)
            r['filters'] = expand(r['filters'], FILTERS)
            r['record_id'] = f"{self.session.session_id}-{r['kind']}-{r['at_ms']}"
            records.append(r)
        return ResearchBatch.model_validate({
            'schema_version': 3, 'event_type': 'research',
            'event_id': f'{self.session.session_id}-research-{self.observed_at_ms}-{self.part}',
            'observed_at_ms': self.observed_at_ms, 'part': self.part,
            'session': self.session.model_dump(),
            'bars': [expand(b, BAR) for b in self.bars], 'records': records})

    def signal(self):
        if self.source is None:
            return None
        s = expand(self.source, SOURCE)
        s['features'] = expand(s['features'], FEATURES)
        s.update({k: getattr(self.session, k) for k in
                  ('ticker', 'stream_id', 'script_version', 'logic_mode', 'timeframe_min', 'is_test')})
        s['settings'] = self.session.settings.model_dump()
        return Signal.model_validate(s)

    def events(self):
        s = self.signal()
        if s is None:
            return []
        events = []
        if self.entry:
            events.append(Event.model_validate({'schema_version': 1, 'event_type': 'signal',
                'event_id': s.signal_id+'-signal', 'signal': s.model_dump()}))
        for values in self.checkpoints:
            o = expand(values, OBSERVATION)
            events.append(Event.model_validate({'schema_version': 1, 'event_type': 'checkpoint',
                'event_id': s.signal_id+'-'+str(o['horizon_min']), 'signal': s.model_dump(), 'observation': o}))
        return events

    @model_validator(mode='after')
    def validate_contents(self):
        if self.session.script_version != '1.3.0':
            raise ValueError('Frames require Pine v1.3.0')
        batch, signal = self.research(), self.signal()
        end = self.observed_at_ms
        if not self.session.activated_at_ms < self.until_ms <= self.session.session_open_ms+360*60000:
            raise ValueError('Invalid collection endpoint')
        if self.until_ms % 60000 or end > self.until_ms+60000:
            raise ValueError('Frame exceeds final recovery minute')
        if any(b.open_at_ms < end-2*60000 for b in batch.bars):
            raise ValueError('Frame exceeds rolling two-minute window')
        if self.records and not self.research_enabled:
            raise ValueError('Research records supplied when research is disabled')
        if signal is None and (self.entry or self.checkpoints):
            raise ValueError('Entry/checkpoints require a signal snapshot')
        if signal:
            if signal.signal_at_ms - signal.bar_open_ms != 60000 or signal.signal_at_ms % 60000:
                raise ValueError('Frames require a complete minute entry candle')
            entry_bar = next((b for b in batch.bars if b.open_at_ms == signal.bar_open_ms), None)
            if entry_bar and abs(entry_bar.close-signal.price) > max(1e-8, signal.price*1e-9):
                raise ValueError('Entry price disagrees with confirmed candle')
            for record in batch.records:
                if record.kind == 'BASELINE' and (record.source_signal_id != signal.signal_id or record.at_ms != signal.signal_at_ms or record.features != signal.features or record.baseline_setup != signal.setup):
                    raise ValueError('Baseline record and signal snapshot disagree')
            if not self.session.activated_at_ms < signal.signal_at_ms <= end:
                raise ValueError('Signal outside this observed session')
            if self.entry and (self.part != 1 or signal.signal_at_ms < end-60000):
                raise ValueError('Only the entry minute and next frame may announce an entry')
            if any(expand(o, OBSERVATION)['observed_at_ms'] not in (end, end-60000) for o in self.checkpoints):
                raise ValueError('Checkpoint outside its transmission/recovery minute')
        self.events()
        return self


class TransactionEngine:
    """Let existing ingest functions share this frame's one transaction."""
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        return nullcontext(self.conn)


def accept_frame(engine, payload, received, body_bytes, discord_enabled=False, option_policy=None):
    data, source = payload.model_dump(), payload.signal()
    with engine.begin() as conn:
        fresh = insert_once(conn, frames, {'event_id': payload.event_id,
            'session_id': payload.session.session_id, 'observed_at_ms': payload.observed_at_ms,
            'received_at_ms': received, 'payload_hash': digest(data),
            'frame_json': canonical(data), 'body_bytes': body_bytes})
        saved = conn.execute(select(frames.c.payload_hash).where(frames.c.event_id == payload.event_id)).scalar_one()
        if saved != digest(data):
            raise PayloadConflict('Frame identity already exists with different contents')
        if not fresh:
            return {'status': 'duplicate', 'event_id': payload.event_id}
        tx = TransactionEngine(conn)
        accept_research(tx, payload.research(), received)
        if source:
            signal, sid = source.model_dump(), source.signal_id
            insert_once(conn, signals, {'signal_id': sid, 'ticker': source.ticker,
                'signal_at_ms': source.signal_at_ms, 'received_at_ms': received,
                'signal_json': canonical(signal), 'signal_hash': digest(signal),
                'initial_received_at_ms': None, 'is_test': source.is_test})
            if conn.execute(select(signals.c.signal_hash).where(signals.c.signal_id == sid)).scalar_one() != digest(signal):
                raise PayloadConflict('Signal identity already exists with different contents')
            bars = [b.model_dump() for b in payload.research().bars
                    if source.signal_at_ms <= b.open_at_ms < source.signal_at_ms+180*60000]
            insert_many_once(conn, stock_bars, [{'signal_id': sid, 'open_at_ms': b['open_at_ms'],
                'bar_json': canonical(b), 'bar_hash': digest(b), 'received_at_ms': received} for b in bars])
            saved = dict(conn.execute(select(stock_bars.c.open_at_ms, stock_bars.c.bar_hash)
                .where(stock_bars.c.signal_id == sid, stock_bars.c.open_at_ms.in_([b['open_at_ms'] for b in bars]))).all())
            if any(saved[b['open_at_ms']] != digest(b) for b in bars):
                raise PayloadConflict('Previously recorded stock candle has different contents')
        for event in payload.events():
            message = signal_message(event.signal, received, option_policy) if event.event_type == 'signal' else None
            accept_event(tx, event, message, discord_enabled, received)
    return {'status': 'accepted', 'event_id': payload.event_id}

