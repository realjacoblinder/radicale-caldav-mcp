from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from icalendar import Alarm, Calendar, Event
from mcp import Client

from radicale_mcp.client import resource_id
from radicale_mcp.errors import CalendarError
from radicale_mcp.events import parse_calendar
from radicale_mcp.recurrence import validate_rule, validate_scope, validate_structure
from radicale_mcp.server import build_server
from radicale_mcp.timeutils import zone

NY = zone("America/New_York")
WINDOW = ("2026-09-01T00:00-04:00", "2026-10-01T00:00-04:00")


def series(start=None, all_day=False, floating=False, rule="FREQ=WEEKLY;COUNT=5"):
    start = start or (date(2026, 9, 1) if all_day else datetime(2026, 9, 1, 14, tzinfo=NY))
    if floating:
        start = start.replace(tzinfo=None)
    event = Event()
    for name, value in {
        "UID": str(uuid4()),
        "DTSTAMP": datetime.now(UTC),
        "DTSTART": start,
        "DTEND": start + timedelta(days=1) if all_day else start + timedelta(hours=1),
        "SUMMARY": "Weekly appointment",
        "LOCATION": "Office",
        "DESCRIPTION": "Bring forms",
        "X-CUSTOM": "Keep this",
        "SEQUENCE": 0,
    }.items():
        event.add(name, value)
    event.add("RRULE", rule)
    event.add("ATTENDEE", "mailto:person@example.test")
    alarm = Alarm()
    alarm.add("ACTION", "DISPLAY")
    alarm.add("TRIGGER", timedelta(minutes=-15))
    alarm.add("DESCRIPTION", "Reminder")
    event.add_component(alarm)
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//V2 Tests//EN")
    calendar.add_component(event)
    return calendar


def install(accounts, obj, user="alice"):
    svc, cid, collection = accounts[user]
    eid = resource_id(str(collection.url) + str(uuid4()) + ".ics", str(collection.url))
    svc.store.write(cid, eid, obj.to_ical().decode())
    return svc, cid, eid


def read(svc, cid, eid, occurrence=None):
    return svc.get_event(cid, eid, occurrence)["event"]


def raw(svc, cid, eid):
    return parse_calendar(svc.store.get(cid, eid)[0])


def listed(svc, cid, window=WINDOW):
    return svc.list_events(*window, calendar_id=cid)["events"]


@pytest.mark.parametrize(
    "scope,rid",
    [
        ("event", None),
        ("event", "2026-09-15"),
        ("series", "2026-09-15"),
        ("occurrence", None),
        ("future", None),
    ],
)
def test_explicit_scope_validation(scope, rid):
    with pytest.raises(CalendarError):
        validate_scope(series(), scope, rid)


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "RRULE:FREQ=WEEKLY",
        "FREQ=SECONDLY",
        "FREQ=WEEKLY;COUNT=0",
        "FREQ=WEEKLY;COUNT=2;UNTIL=20260915T180000Z",
        "FREQ=WEEKLY;INTERVAL=-1",
        "FREQ=WEEKLY\nSUMMARY:injection",
        "FREQ=WEEKLY;BYDAY=WE",
        "FREQ=WEEKLY;BOGUS=1",
        "FREQ=DAILY;UNTIL=20260915",
        "FREQ=DAILY;UNTIL=20260915T000000",
    ],
)
def test_rule_validation(rule):
    with pytest.raises(CalendarError):
        validate_rule(series().walk("VEVENT")[0], rule)


def test_duplicate_and_range_exceptions_rejected():
    obj = series()
    master = obj.walk("VEVENT")[0]
    override = deepcopy(master)
    override.pop("RRULE")
    override.add("RECURRENCE-ID", master.decoded("DTSTART"))
    obj.add_component(override)
    obj.add_component(deepcopy(override))
    with pytest.raises(CalendarError, match="Duplicate"):
        validate_structure(obj)
    obj.subcomponents.pop()
    override["RECURRENCE-ID"].params["RANGE"] = "THISANDFUTURE"
    with pytest.raises(CalendarError, match="RANGE"):
        validate_structure(obj)


