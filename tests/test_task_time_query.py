"""Focused differential experiment for a server-side VTODO time range."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from icalendar import Calendar, Todo

from radicale_mcp.client import CalDAVStore, checked_url, resource_id
from radicale_mcp.service import CalendarService

START = "2026-09-15T00:00:00-04:00"
END = "2026-09-18T00:00:00-04:00"
_DAV_RESPONSE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?response(?:\s|>)", re.IGNORECASE)


class ResourceStore:
    """Expose one immutable, already-fetched resource set to CalendarService."""

    def __init__(self, resources: Sequence[tuple[str, str, str]]) -> None:
        self.resources = list(resources)

    def task_resources(self, calendar_id: str | None) -> list[tuple[str, str, str]]:
        return list(self.resources)


@contextmanager
def aggregate_reports(store: CalDAVStore):
    """Record aggregate REPORT transfer data without retaining sensitive content."""
    original = store.dav.session.request
    totals = {"requests": 0, "response_bytes": 0, "response_resources": 0}

    def recorded(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        response = original(method, url, *args, **kwargs)
        if method.upper() == "REPORT":
            body = response.content
            if isinstance(body, str):
                body = body.encode()
            totals["requests"] += 1
            totals["response_bytes"] += len(body)
            totals["response_resources"] += len(_DAV_RESPONSE.findall(body))
        return response

    store.dav.session.request = recorded
    try:
        yield totals
    finally:
        store.dav.session.request = original


def _task(title: str, marker: str, **fields: Any) -> Todo:
    task = Todo()
    task.add("UID", fields.pop("UID", str(uuid4())))
    task.add("DTSTAMP", datetime(2026, 1, 1, tzinfo=UTC))
    task.add("CREATED", datetime(2026, 1, 1, tzinfo=UTC))
    task.add("SUMMARY", title)
    task.add("DESCRIPTION", marker)
    task.add("STATUS", fields.pop("STATUS", "NEEDS-ACTION"))
    for key, value in fields.items():
        task.add(key, value)
    return task


def _calendar(*tasks: Todo) -> str:
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//Task time query experiment//EN")
    for task in tasks:
        calendar.add_component(task)
    return calendar.to_ical().decode()


def _seed(store: CalDAVStore, calendar_url: str) -> dict[str, str]:
    marker = secrets.token_urlsafe(18)
    labels: dict[str, str] = {}

    def put(label: str, *tasks: Todo) -> None:
        labels[label] = str(tasks[0]["UID"])
        target = calendar_url + str(uuid4()) + ".ics"
        store.dav.request(
            target,
            "PUT",
            _calendar(*tasks),
            {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
        )

    put(
        "due_only",
        _task(secrets.token_urlsafe(12), marker, DUE=datetime(2026, 9, 15, 14, tzinfo=UTC)),
    )
    put(
        "start_only",
        _task(
            secrets.token_urlsafe(12),
            marker,
            DTSTART=datetime(2026, 9, 16, 14, tzinfo=UTC),
        ),
    )
    put("undated", _task(secrets.token_urlsafe(12), marker))
    put(
        "completed",
        _task(
            secrets.token_urlsafe(12),
            marker,
            DUE=datetime(2026, 9, 16, 15, tzinfo=UTC),
            COMPLETED=datetime(2026, 9, 16, 16, tzinfo=UTC),
            STATUS="COMPLETED",
            **{"PERCENT-COMPLETE": 100},
        ),
    )
    put(
        "cancelled",
        _task(
            secrets.token_urlsafe(12),
            marker,
            DUE=datetime(2026, 9, 17, 15, tzinfo=UTC),
            STATUS="CANCELLED",
        ),
    )
    put("all_day_boundary", _task(secrets.token_urlsafe(12), marker, DUE=date(2026, 9, 15)))

    recurring_uid = str(uuid4())
    master = _task(
        secrets.token_urlsafe(12),
        marker,
        UID=recurring_uid,
        DTSTART=datetime(2026, 9, 14, 13, tzinfo=UTC),
        DUE=datetime(2026, 9, 15, 13, tzinfo=UTC),
        RRULE={"FREQ": "DAILY", "COUNT": 4},
    )
    override = _task(
        secrets.token_urlsafe(12),
        marker,
        UID=recurring_uid,
        DTSTART=datetime(2026, 9, 15, 15, tzinfo=UTC),
        DUE=datetime(2026, 9, 16, 16, tzinfo=UTC),
        **{"RECURRENCE-ID": datetime(2026, 9, 15, 13, tzinfo=UTC)},
    )
    put("recurring", master, override)
    labels["marker"] = marker
    return labels


def _narrow_resources(
    store: CalDAVStore, calendar_id: str, start: datetime, end: datetime
) -> list[tuple[str, str, str]]:
    result = []
    for info, calendar in store.select(calendar_id):
        resources = calendar.search(
            todo=True,
            include_completed=True,
            start=start,
            end=end,
            expand=False,
            post_filter=False,
        )
        for resource in resources:
            url = checked_url(str(resource.url), info.url)
            result.append((info.calendar_id, resource_id(url, info.url, "t"), resource.data))
    return result


def _identity(result: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(task["task_id"], task["occurrence_id"]) for task in result["tasks"]]


def _measure(
    store: CalDAVStore, operation: Callable[[], list[tuple[str, str, str]]]
) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    with aggregate_reports(store) as totals:
        resources = operation()
    return resources, totals


def test_narrow_vtodo_time_query_is_not_a_complete_superset(accounts):
    service, calendar_id, calendar = accounts["alice"]
    labels = _seed(service.store, str(calendar.url).rstrip("/") + "/")

    full_resources, full_transfer = _measure(
        service.store, lambda: service.store.task_resources(calendar_id)
    )
    start = datetime.fromisoformat(START).astimezone(UTC)
    end = datetime.fromisoformat(END).astimezone(UTC)
    narrow_resources, narrow_transfer = _measure(
        service.store,
        lambda: _narrow_resources(service.store, calendar_id, start, end),
    )

    full = CalendarService(service.config, ResourceStore(full_resources))  # type: ignore[arg-type]
    narrow = CalendarService(service.config, ResourceStore(narrow_resources))  # type: ignore[arg-type]

    full_default = full.list_tasks(calendar_id, state="all", start=START, end=END)
    narrow_default = narrow.list_tasks(calendar_id, state="all", start=START, end=END)
    full_dated = full.list_tasks(
        calendar_id, state="all", start=START, end=END, include_undated=False
    )
    narrow_dated = narrow.list_tasks(
        calendar_id, state="all", start=START, end=END, include_undated=False
    )
    full_open = full.list_tasks(
        calendar_id, state="open", start=START, end=END, include_undated=False
    )
    narrow_open = narrow.list_tasks(
        calendar_id, state="open", start=START, end=END, include_undated=False
    )
    full_completed = full.list_tasks(
        calendar_id, state="completed", start=START, end=END, include_undated=False
    )
    narrow_completed = narrow.list_tasks(
        calendar_id, state="completed", start=START, end=END, include_undated=False
    )
    full_search = full.search_tasks(
        labels["marker"], calendar_id, state="all", start=START, end=END
    )
    narrow_search = narrow.search_tasks(
        labels["marker"], calendar_id, state="all", start=START, end=END
    )
    full_page = full.list_tasks(
        calendar_id,
        state="all",
        start=START,
        end=END,
        include_undated=False,
        limit=2,
        offset=1,
    )
    narrow_page = narrow.list_tasks(
        calendar_id,
        state="all",
        start=START,
        end=END,
        include_undated=False,
        limit=2,
        offset=1,
    )

    full_ids = set(_identity(full_default))
    narrow_ids = set(_identity(narrow_default))
    full_order = _identity(full_dated)
    narrow_order = _identity(narrow_dated)
    full_dated_uids = {task["uid"] for task in full_dated["tasks"]}
    narrow_default_uids = {task["uid"] for task in narrow_default["tasks"]}
    full_open_uids = {task["uid"] for task in full_open["tasks"]}
    recurring_full = [task for task in full_dated["tasks"] if task["uid"] == labels["recurring"]]
    recurring_narrow = [
        task for task in narrow_dated["tasks"] if task["uid"] == labels["recurring"]
    ]

    semantic = {
        "complete_superset": full_ids <= narrow_ids,
        "default_include_undated_exact": full_default == narrow_default,
        "undated_item_preserved": labels["undated"] in narrow_default_uids,
        "dated_identity_fields_order_totals_exact": full_dated == narrow_dated,
        "common_relative_order_preserved": [item for item in full_order if item in narrow_ids]
        == narrow_order,
        "open_state_exact": full_open == narrow_open,
        "completed_state_exact": full_completed == narrow_completed,
        "text_search_default_exact": full_search == narrow_search,
        "pagination_order_total_exact": full_page == narrow_page,
        "due_only_included": labels["due_only"] in full_dated_uids,
        "dtstart_only_included": labels["start_only"] in full_dated_uids,
        "all_day_boundary_exact": (
            labels["all_day_boundary"] in full_dated_uids
            and labels["all_day_boundary"] in {task["uid"] for task in narrow_dated["tasks"]}
        ),
        "cancelled_in_all_excluded_from_open": (
            labels["cancelled"] in full_dated_uids and labels["cancelled"] not in full_open_uids
        ),
        "recurrence_override_exact": recurring_full == recurring_narrow,
    }

    assert len(full_resources) == 7
    assert len(narrow_resources) == 6
    assert full_default["total"] == 9
    assert narrow_default["total"] == 8
    assert full_dated["total"] == 8
    assert narrow_dated["total"] == 7
    assert full_open["total"] == 6
    assert narrow_open["total"] == 5
    assert full_completed["total"] == narrow_completed["total"] == 1
    assert len(recurring_full) == len(recurring_narrow) == 3
    assert not semantic["complete_superset"]
    assert not semantic["default_include_undated_exact"]
    assert not semantic["dated_identity_fields_order_totals_exact"]
    assert not semantic["open_state_exact"]
    assert not semantic["text_search_default_exact"]
    assert not semantic["pagination_order_total_exact"]
    assert not semantic["all_day_boundary_exact"]
    assert semantic["undated_item_preserved"]
    assert semantic["common_relative_order_preserved"]
    assert semantic["completed_state_exact"]
    assert semantic["due_only_included"]
    assert semantic["dtstart_only_included"]
    assert semantic["cancelled_in_all_excluded_from_open"]
    assert semantic["recurrence_override_exact"]

    byte_reduction = (
        100
        * (full_transfer["response_bytes"] - narrow_transfer["response_bytes"])
        / full_transfer["response_bytes"]
    )
    resource_reduction = (
        100
        * (full_transfer["response_resources"] - narrow_transfer["response_resources"])
        / full_transfer["response_resources"]
    )
    report = {
        "candidate": {
            "component": "VTODO",
            "include_completed": True,
            "expand": False,
            "post_filter": False,
            "range_padding_days_each_side": 0,
        },
        "resource_counts": {"full_scan": len(full_resources), "narrow": len(narrow_resources)},
        "result_totals": {
            "default_full_scan": full_default["total"],
            "default_narrow": narrow_default["total"],
            "dated_full_scan": full_dated["total"],
            "dated_narrow": narrow_dated["total"],
            "open_full_scan": full_open["total"],
            "open_narrow": narrow_open["total"],
            "completed_each": full_completed["total"],
            "recurring_occurrences_each": len(recurring_full),
            "page_items_each": len(full_page["tasks"]),
            "page_total_full_scan": full_page["total"],
            "page_total_narrow": narrow_page["total"],
        },
        "semantic": semantic,
        "transfer": {
            "full_scan": full_transfer,
            "narrow": narrow_transfer,
            "response_byte_reduction_percent": round(byte_reduction, 2),
            "response_resource_reduction_percent": round(resource_reduction, 2),
        },
        "decision": "NO-GO",
    }
    print("task_time_query_experiment=" + json.dumps(report, sort_keys=True))
