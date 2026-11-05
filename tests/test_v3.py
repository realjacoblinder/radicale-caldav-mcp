"""V3 creation, boundary splitting, and failure recovery against real Radicale."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from mcp import Client
from test_recurrence import NY, WINDOW, install, listed, raw, read, series

from radicale_mcp.errors import CalendarError
from radicale_mcp.events import master_event
from radicale_mcp.server import build_server


def update_future(svc, cid, eid, rid="2026-09-15T14:00:00-04:00", **changes):
    return svc.update_event(
        cid, eid, read(svc, cid, eid)["etag"], scope="future", occurrence_id=rid, **changes
    )


@pytest.mark.parametrize(
    "start,all_day,rule,expected",
    [
        ("2026-09-01T14:00-04:00", False, "FREQ=WEEKLY;COUNT=5", 5),
        ("2026-09-01", True, "FREQ=DAILY;COUNT=3", 3),
        ("2026-09-01T14:00-04:00", False, "FREQ=WEEKLY;UNTIL=20260915T180000Z", 3),
        ("2026-09-01T14:00-04:00", False, "FREQ=WEEKLY", 5),
    ],
)
def test_create_recurring(accounts, start, all_day, rule, expected):
    svc, cid, _ = accounts["alice"]
    result = svc.create_event(
        cid,
        "Created series",
        start,
        all_day=all_day,
        duration_minutes=None if all_day else 60,
        recurrence_rule=rule,
    )["event"]
    assert result["recurrence"]["is_recurring"]
    assert result["etag"] and result["uid"]
    assert len(listed(svc, cid)) == expected
    assert all(e["occurrence_id"] for e in listed(svc, cid))
    assert svc.get_free_busy(*WINDOW, calendar_id=cid)["busy"]


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=WEEKLY;COUNT=0",
        "FREQ=WEEKLY;COUNT=3;UNTIL=20261001T000000Z",
        "FREQ=WEEKLY;BYDAY=WE",
        "FREQ=WEEKLY;BYDAY=1TU",
        "FREQ=DAILY;BYMONTH=13",
        "FREQ=DAILY;FREQ=WEEKLY",
        "FREQ=DAILY;COUNT=3;COUNT=4",
        "FREQ=WEEKLY;BYMONTHDAY=1",
        "FREQ=MONTHLY;BYSETPOS=1",
        "RRULE:FREQ=DAILY",
        "FREQ=DAILY;UNTIL=20260930",
        "FREQ=HOURLY;COUNT=2",
    ],
)
def test_bad_creation_is_no_write(accounts, rule):
    svc, cid, _ = accounts["alice"]
    with pytest.raises(CalendarError):
        svc.create_event(
            cid, "Bad", "2026-09-01T14:00-04:00", duration_minutes=60, recurrence_rule=rule
        )
    assert listed(svc, cid) == []


def test_future_move_count_and_preservation(accounts):
    svc, cid, eid = install(accounts, series())
    old = listed(svc, cid)
    result = update_future(svc, cid, eid, start="2026-09-15T15:00-04:00", summary="Later")
    assert result["split"] and result["event"]["event_id"] != eid
    new_id = result["event"]["event_id"]
    assert result["event"]["uid"] != old[0]["uid"]
    events = listed(svc, cid)
    assert [(e["start"], e["summary"]) for e in events[:2]] == [
        (e["start"], e["summary"]) for e in old[:2]
    ]
    assert all(e["start"][11:16] == "15:00" and e["summary"] == "Later" for e in events[2:])
    assert len(events) == 5
    new = raw(svc, cid, new_id)
    assert master_event(new)["RRULE"]["COUNT"] == [3]
    assert master_event(raw(svc, cid, eid))["RRULE"]["COUNT"] == [2]
    assert str(master_event(new)["X-CUSTOM"]) == "Keep this"
    assert "ATTENDEE" in master_event(new) and len(new.walk("VALARM")) == 1
    # Subsequent occurrence edits on the successor remain supported.
    item = events[3]
    current = read(svc, cid, new_id, item["occurrence_id"])
    svc.update_event(
        cid,
        new_id,
        current["etag"],
        scope="occurrence",
        occurrence_id=item["occurrence_id"],
        location="Elsewhere",
    )
    assert read(svc, cid, new_id, item["occurrence_id"])["location"] == "Elsewhere"


def test_future_explicit_rule_new_count(accounts):
    svc, cid, eid = install(accounts, series())
    result = update_future(
        svc,
        cid,
        eid,
        start="2026-09-16T15:00-04:00",
        recurrence_rule="FREQ=WEEKLY;BYDAY=WE;COUNT=2",
    )
    assert len(listed(svc, cid)) == 4
    assert result["event"]["recurrence"]["rrule"] == ["FREQ=WEEKLY;COUNT=2;BYDAY=WE"]


@pytest.mark.parametrize("rule", ["FREQ=WEEKLY", "FREQ=WEEKLY;UNTIL=20260929T180000Z"])
def test_future_original_until_or_infinite(accounts, rule):
    svc, cid, eid = install(accounts, series(rule=rule))
    result = update_future(svc, cid, eid, summary="New title")
    assert result["event"]["recurrence"]["rrule"] == [rule]
    assert len(listed(svc, cid)) == 5


@pytest.mark.parametrize(
    "all_day,floating,rid,move",
    [
        (True, False, "2026-09-15", "2026-09-16"),
        (False, True, "2026-09-15T14:00-04:00", "2026-09-15T16:00-04:00"),
    ],
)
def test_future_date_and_floating(accounts, all_day, floating, rid, move):
    svc, cid, eid = install(accounts, series(all_day=all_day, floating=floating))
    result = update_future(svc, cid, eid, rid, start=move)
    events = listed(svc, cid)
    assert len(events) == 5
    assert all(e["all_day"] == all_day and e["floating"] == floating for e in events)
    anchor = master_event(raw(svc, cid, result["event"]["event_id"])).decoded("DTSTART")
    assert (not isinstance(anchor, datetime)) if all_day else anchor.tzinfo is None


def test_future_dst(accounts):
    svc, cid, eid = install(
        accounts, series(start=datetime(2026, 3, 1, 9, tzinfo=NY), rule="FREQ=WEEKLY;COUNT=4")
    )
    result = update_future(svc, cid, eid, "2026-03-08T09:00-04:00", start="2026-03-08T10:00-04:00")
    events = listed(svc, cid, ("2026-03-01T00:00-05:00", "2026-03-30T00:00-04:00"))
    assert [e["start"] for e in events] == [
        "2026-03-01T09:00:00-05:00",
        "2026-03-08T10:00:00-04:00",
        "2026-03-15T10:00:00-04:00",
        "2026-03-22T10:00:00-04:00",
    ]
    assert result["split"]


def test_future_first_single_write(accounts, monkeypatch):
    svc, cid, eid = install(accounts, series())
    writes = []
    original = svc.store.write

    def spy(*args, **kwargs):
        writes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(svc.store, "write", spy)
    result = update_future(svc, cid, eid, "2026-09-01T14:00-04:00", summary="All")
    assert not result["split"] and result["event"]["event_id"] == eid
    assert len(writes) == 1 and len(listed(svc, cid)) == 5


def test_future_text_preserves_partitioned_exceptions(accounts):
    obj = series()
    master_event(obj).add("EXDATE", datetime(2026, 9, 8, 14, tzinfo=NY))
    master_event(obj).add("EXDATE", datetime(2026, 9, 29, 14, tzinfo=NY))
    override = deepcopy(master_event(obj))
    for p in ("RRULE", "EXDATE"):
        override.pop(p, None)
    override.add("RECURRENCE-ID", datetime(2026, 9, 22, 14, tzinfo=NY))
    override.pop("DTSTART")
    override.add("DTSTART", datetime(2026, 9, 23, 16, tzinfo=NY))
    override.pop("DTEND")
    override.add("DTEND", datetime(2026, 9, 23, 17, tzinfo=NY))
    obj.add_component(override)
    svc, cid, eid = install(accounts, obj)
    result = update_future(svc, cid, eid, summary="From now")
    events = listed(svc, cid)
    assert [e["summary"] for e in events] == ["Weekly appointment", "From now", "From now"]
    assert events[-1]["start"] == "2026-09-23T16:00:00-04:00"
    assert master_event(raw(svc, cid, result["event"]["event_id"]))["RRULE"]["COUNT"] == [3]


def test_future_schedule_allows_prior_exceptions_but_refuses_future(accounts):
    obj = series()
    master_event(obj).add("EXDATE", datetime(2026, 9, 8, 14, tzinfo=NY))
    svc, cid, eid = install(accounts, obj)
    result = update_future(svc, cid, eid, start="2026-09-15T15:00-04:00")
    assert result["split"] and len(listed(svc, cid)) == 4
    obj = series()
    master_event(obj).add("EXDATE", datetime(2026, 9, 22, 14, tzinfo=NY))
    svc, cid, eid = install(accounts, obj)
    before = svc.store.get(cid, eid)
    with pytest.raises(CalendarError, match="future overrides"):
        update_future(svc, cid, eid, start="2026-09-15T15:00-04:00")
    assert svc.store.get(cid, eid) == before


def test_future_delete(accounts):
    svc, cid, eid = install(accounts, series())
    result = svc.delete_event(
        cid,
        eid,
        read(svc, cid, eid)["etag"],
        scope="future",
        occurrence_id="2026-09-15T14:00-04:00",
    )
    assert not result["series_deleted"] and len(listed(svc, cid)) == 2
    assert svc.get_free_busy("2026-09-15T00:00Z", "2026-09-30T00:00Z", cid)["busy"] == []
    result = svc.delete_event(
        cid,
        eid,
        read(svc, cid, eid)["etag"],
        scope="future",
        occurrence_id="2026-09-01T14:00-04:00",
    )
    assert result["series_deleted"] and listed(svc, cid) == []


@pytest.mark.parametrize("rid", ["2026-09-16T14:00-04:00", "2026-10-06T14:00-04:00"])
def test_future_missing_boundary_no_write(accounts, rid):
    svc, cid, eid = install(accounts, series())
    original = svc.store.get(cid, eid)
    with pytest.raises(CalendarError, match="occurrence_not_found"):
        update_future(svc, cid, eid, rid, summary="Wrong")
    assert svc.store.get(cid, eid) == original


def test_future_conflict_rolls_back_only_successor(accounts, monkeypatch):
    svc, cid, eid = install(accounts, series())
    original_write = svc.store.write

    def conflict(calendar_id, target, data, etag=None):
        if target == eid:
            current, token = svc.store.get(cid, eid)
            original_write(
                cid, eid, current.replace("Weekly appointment", "Changed remotely"), token
            )
        return original_write(calendar_id, target, data, etag)

    monkeypatch.setattr(svc.store, "write", conflict)
    with pytest.raises(CalendarError, match="stale_etag"):
        update_future(svc, cid, eid, summary="Future")
    assert len(listed(svc, cid)) == 5
    assert len(svc.store.select(cid)[0][1].events()) == 1


@pytest.mark.parametrize(
    "stage,committed",
    [("create", False), ("create", True), ("truncate", False), ("truncate", True)],
)
def test_uncertain_split_identifies_both_resources(accounts, monkeypatch, stage, committed):
    svc, cid, eid = install(accounts, series())
    original_write = svc.store.write

    def uncertain(calendar_id, target, data, etag=None):
        fail = (target == eid) == (stage == "truncate")
        if not fail or committed:
            result = original_write(calendar_id, target, data, etag)
        if fail:
            raise CalendarError("unreachable", "Lost response")
        return result

    monkeypatch.setattr(svc.store, "write", uncertain)
    with pytest.raises(CalendarError, match="split_recovery_required") as error:
        update_future(svc, cid, eid, summary="Future")
    assert eid in str(error.value) and "successor_event_id" in str(error.value)
    assert svc.config.password not in str(error.value)
    # The old series is never deleted; uncertain writes do not trigger blind rollback.
    assert read(svc, cid, eid)["uid"]
    if stage == "truncate" and committed:
        assert len(listed(svc, cid)) == 5


def test_future_history_limit(accounts):
    svc, cid, eid = install(accounts, series(rule="FREQ=WEEKLY"))
    svc.config = replace(svc.config, max_events=1)
    with pytest.raises(CalendarError, match="limit_exceeded"):
        update_future(svc, cid, eid, summary="Future")


async def test_v3_mcp_workflow(accounts):
    svc, cid, _ = accounts["alice"]
    async with Client(build_server(svc.config, svc)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "recurrence_rule" in tools["create_event"].input_schema["properties"]
        assert "alarms" in tools["create_event"].input_schema["properties"]
        assert "alarms" in tools["update_event"].input_schema["properties"]
        alarm_schema = tools["create_event"].input_schema["properties"]["alarms"]
        assert "array" in str(alarm_schema)
        assert "future" in tools["update_event"].input_schema["properties"]["scope"]["enum"]
        assert not tools["update_event"].annotations.idempotent_hint
        created = await client.call_tool(
            "create_event",
            {
                "calendar_id": cid,
                "summary": "MCP weekly",
                "start": "2026-09-01T14:00-04:00",
                "duration_minutes": 60,
                "recurrence_rule": "FREQ=WEEKLY;COUNT=5",
                "alarms": [{"trigger": "-PT15M", "description": "From MCP"}],
            },
        )
        assert not created.is_error
        e = created.structured_content["event"]
        assert e["alarms"][0]["description"] == "From MCP"
        updated = await client.call_tool(
            "update_event",
            {
                "calendar_id": cid,
                "event_id": e["event_id"],
                "etag": e["etag"],
                "scope": "future",
                "occurrence_id": "2026-09-15T14:00-04:00",
                "summary": "MCP future",
                "alarms": [{"trigger": "-PT30M", "related": "END"}],
            },
        )
        assert not updated.is_error and updated.structured_content["split"]
        assert updated.structured_content["event"]["alarms"][0]["trigger"] == "-PT30M"


@pytest.mark.parametrize(
    "start,rule,rid,expected",
    [
        (date(2026, 1, 31), "FREQ=MONTHLY;COUNT=5", "2026-05-31", 2),
        (date(2024, 2, 29), "FREQ=YEARLY;COUNT=3", "2028-02-29", 1),
        (date(2026, 9, 1), "FREQ=WEEKLY;BYDAY=TU,TH;INTERVAL=2;COUNT=6", "2026-09-17", 3),
        (date(2026, 9, 1), "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=1;COUNT=5", "2026-11-03", 2),
    ],
)
def test_split_complex_calendar_math(start, rule, rid, expected):
    from radicale_mcp.splitting import split_calendar

    obj = series(start=start, all_day=True, rule=rule)
    past, future, before_count = split_calendar(obj, rid, NY, 5000)
    assert before_count == expected
    assert master_event(past)["RRULE"]["COUNT"] == [expected]
    assert master_event(future).decoded("DTSTART") == date.fromisoformat(rid)
    assert (
        int(master_event(future)["RRULE"]["COUNT"][0])
        == int(master_event(obj)["RRULE"]["COUNT"][0]) - expected
    )


def test_remote_v3_through_subpath(accounts, radicale):
    from test_remote_transport import subpath_proxy

    from radicale_mcp.service import CalendarService

    original, _, _ = accounts["alice"]
    with subpath_proxy(radicale["url"]) as url:
        svc = CalendarService(replace(original.config, url=url))
        try:
            cid = svc.list_calendars()["calendars"][0]["calendar_id"]
            event = svc.create_event(
                cid,
                "Remote recurring",
                "2026-09-01T14:00-04:00",
                duration_minutes=60,
                recurrence_rule="FREQ=WEEKLY;COUNT=5",
            )["event"]
            result = update_future(svc, cid, event["event_id"], duration_minutes=90)
            assert result["split"] and len(listed(svc, cid)) == 5
            busy = svc.get_free_busy(*WINDOW, calendar_id=cid)["busy"]
            assert [b["duration_minutes"] for b in busy] == [60, 60, 90, 90, 90]
        finally:
            svc.store.close()


def test_rollback_refuses_remotely_changed_successor(accounts, monkeypatch):
    svc, cid, eid = install(accounts, series())
    original_write = svc.store.write
    successor = []

    def race(calendar_id, target, data, etag=None):
        if target == eid:
            current, token = svc.store.get(cid, successor[0])
            original_write(cid, successor[0], current.replace("Future", "Other client"), token)
            raise CalendarError("stale_etag", "Original changed")
        successor.append(target)
        return original_write(calendar_id, target, data, etag)

    monkeypatch.setattr(svc.store, "write", race)
    with pytest.raises(CalendarError, match="rollback_successor"):
        update_future(svc, cid, eid, summary="Future")
    assert read(svc, cid, successor[0])["summary"] == "Other client"
    assert len(listed(svc, cid)) == 8


def test_identical_retry_after_uncertain_create_does_not_duplicate(accounts, monkeypatch):
    svc, cid, eid = install(accounts, series())
    original_write = svc.store.write

    def lost(calendar_id, target, data, etag=None):
        original_write(calendar_id, target, data, etag)
        raise CalendarError("unreachable", "Lost response")

    monkeypatch.setattr(svc.store, "write", lost)
    with pytest.raises(CalendarError, match="split_recovery_required"):
        update_future(svc, cid, eid, summary="Future")
    monkeypatch.setattr(svc.store, "write", original_write)
    with pytest.raises(CalendarError, match="split_recovery_required"):
        update_future(svc, cid, eid, summary="Future")
    assert len(svc.store.select(cid)[0][1].events()) == 2
    assert len(listed(svc, cid)) == 8


def test_missing_successor_etag_leaves_original_unchanged(accounts, monkeypatch):
    svc, cid, eid = install(accounts, series())
    original = svc.store.get(cid, eid)
    write = svc.store.write

    def missing(*args, **kwargs):
        write(*args, **kwargs)
        return None

    monkeypatch.setattr(svc.store, "write", missing)
    with pytest.raises(CalendarError, match="successor_missing_strong_etag"):
        update_future(svc, cid, eid, summary="Future")
    assert svc.store.get(cid, eid) == original


def test_future_stale_preflight_no_successor(accounts):
    svc, cid, eid = install(accounts, series())
    old = read(svc, cid, eid)["etag"]
    svc.update_event(cid, eid, old, scope="series", description="Changed elsewhere")
    with pytest.raises(CalendarError, match="stale_etag"):
        svc.update_event(
            cid, eid, old, scope="future", occurrence_id="2026-09-15T14:00-04:00", summary="Future"
        )
    assert len(svc.store.select(cid)[0][1].events()) == 1


def test_future_uses_original_boundary_for_moved_override(accounts):
    svc, cid, eid = install(accounts, series())
    svc.update_event(
        cid,
        eid,
        read(svc, cid, eid)["etag"],
        scope="occurrence",
        occurrence_id="2026-09-15T14:00-04:00",
        start="2026-09-30T16:00-04:00",
    )
    result = update_future(svc, cid, eid, summary="After boundary")
    events = listed(svc, cid)
    assert len(events) == 5
    assert events[-1]["start"] == "2026-09-30T16:00:00-04:00"
    assert events[-1]["summary"] == "After boundary"
    assert len([e for e in events if e["event_id"] == result["event"]["event_id"]]) == 3


def test_future_prior_override_moved_after_boundary_is_preserved(accounts):
    svc, cid, eid = install(accounts, series())
    svc.update_event(
        cid,
        eid,
        read(svc, cid, eid)["etag"],
        scope="occurrence",
        occurrence_id="2026-09-08T14:00-04:00",
        start="2026-09-30T16:00-04:00",
    )
    result = update_future(svc, cid, eid, start="2026-09-15T15:00-04:00")
    events = listed(svc, cid)
    assert len(events) == 5 and result["split"]
    assert events[-1]["event_id"] == eid
    assert events[-1]["summary"] == "Weekly appointment"


def test_future_rdate_partition_and_excluded_boundary(accounts):
    obj = series()
    master_event(obj).add(
        "RDATE", [datetime(2026, 9, 2, 14, tzinfo=NY), datetime(2026, 9, 16, 14, tzinfo=NY)]
    )
    svc, cid, eid = install(accounts, obj)
    result = update_future(svc, cid, eid, summary="New")
    assert len(listed(svc, cid)) == 7
    assert len([e for e in listed(svc, cid) if e["event_id"] == eid]) == 3
    assert result["split"]
    obj = series()
    master_event(obj).add("EXDATE", datetime(2026, 9, 15, 14, tzinfo=NY))
    svc, cid, eid = install(accounts, obj)
    before = svc.store.get(cid, eid)
    with pytest.raises(CalendarError, match="occurrence_not_found"):
        update_future(svc, cid, eid, summary="No")
    assert svc.store.get(cid, eid) == before


def test_future_cannot_cross_identity_boundary(accounts):
    bob, cid, eid = install(accounts, series(), user="bob")
    alice, _, _ = accounts["alice"]
    with pytest.raises(CalendarError, match="calendar_not_found"):
        alice.update_event(
            cid,
            eid,
            read(bob, cid, eid)["etag"],
            scope="future",
            occurrence_id="2026-09-15T14:00-04:00",
            summary="No",
        )
    with pytest.raises(CalendarError, match="calendar_not_found"):
        alice.create_event(
            cid, "No", "2026-09-01T14:00-04:00", duration_minutes=60, recurrence_rule="FREQ=WEEKLY"
        )


def test_future_utc_boundary_equivalent_offset(accounts):
    svc, cid, eid = install(accounts, series(start=datetime(2026, 9, 1, 18, tzinfo=UTC)))
    result = update_future(svc, cid, eid, "2026-09-15T14:00-04:00", start="2026-09-15T19:00Z")
    assert result["event"]["start"] == "2026-09-15T19:00:00+00:00"
    assert len(listed(svc, cid)) == 5


@pytest.mark.parametrize(
    "all_day,rule,rid",
    [
        (True, "FREQ=WEEKLY;UNTIL=20260929", "2026-09-15"),
        (False, "FREQ=WEEKLY;UNTIL=20260929T140000", "2026-09-15T14:00-04:00"),
    ],
)
def test_future_until_date_and_floating(accounts, all_day, rule, rid):
    svc, cid, eid = install(accounts, series(all_day=all_day, floating=not all_day, rule=rule))
    result = update_future(svc, cid, eid, rid, summary="Later")
    assert result["event"]["recurrence"]["rrule"] == [rule]
    assert len(listed(svc, cid)) == 5


def test_future_boundary_rdate_only_is_explicitly_unsupported(accounts):
    obj = series()
    master_event(obj).add("RDATE", datetime(2026, 9, 16, 14, tzinfo=NY))
    svc, cid, eid = install(accounts, obj)
    before = svc.store.get(cid, eid)
    with pytest.raises(CalendarError, match="must be generated by the RRULE"):
        update_future(svc, cid, eid, "2026-09-16T14:00-04:00", summary="No")
    assert svc.store.get(cid, eid) == before


def test_future_nonexistent_dst_history_is_refused(accounts):
    svc, cid, eid = install(
        accounts, series(start=datetime(2026, 3, 1, 2, 30, tzinfo=NY), rule="FREQ=WEEKLY")
    )
    before = svc.store.get(cid, eid)
    with pytest.raises(CalendarError, match="DST transition"):
        update_future(svc, cid, eid, "2026-03-15T02:30-04:00", summary="No")
    assert svc.store.get(cid, eid) == before


def test_utc_creation_keeps_utc_cadence_across_dst(accounts):
    svc, cid, _ = accounts["alice"]
    created = svc.create_event(
        cid,
        "UTC weekly",
        "2026-03-01T14:00Z",
        timezone="UTC",
        duration_minutes=60,
        recurrence_rule="FREQ=WEEKLY;COUNT=4",
    )["event"]
    events = listed(svc, cid, ("2026-03-01T00:00Z", "2026-03-30T00:00Z"))
    assert len(events) == 4
    assert all(e["start"][11:] == "14:00:00+00:00" for e in events)
    result = update_future(
        svc, cid, created["event_id"], "2026-03-08T14:00Z", start="2026-03-08T15:00Z"
    )
    assert result["event"]["timezone"] == "UTC"
    events = listed(svc, cid, ("2026-03-01T00:00Z", "2026-03-30T00:00Z"))
    assert [e["start"][11:] for e in events] == ["14:00:00+00:00"] + ["15:00:00+00:00"] * 3
