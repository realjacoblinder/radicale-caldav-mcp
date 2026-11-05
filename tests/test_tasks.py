"""VTODO domain and real-Radicale behavior."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import requests
from icalendar import Alarm, Calendar, Todo

from radicale_mcp.client import resource_id
from radicale_mcp.errors import CalendarError
from radicale_mcp.tasks import (
    expand_tasks,
    normalize_task,
    parse_task_calendar,
    recurrence_info,
)
from radicale_mcp.timeutils import zone


def task_calendar(**fields):
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//Tests//EN")
    task = Todo()
    task.add("UID", fields.pop("UID", "task-1"))
    for key, value in fields.items():
        task.add(key, value)
    calendar.add_component(task)
    return calendar


def test_normalize_vtodo_fields_dates_and_floating():
    ny = zone("America/New_York")
    timed = task_calendar(
        SUMMARY="File taxes",
        DTSTART=datetime(2026, 9, 15, 9),
        DUE=datetime(2026, 9, 15, 17),
        DESCRIPTION="Bring forms",
        LOCATION="Desk",
        CATEGORIES=["Finance", "Home"],
        STATUS="IN-PROCESS",
        PRIORITY=2,
        **{"PERCENT-COMPLETE": 40},
    )
    item = normalize_task(timed.walk("VTODO")[0], "c_1", "t_1", ny)
    assert item.start == "2026-09-15T09:00:00-04:00"
    assert item.due == "2026-09-15T17:00:00-04:00"
    assert item.floating and not item.all_day
    assert item.categories == ["Finance", "Home"]
    assert item.priority == 2 and item.percent_complete == 40

    all_day = task_calendar(SUMMARY="Renew tag", DUE=datetime(2026, 9, 20).date())
    normalized = normalize_task(all_day.walk("VTODO")[0], "c_1", "t_2", ny)
    assert normalized.all_day and normalized.due == "2026-09-20"


@pytest.mark.parametrize(
    "body",
    [
        "STATUS:NOT-A-STATUS\r\n",
        "PERCENT-COMPLETE:101\r\n",
        "PRIORITY:10\r\n",
        "DTSTART:20260915T090000Z\r\nDUE;VALUE=DATE:20260916\r\n",
        "DTSTART:20260915T090000Z\r\nDUE:20260915T080000Z\r\n",
        "DTSTART:20260915T090000Z\r\nDUE:20260915T100000Z\r\nDURATION:PT1H\r\n",
        "COMPLETED:20260915T120000\r\n",
    ],
)
def test_invalid_vtodo_fails_closed(body):
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Tests//EN\r\nBEGIN:VTODO\r\n"
        "UID:bad\r\n" + body + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(CalendarError, match="invalid_calendar_data"):
        parsed = parse_task_calendar(data)
        normalize_task(parsed.walk("VTODO")[0], "c", "t", zone("UTC"))


def test_recurring_vtodo_read_expansion_and_metadata():
    ny = zone("America/New_York")
    calendar = task_calendar(
        SUMMARY="Water plants",
        DTSTART=datetime(2026, 9, 15, 9, tzinfo=ny),
        DUE=datetime(2026, 9, 15, 10, tzinfo=ny),
        RRULE={"FREQ": ["DAILY"], "COUNT": [3]},
    )
    parsed = parse_task_calendar(calendar.to_ical().decode())
    info = recurrence_info(parsed)
    assert info and not info["mutation_supported"] and info["read_expansion_supported"]
    tasks = expand_tasks(
        parsed,
        "c_1",
        "t_1",
        ny,
        datetime(2026, 9, 15, tzinfo=ny).astimezone(UTC),
        datetime(2026, 9, 18, tzinfo=ny).astimezone(UTC),
        100,
    )
    assert [task.due for task in tasks] == [
        "2026-09-15T10:00:00-04:00",
        "2026-09-16T10:00:00-04:00",
        "2026-09-17T10:00:00-04:00",
    ]
    assert all(task.occurrence_id and task.representation == "occurrence" for task in tasks)

    # Filtering is by DUE, so expansion must compensate for a long DTSTART-to-DUE span.
    long = task_calendar(
        SUMMARY="Long task",
        DTSTART=datetime(2026, 9, 1, 9, tzinfo=ny),
        DUE=datetime(2026, 9, 11, 9, tzinfo=ny),
        RRULE={"FREQ": ["DAILY"], "COUNT": [1]},
    )
    expanded = expand_tasks(
        long,
        "c_1",
        "t_2",
        ny,
        datetime(2026, 9, 11, tzinfo=ny).astimezone(UTC),
        datetime(2026, 9, 12, tzinfo=ny).astimezone(UTC),
        100,
    )
    assert [task.due for task in expanded] == ["2026-09-11T09:00:00-04:00"]


@pytest.mark.integration
def test_vtodo_crud_search_completion_and_filters(accounts):
    service, cid, _ = accounts["alice"]
    created = service.create_task(
        cid,
        "Submit school form",
        start="2026-09-15T09:00-04:00",
        due="2026-09-15T17:00-04:00",
        description="Casey's enrollment",
        location="School office",
        categories=["Family", "School"],
        priority=1,
    )["task"]
    assert created["task_id"].startswith("t_") and created["etag"]
    assert len(service.search_tasks("CASEY", calendar_id=cid)["tasks"]) == 1
    assert len(service.search_tasks("school", calendar_id=cid)["tasks"]) == 1
    assert (
        len(
            service.list_tasks(
                cid,
                start="2026-09-15T00:00-04:00",
                end="2026-09-16T00:00-04:00",
            )["tasks"]
        )
        == 1
    )
    assert (
        service.list_tasks(
            cid,
            start="2026-09-16T00:00-04:00",
            end="2026-09-17T00:00-04:00",
        )["tasks"]
        == []
    )

    current = service.get_task(cid, created["task_id"])["task"]
    raw, raw_etag = service.store.get_task(cid, created["task_id"])
    parsed = parse_task_calendar(raw)
    component = parsed.walk("VTODO")[0]
    component.add("X-CUSTOM", "preserve me")
    alarm = Alarm()
    alarm.add("ACTION", "DISPLAY")
    alarm.add("DESCRIPTION", "Task reminder")
    alarm.add("TRIGGER", timedelta(minutes=-15))
    component.add_component(alarm)
    service.store.write_task(cid, created["task_id"], parsed.to_ical().decode(), raw_etag)
    current = service.get_task(cid, created["task_id"])["task"]
    changed = service.update_task(
        cid,
        current["task_id"],
        current["etag"],
        due="2026-09-15T18:00-04:00",
        percent_complete=50,
        status="IN-PROCESS",
        categories=[],
    )["task"]
    assert changed["due"] == "2026-09-15T18:00:00-04:00"
    assert changed["start"] == "2026-09-15T09:00:00-04:00"
    assert changed["percent_complete"] == 50 and changed["categories"] == []
    preserved, _ = service.store.get_task(cid, created["task_id"])
    preserved_task = parse_task_calendar(preserved).walk("VTODO")[0]
    assert str(preserved_task["X-CUSTOM"]) == "preserve me"
    assert len(preserved_task.walk("VALARM")) == 1

    completed = service.complete_task(
        cid,
        changed["task_id"],
        changed["etag"],
        "2026-09-15T22:00:00Z",
    )["task"]
    assert completed["completed"] and completed["percent_complete"] == 100
    assert completed["completed_at"] == "2026-09-15T22:00:00+00:00"
    assert service.list_tasks(cid)["tasks"] == []
    assert len(service.list_tasks(cid, state="completed")["tasks"]) == 1
    with pytest.raises(CalendarError, match="reopen_task"):
        service.update_task(
            cid,
            completed["task_id"],
            completed["etag"],
            percent_complete=50,
        )

    reopened = service.reopen_task(cid, completed["task_id"], completed["etag"])["task"]
    assert not reopened["completed"] and reopened["status"] == "NEEDS-ACTION"
    assert reopened["completed_at"] is None and reopened["percent_complete"] == 0
    with pytest.raises(CalendarError, match="stale_etag"):
        service.delete_task(cid, reopened["task_id"], changed["etag"])
    service.delete_task(cid, reopened["task_id"], reopened["etag"])
    with pytest.raises(CalendarError, match="not_found"):
        service.get_task(cid, reopened["task_id"])


@pytest.mark.integration
def test_undated_all_day_and_clear_fields(accounts):
    service, cid, _ = accounts["alice"]
    undated = service.create_task(cid, "Someday", categories=["Maybe"])["task"]
    dated = service.create_task(cid, "Renew tag", due="2026-09-20", all_day=True)["task"]
    result = service.list_tasks(
        cid,
        start="2026-09-15T00:00-04:00",
        end="2026-09-22T00:00-04:00",
        include_undated=False,
    )["tasks"]
    assert [task["summary"] for task in result] == ["Renew tag"]
    assert result[0]["all_day"] and result[0]["due"] == "2026-09-20"
    changed = service.update_task(
        cid,
        dated["task_id"],
        service.get_task(cid, dated["task_id"])["task"]["etag"],
        clear_due=True,
        clear_percent_complete=True,
    )["task"]
    assert changed["due"] is None and changed["percent_complete"] is None
    assert len(service.list_tasks(cid)["tasks"]) == 2
    assert undated["summary"] == "Someday"
    cancelled = service.update_task(
        cid,
        undated["task_id"],
        service.get_task(cid, undated["task_id"])["task"]["etag"],
        status="CANCELLED",
    )["task"]
    assert cancelled["status"] == "CANCELLED"
    assert len(service.list_tasks(cid)["tasks"]) == 1
    assert len(service.list_tasks(cid, state="all")["tasks"]) == 2

    with pytest.raises(CalendarError, match="both start and end"):
        service.list_tasks(cid, start="2026-09-15T00:00Z")


@pytest.mark.integration
def test_recurring_task_mutation_refused_on_real_radicale(accounts):
    service, cid, calendar = accounts["alice"]
    uid = str(uuid4())
    url = str(calendar.url) + uid + ".ics"
    task_id = resource_id(url, str(calendar.url), "t")
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Tests//EN\r\nBEGIN:VTODO\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260901T000000Z\r\n"
        "DTSTART;TZID=America/New_York:20260915T090000\r\n"
        "DUE;TZID=America/New_York:20260915T100000\r\n"
        "RRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Daily task\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )
    service.store.write_task(cid, task_id, data)
    tasks = service.list_tasks(
        cid,
        start="2026-09-15T00:00-04:00",
        end="2026-09-18T00:00-04:00",
    )["tasks"]
    assert len(tasks) == 3
    occurrence = service.get_task(cid, task_id, tasks[0]["occurrence_id"])
    assert occurrence["representation"] == "occurrence"
    assert occurrence["task"]["due"] == tasks[0]["due"]
    current = service.get_task(cid, task_id)["task"]
    for action in (service.complete_task, service.delete_task):
        with pytest.raises(CalendarError, match="unsupported_recurrence_mutation"):
            action(cid, task_id, current["etag"])


@pytest.mark.integration
def test_vtodo_owner_only_isolation(accounts, radicale):
    alice, _, _ = accounts["alice"]
    bob, bob_id, bob_calendar = accounts["bob"]
    task = bob.create_task(bob_id, "Bob private task")["task"]
    target = str(bob_calendar.url) + task["uid"] + ".ics"
    response = requests.get(
        target,
        auth=("alice", radicale["passwords"]["alice"]),
        timeout=5,
    )
    assert response.status_code in (401, 403)
    assert all(item["summary"] != "Bob private task" for item in alice.list_tasks()["tasks"])
