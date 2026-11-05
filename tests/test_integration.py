import base64
import json
import os
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
import requests
from icalendar import Alarm
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from radicale_mcp.client import resource_id
from radicale_mcp.errors import CalendarError
from radicale_mcp.events import parse_calendar
from radicale_mcp.server import build_server
from radicale_mcp.service import CalendarService

pytestmark = pytest.mark.integration
START, END = "2026-09-15T00:00:00-04:00", "2026-09-22T00:00:00-04:00"


def create(service, cid, **kwargs):
    return service.create_event(
        cid,
        kwargs.pop("summary", "Dentist"),
        kwargs.pop("start", "2026-09-15T14:00:00-04:00"),
        duration_minutes=60,
        **kwargs,
    )["event"]


def fresh(service, cid, event):
    return service.get_event(cid, event["event_id"])["event"]


def seed(service, cid, calendar, text):
    url = str(calendar.url) + str(uuid4()) + ".ics"
    eid = resource_id(url, str(calendar.url))
    body = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Tests//EN\r\n" + text + "END:VCALENDAR\r\n"
    service.store.write(cid, eid, body)
    return eid


def test_crud_search_range_and_preservation(accounts):
    service, cid, calendar = accounts["alice"]
    assert any(c["calendar_id"] == cid for c in service.list_calendars()["calendars"])
    event = create(service, cid, description="Casey's school forms", location="Downtown")
    event = fresh(service, cid, event)
    assert event["uid"] and event["etag"]
    assert len(service.search_events("CASEY", START, END, cid)["events"]) == 1
    assert len(service.search_events("town", START, END, cid)["events"]) == 1
    assert service.list_events("2026-09-16T00:00Z", "2026-09-17T00:00Z", cid)["events"] == []
    # Introduce fields unknown to the public tool; updates must preserve them.
    data, etag = service.store.get(cid, event["event_id"])
    obj = parse_calendar(data)
    component = obj.walk("VEVENT")[0]
    component.add("X-CUSTOM", "keep me")
    component.add("ATTENDEE", "mailto:person@example.test")
    alarm = Alarm()
    alarm.add("ACTION", "DISPLAY")
    alarm.add("DESCRIPTION", "Reminder")
    from datetime import timedelta

    alarm.add("TRIGGER", timedelta(minutes=-15))
    component.add_component(alarm)
    service.store.write(cid, event["event_id"], obj.to_ical().decode(), etag)
    event = fresh(service, cid, event)
    changed = service.update_event(
        cid, event["event_id"], event["etag"], start="2026-09-15T15:00-04:00"
    )["event"]
    assert changed["start"] == "2026-09-15T15:00:00-04:00"
    assert changed["end"] == "2026-09-15T16:00:00-04:00"
    assert changed["description"] == "Casey's school forms"
    data, _ = service.store.get(cid, event["event_id"])
    obj = parse_calendar(data)
    assert obj.walk("VEVENT")[0]["X-CUSTOM"] == "keep me"
    assert len(obj.walk("VALARM")) == 1
    assert "ATTENDEE" in obj.walk("VEVENT")[0]
    with pytest.raises(CalendarError, match="stale_etag"):
        service.update_event(cid, event["event_id"], event["etag"], summary="stale write")
    event = fresh(service, cid, event)
    service.delete_event(cid, event["event_id"], event["etag"])
    with pytest.raises(CalendarError, match="not_found"):
        fresh(service, cid, event)


def test_structured_alarms_create_update_and_validation(accounts):
    service, cid, _ = accounts["alice"]
    alarm = {"trigger": "-PT15M", "description": "Soon", "action": "DISPLAY"}
    event = create(service, cid, alarms=[alarm])
    assert event["alarms"] == [
        {"trigger": "-PT15M", "description": "Soon", "action": "DISPLAY", "related": "START"}
    ]
    raw, etag = service.store.get(cid, event["event_id"])
    assert "BEGIN:VALARM" in raw and "TRIGGER:-PT15M" in raw
    preserved = service.update_event(cid, event["event_id"], etag, summary="Changed")["event"]
    assert preserved["alarms"] == event["alarms"]
    cleared = service.update_event(cid, preserved["event_id"], preserved["etag"], alarms=[])[
        "event"
    ]
    assert cleared["alarms"] == []
    before, current_etag = service.store.get(cid, event["event_id"])
    for invalid_alarms in (
        [{"trigger": "garbage"}],
        [{"trigger": "-PT15M", "action": "AUDIO"}],
        [{"trigger": "-PT15M", "extra": "no"}],
    ):
        with pytest.raises(CalendarError):
            service.update_event(cid, event["event_id"], current_etag, alarms=invalid_alarms)
        assert service.store.get(cid, event["event_id"])[0] == before


