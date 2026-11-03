"""News-pause parser repair (2026-11-02).

The vendor stores economic-event times as 12-hour AM/PM strings;
the pre-repair parser accepted only bare 24-hour "HH:MM" and
skipped every AM/PM row, so should_pause_for_news could never
fire (2026-11-02 trading-info audit, headline finding #1).

Fixtures are this week's REAL stored intel:economic rows, captured
verbatim from production /api/tradermatrix-intel on 2026-11-02
(fetched_at 2026-11-02T20:15:11-06:00). The stored list is the
keyword/impact-filtered high_impact array: the FOMC speaker rows
ride in on the "FOMC" title keyword with impact="low" and must
never open a pause window — the stored impact field governs.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from compass import entry_evidence
from compass import tradermatrix_intel as ti

CT = ZoneInfo("America/Chicago")

_FRI_HIGHS = ('Average Hourly Earnings m/m',
              'Non-Farm Employment Change', 'Unemployment Rate')

WEEK = [
    {'title': 'Congressional Elections', 'date': '2026-11-03',
     'time': '10:16 AM', 'impact': 'high', 'description': '',
     'country': 'US'},
    {'title': 'FOMC Member Hammack Speaks', 'date': '2026-11-05',
     'time': '8:50 AM', 'impact': 'low', 'description': '',
     'country': 'US'},
    {'title': 'FOMC Member Barkin Speaks', 'date': '2026-11-05',
     'time': '9:10 AM', 'impact': 'low', 'description': '',
     'country': 'US'},
    {'title': 'FOMC Member Williams Speaks', 'date': '2026-11-06',
     'time': '4:30 AM', 'impact': 'low', 'description': '',
     'country': 'US'},
    {'title': 'Average Hourly Earnings m/m', 'date': '2026-11-06',
     'time': '8:30 AM', 'impact': 'high', 'description': '',
     'country': 'US'},
    {'title': 'Non-Farm Employment Change', 'date': '2026-11-06',
     'time': '8:30 AM', 'impact': 'high', 'description': '',
     'country': 'US'},
    {'title': 'Unemployment Rate', 'date': '2026-11-06',
     'time': '8:30 AM', 'impact': 'high', 'description': '',
     'country': 'US'},
    {'title': 'FOMC Member Musalem Speaks', 'date': '2026-11-06',
     'time': '9:10 AM', 'impact': 'low', 'description': '',
     'country': 'US'},
    {'title': 'FOMC Member Bowman Speaks', 'date': '2026-11-06',
     'time': '12:15 PM', 'impact': 'low', 'description': '',
     'country': 'US'},
]


def _ct(day, hour, minute):
    return datetime(2026, 11, day, hour, minute, tzinfo=CT)


class _StubDB:
    """Minimal db stand-in: should_pause_for_news / entry_evidence
    only call db.get(c, key, default)."""

    def __init__(self, rows=None):
        self.rows = rows or {}

    def get(self, c, key, default=None):
        return self.rows.get(key, default)


def _stored(day_key, events=WEEK):
    return _StubDB({'intel:economic:' + day_key:
                    {'date': day_key, 'high_impact': events,
                     'total_events': 32, 'fetched_at': 'x'}})


# ---------------------------------------------------------------------
# parse_event_time
# ---------------------------------------------------------------------
def test_parse_ampm_strings():
    assert ti.parse_event_time('8:30 AM') == (8, 30)
    assert ti.parse_event_time('10:16 AM') == (10, 16)
    assert ti.parse_event_time('4:30 AM') == (4, 30)
    assert ti.parse_event_time('9:10 AM') == (9, 10)
    assert ti.parse_event_time('12:15 PM') == (12, 15)
    assert ti.parse_event_time('1:05 PM') == (13, 5)
    assert ti.parse_event_time('11:59 PM') == (23, 59)


def test_parse_twelve_hour_edges():
    assert ti.parse_event_time('12:00 AM') == (0, 0)
    assert ti.parse_event_time('12:00 PM') == (12, 0)
    assert ti.parse_event_time('12:30 AM') == (0, 30)
    assert ti.parse_event_time('12:30 PM') == (12, 30)


def test_parse_bare_24h_still_works():
    assert ti.parse_event_time('08:30') == (8, 30)
    assert ti.parse_event_time('8:30') == (8, 30)
    assert ti.parse_event_time('13:45') == (13, 45)
    assert ti.parse_event_time('00:00') == (0, 0)


def test_parse_rejects_non_clock_values():
    for bad in (None, '', 'All Day', 'BMO', 'AMC', 'Tentative',
                '2026-11-06T08:30:00', '25:00', '8:75 AM',
                '13:30 PM', '0:30 AM', 830):
        assert ti.parse_event_time(bad) is None, bad


def test_event_opens_pause_impact_governs():
    assert ti.event_opens_pause({'impact': 'high'}) is True
    assert ti.event_opens_pause({'impact': 'HIGH'}) is True
    assert ti.event_opens_pause({'impact': 'low'}) is False
    assert ti.event_opens_pause({'impact': 'medium'}) is False
    # Keyword-stored rows with no rating keep the pre-existing
    # semantics (the filter already vetted them).
    assert ti.event_opens_pause({}) is True
    assert ti.event_opens_pause({'impact': ''}) is True
    assert ti.event_opens_pause({'title': 'FOMC Rate Decision'}) is True


# ---------------------------------------------------------------------
# is_news_window_now over the raw vendor-shaped list
# ---------------------------------------------------------------------
def test_window_friday_nfp_stack():
    # Window is event time +/- 15 min: 08:15-08:45 CT inclusive.
    for hour, minute, want in ((8, 14, False), (8, 15, True),
                               (8, 30, True), (8, 45, True),
                               (8, 46, False)):
        pause, ev = ti.is_news_window_now(WEEK, now=_ct(6, hour, minute))
        assert pause is want, (hour, minute)
        if want:
            assert ev['title'] in _FRI_HIGHS


def test_window_tuesday_congressional_elections():
    for hour, minute, want in ((10, 0, False), (10, 1, True),
                               (10, 16, True), (10, 31, True),
                               (10, 32, False)):
        pause, ev = ti.is_news_window_now(WEEK, now=_ct(3, hour, minute))
        assert pause is want, (hour, minute)
        if want:
            assert ev['title'] == 'Congressional Elections'


def test_window_low_impact_speakers_never_pause():
    # Thu has ONLY low-impact FOMC speakers in the stored list.
    for hour, minute in ((8, 50), (9, 10)):
        pause, _ = ti.is_news_window_now(WEEK, now=_ct(5, hour, minute))
        assert pause is False
    # Friday's low rows sit minutes from the high stack; at their
    # own times (outside the stack's window) nothing pauses.
    for hour, minute in ((4, 30), (9, 10), (12, 15)):
        pause, _ = ti.is_news_window_now(WEEK, now=_ct(6, hour, minute))
        assert pause is False, (hour, minute)


def test_window_quiet_day():
    pause, ev = ti.is_news_window_now(WEEK, now=_ct(2, 8, 30))
    assert pause is False and ev is None
    pause, ev = ti.is_news_window_now(WEEK, now=_ct(4, 8, 30))
    assert pause is False and ev is None


# ---------------------------------------------------------------------
# should_pause_for_news over the stored row (the production gate)
# ---------------------------------------------------------------------
def test_should_pause_friday_window_edges():
    db = _stored('2026-11-06')
    for hour, minute, want in ((8, 14, False), (8, 15, True),
                               (8, 30, True), (8, 45, True),
                               (8, 46, False)):
        pause, ev = ti.should_pause_for_news(
            db, None, now=_ct(6, hour, minute))
        assert pause is want, (hour, minute)
        if want:
            assert ev['title'] in _FRI_HIGHS


def test_should_pause_tuesday_event():
    db = _stored('2026-11-03')
    pause, ev = ti.should_pause_for_news(db, None, now=_ct(3, 10, 16))
    assert pause is True
    assert ev['title'] == 'Congressional Elections'


def test_should_pause_low_only_day_never_pauses():
    db = _stored('2026-11-05')
    for hour, minute in ((8, 50), (9, 10)):
        pause, _ = ti.should_pause_for_news(
            db, None, now=_ct(5, hour, minute))
        assert pause is False


def test_should_pause_no_stored_row():
    pause, ev = ti.should_pause_for_news(_StubDB(), None,
                                         now=_ct(6, 8, 30))
    assert pause is False and ev is None


def test_should_pause_bare_hhmm_regression():
    rows = [{'title': 'CPI m/m', 'date': '2026-11-06', 'time': '08:30',
             'impact': 'high', 'description': '', 'country': 'US'},
            {'title': 'Core CPI m/m', 'date': '2026-11-06',
             'time': '8:30', 'impact': 'high', 'description': '',
             'country': 'US'}]
    db = _stored('2026-11-06', events=rows)
    pause, _ = ti.should_pause_for_news(db, None, now=_ct(6, 8, 30))
    assert pause is True


def test_should_pause_keyword_row_without_impact_rating():
    rows = [{'title': 'CPI m/m', 'date': '2026-11-06',
             'time': '8:30 AM', 'description': '', 'country': 'US'}]
    db = _stored('2026-11-06', events=rows)
    pause, ev = ti.should_pause_for_news(db, None, now=_ct(6, 8, 30))
    assert pause is True and ev['title'] == 'CPI m/m'


# ---------------------------------------------------------------------
# entry_evidence mirror agrees with the gate
# ---------------------------------------------------------------------
def test_evidence_stamps_agree_friday():
    db = _stored('2026-11-06')
    at = _ct(6, 8, 30).timestamp()
    out = entry_evidence._calendar(db, None, 'SPY', at)
    assert out['high_impact_news_today'] is True
    assert out['news_window_now'] is True
    # 09:10 CT: inside only the low Musalem window — no stamp.
    out = entry_evidence._calendar(db, None, 'SPY',
                                   _ct(6, 9, 10).timestamp())
    assert out['high_impact_news_today'] is True
    assert out['news_window_now'] is False


def test_evidence_low_only_day_stamps_nothing():
    db = _stored('2026-11-05')
    out = entry_evidence._calendar(db, None, 'SPY',
                                   _ct(5, 8, 50).timestamp())
    assert out['high_impact_news_today'] is False
    assert out['news_window_now'] is False