@pytest.mark.integration
class TestRadicaleRecurrence:
    def test_utc_occurrence_and_equivalent_offset_identifier(self, accounts):
        svc, cid, eid = install(accounts, series(start=datetime(2026, 9, 1, 18, tzinfo=UTC)))
        rid = "2026-09-08T14:00-04:00"
        current = read(svc, cid, eid, rid)
        assert current["occurrence_id"] == "2026-09-08T18:00:00+00:00"
        svc.update_event(
            cid,
            eid,
            current["etag"],
            scope="occurrence",
            occurrence_id=rid,
            start="2026-09-08T19:00Z",
        )
        moved = read(svc, cid, eid, rid)
        assert moved["start"] == "2026-09-08T19:00:00+00:00"
        assert moved["end"] == "2026-09-08T20:00:00+00:00"

    @pytest.mark.parametrize("all_day,floating", [(True, False), (False, True)])
    def test_simple_date_and_floating_series_reschedule(self, accounts, all_day, floating):
        svc, cid, eid = install(accounts, series(all_day=all_day, floating=floating))
        start = "2026-09-02" if all_day else "2026-09-01T15:30"
        svc.update_event(cid, eid, read(svc, cid, eid)["etag"], scope="series", start=start)
        events = listed(svc, cid)
        assert len(events) == 5
        master = raw(svc, cid, eid).walk("VEVENT")[0]
        if all_day:
            assert master.decoded("DTSTART") == date(2026, 9, 2)
            assert all(e["all_day"] for e in events)
        else:
            assert master.decoded("DTSTART").tzinfo is None
            assert all(e["floating"] and "T15:30:00" in e["start"] for e in events)

    def test_until_changes_require_explicit_revised_rule(self, accounts):
        svc, cid, eid = install(accounts, series(rule="FREQ=WEEKLY;UNTIL=20260929T180000Z"))
        before = svc.store.get(cid, eid)
        with pytest.raises(CalendarError, match="supply recurrence_rule explicitly"):
            svc.update_event(cid, eid, before[1], scope="series", start="2026-09-01T15:00-04:00")
        assert svc.store.get(cid, eid) == before
        svc.update_event(
            cid,
            eid,
            before[1],
            scope="series",
            start="2026-09-01T15:00-04:00",
            recurrence_rule="FREQ=WEEKLY;UNTIL=20260929T190000Z",
        )
        assert len(listed(svc, cid)) == 5

    def test_range_series_only_allows_whole_resource_deletion(self, accounts):
        obj = series()
        master = obj.walk("VEVENT")[0]
        override = deepcopy(master)
        override.pop("RRULE")
        override.add(
            "RECURRENCE-ID",
            datetime(2026, 9, 8, 14, tzinfo=NY),
            parameters={"RANGE": "THISANDFUTURE"},
        )
        override.pop("DTSTART")
        override.pop("DTEND")
        override.add("DTSTART", datetime(2026, 9, 8, 15, tzinfo=NY))
        override.add("DTEND", datetime(2026, 9, 8, 16, tzinfo=NY))
        obj.add_component(override)
        svc, cid, eid = install(accounts, obj)
        before = svc.store.get(cid, eid)
        with pytest.raises(CalendarError, match="RANGE"):
            svc.update_event(cid, eid, before[1], scope="series", summary="Unsafe")
        assert svc.store.get(cid, eid) == before
        result = svc.delete_event(cid, eid, before[1], scope="series")
        assert result["series_deleted"]

    def test_cancelled_override_is_not_resurrected(self, accounts):
        obj = series()
        override = deepcopy(obj.walk("VEVENT")[0])
        override.pop("RRULE")
        override.add("RECURRENCE-ID", override.decoded("DTSTART"))
        override.add("STATUS", "CANCELLED")
        obj.add_component(override)
        svc, cid, eid = install(accounts, obj)
        before = svc.store.get(cid, eid)
        with pytest.raises(CalendarError, match="occurrence_not_found"):
            svc.update_event(
                cid,
                eid,
                before[1],
                scope="occurrence",
                occurrence_id="2026-09-01T14:00-04:00",
                summary="Bad",
            )
        assert svc.store.get(cid, eid) == before

    def test_new_time_releases_old_busy_interval(self, accounts):
        svc, cid, eid = install(accounts, series())
        rid = listed(svc, cid)[1]["occurrence_id"]
        svc.update_event(
            cid,
            eid,
            read(svc, cid, eid)["etag"],
            scope="occurrence",
            occurrence_id=rid,
            start="2026-09-08T16:00-04:00",
        )
        old_window = ("2026-09-08T14:00-04:00", "2026-09-08T15:00-04:00")
        new_window = ("2026-09-08T16:00-04:00", "2026-09-08T17:00-04:00")
        assert svc.get_free_busy(*old_window, calendar_id=cid)["busy"] == []
        assert svc.find_available_slots(*old_window, 60, calendar_id=cid)["slots"]
        assert len(svc.get_free_busy(*new_window, calendar_id=cid)["busy"]) == 1
        svc.delete_event(
            cid, eid, read(svc, cid, eid)["etag"], scope="occurrence", occurrence_id=rid
        )
        assert svc.get_free_busy(*new_window, calendar_id=cid)["busy"] == []

    def test_recurring_writes_cannot_target_another_account(self, accounts):
        bob, bob_cid, bob_eid = install(accounts, series(), user="bob")
        alice, _, _ = accounts["alice"]
        etag = read(bob, bob_cid, bob_eid)["etag"]
        with pytest.raises(CalendarError, match="calendar_not_found"):
            alice.update_event(bob_cid, bob_eid, etag, scope="series", summary="Not allowed")
        with pytest.raises(CalendarError, match="calendar_not_found"):
            alice.delete_event(bob_cid, bob_eid, etag, scope="series")
        assert read(bob, bob_cid, bob_eid)["etag"] == etag

    def test_move_occurrence_and_reedit_original_identifier(self, accounts):
        svc, cid, eid = install(accounts, series())
        before = listed(svc, cid)
        rid = before[2]["occurrence_id"]
        current = read(svc, cid, eid, rid)
        assert current["start"] == "2026-09-15T14:00:00-04:00"
        changed = svc.update_event(
            cid,
            eid,
            current["etag"],
            scope="occurrence",
            occurrence_id=rid,
            start="2026-10-12T15:00-04:00",
            summary="Moved appointment",
        )["event"]
        assert changed["occurrence_id"] == rid and changed["uid"] == current["uid"]
        assert changed["end"] == "2026-10-12T16:00:00-04:00"
        assert len(listed(svc, cid)) == 4
        october = listed(svc, cid, ("2026-10-01T00:00Z", "2026-11-01T00:00Z"))
        assert len(october) == 1 and october[0]["occurrence_id"] == rid
        current = read(svc, cid, eid, rid)
        svc.update_event(
            cid,
            eid,
            current["etag"],
            scope="occurrence",
            occurrence_id=rid,
            location="New office",
            description="",
        )
        persisted = raw(svc, cid, eid)
        assert len(persisted.walk("VEVENT")) == 2
        override = next(e for e in persisted.walk("VEVENT") if "RECURRENCE-ID" in e)
        assert override["X-CUSTOM"] == "Keep this" and "ATTENDEE" in override
        assert len(override.walk("VALARM")) == 1
        assert "RRULE" not in override and "EXDATE" not in override
        assert override["LOCATION"] == "New office" and not override["DESCRIPTION"]
        remaining = listed(svc, cid)
        assert [e["start"] for e in remaining] == [
            e["start"] for e in before if e["occurrence_id"] != rid
        ]
        assert all(e["summary"] == "Weekly appointment" for e in remaining)

    def test_delete_first_and_moved_occurrence(self, accounts):
        svc, cid, eid = install(accounts, series())
        before = listed(svc, cid)
        first, moved = before[0]["occurrence_id"], before[2]["occurrence_id"]
        etag = read(svc, cid, eid)["etag"]
        answer = svc.delete_event(cid, eid, etag, scope="occurrence", occurrence_id=first)
        assert answer["deleted"] and not answer["series_deleted"]
        assert len(listed(svc, cid)) == 4
        original_start = raw(svc, cid, eid).walk("VEVENT")[0].decoded("DTSTART")
        assert original_start.day == 1
        etag = read(svc, cid, eid)["etag"]
        svc.update_event(
            cid, eid, etag, scope="occurrence", occurrence_id=moved, start="2026-10-12T14:00-04:00"
        )
        etag = read(svc, cid, eid, moved)["etag"]
        svc.delete_event(cid, eid, etag, scope="occurrence", occurrence_id=moved)
        persisted = raw(svc, cid, eid)
        assert len(persisted.walk("VEVENT")) == 1
        assert persisted.walk("VEVENT")[0]["RRULE"]["COUNT"] == [5]
        assert len(listed(svc, cid)) == 3
        assert listed(svc, cid, ("2026-10-01T00:00Z", "2026-11-01T00:00Z")) == []
        with pytest.raises(CalendarError, match="occurrence_not_found"):
            svc.delete_event(
                cid, eid, read(svc, cid, eid)["etag"], scope="occurrence", occurrence_id=first
            )

    def test_series_text_updates_all_overrides_and_series_delete(self, accounts):
        svc, cid, eid = install(accounts, series())
        rid = listed(svc, cid)[1]["occurrence_id"]
        svc.update_event(
            cid,
            eid,
            read(svc, cid, eid)["etag"],
            scope="occurrence",
            occurrence_id=rid,
            summary="Specific title",
            start="2026-09-08T16:00-04:00",
        )
        response = svc.update_event(
            cid,
            eid,
            read(svc, cid, eid)["etag"],
            scope="series",
            summary="All appointments",
            location="Family office",
        )
        assert response["scope"] == "series"
        assert all(e["summary"] == "All appointments" for e in listed(svc, cid))
        assert read(svc, cid, eid, rid)["start"] == "2026-09-08T16:00:00-04:00"
        assert len(raw(svc, cid, eid).walk("VEVENT")) == 2
        answer = svc.delete_event(cid, eid, read(svc, cid, eid)["etag"], scope="series")
        assert answer["series_deleted"]
        assert listed(svc, cid) == []

    def test_series_schedule_and_repeat_rule(self, accounts):
        svc, cid, eid = install(accounts, series(rule="FREQ=WEEKLY;COUNT=5;BYDAY=TU"))
        before = read(svc, cid, eid)
        moved = svc.update_event(
            cid,
            eid,
            before["etag"],
            scope="series",
            start="2026-09-01T15:00-04:00",
            duration_minutes=90,
        )["event"]
        assert all(e["start"].endswith("T15:00:00-04:00") for e in listed(svc, cid))
        assert moved["uid"] == before["uid"]
        with pytest.raises(CalendarError, match="series start must match"):
            svc.update_event(
                cid, eid, moved["etag"], scope="series", start="2026-09-02T15:00-04:00"
            )
        assert read(svc, cid, eid)["etag"] == moved["etag"]
        svc.update_event(
            cid,
            eid,
            moved["etag"],
            scope="series",
            start="2026-09-02T15:00-04:00",
            recurrence_rule="FREQ=WEEKLY;BYDAY=WE;COUNT=3",
        )
        result = listed(svc, cid)
        assert [e["start"][:10] for e in result] == ["2026-09-02", "2026-09-09", "2026-09-16"]
        assert all(e["end"].endswith("T16:30:00-04:00") for e in result)

    @pytest.mark.parametrize("kind", ["override", "exdate", "rdate"])
    def test_unsafe_series_reschedule_rejected_without_writing(self, accounts, kind):
        obj = series()
        master = obj.walk("VEVENT")[0]
        if kind == "exdate":
            master.add("EXDATE", datetime(2026, 9, 8, 14, tzinfo=NY))
        if kind == "rdate":
            master.add("RDATE", datetime(2026, 9, 9, 14, tzinfo=NY))
        svc, cid, eid = install(accounts, obj)
        if kind == "override":
            rid = listed(svc, cid)[1]["occurrence_id"]
            svc.update_event(
                cid,
                eid,
                read(svc, cid, eid)["etag"],
                scope="occurrence",
                occurrence_id=rid,
                description="An exception",
            )
        before = svc.store.get(cid, eid)
        with pytest.raises(CalendarError, match="unsupported_recurrence_mutation"):
            svc.update_event(cid, eid, before[1], scope="series", start="2026-09-01T16:00-04:00")
        assert svc.store.get(cid, eid) == before

    @pytest.mark.parametrize("all_day,floating", [(True, False), (False, True)])
    def test_date_and_floating_occurrence_types(self, accounts, all_day, floating):
        svc, cid, eid = install(accounts, series(all_day=all_day, floating=floating))
        rid = listed(svc, cid)[1]["occurrence_id"]
        new_start = "2026-09-09" if all_day else "2026-09-09T15:00-04:00"
        svc.update_event(
            cid,
            eid,
            read(svc, cid, eid, rid)["etag"],
            scope="occurrence",
            occurrence_id=rid,
            start=new_start,
        )
        persisted = raw(svc, cid, eid)
        override = next(e for e in persisted.walk("VEVENT") if "RECURRENCE-ID" in e)
        value = override.decoded("RECURRENCE-ID")
        assert isinstance(value, datetime) != all_day
        if floating:
            assert value.tzinfo is None and override.decoded("DTSTART").tzinfo is None
        svc.delete_event(
            cid, eid, read(svc, cid, eid)["etag"], scope="occurrence", occurrence_id=rid
        )
        master = raw(svc, cid, eid).walk("VEVENT")[0]
        assert master["EXDATE"].params.get("VALUE", "DATE-TIME") == (
            "DATE" if all_day else "DATE-TIME"
        )
        assert "TZID" not in master["EXDATE"].params
        assert len(listed(svc, cid)) == 4

    def test_occurrence_edit_across_dst(self, accounts):
        svc, cid, eid = install(
            accounts, series(start=datetime(2026, 3, 1, 9, tzinfo=NY), rule="FREQ=WEEKLY;COUNT=4")
        )
        window = ("2026-03-01T00:00Z", "2026-03-30T00:00Z")
        before = listed(svc, cid, window)
        rid = before[1]["occurrence_id"]
        assert before[0]["start"].endswith("-05:00") and rid.endswith("-04:00")
        svc.update_event(
            cid,
            eid,
            read(svc, cid, eid, rid)["etag"],
            scope="occurrence",
            occurrence_id=rid,
            start="2026-03-08T11:00-04:00",
        )
        assert read(svc, cid, eid, rid)["end"] == "2026-03-08T12:00:00-04:00"
        assert listed(svc, cid, window)[2]["start"] == before[2]["start"]

    def test_resource_etag_protects_different_occurrences(self, accounts):
        svc, cid, eid = install(accounts, series())
        events = listed(svc, cid)
        old = read(svc, cid, eid)["etag"]
        svc.update_event(
            cid,
            eid,
            old,
            scope="occurrence",
            occurrence_id=events[0]["occurrence_id"],
            location="New",
        )
        for operation in (svc.update_event, svc.delete_event):
            extra = {"summary": "stale"} if operation == svc.update_event else {}
            with pytest.raises(CalendarError, match="stale_etag"):
                operation(
                    cid,
                    eid,
                    old,
                    scope="occurrence",
                    occurrence_id=events[1]["occurrence_id"],
                    **extra,
                )
        assert len(listed(svc, cid)) == 5

    def test_nonexistent_or_excluded_occurrence_never_created(self, accounts):
        obj = series()
        obj.walk("VEVENT")[0].add("EXDATE", datetime(2026, 9, 8, 14, tzinfo=NY))
        svc, cid, eid = install(accounts, obj)
        before = svc.store.get(cid, eid)
        for rid in ("2026-09-08T14:00-04:00", "2026-09-09T14:00-04:00", "2026-10-20T14:00-04:00"):
            with pytest.raises(CalendarError, match="occurrence_not_found"):
                svc.update_event(
                    cid, eid, before[1], scope="occurrence", occurrence_id=rid, summary="Bad"
                )
        assert svc.store.get(cid, eid) == before

    def test_rdate_occurrence_edit(self, accounts):
        obj = series()
        obj.walk("VEVENT")[0].add("RDATE", datetime(2026, 9, 9, 14, tzinfo=NY))
        svc, cid, eid = install(accounts, obj)
        rid = "2026-09-09T14:00-04:00"
        svc.update_event(
            cid,
            eid,
            read(svc, cid, eid, rid)["etag"],
            scope="occurrence",
            occurrence_id=rid,
            start="2026-09-09T16:00-04:00",
        )
        assert len(listed(svc, cid)) == 6
        assert read(svc, cid, eid, rid)["start"] == "2026-09-09T16:00:00-04:00"

    async def test_mcp_scopes_and_occurrence_workflow(self, accounts):
        svc, cid, eid = install(accounts, series())
        rid = listed(svc, cid)[1]["occurrence_id"]
        async with Client(build_server(svc.config, svc)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["update_event"].input_schema["properties"]["scope"]["enum"] == [
                "event",
                "occurrence",
                "series",
                "future",
            ]
            assert not tools["update_event"].annotations.read_only_hint
            assert tools["get_event"].annotations.read_only_hint
            current = await client.call_tool(
                "get_event", {"calendar_id": cid, "event_id": eid, "occurrence_id": rid}
            )
            assert current.structured_content["representation"] == "occurrence"
            base = {
                "calendar_id": cid,
                "event_id": eid,
                "etag": current.structured_content["event"]["etag"],
            }
            wrong = await client.call_tool("update_event", {**base, "summary": "Bad"})
            assert wrong.is_error and "recurrence_scope_required" in str(wrong)
            moved = await client.call_tool(
                "update_event",
                {
                    **base,
                    "scope": "occurrence",
                    "occurrence_id": rid,
                    "start": "2026-09-08T16:00-04:00",
                },
            )
            assert not moved.is_error
            deleted = await client.call_tool(
                "delete_event",
                {
                    **base,
                    "etag": moved.structured_content["event"]["etag"],
                    "scope": "occurrence",
                    "occurrence_id": rid,
                },
            )
            assert not deleted.is_error and not deleted.structured_content["series_deleted"]