def test_ics_properties_create_read_update_and_validation(accounts):
    service, cid, _ = accounts["alice"]
    values = {
        "categories": ["work", "urgent"],
        "priority": 0,
        "geo": {"latitude": 40.7, "longitude": -74.0},
        "attach": ["https://example.test/file"],
        "comment": ["first", "second"],
        "resources": ["projector", "room"],
        "url": "https://example.test/event",
        "organizer": {"address": "mailto:org@example.test", "cn": "Owner"},
        "attendee": [
            {
                "address": "mailto:a@example.test",
                "cn": "Alice",
                "role": "REQ-PARTICIPANT",
                "partstat": "ACCEPTED",
                "rsvp": True,
            }
        ],
        "contact": ["mailto:help@example.test"],
    }
    event = create(service, cid, **values)
    expected = {**values, "attendee": [{**values["attendee"][0], "rsvp": "TRUE"}]}
    assert {key: event[key] for key in values} == expected
    reread = fresh(service, cid, event)
    assert {key: reread[key] for key in values} == expected
    listed = next(
        item
        for item in service.list_events(START, END, cid)["events"]
        if item["event_id"] == event["event_id"]
    )
    assert {key: listed[key] for key in values} == expected
    raw, etag = service.store.get(cid, event["event_id"])
    component = parse_calendar(raw).walk("VEVENT")[0]
    assert component["PRIORITY"] == 0 and len(component.get("CATEGORIES")) == 2
    assert len(component.get("COMMENT")) == 2 and len(component.get("RESOURCES")) == 2
    assert str(component["ATTACH"]) == "https://example.test/file"
    assert component["GEO"].latitude == pytest.approx(40.7)
    assert "BEGIN:VALARM" not in raw

    # Null and omission preserve values; repeatable empty lists clear them.
    kept = service.update_event(
        cid, event["event_id"], etag, summary="Dentist", categories=None, priority=None
    )["event"]
    assert kept["categories"] == values["categories"] and kept["priority"] == 0
    # Existing organizer/attendee parameters survive an unrelated update.
    preserved = service.update_event(cid, event["event_id"], kept["etag"], summary="Unrelated")[
        "event"
    ]
    persisted = parse_calendar(service.store.get(cid, event["event_id"])[0]).walk("VEVENT")[0]
    assert persisted["ATTENDEE"].params["CN"] == "Alice"
    assert persisted["ORGANIZER"].params["CN"] == "Owner"

    changed = service.update_event(
        cid,
        event["event_id"],
        preserved["etag"],
        priority=9,
        url="https://example.test/new",
        categories=[],
        attach=[],
        comment=[],
        resources=[],
        attendee=[],
        contact=[],
    )["event"]
    assert changed["priority"] == 9 and changed["url"] == "https://example.test/new"
    for field in ("categories", "attach", "comment", "resources", "attendee", "contact"):
        assert changed[field] == []

    baseline, baseline_etag = service.store.get(cid, event["event_id"])
    invalid = [
        {"priority": -1},
        {"priority": 10},
        {"priority": True},
        {"geo": {"latitude": 91, "longitude": 0}},
        {"geo": [1, 2]},
        {"categories": [3]},
        {"comment": ["x" * 16001]},
        {"resources": ["ok"] * 101},
        {"attach": ["relative/path"]},
        {"attach": ["https://"]},
        {"attach": ["mailto:"]},
        {"attach": ["mailto:?subject=empty"]},
        {"attach": ["mailto:a b@example.test"]},
        {"attach": ["https://example.test/a\npath"]},
        {"attach": ["https://example.test:bad/path"]},
        {"url": "https://@/path"},
        {"url": "https://:443/path"},
        {"attendee": [{"address": "https://@/path"}]},
        {"attach": ["https://example.test/%zz"]},
        {"attach": ["https://e.test/" + "x" * 2050]},
        {"organizer": {"address": ""}},
        {"organizer": {"address": "mailto:o@example.test", "unknown": "no"}},
        {"organizer": {"address": "mailto:o@example.test", "role": "REQ-PARTICIPANT"}},
        {"attendee": [{"address": "mailto:a@example.test", "cn": 5}]},
        {"attendee": [{"address": "mailto:a@example.test", "rsvp": "yes"}]},
    ]
    for update in invalid:
        with pytest.raises(CalendarError):
            service.update_event(cid, event["event_id"], baseline_etag, **update)
        assert service.store.get(cid, event["event_id"])[0] == baseline


