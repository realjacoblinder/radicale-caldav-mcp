"""Focused differential experiment for narrower VEVENT REPORT padding."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event

from radicale_mcp.client import CalDAVStore, checked_url, resource_id
from radicale_mcp.service import CalendarService
from radicale_mcp.timeutils import bounded_range

pytestmark = pytest.mark.integration

START = "2026-03-07T00:00:00-05:00"
END = "2026-03-10T00:00:00-04:00"
_DAV_RESPONSE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?response(?:\s|>)", re.IGNORECASE)
_NY = ZoneInfo("America/New_York")


class ResourceStore:
    """Expose one immutable, already-fetched resource set to CalendarService."""

    def __init__(self, resources: Sequence[tuple[str, str, str]]) -> None:
        self.resources_snapshot = list(resources)

    def resources(
        self, calendar_id: str | None, start: datetime, end: datetime
    ) -> list[tuple[str, str, str]]:
        return list(self.resources_snapshot)


@contextmanager
def aggregate_reports(store: CalDAVStore):
    """Record only aggregate REPORT transfer facts, never request or response content."""
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


def _event(**fields: Any) -> Event:
    event = Event()
    event.add("UID", fields.pop("UID", str(uuid4())))
    event.add("DTSTAMP", datetime(2026, 1, 1, tzinfo=UTC))
    event.add("SUMMARY", fields.pop("SUMMARY", secrets.token_urlsafe(12)))
    event.add("DESCRIPTION", fields.pop("DESCRIPTION"))
    for key, value in fields.items():
        event.add(key, value)
    return event


def _calendar(*events: Event) -> str:
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//Event report padding experiment//EN")
    for event in events:
        calendar.add_component(event)
    return calendar.to_ical().decode()


def _seed(store: CalDAVStore, calendar_url: str) -> str:
    marker = secrets.token_urlsafe(18)

    def put(*events: Event) -> None:
        target = calendar_url + str(uuid4()) + ".ics"
        store.dav.request(
            target,
            "PUT",
            _calendar(*events),
            {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
        )

    # Exact query-boundary cases: local all-day and floating values.
    put(_event(DESCRIPTION=marker, DTSTART=date(2026, 3, 7), DTEND=date(2026, 3, 8)))
    put(
        _event(
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 9, 23, 30),
            DTEND=datetime(2026, 3, 10, 0, 30),
        )
    )

    # A valid local time immediately after the DST spring-forward boundary.
    put(
        _event(
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 8, 3, 15, tzinfo=_NY),
            DTEND=datetime(2026, 3, 8, 4, 15, tzinfo=_NY),
        )
    )

    # Starts before the requested window but legitimately overlaps it.
    put(
        _event(
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 5, 12, tzinfo=_NY),
            DTEND=datetime(2026, 3, 7, 1, tzinfo=_NY),
        )
    )

    recurring_uid = str(uuid4())
    put(
        _event(
            UID=recurring_uid,
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 6, 9, tzinfo=_NY),
            DTEND=datetime(2026, 3, 6, 10, tzinfo=_NY),
            RRULE={"FREQ": "DAILY", "COUNT": 5},
        ),
        _event(
            UID=recurring_uid,
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 8, 11, tzinfo=_NY),
            DTEND=datetime(2026, 3, 8, 12, tzinfo=_NY),
            **{"RECURRENCE-ID": datetime(2026, 3, 8, 9, tzinfo=_NY)},
        ),
    )

    # Transfer-only controls: inside the current ±2-day REPORT, outside ±1 day and
    # outside the authoritative result window.
    put(
        _event(
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 5, 12, tzinfo=UTC),
            DTEND=datetime(2026, 3, 5, 13, tzinfo=UTC),
        )
    )
    put(
        _event(
            DESCRIPTION=marker,
            DTSTART=datetime(2026, 3, 11, 12, tzinfo=UTC),
            DTEND=datetime(2026, 3, 11, 13, tzinfo=UTC),
        )
    )
    return marker


def _reference_resources(
    store: CalDAVStore, calendar_id: str, start: datetime, end: datetime
) -> list[tuple[str, str, str]]:
    result = []
    for info, calendar in store.select(calendar_id):
        resources = calendar.search(
            event=True,
            start=start - timedelta(days=2),
            end=end + timedelta(days=2),
            expand=False,
            post_filter=False,
        )
        for resource in resources:
            url = checked_url(str(resource.url), info.url)
            result.append((info.calendar_id, resource_id(url, info.url), resource.data))
    return result


def _measure(
    store: CalDAVStore, operation: Callable[[], list[tuple[str, str, str]]]
) -> tuple[list[tuple[str, str, str]], dict[str, int | float]]:
    started = time.perf_counter()
    with aggregate_reports(store) as totals:
        resources = operation()
    metrics: dict[str, int | float] = dict(totals)
    metrics["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return resources, metrics


def _identities(result: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(event["event_id"], event["occurrence_id"]) for event in result["events"]]


def test_one_day_event_report_padding_is_equivalent_and_reduces_transfer(accounts, capsys):
    service, calendar_id, calendar = accounts["alice"]
    marker = _seed(service.store, str(calendar.url).rstrip("/") + "/")
    start, end = bounded_range(START, END, service.tz, service.config.max_range_days)

    current_resources, current_transfer = _measure(
        service.store,
        lambda: _reference_resources(service.store, calendar_id, start, end),
    )
    candidate_resources, candidate_transfer = _measure(
        service.store,
        lambda: service.store.resources(calendar_id, start, end),
    )

    current = CalendarService(service.config, ResourceStore(current_resources))  # type: ignore[arg-type]
    candidate = CalendarService(  # type: ignore[arg-type]
        service.config, ResourceStore(candidate_resources)
    )

    current_list = current.list_events(START, END, calendar_id)
    candidate_list = candidate.list_events(START, END, calendar_id)
    current_page_1 = current.list_events(START, END, calendar_id, limit=2)
    candidate_page_1 = candidate.list_events(START, END, calendar_id, limit=2)
    current_page_2 = current.list_events(START, END, calendar_id, limit=2, offset=2)
    candidate_page_2 = candidate.list_events(START, END, calendar_id, limit=2, offset=2)
    current_search = current.search_events(marker, START, END, calendar_id)
    candidate_search = candidate.search_events(marker, START, END, calendar_id)
    current_busy = current.get_free_busy(START, END, calendar_id)
    candidate_busy = candidate.get_free_busy(START, END, calendar_id)
    current_slots = current.find_available_slots(
        START, END, 30, calendar_id, "08:00", "18:00", [5, 6, 0], "America/New_York"
    )
    candidate_slots = candidate.find_available_slots(
        START, END, 30, calendar_id, "08:00", "18:00", [5, 6, 0], "America/New_York"
    )

    equivalence = {
        "returned_occurrences_exact": current_list == candidate_list,
        "identity_and_order_exact": _identities(current_list) == _identities(candidate_list),
        "total_exact": current_list["total"] == candidate_list["total"],
        "first_page_exact": current_page_1 == candidate_page_1,
        "second_page_exact": current_page_2 == candidate_page_2,
        "search_exact": current_search == candidate_search,
        "free_busy_exact": current_busy == candidate_busy,
        "slots_exact": current_slots == candidate_slots,
    }
    bytes_reduction = 1 - (
        candidate_transfer["response_bytes"] / current_transfer["response_bytes"]
    )
    resources_reduction = 1 - (
        candidate_transfer["response_resources"] / current_transfer["response_resources"]
    )
    summary = {
        "candidate_bounds": {"before_days": 1, "after_days": 1},
        "equivalence": equivalence,
        "current": current_transfer,
        "candidate": candidate_transfer,
        "response_bytes_reduction_percent": round(bytes_reduction * 100, 3),
        "response_resources_reduction_percent": round(resources_reduction * 100, 3),
    }

    assert all(equivalence.values())
    assert current_list["total"] >= 7
    assert current_transfer["requests"] == candidate_transfer["requests"] == 1
    assert resources_reduction >= 0.25
    with capsys.disabled():
        print(json.dumps(summary, sort_keys=True))
