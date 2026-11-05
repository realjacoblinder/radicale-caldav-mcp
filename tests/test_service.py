import base64
import logging
from dataclasses import replace

import pytest
from mcp import Client

from radicale_mcp.client import GuardedSession, safe_errors
from radicale_mcp.errors import CalendarError
from radicale_mcp.server import build_server, configure_logging, redact
from radicale_mcp.service import CalendarService


class MemoryStore:
    def __init__(self, text):
        self.text = text

    def resources(self, *args):
        return [("c_test", "e_test", self.text)]

    def close(self):
        pass


def service(config, event):
    data = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:test\n" + event
    data += "\nEND:VEVENT\nEND:VCALENDAR\n"
    return CalendarService(config, MemoryStore(data))


@pytest.mark.parametrize("query", ["CASEY", "downtown", "DENTIST"])
def test_local_search(config, query):
    svc = service(
        config,
        "DTSTART:20260915T140000Z\nDTEND:20260915T150000Z\n"
        "SUMMARY:Dentist\nLOCATION:Downtown\nDESCRIPTION:Casey's forms",
    )
    assert svc.search_events(query, "2026-09-15T00:00Z", "2026-09-16T00:00Z")["total"] == 1
    assert svc.search_events("absent", "2026-09-15T00:00Z", "2026-09-16T00:00Z")["total"] == 0


@pytest.mark.parametrize("extra", ["STATUS:CANCELLED", "TRANSP:TRANSPARENT"])
def test_ignored_busy_events(config, extra):
    svc = service(config, "DTSTART:20260915T140000Z\nDTEND:20260915T150000Z\n" + extra)
    assert svc.get_free_busy("2026-09-15T00:00Z", "2026-09-16T00:00Z")["busy"] == []


def test_all_day_dst_and_clipping(config):
    svc = service(config, "DTSTART;VALUE=DATE:20260308\nDTEND;VALUE=DATE:20260309")
    busy = svc.get_free_busy("2026-03-08T00:00-05:00", "2026-03-09T00:00-04:00")["busy"]
    assert busy[0]["duration_minutes"] == 23 * 60
    clipped = svc.get_free_busy("2026-03-08T12:00-04:00", "2026-03-09T12:00-04:00")["busy"]
    assert clipped[0]["duration_minutes"] == 12 * 60


def test_imported_duration_across_dst(config):
    svc = service(config, "DTSTART;TZID=America/New_York:20260308T013000\nDURATION:PT2H")
    busy = svc.get_free_busy("2026-03-08T00:00-05:00", "2026-03-09T00:00-04:00")["busy"]
    assert busy[0]["duration_minutes"] == 120


def test_recurring_duration_across_dst_fails_closed(config):
    svc = service(
        config,
        "DTSTART;TZID=America/New_York:20260301T013000\nDURATION:PT2H\nRRULE:FREQ=WEEKLY;COUNT=3",
    )
    with pytest.raises(CalendarError, match="unsupported_recurrence"):
        svc.get_free_busy("2026-03-08T00:00-05:00", "2026-03-09T00:00-04:00")


def test_no_false_availability_on_bad_data(config):
    svc = service(config, "SUMMARY:Missing start")
    with pytest.raises(CalendarError, match="invalid_calendar_data"):
        svc.find_available_slots("2026-09-15T00:00Z", "2026-09-16T00:00Z", 60)


def test_floating_recurrence(config):
    svc = service(
        config, "DTSTART:20260901T003000\nDTEND:20260901T013000\nRRULE:FREQ=DAILY;COUNT=30"
    )
    events = svc.list_events("2026-09-15T00:00-04:00", "2026-09-16T00:00-04:00")["events"]
    assert len(events) == 1
    assert events[0]["floating"] and events[0]["start"] == "2026-09-15T00:30:00-04:00"


def test_output_budget(config):
    svc = service(
        config, "DTSTART:20260915T140000Z\nDTEND:20260915T150000Z\nDESCRIPTION:" + "x" * 16000
    )
    events, a, b = svc._events("2026-09-15T00:00Z", "2026-09-16T00:00Z", None)
    page = svc._page(events * 200, a, b, 200, 0)
    assert 0 < len(page["events"]) < 200 and page["next_offset"] == len(page["events"])


def test_redaction_and_logging(config, capsys):
    token = base64.b64encode(f"{config.username}:{config.password}".encode()).decode()
    assert config.password not in str(redact({"nested": [config.password, token]}, config))
    old_handlers = logging.root.handlers[:]
    old_level = logging.root.level
    try:
        configure_logging(replace(config, log_level="DEBUG"))
        logging.getLogger("caldav").debug("Authorization: %s", token)
        logging.getLogger("radicale_mcp.test").info("Safe status")
        output = capsys.readouterr()
        assert output.out == "" and "Safe status" in output.err
        assert token not in output.err and "Authorization" not in output.err
    finally:
        logging.root.handlers = old_handlers
        logging.root.setLevel(old_level)


async def test_unknown_backend_error_is_safe(config):
    class BrokenStore:
        def list_calendars(self):
            raise RuntimeError(f"Authorization: Basic {config.password}")

        def close(self):
            pass

    svc = CalendarService(config, BrokenStore())
    async with Client(build_server(config, svc)) as client:
        result = await client.call_tool("list_calendars", {})
        assert result.is_error and "caldav_error" in str(result)
        assert config.password not in str(result) and "Authorization:" not in str(result)


def test_session_refuses_redirect_and_ambient_identity(config, monkeypatch):
    class Response:
        status_code = 302

    seen = {}

    def request(self, method, url, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr("niquests.Session.request", request)
    with GuardedSession(config.url) as session:
        assert session.trust_env is False
        with pytest.raises(CalendarError, match="redirect_refused"):
            session.request("GET", config.url)
        assert seen["allow_redirects"] is False


def test_transport_error_redaction():
    with pytest.raises(CalendarError, match="unreachable") as error:
        with safe_errors():
            raise OSError("secret details")
    assert "secret details" not in str(error.value)
