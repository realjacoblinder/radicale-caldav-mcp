from datetime import UTC, date, datetime, timedelta

import pytest
from icalendar import Event

from radicale_mcp.client import calendar_info, checked_url, resource_id, resource_url
from radicale_mcp.config import Config
from radicale_mcp.errors import CalendarError
from radicale_mcp.events import expand, normalize, parse_calendar
from radicale_mcp.timeutils import (
    bounded_range,
    event_times,
    free_intervals,
    localize,
    merge_intervals,
    timestamp,
    working_windows,
    zone,
)

TZ = zone("America/New_York")


def ics(*events):
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        + "\n".join("BEGIN:VEVENT\n" + e + "\nEND:VEVENT" for e in events)
        + "\nEND:VCALENDAR\n"
    )


def test_config(config):
    assert config.timeout == 30 and config.verify_ssl
    assert config.password not in repr(config)
    assert (
        Config.from_env(
            {
                "CALDAV_URL": "http://localhost:5232",
                "CALDAV_USERNAME": "alice",
                "CALDAV_PASSWORD": "x",
            }
        ).timezone
        == "UTC"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("CALDAV_URL", "http://user:secret@host/"),
        ("CALDAV_URL", "ftp://host/"),
        ("CALDAV_URL", "http://host/?key=secret"),
        ("CALDAV_URL", "http://host:bad/"),
        ("CALDAV_PASSWORD", ""),
        ("CALDAV_USERNAME", "${MISSING}"),
        ("CALDAV_TIMEOUT", "nan"),
        ("CALDAV_TIMEOUT", "0"),
        ("CALDAV_VERIFY_SSL", "maybe"),
        ("CALDAV_DEFAULT_TIMEZONE", "Not/AZone"),
        ("CALDAV_MAX_EVENTS", "0"),
        ("LOG_LEVEL", "trace"),
    ],
)
def test_bad_config(key, value):
    env = {
        "CALDAV_URL": "http://localhost:5232",
        "CALDAV_USERNAME": "alice",
        "CALDAV_PASSWORD": "x",
    }
    env[key] = value
    with pytest.raises(CalendarError):
        Config.from_env(env)


def test_missing_config():
    with pytest.raises(CalendarError, match="CALDAV_URL"):
        Config.from_env({})


def test_calendar_identity_and_roundtrip():
    a = calendar_info("https://host/alice/a/", "Same")
    b = calendar_info("https://host/alice/b/", "Same")
    assert a.calendar_id != b.calendar_id
    assert a.calendar_id == calendar_info(a.url, "Renamed").calendar_id
    eid = resource_id(a.url + "some%20event.ics", a.url)
    assert resource_url(eid, a.url) == a.url + "some%20event.ics"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/r/",
        "http://host/r/",
        "https://x:y@host/r/",
        "https://host/r/../secret",
        "https://host/r/%252e%252e/secret",
        "https://host/other/",
        "https://host/r/event?next=x",
        "https://host/r/%5csecret",
    ],
)
def test_url_boundary(url):
    with pytest.raises(CalendarError):
        checked_url(url, "https://host/r/")


@pytest.mark.parametrize(
    "value",
    [
        "2026-03-08T02:30:00",
        "2026-11-01T01:30:00",
        "2026-02-30T12:00:00Z",
        "tomorrow",
        "2026-09-15",
        "20260915T140000",
        "",
    ],
)
def test_bad_timestamps(value):
    with pytest.raises(CalendarError):
        timestamp(value, TZ)


def test_timezone_and_dst():
    assert timestamp("2026-09-15T14:00", TZ).isoformat() == "2026-09-15T14:00:00-04:00"
    a = timestamp("2026-11-01T01:30:00-04:00", TZ)
    b = timestamp("2026-11-01T01:30:00-05:00", TZ)
    assert b.astimezone(UTC) - a.astimezone(UTC) == timedelta(hours=1)
    assert localize(datetime(2026, 11, 1, 1, 30), TZ, existing=True).fold == 0
    start, end = event_times("2026-03-08T01:30", None, 120, False, TZ)
    assert end.isoformat() == "2026-03-08T04:30:00-04:00"
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=2)


@pytest.mark.parametrize(
    "args",
    [
        ("2026-09-15T14:00Z", None, 0, False),
        ("2026-09-15T14:00Z", "2026-09-15T15:00Z", 30, False),
        ("2026-09-15T14:00Z", "2026-09-15T13:00Z", None, False),
        ("2026-09-15", None, 60, True),
        ("2026-09-15", "2026-09-15", None, True),
    ],
)
def test_bad_event_times(args):
    with pytest.raises(CalendarError):
        event_times(*args, TZ)


