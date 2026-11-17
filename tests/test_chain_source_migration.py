"""Massive->Alpaca migration (2026-11-14): OPTION_CHAIN_SOURCE flag.

Pins the migration lever Josh approved ("move to Alpaca this week, if
it works we can drop Massive"):

- chain_primary_source: the flag wins when that vendor is configured;
  otherwise whichever vendor is configured serves. Default 'massive'
  preserves the historical Massive-primary policy.
- Collectors.chains: with chain_source='alpaca', Alpaca serves first
  and the stored chain/exposure rows are source-tagged 'alpaca'; a
  failure falls back to Massive. With the default, Massive serves
  first exactly as before.
- chain_eod.fetch_chain follows the same policy (snapshot archives).

Nothing here flips a default: every test either uses the default and
asserts the historical behavior, or sets the flag explicitly.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.providers import Collectors, chain_primary_source
from compass import chain_eod
# Importing the app registers every table-defining module before
# Store.initialize() runs create_all (the lazy-import table lesson).
from compass.app import create_app  # noqa: F401

NOW = datetime(2026, 9, 14, 10, 15, tzinfo=NY).timestamp()
STOCKS = ('SPY', 'QQQ', 'IWM', 'AAA')


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'chain_source.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def contract(symbol):
    return {'symbol': 'O:' + symbol, 'underlying': symbol, 'strike': 100,
            'type': 'call', 'multiplier': 100, 'expiry': '2026-09-21',
            'oi': 100, 'gamma': .1, 'iv': .2,
            'quote': {'ts': NOW - 10, 'bid': 2.0, 'ask': 2.01,
                      'bid_size': 100, 'ask_size': 100}}


# --- the policy helper ---------------------------------------------------

def _cfg(**kw):
    base = dict(massive='k', alpaca_key='k', alpaca_secret='s',
                chain_source='massive')
    base.update(kw)
    return SimpleNamespace(**base)


def test_primary_defaults_to_massive():
    assert chain_primary_source(_cfg()) == 'massive'


def test_primary_flag_alpaca_wins_when_configured():
    assert chain_primary_source(_cfg(chain_source='alpaca')) == 'alpaca'


def test_primary_falls_back_to_whichever_vendor_is_configured():
    assert chain_primary_source(_cfg(chain_source='alpaca',
                                     alpaca_key='', alpaca_secret='')) == 'massive'
    assert chain_primary_source(_cfg(massive='')) == 'alpaca'


def test_primary_tolerates_missing_flag_attr():
    # Missing flag attr defaults to 'alpaca' since 2026-11-16
    # (Massive cancelled; a dropped env var must never resurrect
    # Massive-primary).
    cfg = SimpleNamespace(massive='k', alpaca_key='k', alpaca_secret='s')
    assert chain_primary_source(cfg) == 'alpaca'


# --- Collectors.chains ----------------------------------------------------

def _make_collector(db, monkeypatch, calls, fail=(), **cfgkw):
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW)
    monkeypatch.setattr('compass.providers.focus_symbols',
                        lambda *a, **k: [])
    monkeypatch.setattr('compass.swing_ideas.active_underlyings',
                        lambda c: [])
    cfg = Config(local=True, stocks=STOCKS, massive='fixture',
                 alpaca_key='k', alpaca_secret='s', **cfgkw)
    collector = Collectors(db, cfg)

    async def massive(symbol):
        calls.append(('massive', symbol))
        if 'massive' in fail:
            raise RuntimeError('massive down')
        return [contract(symbol)], True

    async def alpaca(symbol):
        calls.append(('alpaca', symbol))
        if 'alpaca' in fail:
            raise RuntimeError('alpaca down')
        return [contract(symbol)], True

    collector.massive_chain = massive
    collector.alpaca_chain = alpaca
    return collector


def _stored_source(db, symbol):
    with db.tx() as c:
        return db.get(c, 'chain:' + symbol, {}).get('source')


def test_chains_default_serves_alpaca_first(db, monkeypatch):
    # Default flipped 2026-11-16 with the Massive cancellation:
    # Config's chain_source default is now 'alpaca'.
    calls = []
    collector = _make_collector(db, monkeypatch, calls)
    asyncio.run(collector.chains('indices'))
    assert calls and all(vendor == 'alpaca' for vendor, _ in calls)
    assert _stored_source(db, calls[0][1]) == 'alpaca'


def test_chains_flag_alpaca_serves_alpaca_first(db, monkeypatch):
    calls = []
    collector = _make_collector(db, monkeypatch, calls,
                                chain_source='alpaca')
    asyncio.run(collector.chains('indices'))
    assert calls and all(vendor == 'alpaca' for vendor, _ in calls)
    assert _stored_source(db, calls[0][1]) == 'alpaca'


def test_chains_flag_alpaca_falls_back_to_massive(db, monkeypatch):
    calls = []
    collector = _make_collector(db, monkeypatch, calls, fail=('alpaca',),
                                chain_source='alpaca')
    asyncio.run(collector.chains('indices'))
    assert calls[0][0] == 'alpaca' and ('massive', calls[0][1]) in calls
    assert _stored_source(db, calls[0][1]) == 'massive'


# --- chain_eod.fetch_chain -------------------------------------------------

class _FakeCollectors:
    def __init__(self, cfg, fail=()):
        self.cfg = cfg
        self.fail = fail
        self.calls = []

    async def massive_chain(self, symbol, **kw):
        self.calls.append('massive')
        if 'massive' in self.fail:
            raise RuntimeError('massive down')
        return ([contract(symbol)], True)

    async def alpaca_chain(self, symbol, **kw):
        self.calls.append('alpaca')
        if 'alpaca' in self.fail:
            raise RuntimeError('alpaca down')
        return ([contract(symbol)], True)


def test_fetch_chain_default_massive_primary():
    coll = _FakeCollectors(_cfg())
    _, _, source = asyncio.run(chain_eod.fetch_chain(
        coll, 'SPY', '2026-09-14', '2026-12-25'))
    assert source == 'massive' and coll.calls == ['massive']


def test_fetch_chain_flag_alpaca_primary():
    coll = _FakeCollectors(_cfg(chain_source='alpaca'))
    _, _, source = asyncio.run(chain_eod.fetch_chain(
        coll, 'SPY', '2026-09-14', '2026-12-25'))
    assert source == 'alpaca' and coll.calls == ['alpaca']


def test_fetch_chain_flag_alpaca_falls_back_to_massive():
    coll = _FakeCollectors(_cfg(chain_source='alpaca'), fail=('alpaca',))
    _, _, source = asyncio.run(chain_eod.fetch_chain(
        coll, 'SPY', '2026-09-14', '2026-12-25'))
    assert source == 'massive' and coll.calls == ['alpaca', 'massive']
