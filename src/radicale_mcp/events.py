"""iCalendar parsing/serialization and library-based recurrence expansion."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar, Event

from .errors import CalendarError, invalid
from .models import EventInfo
from .timeutils import as_aware

TEXT_LIMIT = 16000


def parse_calendar(data: str) -> Calendar:
    if len(data.encode()) > 2_000_000:
        raise CalendarError("limit_exceeded", "An event resource exceeds the 2 MB safety limit.")
    try:
        calendar = Calendar.from_ical(data)
        events = calendar.walk("VEVENT")
        if not events or any(e.errors or "DTSTART" not in e or "UID" not in e for e in events):
            raise ValueError
        if len({str(e["UID"]) for e in events}) != 1:
            raise ValueError
        return calendar
    except Exception:
        raise CalendarError(
            "invalid_calendar_data",
            "A calendar resource contains invalid event data; repair it in a calendar client.",
        ) from None


def is_recurring(calendar: Calendar) -> bool:
    return any(
        any(k in e for k in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"))
        for e in calendar.walk("VEVENT")
    )


def master_event(calendar: Calendar) -> Event:
    events = calendar.walk("VEVENT")
    return cast(Event, next((e for e in events if "RECURRENCE-ID" not in e), events[0]))


def serialize(calendar: Calendar) -> str:
    """Include timezone definitions for clients that cannot resolve IANA names themselves."""
    times = [e.decoded(k) for e in calendar.walk("VEVENT") for k in ("DTSTART", "DTEND") if k in e]
    first = min(times, key=lambda v: v.year)
    last = max(times, key=lambda v: v.year)
    calendar.add_missing_timezones(
        first_date=date(max(1, first.year - 1), 1, 1),
        last_date=date(min(9999, last.year + 1), 12, 31),
    )
    data = calendar.to_ical()
    if len(data) > 2_000_000:
        raise CalendarError("limit_exceeded", "The updated event resource would exceed 2 MB.")
    return data.decode()


def recurrence_info(calendar: Calendar) -> dict[str, Any] | None:
    if not is_recurring(calendar):
        return None
    master = master_event(calendar)
    result: dict[str, Any] = {
        "is_recurring": True,
        "mutation_supported": True,
        "mutation_scopes": ["occurrence", "series", "future"],
        "explicit_scope_required": True,
    }
    for key in ("RRULE", "RDATE", "EXDATE"):
        values = master.get(key, [])
        if not isinstance(values, list):
            values = [values]
        result[key.lower()] = [v.to_ical().decode() for v in values]
    relations = master.get("RELATED-TO", [])
    result["related_to"] = [
        str(v) for v in (relations if isinstance(relations, list) else [relations])
    ]
    result["has_exceptions"] = any("RECURRENCE-ID" in e for e in calendar.walk("VEVENT"))
    return result


def event_span(event: Event, tz: ZoneInfo) -> tuple[date | datetime, date | datetime]:
    start = event.decoded("DTSTART")
    # A TZID that icalendar cannot resolve must not silently become a floating time.
    for key in ("DTSTART", "DTEND", "RECURRENCE-ID"):
        if key in event:
            value = event.decoded(key)
            if (
                isinstance(value, datetime)
                and value.tzinfo is not None
                and value.astimezone(UTC).astimezone(value.tzinfo).replace(tzinfo=None)
                != value.replace(tzinfo=None)
            ):
                raise CalendarError(
                    "invalid_calendar_data",
                    "An event or recurrence falls in a nonexistent DST local time; "
                    "use a calendar client to repair or replace the series.",
                )
            if (
                isinstance(value, datetime)
                and value.tzinfo is None
                and event[key].params.get("TZID")
            ):
                raise CalendarError("invalid_calendar_data", "An event uses an unresolved TZID.")
    if "DTEND" in event:
        end = event.decoded("DTEND")
    elif "DURATION" in event:
        duration = event.decoded("DURATION")
        if isinstance(start, datetime):
            # RFC 5545: apply nominal days first, then elapsed hours/minutes/seconds.
            nominal = as_aware(start, tz) + timedelta(days=duration.days)
            end = (nominal.astimezone(UTC) + timedelta(seconds=duration.seconds)).astimezone(
                nominal.tzinfo
            )
        else:
            end = start + duration
    else:
        end = start if isinstance(start, datetime) else start + timedelta(days=1)
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise CalendarError("invalid_calendar_data", "Event start/end types do not match.")
    if as_aware(end, tz).astimezone(UTC) < as_aware(start, tz).astimezone(UTC):
        raise CalendarError("invalid_calendar_data", "An event ends before it starts.")
    return start, end


def check_recurring_span(event: Event, tz: ZoneInfo) -> None:
    start, end = event_span(event, tz)
    if (
        isinstance(start, datetime)
        and as_aware(start, tz).utcoffset() != as_aware(end, tz).utcoffset()
    ):
        raise CalendarError(
            "unsupported_recurrence",
            "A timed recurring event spans a DST offset change; "
            "this duration cannot be expanded safely in v2. Use a calendar client.",
        )


def normalize(
    event: Event,
    calendar_id: str,
    event_id: str,
    tz: ZoneInfo,
    recurrence: dict[str, Any] | None = None,
    etag: str | None = None,
    floating: bool | None = None,
    occurrence: bool = False,
) -> EventInfo:
    start, end = event_span(event, tz)
    all_day = not isinstance(start, datetime)
    originally_floating = isinstance(start, datetime) and start.tzinfo is None
    if not all_day:
        start, end = as_aware(start, tz), as_aware(end, tz)
    tz_name = (
        str(getattr(start.tzinfo, "key", start.tzinfo)) if isinstance(start, datetime) else None
    )
    truncated = []

    def text(key: str) -> str:
        value = str(event.get(key, ""))
        if len(value) > TEXT_LIMIT:
            truncated.append(key.lower())
        return value[:TEXT_LIMIT]

    def alarm_trigger(alarm: Any) -> str:
        value = alarm.get("TRIGGER")
        if value is None:
            return ""
        encoded = value.to_ical()
        return encoded.decode() if isinstance(encoded, bytes) else str(encoded)

    rid = event.decoded("RECURRENCE-ID") if "RECURRENCE-ID" in event else start
    if isinstance(rid, datetime):
        rid = as_aware(rid, tz)

    def repeated(key: str) -> list[Any]:
        if key == "CATEGORIES" and key in event:
            value = event.decoded(key)
            values = value if isinstance(value, list) else [value]
            return [
                item for group in values for item in (group if isinstance(group, list) else [group])
            ]
        value = event.get(key, [])
        return list(value) if isinstance(value, list) else ([value] if value else [])

    def params(value: Any) -> dict[str, str]:
        allowed = ("CN", "ROLE", "PARTSTAT", "RSVP")
        return {k.lower(): str(value.params[k]) for k in allowed if k in value.params}

    organizer = event.get("ORGANIZER")
    geo = event.get("GEO")
    return EventInfo(
        event_id=event_id,
        calendar_id=calendar_id,
        uid=str(event["UID"]),
        summary=text("SUMMARY"),
        start=start.isoformat(),
        end=end.isoformat(),
        all_day=all_day,
        timezone=tz_name,
        floating=originally_floating if floating is None else floating,
        location=text("LOCATION"),
        description=text("DESCRIPTION"),
        status=str(event.get("STATUS", "CONFIRMED")),
        transparent=str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT",
        etag=etag,
        recurrence=recurrence,
        occurrence_id=rid.isoformat() if recurrence and occurrence else None,
        categories=[str(v) for v in repeated("CATEGORIES")],
        priority=int(event.decoded("PRIORITY")) if "PRIORITY" in event else None,
        geo={"latitude": float(geo.latitude), "longitude": float(geo.longitude)} if geo else None,
        attach=[str(v) for v in repeated("ATTACH")],
        comment=[str(v) for v in repeated("COMMENT")],
        resources=[str(v) for v in repeated("RESOURCES")],
        url=str(event.get("URL")) if event.get("URL") is not None else None,
        organizer=({"address": str(organizer), **params(organizer)} if organizer else None),
        attendee=[{"address": str(v), **params(v)} for v in repeated("ATTENDEE")],
        contact=[str(v) for v in repeated("CONTACT")],
        alarms=[
            {
                "trigger": alarm_trigger(alarm),
                "description": str(alarm.get("DESCRIPTION", "")),
                "action": str(alarm.get("ACTION", "DISPLAY")).upper(),
                "related": str(
                    getattr(alarm.get("TRIGGER"), "params", {}).get("RELATED", "START")
                ).upper(),
            }
            for alarm in event.subcomponents
            if alarm.name == "VALARM"
        ],
        text_truncated=truncated,
    )


def overlaps(event: EventInfo, a: datetime, b: datetime, tz: ZoneInfo) -> bool:
    start, end = interval(event, tz)
    return (start < b and end > a) or (start == end and a <= start < b)


def interval(event: EventInfo, tz: ZoneInfo) -> tuple[datetime, datetime]:
    parse = date.fromisoformat if event.all_day else datetime.fromisoformat
    return tuple(as_aware(parse(v), tz).astimezone(UTC) for v in (event.start, event.end))  # type: ignore[return-value]


def expansion_calendar(calendar: Calendar, tz: ZoneInfo) -> Calendar:
    """Resolve floating values only, without changing the cadence of UTC series.

    X-WR-TIMEZONE conversion in the expansion dependency converts UTC DTSTART
    into local wall time and leaves floating UNTIL unconverted. Normalize an
    in-memory copy explicitly so UTC stays UTC and floating bounds stay local.
    Stored resources retain their original floating/timezone representation.
    """
    copied = deepcopy(calendar)
    copied.pop("X-WR-TIMEZONE", None)

    def resolve(value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            return as_aware(value, tz)
        if isinstance(value, tuple):
            return tuple(resolve(part) for part in value)
        return value

    for event in copied.walk("VEVENT"):
        anchor = event.decoded("DTSTART")
        floating = isinstance(anchor, datetime) and anchor.tzinfo is None
        for name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            if name not in event:
                continue
            value = event.decoded(name)
            if isinstance(value, datetime) and value.tzinfo is None:
                event.pop(name)
                event.add(name, resolve(value))
        for name in ("RDATE", "EXDATE"):
            properties = event.get(name, [])
            properties = properties if isinstance(properties, list) else [properties]
            if properties:
                event.pop(name)
                for prop in properties:
                    event.add(name, [resolve(value.dt) for value in prop.dts])
        if floating:
            rules = event.get("RRULE", [])
            for rule in rules if isinstance(rules, list) else [rules]:
                untils = rule.get("UNTIL", [])
                if untils and isinstance(untils[0], datetime) and untils[0].tzinfo is None:
                    rule["UNTIL"] = [resolve(untils[0]).astimezone(UTC)]
    return copied


def expand(
    calendar: Calendar,
    calendar_id: str,
    event_id: str,
    tz: ZoneInfo,
    a: datetime,
    b: datetime,
    max_events: int,
) -> list[EventInfo]:
    rec = recurrence_info(calendar)
    source = master_event(calendar)
    for component in calendar.walk("VEVENT"):
        event_span(cast(Event, component), tz)
    floating = (
        isinstance(source.decoded("DTSTART"), datetime) and source.decoded("DTSTART").tzinfo is None
    )
    if rec is None:
        item = normalize(source, calendar_id, event_id, tz)
        return [item] if overlaps(item, a, b, tz) else []
    check_recurring_span(source, tz)
    copied = expansion_calendar(calendar, tz)
    result: dict[str, EventInfo] = {}
    try:
        query = recurring_ical_events.of(copied, skip_bad_series=False)
        # dateutil-based recurrence engines can count nonexistent DST wall times toward
        # COUNT. Checking only the requested window would then give incorrect later results.
        # Validate the original COUNT rule's history, without EXDATE/overrides masking gaps.
        rules = source.get("RRULE", [])
        rules = rules if isinstance(rules, list) else [rules]
        if isinstance(source.decoded("DTSTART"), datetime) and any("COUNT" in r for r in rules):
            validation = deepcopy(copied)
            validation.subcomponents = [
                e
                for e in validation.subcomponents
                if e.name != "VEVENT" or "RECURRENCE-ID" not in e
            ]
            master_event(validation).pop("EXDATE", None)
            check = recurring_ical_events.of(validation, skip_bad_series=False)
            history = as_aware(source.decoded("DTSTART"), tz).astimezone(tz)
            history_end = b.astimezone(tz)
            seen: set[str] = set()
            chunks = 0
            while history < history_end:
                chunk_end = min(history + timedelta(days=31), history_end)
                for instance in check.between(history, chunk_end):
                    check_recurring_span(instance, tz)
                    seen.add(instance["DTSTART"].to_ical().decode())
                    if len(seen) > max_events:
                        raise CalendarError(
                            "limit_exceeded",
                            "A COUNT series has too much history to validate safely.",
                        )
                chunks += 1
                if chunks > 1200:
                    raise CalendarError(
                        "limit_exceeded", "A COUNT series is too old to validate safely."
                    )
                history = chunk_end
        # Daily chunks bound expansion memory; final UTC overlap filtering is authoritative.
        cursor = a.astimezone(tz) - timedelta(days=2)
        stop = b.astimezone(tz) + timedelta(days=2)
        while cursor < stop:
            next_day = min(cursor + timedelta(days=1), stop)
            for instance in query.between(cursor, next_day):
                check_recurring_span(instance, tz)
                item = normalize(
                    instance, calendar_id, event_id, tz, rec, floating=floating, occurrence=True
                )
                if overlaps(item, a, b, tz):
                    result[item.occurrence_id or item.start] = item
                    if len(result) > max_events:
                        raise CalendarError(
                            "limit_exceeded",
                            "Too many event occurrences; use a smaller time range.",
                        )
            cursor = next_day
    except CalendarError:
        raise
    except Exception:
        raise CalendarError(
            "invalid_recurrence",
            "Recurrence could not be expanded reliably; repair the series in a calendar client.",
        ) from None
    return list(result.values())


def validate_text(summary: str | None, description: str | None, location: str | None) -> None:
    if summary is not None and not summary.strip():
        raise invalid("summary must not be empty.")
    if any(
        v is not None and (len(v) > TEXT_LIMIT or "\x00" in v)
        for v in (summary, description, location)
    ):
        raise invalid("Text fields must be at most 16000 characters and contain no NUL characters.")