def test_ics_property_recurrence_scopes_and_future_identity(accounts):
    service, cid, _ = accounts["alice"]
    event = create(service, cid, recurrence_rule="FREQ=WEEKLY;COUNT=5", categories=["original"])
    second = create(service, cid, recurrence_rule="FREQ=WEEKLY;COUNT=5", categories=["second"])
    listing = service.list_events("2026-09-15T00:00:00-04:00", "2026-10-15T00:00:00-04:00", cid)[
        "events"
    ]
    rid = listing[1]["occurrence_id"]
    occurrence = service.get_event(cid, event["event_id"], rid)["event"]
    updated = service.update_event(
        cid,
        event["event_id"],
        occurrence["etag"],
        scope="occurrence",
        occurrence_id=rid,
        categories=["one"],
    )["event"]
    assert updated["categories"] == ["one"]
    series = service.update_event(
        cid, event["event_id"], updated["etag"], scope="series", categories=["series"]
    )["event"]
    assert series["categories"] == ["series"]
    listing = service.list_events("2026-09-15T00:00:00-04:00", "2026-10-15T00:00:00-04:00", cid)[
        "events"
    ]
    rid = listing[2]["occurrence_id"]
    result = service.update_event(
        cid,
        event["event_id"],
        series["etag"],
        scope="future",
        occurrence_id=rid,
        categories=["future"],
    )
    assert result["split"] and result["event"]["categories"] == ["future"]
    assert result["event"]["event_id"] != event["event_id"]
    assert fresh(service, cid, event)["categories"] == ["series"]
    second_listing = service.list_events(
        "2026-09-15T00:00:00-04:00", "2026-10-15T00:00:00-04:00", cid
    )["events"]
    second_rid = next(
        item["occurrence_id"]
        for item in second_listing
        if item["uid"] == second["uid"] and item["start"].startswith("2026-09-22")
    )
    second_result = service.update_event(
        cid,
        second["event_id"],
        second["etag"],
        scope="future",
        occurrence_id=second_rid,
        categories=["other-future"],
    )
    assert second_result["split"]
    assert second_result["event"]["event_id"] != result["event"]["event_id"]


def test_alarm_future_split(accounts):
    service, cid, _ = accounts["alice"]
    event = create(
        service,
        cid,
        recurrence_rule="FREQ=WEEKLY;COUNT=5",
        alarms=[{"trigger": "-PT15M"}],
    )
    rid = service.list_events("2026-09-15T00:00:00-04:00", "2026-10-15T00:00:00-04:00", cid)[
        "events"
    ][1]["occurrence_id"]
    listed = service.list_events("2026-09-15T00:00:00-04:00", "2026-10-15T00:00:00-04:00", cid)[
        "events"
    ]
    assert listed[0]["alarms"] == event["alarms"]
    changed = service.update_event(
        cid,
        event["event_id"],
        event["etag"],
        occurrence_id=rid,
        scope="future",
        alarms=[{"trigger": "-PT30M", "related": "END"}],
    )
    assert changed["split"]
    assert changed["event"]["alarms"] == [
        {"trigger": "-PT30M", "description": "Reminder", "action": "DISPLAY", "related": "END"}
    ]
    original = fresh(service, cid, {"event_id": event["event_id"]})
    assert original["alarms"] == event["alarms"]


