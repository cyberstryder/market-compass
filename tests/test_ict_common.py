"""resolve_bar_key: alias, alias@iid, and dated-contract resolution."""
import pytest
from compass.store import Store
from compass import ict_common


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'resolve.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def put(c, s, key, n=10, start=1_700_000_000):
    s.put(c, key, [[start + i * 60, 100, 101, 99, 100.5, 10]
                   for i in range(n)])


def test_exact_alias_hit_wins(db):
    with db.tx() as c:
        put(c, db, 'bar_window:MNQ.c.0')
        put(c, db, 'bar_window:MNQZ25@999', start=1_800_000_000)
        assert ict_common.resolve_bar_key(db, c, 'MNQ.c.0') == 'MNQ.c.0'


def test_alias_at_iid_form(db):
    with db.tx() as c:
        put(c, db, 'bar_window:MNQ.c.0@555')
        assert ict_common.resolve_bar_key(db, c, 'MNQ.c.0') == 'MNQ.c.0@555'


def test_dated_contract_key_resolves_index_micro(db):
    # production form: bar_window:<RAW>@<iid>
    with db.tx() as c:
        put(c, db, 'bar_window:MNQZ25@987654')
        assert ict_common.resolve_bar_key(db, c, 'MNQ.c.0') == 'MNQZ25@987654'
        assert len(ict_common.ict_bars(db, c, 'MNQ.c.0')) == 10


def test_dated_contract_key_resolves_metal_micro(db):
    with db.tx() as c:
        put(c, db, 'bar_window:MGCQ26@123456')
        assert ict_common.resolve_bar_key(db, c, 'MGC.v.0') == 'MGCQ26@123456'


def test_newest_expiry_wins(db):
    with db.tx() as c:
        put(c, db, 'bar_window:MNQZ25@111', start=1_700_000_000)
        put(c, db, 'bar_window:MNQH26@222', start=1_800_000_000)
        assert ict_common.resolve_bar_key(db, c, 'MNQ.c.0') == 'MNQH26@222'


def test_si_does_not_match_sil(db):
    # SI.v.0 must not resolve to micro-silver's window (shared 'SI' prefix).
    with db.tx() as c:
        put(c, db, 'bar_window:SILV25@777')
        assert ict_common.resolve_bar_key(db, c, 'SI.v.0') is None
        put(c, db, 'bar_window:SIZ25@888')
        assert ict_common.resolve_bar_key(db, c, 'SI.v.0') == 'SIZ25@888'
        assert ict_common.resolve_bar_key(db, c, 'SIL.v.0') == 'SILV25@777'


def test_none_when_nothing_matches(db):
    with db.tx() as c:
        put(c, db, 'bar_window:SPY', n=5)
        assert ict_common.resolve_bar_key(db, c, 'MNQ.c.0') is None
        assert ict_common.ict_bars(db, c, 'MNQ.c.0') == []


def test_non_future_alias_unaffected(db):
    with db.tx() as c:
        put(c, db, 'bar_window:AAPL', n=5)
        assert ict_common.resolve_bar_key(db, c, 'AAPL') == 'AAPL'
        assert ict_common.resolve_bar_key(db, c, 'MSFT') is None
