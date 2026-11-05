"""Explicit recurrence mutation using RFC 5545 overrides and exclusions.

The recurrence library determines membership. This module never invents an
occurrence, expands a series into stored standalone events, or guesses scope.
"""

import re
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar, Event, vRecur

from .errors import CalendarError, invalid
from .events import check_recurring_span, event_span, expand, is_recurring, master_event
from .timeutils import as_aware, day, timestamp

MutationScope = Literal["event", "occurrence", "series", "future"]


def unsupported(message: str) -> CalendarError:
    return CalendarError("unsupported_recurrence_mutation", message)


def values(component: Event, key: str) -> list[Any]:
    value = component.get(key, [])
    return value if isinstance(value, list) else [value]


def key(value: date | datetime) -> str:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


def validate_scope(calendar: Calendar, scope: MutationScope, occurrence_id: str | None) -> None:
    if scope not in ("event", "occurrence", "series", "future"):
        raise invalid("scope must be event, occurrence, series, or future.")
    if is_recurring(calendar):
        if scope == "event":
            raise CalendarError(
                "recurrence_scope_required",
                "Choose scope='occurrence' or 'future' with occurrence_id, or scope='series'.",
            )
    elif scope != "event":
        raise invalid("This resource is not recurring; use scope='event'.")
    if (scope in ("occurrence", "future")) != (occurrence_id is not None):
        raise invalid("occurrence_id is required with scope='occurrence' or 'future' only.")


def validate_structure(calendar: Calendar) -> Event:
    events = cast(list[Event], calendar.walk("VEVENT"))
    masters = [e for e in events if "RECURRENCE-ID" not in e]
    if len(masters) != 1:
        raise unsupported("Recurring edits require exactly one series master.")
    master = masters[0]
    anchor = master.decoded("DTSTART")
    seen: set[str] = set()
    for event in events:
        if "EXRULE" in event or len(values(event, "RRULE")) > 1:
            raise unsupported("EXRULE and multiple RRULE properties are not supported for edits.")
        if "RECURRENCE-ID" not in event:
            continue
        if event["RECURRENCE-ID"].params.get("RANGE"):
            raise unsupported("This series contains a RANGE exception; use a calendar client.")
        if any(p in event for p in ("RRULE", "RDATE", "EXDATE")):
            raise unsupported("Nested recurrence rules on overrides are unsupported.")
        rid = event.decoded("RECURRENCE-ID")
        if isinstance(rid, datetime) != isinstance(anchor, datetime):
            raise unsupported("An override's recurrence identifier has an incompatible value type.")
        if isinstance(anchor, datetime) and (anchor.tzinfo is None) != (rid.tzinfo is None):
            raise unsupported("An override mixes floating and zoned recurrence identifiers.")
        if key(rid) in seen:
            raise unsupported(
                "Duplicate recurrence identifiers require repair in a calendar client."
            )
        seen.add(key(rid))
    for prop in values(master, "RDATE"):
        if any(isinstance(v.dt, tuple) for v in prop.dts):
            raise unsupported("RDATE periods are not supported for edits; use a calendar client.")
    return master


def parse_occurrence(value: str, master: Event, tz: ZoneInfo) -> date | datetime:
    anchor = master.decoded("DTSTART")
    if not isinstance(anchor, datetime):
        return day(value)
    # The output policy gives floating occurrences the configured offset. Convert it
    # back to a floating wall time for the stored RECURRENCE-ID/EXDATE value.
    parsed = timestamp(value, tz).astimezone(anchor.tzinfo or tz)
    return parsed.replace(tzinfo=None) if anchor.tzinfo is None else parsed


def is_excluded(master: Event, rid: date | datetime) -> bool:
    return any(key(value.dt) == key(rid) for prop in values(master, "EXDATE") for value in prop.dts)


def not_found() -> CalendarError:
    return CalendarError(
        "occurrence_not_found",
        "That occurrence is absent, excluded, or cancelled; list the series again.",
    )