def test_alarm_recurrence_series_and_occurrence_scopes(accounts):
    service, cid, _ = accounts["alice"]
    event = create(service, cid, recurrence_rule="FREQ=WEEKLY;COUNT=4")
    series_alarm = [{"trigger": "-PT10M"}]
    updated = service.update_event(
        cid, event["event_id"], event["etag"], scope="series", alarms=series_alarm
    )["event"]
    assert updated["alarms"][0]["trigger"] == "-PT10M"
    listing = service.list_events("2026-09-15T00:00:00-04:00", "2026-10-15T00:00:00-04:00", cid)[
        "events"
    ]
    rid = listing[1]["occurrence_id"]
    occurrence = service.get_event(cid, event["event_id"], rid)["event"]
    cleared = service.update_event(
        cid,
        event["event_id"],
        occurrence["etag"],
        scope="occurrence",
        occurrence_id=rid,
        alarms=[],
    )["event"]
    assert cleared["alarms"] == []
    assert service.get_event(cid, event["event_id"])["event"]["alarms"][0]["trigger"] == "-PT10M"


def test_actual_radicale_authorization(accounts, radicale):
    alice, alice_id, _ = accounts["alice"]
    bob, bob_id, bob_calendar = accounts["bob"]
    event = create(bob, bob_id, summary="Bob private")
    bob_url = str(bob_calendar.url)
    assert all("/bob/" not in c["url"] for c in alice.list_calendars()["calendars"])
    # Bypass the MCP discovery layer deliberately: Radicale must reject the HTTP request.
    auth = ("alice", radicale["passwords"]["alice"])
    for method in ("PROPFIND", "REPORT", "GET", "PUT", "DELETE"):
        target = bob_url if method in ("PROPFIND", "REPORT") else bob_url + event["uid"] + ".ics"
        response = requests.request(
            method,
            target,
            auth=auth,
            timeout=5,
            headers={"Depth": "1", "Content-Type": "text/calendar"},
        )
        assert response.status_code in (401, 403), (method, response.status_code)
    with pytest.raises(CalendarError, match="calendar_not_found"):
        alice.get_event(bob_id, event["event_id"])
    assert fresh(bob, bob_id, event)["summary"] == "Bob private"
    assert alice.list_events(START, END, alice_id)["events"] == []


def test_http_etag_protection_and_create_collision(accounts):
    service, cid, _ = accounts["alice"]
    event = create(service, cid)
    data, old_etag = service.store.get(cid, event["event_id"])
    changed = data.replace("Dentist", "Changed remotely")
    service.store.write(cid, event["event_id"], changed, old_etag)
    # Direct conditional writes/deletes exercise Radicale's race protection, beyond preflight.
    with pytest.raises(CalendarError, match="stale_etag"):
        service.store.write(cid, event["event_id"], data, old_etag)
    with pytest.raises(CalendarError, match="stale_etag"):
        service.store.delete(cid, event["event_id"], old_etag)
    with pytest.raises(CalendarError, match="stale_etag"):
        service.store.write(cid, event["event_id"], data)
    assert fresh(service, cid, event)["summary"] == "Changed remotely"


def test_recurrence_on_radicale(accounts):
    service, cid, calendar = accounts["alice"]
    eid = seed(
        service,
        cid,
        calendar,
        "BEGIN:VEVENT\r\nUID:weekly\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;TZID=America/New_York:20260901T140000\r\n"
        "DTEND;TZID=America/New_York:20260901T150000\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=5\r\nEXDATE;TZID=America/New_York:20260922T140000\r\n"
        "SUMMARY:School meeting\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:weekly\r\nDTSTAMP:20260101T000000Z\r\n"
        "RECURRENCE-ID;TZID=America/New_York:20260915T140000\r\n"
        "DTSTART;TZID=America/New_York:20260915T160000\r\n"
        "DTEND;TZID=America/New_York:20260915T170000\r\n"
        "SUMMARY:School moved\r\nEND:VEVENT\r\n",
    )
    events = service.list_events(START, "2026-09-30T00:00-04:00", cid)["events"]
    assert [e["start"] for e in events] == [
        "2026-09-15T16:00:00-04:00",
        "2026-09-29T14:00:00-04:00",
    ]
    assert events[0]["occurrence_id"] == "2026-09-15T14:00:00-04:00"
    master = service.get_event(cid, eid)["event"]
    for action in (service.update_event, service.delete_event):
        extra = {"summary": "bad"} if action == service.update_event else {}
        with pytest.raises(CalendarError, match="recurrence_scope_required"):
            action(cid, eid, master["etag"], **extra)
        with pytest.raises(CalendarError, match="recurrence_scope_required"):
            action(cid, eid, master["etag"], occurrence_id=events[0]["occurrence_id"], **extra)


