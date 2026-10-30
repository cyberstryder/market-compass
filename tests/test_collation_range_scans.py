"""Collation-safe range scans outside store.py (2026-10-30).

Bundle 1 (dd22055) fixed Store.prefix()/recent_trades(): under a
glibc/ICU database collation '\\uffff' sorts BELOW ordinary letters, so
range bounds in database order silently return nothing. The same
idiom survived in two raw-SQL scans, both reproduced empty on a
glibc-collated (en_US.utf8) PG 18 cluster before this fix:

- survivors.seed_history's _SEED_SQL (the live survivors' 26-day ATR
  seed): symbol < root+'\\uffff' excluded every dated-contract key, so
  the seed installed 0 sessions;
- /api/bars' events fallback: symbol < alias+'@\\uffff' found no
  "<alias>@<iid>" keys.

The /api/bars fallback now compares with COLLATE "C" on Postgres
(Store._key()'s treatment); SQLite's BINARY collation already is
codepoint order.

The survivors seed took a second fix the same day: the COLLATE "C"
range returned the right rows on a small cluster but the
glibc-collated (kind, symbol, ts) index cannot serve a codepoint
range, so in production the query seq-scanned the ~45M-row events
table and died on statement timeout. The seed now discovers dated
symbols with one DISTINCT lookup ranged in database collation (the
index's own collation) between the root and its last-letter
successor, then reads bars per symbol with (kind, symbol) EQUALITY
plus a ts range — every step index-served, no COLLATE anywhere.
"""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text

from compass import survivors
from compass.app import _bars_fallback_stmt
from compass.store import Store

CT = ZoneInfo("America/Chicago")
BAR = {'o': 100.0, 'h': 101.0, 'l': 99.0, 'c': 100.5, 'v': 10}


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'coll.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def test_seed_bar_reads_are_equality_only():
    """Construction pin for the second seed fix: bar reads must be
    servable from the (kind, symbol, ts) index — equality on symbol,
    range on ts only. No COLLATE, no LIKE, no symbol range, no
    '\\uffff' idiom anywhere in the seed's statements."""
    bars_sql = survivors._SEED_BARS_SQL
    assert 'symbol = :symbol' in bars_sql
    assert 'ts >= :tlo' in bars_sql
    for stmt in (survivors._SEED_BARS_SQL, survivors._SEED_DISCOVER_SQL):
        assert 'COLLATE' not in stmt
        assert 'LIKE' not in stmt
        assert '\uffff' not in stmt
    assert 'DISTINCT symbol' in survivors._SEED_DISCOVER_SQL


def test_root_successor_bounds_the_subtree():
    assert survivors._root_successor('MNQ') == 'MNR'
    assert survivors._root_successor('MCL') == 'MCM'
    assert survivors._root_successor('MGC') == 'MGD'


def _append_seed_bars(db):
    """Sixteen complete CT sessions of dated-contract bars for root
    MNQ (00:00-13:29 CT, safely inside one trade date each; >= 15
    sessions so the ATR warm-up engages), plus rows the seed must
    ignore: the continuous alias and another root."""
    now = time.time()
    today = datetime.fromtimestamp(now, CT).date()
    days = [today - timedelta(days=back) for back in range(18, 2, -1)]
    with db.tx() as c:
        for d in days:
            base = datetime(d.year, d.month, d.day, 0, 0,
                            tzinfo=CT).timestamp()
            db.append_bars(c, 'bar', 'databento',
                           [('MNQU6@4242', base + 60 * i, dict(BAR))
                            for i in range(810)])
        base = datetime(days[-1].year, days[-1].month, days[-1].day,
                        0, 0, tzinfo=CT).timestamp()
        db.append_bars(c, 'bar', 'databento',
                       [('MNQ.c.0', base + 60 * i, dict(BAR))
                        for i in range(810)])
        db.append_bars(c, 'bar', 'databento',
                       [('MGCZ6@9', base + 60 * i, dict(BAR))
                        for i in range(810)])
    return now, [d.isoformat() for d in days]


def _assert_seed(db):
    now, days = _append_seed_bars(db)
    feed = survivors.SurvivorFeed('MNQ', 0.25)
    with db.tx() as c:
        n = survivors.seed_history(db, c, feed, 'MNQ', now)
    assert n == 16
    assert [s['sdate'] for s in feed.history] == days
    # Flat bars: every true range is h-l = 2.0, so the warmed ATR is 2.
    assert feed.atr == pytest.approx(2.0)


def test_seed_history_sqlite(db):
    _assert_seed(db)


from tests.test_smoothers_postgres_handoff import pg


def test_seed_history_on_postgres(pg):
    """Regression pin: on the glibc-collated CI cluster this returned
    0 sessions before the COLLATE "C" fix. Skipped where the pg
    fixture's isolated service does not exist."""
    _assert_seed(pg)


def test_dated_symbol_discovery_filters(db):
    """Discovery returns exactly the root's dated-contract symbols:
    bare dated codes and @iid forms are kept; the bare root, dotted
    alias keys, and non-contract suffixes (W is not a month code) are
    excluded."""
    now = time.time()
    with db.tx() as c:
        for sym in ('MNQZ6@7', 'MNQH7', 'MNQ.c.0', 'MNQ', 'MNQW6@1'):
            db.append_bars(c, 'bar', 'databento',
                           [(sym, now - 60 * i, dict(BAR))
                            for i in range(3)])
    with db.tx() as c:
        found = survivors._dated_symbols(c, 'MNQ', now - 3600)
    assert found == ['MNQH7', 'MNQZ6@7']


def test_bars_fallback_stmt_collates_only_on_postgres():
    assert 'COLLATE "C"' in _bars_fallback_stmt('postgresql')
    assert 'COLLATE' not in _bars_fallback_stmt('sqlite')


def test_bars_fallback_on_postgres(pg):
    """The exact statement /api/bars' fallback executes must find
    "MES.c.0@222" under the glibc collation; the pre-fix statement
    (plain database-order bounds) found nothing and the route
    returned []."""
    base = time.time() - 3600
    with pg.tx() as c:
        pg.append_bars(c, 'bar', 'databento',
                       [('MES.c.0@222', base + 60 * i, dict(BAR))
                        for i in range(3)])
    params = {"lo": "MES.c.0@", "hi": "MES.c.0@\uffff",
              "since": time.time() - 14 * 86400}
    with pg.tx() as c:
        found = c.execute(sql_text(
            _bars_fallback_stmt('postgresql')), params).scalar()
    assert found == 'MES.c.0@222'