def occurrence_event(
    calendar: Calendar, occurrence_id: str, tz: ZoneInfo, max_events: int
) -> Event:
    master = validate_structure(calendar)
    rid = parse_occurrence(occurrence_id, master, tz)
    if is_excluded(master, rid) or str(master.get("STATUS", "")).upper() == "CANCELLED":
        raise not_found()
    existing = [
        e
        for e in calendar.walk("VEVENT")
        if "RECURRENCE-ID" in e and key(e.decoded("RECURRENCE-ID")) == key(rid)
    ]
    if existing:
        event = cast(Event, deepcopy(existing[0]))
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            raise not_found()
        event_span(event, tz)
        return event
    instant = as_aware(rid, tz).astimezone(UTC)
    candidates = expand(
        calendar,
        "",
        "",
        tz,
        instant - timedelta(seconds=1),
        instant + timedelta(seconds=1),
        max_events,
    )
    selected = next(
        (
            e
            for e in candidates
            if e.occurrence_id is not None
            and key(parse_occurrence(e.occurrence_id, master, tz)) == key(rid)
        ),
        None,
    )
    if selected is None or selected.status.upper() == "CANCELLED":
        raise not_found()
    # Clone the entire master, retaining alarms, attendees and unknown properties.
    event = deepcopy(master)
    for name in ("RRULE", "RDATE", "EXDATE", "EXRULE", "DTSTART", "DTEND", "DURATION"):
        event.pop(name, None)
    for name, value in (("DTSTART", selected.start), ("DTEND", selected.end)):
        parsed = parse_occurrence(value, master, tz)
        event.add(name, parsed)
    event.add("RECURRENCE-ID", rid, parameters=deepcopy(master["DTSTART"].params))
    return event


def replace_occurrence(calendar: Calendar, event: Event) -> None:
    rid = event.decoded("RECURRENCE-ID")
    calendar.subcomponents = [
        e
        for e in calendar.subcomponents
        if e.name != "VEVENT"
        or "RECURRENCE-ID" not in e
        or key(e.decoded("RECURRENCE-ID")) != key(rid)
    ]
    calendar.add_component(event)


def exclude_occurrence(calendar: Calendar, event: Event) -> None:
    rid = event.decoded("RECURRENCE-ID")
    master = master_event(calendar)
    if not is_excluded(master, rid):
        master.add("EXDATE", rid, parameters=deepcopy(master["DTSTART"].params))
    calendar.subcomponents = [
        e
        for e in calendar.subcomponents
        if e.name != "VEVENT"
        or "RECURRENCE-ID" not in e
        or key(e.decoded("RECURRENCE-ID")) != key(rid)
    ]


def guard_series_schedule(calendar: Calendar, timezone: str | None, rule: str | None) -> None:
    master = validate_structure(calendar)
    if len(calendar.walk("VEVENT")) > 1 or any(p in master for p in ("RDATE", "EXDATE")):
        raise unsupported(
            "Series schedule/rule changes with overrides, RDATE, or EXDATE are refused; "
            "edit an occurrence or use a calendar client."
        )
    anchor = master.decoded("DTSTART")
    if isinstance(anchor, datetime):
        tz_name = getattr(anchor.tzinfo, "key", None)
        if anchor.tzinfo is not None and tz_name is None and str(anchor.tzinfo) != "UTC":
            raise unsupported(
                "Series schedule changes require an IANA timezone, UTC, or floating time."
            )
        if timezone is not None and timezone != (tz_name or ("UTC" if anchor.tzinfo else None)):
            raise unsupported("Changing a recurring series' timezone is unsupported.")
    elif timezone is not None:
        raise invalid("All-day series do not accept a timezone change.")
    old_rule = master.get("RRULE")
    if (
        old_rule
        and rule is None
        and any(k in old_rule for k in ("UNTIL", "BYHOUR", "BYMINUTE", "BYSECOND"))
    ):
        raise unsupported(
            "This rule constrains clock times or UNTIL; "
            "supply recurrence_rule explicitly with the new schedule."
        )


