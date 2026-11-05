"""VTODO parsing, normalization, filtering, and conservative recurrence reads."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar, Todo

from .errors import CalendarError, invalid
from .models import TaskInfo
from .timeutils import as_aware

TEXT_LIMIT = 16000
VALID_STATUSES = {"NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"}


def parse_task_calendar(data: str) -> Calendar:
    if len(data.encode()) > 2_000_000:
        raise CalendarError("limit_exceeded", "A task resource exceeds the 2 MB safety limit.")
    try:
        calendar = Calendar.from_ical(data)
        tasks = calendar.walk("VTODO")
        if any(
            component.name not in {"VTODO", "VTIMEZONE"} for component in calendar.subcomponents
        ):
            raise ValueError
        if not tasks or any(task.errors or "UID" not in task for task in tasks):
            raise ValueError
        if len({str(task["UID"]) for task in tasks}) != 1:
            raise ValueError
        for task in tasks:
            validate_task(cast(Todo, task))
        return calendar
    except CalendarError:
        raise
    except Exception:
        raise CalendarError(
            "invalid_calendar_data",
            "A calendar resource contains invalid task data; repair it in a calendar client.",
        ) from None


def master_task(calendar: Calendar) -> Todo:
    tasks = calendar.walk("VTODO")
    return cast(Todo, next((task for task in tasks if "RECURRENCE-ID" not in task), tasks[0]))


def is_recurring_task(calendar: Calendar) -> bool:
    return any(
        any(key in task for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"))
        for task in calendar.walk("VTODO")
    )


def recurrence_info(calendar: Calendar) -> dict[str, Any] | None:
    if not is_recurring_task(calendar):
        return None
    master = master_task(calendar)
    result: dict[str, Any] = {
        "is_recurring": True,
        "mutation_supported": False,
        "read_expansion_supported": "DTSTART" in master,
        "limitation": (
            "Recurring VTODO mutation is refused in v4 because completion and exception "
            "semantics are not consistently interoperable across CalDAV clients."
        ),
    }
    for key in ("RRULE", "RDATE", "EXDATE"):
        values = master.get(key, [])
        if not isinstance(values, list):
            values = [values]
        result[key.lower()] = [value.to_ical().decode() for value in values]
    result["has_exceptions"] = any("RECURRENCE-ID" in task for task in calendar.walk("VTODO"))
    return result


def _date_value(task: Todo, key: str) -> date | datetime | None:
    if key not in task:
        return None
    value = task.decoded(key)
    if not isinstance(value, (date, datetime)):
        raise CalendarError("invalid_calendar_data", f"A task has an invalid {key} value.")
    if isinstance(value, datetime) and value.tzinfo is None and task[key].params.get("TZID"):
        raise CalendarError("invalid_calendar_data", "A task uses an unresolved TZID.")
    if (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.astimezone(UTC).astimezone(value.tzinfo).replace(tzinfo=None)
        != value.replace(tzinfo=None)
    ):
        raise CalendarError(
            "invalid_calendar_data", "A task date falls in a nonexistent DST local time."
        )
    return value


def task_dates(task: Todo, tz: ZoneInfo) -> tuple[date | datetime | None, date | datetime | None]:
    start = _date_value(task, "DTSTART")
    due = _date_value(task, "DUE")
    if due is not None and "DURATION" in task:
        raise CalendarError("invalid_calendar_data", "A task has both DUE and DURATION.")
    if "DURATION" in task:
        if start is None:
            raise CalendarError("invalid_calendar_data", "A task DURATION requires DTSTART.")
        duration = task.decoded("DURATION")
        if not isinstance(duration, timedelta) or duration <= timedelta(0):
            raise CalendarError("invalid_calendar_data", "A task has an invalid DURATION.")
        if isinstance(start, datetime):
            anchor = as_aware(start, tz)
            nominal = anchor + timedelta(days=duration.days)
            due = (nominal.astimezone(UTC) + timedelta(seconds=duration.seconds)).astimezone(
                anchor.tzinfo
            )
            if start.tzinfo is None:
                due = due.replace(tzinfo=None)
        else:
            due = start + duration
    if start is not None and due is not None:
        if isinstance(start, datetime) != isinstance(due, datetime):
            raise CalendarError("invalid_calendar_data", "Task DTSTART and DUE types do not match.")
        if as_aware(due, tz).astimezone(UTC) <= as_aware(start, tz).astimezone(UTC):
            raise CalendarError("invalid_calendar_data", "Task DUE must be later than DTSTART.")
    return start, due


def validate_task(task: Todo, tz: ZoneInfo | None = None) -> None:
    status = str(task.get("STATUS", "NEEDS-ACTION")).upper()
    if status not in VALID_STATUSES:
        raise CalendarError("invalid_calendar_data", "A task has an invalid STATUS.")
    for key, low, high in (("PERCENT-COMPLETE", 0, 100), ("PRIORITY", 0, 9)):
        if key in task:
            value = task.decoded(key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise CalendarError("invalid_calendar_data", f"A task has an invalid {key}.")
    if "COMPLETED" in task:
        completed = task.decoded("COMPLETED")
        if not isinstance(completed, datetime) or completed.tzinfo is None:
            raise CalendarError(
                "invalid_calendar_data", "A task COMPLETED value must be a UTC date-time."
            )
        if completed.utcoffset() != timedelta(0):
            raise CalendarError(
                "invalid_calendar_data", "A task COMPLETED value must be a UTC date-time."
            )
    if tz is not None:
        task_dates(task, tz)


def _categories(task: Todo) -> list[str]:
    values = task.get("CATEGORIES", [])
    values = values if isinstance(values, list) else [values]
    result: list[str] = []
    for value in values:
        cats = getattr(value, "cats", None)
        if cats is not None:
            result.extend(str(item) for item in cats)
        else:
            result.extend(part.strip() for part in str(value).split(",") if part.strip())
    return sorted(set(result), key=str.casefold)


def _normalized_date(value: date | datetime | None, tz: ZoneInfo) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_aware(value, tz).isoformat()
    return value.isoformat()


def _optional_timestamp(task: Todo, key: str, tz: ZoneInfo, *, utc: bool = False) -> str | None:
    if key not in task:
        return None
    value = task.decoded(key)
    if not isinstance(value, datetime):
        return None
    aware = as_aware(value, tz)
    return (aware.astimezone(UTC) if utc else aware).isoformat()


def normalize_task(
    task: Todo,
    calendar_id: str,
    task_id: str,
    tz: ZoneInfo,
    recurrence: dict[str, Any] | None = None,
    etag: str | None = None,
    *,
    occurrence: bool = False,
    floating: bool | None = None,
) -> TaskInfo:
    validate_task(task, tz)
    start, due = task_dates(task, tz)
    sources = [value for value in (start, due) if value is not None]
    all_day = bool(sources) and all(not isinstance(value, datetime) for value in sources)
    originally_floating = any(
        isinstance(value, datetime) and value.tzinfo is None for value in sources
    )
    timed = next((value for value in (due, start) if isinstance(value, datetime)), None)
    timezone = (
        str(getattr(as_aware(timed, tz).tzinfo, "key", as_aware(timed, tz).tzinfo))
        if timed is not None
        else None
    )
    truncated: list[str] = []

    def text(key: str) -> str:
        value = str(task.get(key, ""))
        if len(value) > TEXT_LIMIT:
            truncated.append(key.lower())
        return value[:TEXT_LIMIT]

    rid: date | datetime | None = None
    if "RECURRENCE-ID" in task:
        rid = task.decoded("RECURRENCE-ID")
    elif recurrence and start is not None:
        rid = start
    occurrence_id = _normalized_date(rid, tz) if occurrence else None
    status = str(task.get("STATUS", "NEEDS-ACTION")).upper()
    priority = task.decoded("PRIORITY") if "PRIORITY" in task else None
    percent = task.decoded("PERCENT-COMPLETE") if "PERCENT-COMPLETE" in task else None
    return TaskInfo(
        task_id=task_id,
        calendar_id=calendar_id,
        uid=str(task["UID"]),
        summary=text("SUMMARY"),
        start=_normalized_date(start, tz),
        due=_normalized_date(due, tz),
        all_day=all_day,
        timezone=timezone,
        floating=originally_floating if floating is None else floating,
        description=text("DESCRIPTION"),
        location=text("LOCATION"),
        categories=_categories(task),
        status=status,
        completed=status == "COMPLETED",
        completed_at=_optional_timestamp(task, "COMPLETED", tz, utc=True),
        percent_complete=cast(int | None, percent),
        priority=cast(int | None, priority),
        created=_optional_timestamp(task, "CREATED", tz),
        last_modified=_optional_timestamp(task, "LAST-MODIFIED", tz),
        etag=etag,
        recurrence=recurrence,
        occurrence_id=occurrence_id,
        representation="occurrence" if occurrence else ("series" if recurrence else "task"),
        text_truncated=truncated,
    )


def effective_time(task: TaskInfo, tz: ZoneInfo) -> datetime | None:
    value = task.due or task.start
    if value is None:
        return None
    parsed: date | datetime
    parsed = date.fromisoformat(value) if task.all_day else datetime.fromisoformat(value)
    return as_aware(parsed, tz).astimezone(UTC)


def _expansion_calendar(calendar: Calendar, tz: ZoneInfo) -> Calendar:
    copied = deepcopy(calendar)
    copied.pop("X-WR-TIMEZONE", None)

    def resolve(value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            return as_aware(value, tz)
        if isinstance(value, tuple):
            return tuple(resolve(part) for part in value)
        return value

    for task in copied.walk("VTODO"):
        floating = False
        for key in ("DTSTART", "DUE", "RECURRENCE-ID"):
            if key not in task:
                continue
            value = task.decoded(key)
            if isinstance(value, datetime) and value.tzinfo is None:
                floating = True
                task.pop(key)
                task.add(key, resolve(value))
        for key in ("RDATE", "EXDATE"):
            properties = task.get(key, [])
            properties = properties if isinstance(properties, list) else [properties]
            if properties:
                task.pop(key)
                for prop in properties:
                    task.add(key, [resolve(value.dt) for value in prop.dts])
        if floating:
            rules = task.get("RRULE", [])
            rules = rules if isinstance(rules, list) else [rules]
            for rule in rules:
                untils = rule.get("UNTIL", [])
                if untils and isinstance(untils[0], datetime) and untils[0].tzinfo is None:
                    rule["UNTIL"] = [as_aware(untils[0], tz).astimezone(UTC)]
    return copied


def _check_recurring_dates(task: Todo, tz: ZoneInfo) -> None:
    start, due = task_dates(task, tz)
    if (
        isinstance(start, datetime)
        and isinstance(due, datetime)
        and as_aware(start, tz).utcoffset() != as_aware(due, tz).utcoffset()
    ):
        raise CalendarError(
            "unsupported_recurrence",
            "A timed recurring task spans a DST offset change; use a task client.",
        )


def expand_tasks(
    calendar: Calendar,
    calendar_id: str,
    task_id: str,
    tz: ZoneInfo,
    start: datetime | None,
    end: datetime | None,
    max_tasks: int,
) -> list[TaskInfo]:
    recurrence = recurrence_info(calendar)
    master = master_task(calendar)
    if recurrence is None:
        return [normalize_task(master, calendar_id, task_id, tz)]
    if start is None or end is None:
        return [normalize_task(master, calendar_id, task_id, tz, recurrence)]
    if "DTSTART" not in master:
        raise CalendarError(
            "unsupported_recurrence",
            "A recurring VTODO without DTSTART cannot be expanded safely; use a task client.",
        )
    _check_recurring_dates(master, tz)
    source_start = master.decoded("DTSTART")
    floating = isinstance(source_start, datetime) and source_start.tzinfo is None
    result: dict[str, TaskInfo] = {}
    try:
        copied = _expansion_calendar(calendar, tz)
        query = recurring_ical_events.of(
            copied,
            components=("VTODO",),
            skip_bad_series=False,
        )
        master_start, master_due = task_dates(master, tz)
        due_offset = timedelta(0)
        if master_start is not None and master_due is not None:
            due_offset = as_aware(master_due, tz).astimezone(UTC) - as_aware(
                master_start, tz
            ).astimezone(UTC)
        rules = master.get("RRULE", [])
        rules = rules if isinstance(rules, list) else [rules]
        if isinstance(source_start, datetime) and any("COUNT" in rule for rule in rules):
            validation = deepcopy(copied)
            validation.subcomponents = [
                component
                for component in validation.subcomponents
                if component.name != "VTODO" or "RECURRENCE-ID" not in component
            ]
            master_task(validation).pop("EXDATE", None)
            history_query = recurring_ical_events.of(
                validation, components=("VTODO",), skip_bad_series=False
            )
            history = as_aware(source_start, tz).astimezone(tz)
            history_end = (end - due_offset).astimezone(tz)
            seen: set[str] = set()
            chunks = 0
            while history < history_end:
                chunk_end = min(history + timedelta(days=31), history_end)
                for instance in history_query.between(history, chunk_end):
                    _check_recurring_dates(cast(Todo, instance), tz)
                    normalize_task(cast(Todo, instance), calendar_id, task_id, tz)
                    seen.add(instance["DTSTART"].to_ical().decode())
                    if len(seen) > max_tasks:
                        raise CalendarError(
                            "limit_exceeded",
                            "A recurring task has too much history to validate safely.",
                        )
                chunks += 1
                if chunks > 1200:
                    raise CalendarError(
                        "limit_exceeded", "A recurring task is too old to validate safely."
                    )
                history = chunk_end
        # Expansion selects by DTSTART, while list_tasks filters by DUE when present.
        cursor = (start - due_offset).astimezone(tz) - timedelta(days=2)
        stop = end.astimezone(tz) + timedelta(days=2)
        while cursor < stop:
            next_day = min(cursor + timedelta(days=1), stop)
            for instance in query.between(cursor, next_day):
                _check_recurring_dates(cast(Todo, instance), tz)
                item = normalize_task(
                    cast(Todo, instance),
                    calendar_id,
                    task_id,
                    tz,
                    recurrence,
                    occurrence=True,
                    floating=floating,
                )
                key = item.occurrence_id or item.start or item.due
                if key is not None:
                    result[key] = item
                if len(result) > max_tasks:
                    raise CalendarError(
                        "limit_exceeded", "Too many task occurrences; use a smaller range."
                    )
            cursor = next_day
    except CalendarError:
        raise
    except Exception:
        raise CalendarError(
            "invalid_recurrence",
            "Task recurrence could not be expanded reliably; repair it in a task client.",
        ) from None
    return list(result.values())


def serialize_task_calendar(calendar: Calendar) -> str:
    values = [
        task.decoded(key)
        for task in calendar.walk("VTODO")
        for key in ("DTSTART", "DUE", "RECURRENCE-ID")
        if key in task
    ]
    if values:
        first = min(values, key=lambda value: value.year)
        last = max(values, key=lambda value: value.year)
        calendar.add_missing_timezones(
            first_date=date(max(1, first.year - 1), 1, 1),
            last_date=date(min(9999, last.year + 1), 12, 31),
        )
    data = calendar.to_ical()
    if len(data) > 2_000_000:
        raise CalendarError("limit_exceeded", "The updated task resource would exceed 2 MB.")
    return data.decode()


def validate_task_text(
    summary: str | None, description: str | None, location: str | None, categories: list[str] | None
) -> None:
    if summary is not None and not summary.strip():
        raise invalid("summary must not be empty.")
    values = [value for value in (summary, description, location) if value is not None]
    if any(len(value) > TEXT_LIMIT or "\x00" in value for value in values):
        raise invalid("Task text fields must be at most 16000 characters and contain no NULs.")
    if categories is not None and (
        len(categories) > 100
        or any(
            not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value
            for value in categories
        )
    ):
        raise invalid("categories must contain at most 100 nonempty strings of at most 256 chars.")


def touch_task(task: Todo) -> None:
    now = datetime.now(UTC)
    task.pop("DTSTAMP", None)
    task.add("DTSTAMP", now)
    task.pop("LAST-MODIFIED", None)
    task.add("LAST-MODIFIED", now)
    sequence = task.decoded("SEQUENCE") if "SEQUENCE" in task else 0
    task.pop("SEQUENCE", None)
    task.add("SEQUENCE", int(sequence) + 1)