def test_all_day_and_serialization():
    a, b = event_times("2026-09-15", None, None, True, TZ)
    assert (a, b) == (date(2026, 9, 15), date(2026, 9, 16))
    event = Event()
    for k, v in {"UID": "a", "DTSTART": a, "SUMMARY": "A day", "LOCATION": "School"}.items():
        event.add(k, v)
    item = normalize(event, "c", "e", TZ).json()
    assert item["all_day"] and item["end"] == "2026-09-16"
    assert item["timezone"] is None and item["location"] == "School"


def test_floating_and_zero_duration():
    cal = parse_calendar(ics("UID:f\nDTSTART:20260915T140000\nSUMMARY:Floating"))
    item = normalize(cal.walk("VEVENT")[0], "c", "e", TZ)
    assert item.floating and item.start.endswith("-04:00")
    assert item.start == item.end


def test_recurrence_exceptions_and_dst():
    cal = parse_calendar(
        ics(
            "UID:r\nDTSTART;TZID=America/New_York:20260301T090000\nDTEND;TZID=America/New_York:20260301T100000\nRRULE:FREQ=WEEKLY;COUNT=4\nEXDATE;TZID=America/New_York:20260315T090000\nSUMMARY:Weekly",
            "UID:r\nRECURRENCE-ID;TZID=America/New_York:20260308T090000\nDTSTART;TZID=America/New_York:20260308T110000\nDTEND;TZID=America/New_York:20260308T120000\nSUMMARY:Moved",
        )
    )
    a, b = bounded_range("2026-03-01T00:00Z", "2026-03-29T00:00Z", TZ, 93)
    items = sorted(expand(cal, "c", "e", TZ, a, b, 100), key=lambda x: x.start)
    assert [e.start for e in items] == [
        "2026-03-01T09:00:00-05:00",
        "2026-03-08T11:00:00-04:00",
        "2026-03-22T09:00:00-04:00",
    ]
    assert items[1].occurrence_id == "2026-03-08T09:00:00-04:00"
    assert all(e.uid == "r" and e.recurrence["has_exceptions"] for e in items)


def test_recurring_all_day_rdate_and_boundaries():
    cal = parse_calendar(
        ics(
            "UID:a\nDTSTART;VALUE=DATE:20260101\nDTEND;VALUE=DATE:20260102\nRDATE;VALUE=DATE:20260915,20260916\nSUMMARY:Days"
        )
    )
    a, b = bounded_range("2026-09-15T00:00-04:00", "2026-09-16T00:00-04:00", TZ, 93)
    items = expand(cal, "c", "e", TZ, a, b, 100)
    assert len(items) == 1 and items[0].start == "2026-09-15" and items[0].all_day


def test_recurrence_limit_and_malformed():
    cal = parse_calendar(
        ics("UID:r\nDTSTART:20260915T090000Z\nRRULE:FREQ=DAILY;COUNT=10\nSUMMARY:Daily")
    )
    a, b = bounded_range("2026-09-15T00:00Z", "2026-09-30T00:00Z", TZ, 93)
    with pytest.raises(CalendarError, match="limit_exceeded"):
        expand(cal, "c", "e", TZ, a, b, 2)
    with pytest.raises(CalendarError, match="invalid_calendar_data"):
        parse_calendar("not an icalendar")


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-03-08T00:00Z", "2026-03-09T00:00Z"),
        ("2026-03-22T00:00Z", "2026-03-23T00:00Z"),
    ],
)
def test_nonexistent_count_occurrence_fails_even_in_later_window(start, end):
    cal = parse_calendar(
        ics(
            "UID:dst\nDTSTART;TZID=America/New_York:20260301T023000\n"
            "DTEND;TZID=America/New_York:20260301T033000\nRRULE:FREQ=WEEKLY;COUNT=3"
        )
    )
    a, b = bounded_range(start, end, TZ, 93)
    with pytest.raises(CalendarError, match="nonexistent DST"):
        expand(cal, "c", "e", TZ, a, b, 100)


def test_busy_and_available_math():
    a = datetime(2026, 9, 15, 9, tzinfo=UTC)
    h = timedelta(hours=1)
    busy = merge_intervals([(a + h, a + 2 * h), (a + 2 * h, a + 3 * h), (a + 1.5 * h, a + 2.5 * h)])
    assert busy == [(a + h, a + 3 * h)]
    assert free_intervals(busy, [(a, a + 5 * h)], 90) == [(a + 3 * h, a + 5 * h)]


def test_working_hours():
    a, b = bounded_range("2026-03-06T00:00Z", "2026-03-10T00:00Z", TZ, 93)
    windows = working_windows(a, b, TZ, "09:00", "17:00", [0, 1, 2, 3, 4])
    assert len(windows) == 2
    assert windows[0][0].hour == 14 and windows[1][0].hour == 13
    with pytest.raises(CalendarError):
        working_windows(a, b, TZ, "17:00", "09:00", None)
    with pytest.raises(CalendarError):
        bounded_range("2026-01-01T00:00Z", "2027-01-01T00:00Z", TZ, 93)