def validate_rule(event: Event, rule: str | None = None) -> None:
    """Validate supported replacement rules with icalendar and python-dateutil."""
    if rule is not None:
        if not rule or len(rule) > 1000 or any(c.isspace() for c in rule) or ":" in rule:
            raise invalid(
                "recurrence_rule must be an RFC 5545 RRULE value, without RRULE: or whitespace."
            )
        try:
            parts = rule.upper().split(";")
            names = [part.split("=")[0] for part in parts]
            if len(names) != len(set(names)) or any(part.count("=") != 1 for part in parts):
                raise ValueError
            parsed = vRecur.from_ical(rule.upper())
            if "BYSETPOS" in parsed and not any(
                k in parsed for k in ("BYDAY", "BYMONTHDAY", "BYMONTH")
            ):
                raise ValueError
            if parsed.get("FREQ") == ["WEEKLY"] and "BYMONTHDAY" in parsed:
                raise ValueError
            for name, low, high in (
                ("BYMONTH", 1, 12),
                ("BYMONTHDAY", -31, 31),
                ("BYSETPOS", -366, 366),
            ):
                if any(not low <= int(v) <= high or int(v) == 0 for v in parsed.get(name, [])):
                    raise ValueError
            for weekday in parsed.get("BYDAY", []):
                match = re.fullmatch(r"([+-]?[0-9]{1,2})?(MO|TU|WE|TH|FR|SA|SU)", str(weekday))
                if match is None:
                    raise ValueError
                if match[1] is not None and (
                    parsed.get("FREQ") not in (["MONTHLY"], ["YEARLY"])
                    or not 1 <= abs(int(match[1])) <= 53
                ):
                    raise ValueError
            allowed = {
                "FREQ",
                "INTERVAL",
                "COUNT",
                "UNTIL",
                "BYDAY",
                "BYMONTHDAY",
                "BYMONTH",
                "BYSETPOS",
                "WKST",
            }
            if set(parsed) - allowed or parsed.get("FREQ") not in (
                ["DAILY"],
                ["WEEKLY"],
                ["MONTHLY"],
                ["YEARLY"],
            ):
                raise ValueError
            if "COUNT" in parsed and "UNTIL" in parsed:
                raise ValueError
            for name in ("INTERVAL", "COUNT"):
                if name in parsed and (
                    len(parsed[name]) != 1 or not 1 <= int(parsed[name][0]) <= 10000
                ):
                    raise ValueError
            if len(parsed.get("UNTIL", [])) > 1:
                raise ValueError
            event.pop("RRULE", None)
            event.add("RRULE", parsed)
        except (ValueError, TypeError, KeyError):
            raise invalid(
                "Invalid or unsupported recurrence_rule. Use DAILY/WEEKLY/MONTHLY/YEARLY, "
                "positive COUNT/INTERVAL, and standard date filters."
            ) from None
    recurrence = event.get("RRULE")
    if recurrence is None:
        raise unsupported("A whole-series schedule change requires an RRULE.")
    anchor = event.decoded("DTSTART")
    dt = anchor if isinstance(anchor, datetime) else datetime.combine(anchor, datetime.min.time())
    try:
        if "UNTIL" in recurrence:
            until = recurrence["UNTIL"][0]
            if isinstance(anchor, datetime) != isinstance(until, datetime):
                raise ValueError
            if isinstance(anchor, datetime) and (anchor.tzinfo is None) != (until.tzinfo is None):
                raise ValueError
        generated = rrulestr(recurrence.to_ical().decode(), dtstart=dt)
        if generated.after(dt, inc=True) != dt:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise invalid(
            "The series start must match the recurrence rule, and UNTIL must have the "
            "correct date/time type. Adjust start and recurrence_rule together."
        ) from None


def touch(calendar: Calendar) -> None:
    events = calendar.walk("VEVENT")
    sequence = max(int(e.get("SEQUENCE", 0)) for e in events) + 1
    now = datetime.now(UTC)
    for event in events:
        for name, value in (("SEQUENCE", sequence), ("DTSTAMP", now), ("LAST-MODIFIED", now)):
            event.pop(name, None)
            event.add(name, value)


def validate_changed_event(event: Event, tz: ZoneInfo, recurring: bool) -> None:
    event_span(event, tz)
    if recurring:
        check_recurring_span(event, tz)
