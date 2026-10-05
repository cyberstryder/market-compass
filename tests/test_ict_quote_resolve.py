"""resolve_quote_key: alias, alias@iid, and dated-contract quote resolution.

Regression: ICT futures paper positions are stored under the alias form
(MCL.v.0) while Databento collectors write quotes under dated-contract keys
(quote:MCLZ25@<iid>). The engine exit monitor must resolve the alias or every
open futures position reports DATA BLOCKED ("No fresh exit quote").
"""
import pytest
from compass.store import Store
from compass import ict_common


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'resolve_quote.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def quote(ts, bid=100.0, ask=100.5):
    return {'ts': ts, 'bid': bid, 'ask': ask}


def test_exact_alias_quote_hit_wins(db):
    with db.tx() as c:
        db.put(c, 'quote:MNQ.c.0', quote(1_700_000_000))
        db.put(c, 'quote:MNQZ25@999', quote(1_800_000_000))
        assert ict_common.resolve_quote_key(db, c, 'MNQ.c.0') == 'MNQ.c.0'


def test_alias_at_iid_quote_form(db):
    with db.tx() as c:
        db.put(c, 'quote:MNQ.c.0@555', quote(1_700_000_000))
        assert ict_common.resolve_quote_key(db, c, 'MNQ.c.0') == 'MNQ.c.0@555'


def test_dated_contract_quote_resolves_newest(db):
    # production form: quote:<RAW>@<iid>
    with db.tx() as c:
        db.put(c, 'quote:MCLZ25@111', quote(1_700_000_000))
        db.put(c, 'quote:MCLZ25@222', quote(1_800_000_000))
        assert ict_common.resolve_quote_key(db, c, 'MCL.v.0') == 'MCLZ25@222'


def test_dated_quote_ignores_other_roots(db):
    with db.tx() as c:
        db.put(c, 'quote:MGCZ25@333', quote(1_800_000_000))
        assert ict_common.resolve_quote_key(db, c, 'MCL.v.0') is None


def test_no_quote_returns_none(db):
    with db.tx() as c:
        assert ict_common.resolve_quote_key(db, c, 'MCL.v.0') is None