def test_all_day_floating_busy_and_availability(accounts):
    service, cid, calendar = accounts["alice"]
    service.create_event(cid, "Day off", "2026-09-16", all_day=True)
    create(service, cid)
    create(service, cid, start="2026-09-15T14:30-04:00", summary="Overlap")
    seed(
        service,
        cid,
        calendar,
        "BEGIN:VEVENT\r\nUID:floating\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART:20260915T001500\r\nDTEND:20260915T004500\r\nSUMMARY:Floating\r\nEND:VEVENT\r\n",
    )
    for uid, extra in (("cancelled", "STATUS:CANCELLED"), ("transparent", "TRANSP:TRANSPARENT")):
        seed(
            service,
            cid,
            calendar,
            f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
            "DTSTART:20260915T130000Z\r\nDTEND:20260915T210000Z\r\n"
            f"SUMMARY:Ignored\r\n{extra}\r\nEND:VEVENT\r\n",
        )
    events = service.list_events(START, END, cid)["events"]
    floating = next(e for e in events if e["uid"] == "floating")
    assert floating["floating"] and floating["start"] == "2026-09-15T00:15:00-04:00"
    busy = service.get_free_busy(START, END, cid)["busy"]
    assert len(busy) == 3
    assert busy[-1]["duration_minutes"] == 1440
    available = service.find_available_slots(
        START, "2026-09-16T00:00-04:00", 60, cid, "09:00", "17:00", [1]
    )["slots"]
    assert [(s["start"], s["end"]) for s in available] == [
        ("2026-09-15T13:00:00+00:00", "2026-09-15T18:00:00+00:00"),
        ("2026-09-15T19:30:00+00:00", "2026-09-15T21:00:00+00:00"),
    ]


def test_paging_and_limit_fails_closed(accounts):
    service, cid, _ = accounts["alice"]
    for _ in range(3):
        create(service, cid)
    page = service.list_events(START, END, cid, limit=2)
    assert page["next_offset"] == 2 and len(page["events"]) == 2
    assert len(service.list_events(START, END, cid, offset=2)["events"]) == 1
    limited = CalendarService(replace(service.config, max_events=2), service.store)
    for method in (limited.list_events, limited.get_free_busy):
        with pytest.raises(CalendarError, match="limit_exceeded"):
            method(START, END, cid)


async def test_mcp_errors_and_metadata(accounts, radicale):
    service, cid, _ = accounts["alice"]
    async with Client(build_server(service.config, service)) as client:
        tools = (await client.list_tools()).tools
        tools_by_name = {tool.name: tool for tool in tools}
        assert len(tools) == 17
        read_names = {
            "list_calendars",
            "list_events",
            "get_event",
            "search_events",
            "get_free_busy",
            "find_available_slots",
            "list_tasks",
            "search_tasks",
            "get_task",
        }
        for tool in tools:
            assert tool.annotations.read_only_hint == (tool.name in read_names)
            forbidden = {"username", "password", "host"}.intersection(
                tool.input_schema["properties"]
            )
            if tool.name not in {"create_event", "update_event"}:
                forbidden |= {"url"}.intersection(tool.input_schema["properties"])
            assert not forbidden
        assert "url" in tools_by_name["create_event"].input_schema["properties"]
        assert "url" in tools_by_name["update_event"].input_schema["properties"]
        for tool_name in ("create_event", "update_event"):
            schema = tools_by_name[tool_name].input_schema
            geo_schema = schema["$defs"]["GeoInput"]
            assert set(geo_schema["required"]) == {"latitude", "longitude"}
            assert set(geo_schema["properties"]) == {"latitude", "longitude"}
            assert all(prop["type"] == "number" for prop in geo_schema["properties"].values())
            organizer_schema = schema["$defs"]["OrganizerInput"]
            assert set(organizer_schema["properties"]) == {"address", "cn"}
            assert organizer_schema["required"] == ["address"]
            assert all(prop["type"] == "string" for prop in organizer_schema["properties"].values())
            attendee_schema = schema["$defs"]["AttendeeInput"]
            assert set(attendee_schema["properties"]) == {
                "address",
                "cn",
                "role",
                "partstat",
                "rsvp",
            }
            assert attendee_schema["required"] == ["address"]
            assert all(
                attendee_schema["properties"][name]["type"] == "string"
                for name in ("address", "cn", "role", "partstat")
            )
            assert attendee_schema["properties"]["rsvp"]["type"] == "boolean"
        result = await client.call_tool(
            "create_event",
            {
                "calendar_id": cid,
                "summary": "MCP event",
                "start": "2026-09-15T14:00-04:00",
                "duration_minutes": 60,
            },
        )
        assert not result.is_error and result.structured_content["event"]["uid"]
        with_url = await client.call_tool(
            "create_event",
            {
                "calendar_id": cid,
                "summary": "URL metadata",
                "start": "2026-09-15T14:00-04:00",
                "duration_minutes": 30,
                "url": "https://example.test/event",
            },
        )
        assert not with_url.is_error
        assert with_url.structured_content["event"]["url"] == "https://example.test/event"
        bad = await client.call_tool("list_events", {"start": "bad", "end": END})
        assert bad.is_error and "ISO-8601" in str(bad.content)
        wrong = await client.call_tool("get_event", {"calendar_id": "missing", "event_id": "e_bad"})
        assert wrong.is_error and "calendar_not_found" in str(wrong.content)
        raw = (
            json.dumps([t.model_dump(mode="json") for t in tools])
            + str(bad)
            + str(result)
            + str(wrong)
        )
        for secret in radicale["passwords"].values():
            assert secret not in raw
        assert "Authorization:" not in raw and "Traceback" not in raw


async def test_auth_failure_is_safe(accounts):
    service, _, _ = accounts["alice"]
    cfg = replace(service.config, password="deliberately-wrong-test-password")
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool("list_calendars", {})
        assert result.is_error and "authentication_failed" in str(result)
        assert cfg.password not in str(result)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_stdio_startup(accounts, project_root, mode):
    service, cid, _ = accounts["alice"]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "radicale_mcp.server"],
        cwd=str(project_root),
        env={
            "PYTHONPATH": str(project_root / "src"),
            "CALDAV_URL": service.config.url,
            "CALDAV_USERNAME": service.config.username,
            "CALDAV_PASSWORD": service.config.password,
            "LOG_LEVEL": "DEBUG",
        },
    )
    async with Client(params, mode=mode) as client:
        assert len((await client.list_tools()).tools) == 17
        result = await client.call_tool("list_calendars", {})
        assert not result.is_error
        assert cid in str(result.structured_content)


def test_check_cli_and_missing_config(accounts, project_root):
    service, _, _ = accounts["alice"]
    env = {
        **os.environ,
        "PYTHONPATH": str(project_root / "src"),
        "CALDAV_URL": service.config.url,
        "CALDAV_USERNAME": service.config.username,
        "CALDAV_PASSWORD": service.config.password,
    }
    check = subprocess.run(
        [sys.executable, "-m", "radicale_mcp.server", "--check"],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout)["calendars"]
    bad_env = {k: v for k, v in env.items() if not k.startswith("CALDAV_")}
    bad = subprocess.run(
        [sys.executable, "-m", "radicale_mcp.server"],
        env=bad_env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert bad.returncode == 2 and "CALDAV_URL" in bad.stderr and not bad.stdout
    token = base64.b64encode(
        f"{service.config.username}:{service.config.password}".encode()
    ).decode()
    assert service.config.password not in check.stdout + check.stderr
    assert token not in check.stdout + check.stderr


def test_installed_entrypoint_and_smoke_script(accounts, project_root):
    service, cid, _ = accounts["alice"]
    executable = project_root / ".venv" / "bin" / "radicale-mcp"
    assert executable.exists(), "Run uv sync --locked before the integration suite."
    env = {
        **os.environ,
        "CALDAV_URL": service.config.url,
        "CALDAV_USERNAME": service.config.username,
        "CALDAV_PASSWORD": service.config.password,
        "CALDAV_DEFAULT_TIMEZONE": service.config.timezone,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "smoke.py"),
            "--command",
            str(executable),
            "--write-calendar",
            cid,
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "17 tools discovered" in result.stdout
    assert "VTODO create/read/complete/reopen succeeded" in result.stdout
    assert "Temporary event deleted" in result.stdout
    assert service.config.password not in result.stdout + result.stderr
