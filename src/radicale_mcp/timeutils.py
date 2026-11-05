"""Explicit local-time policy and UTC interval arithmetic."""

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import invalid

Interval = tuple[datetime, datetime]


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise invalid("Use a valid IANA timezone, such as America/New_York.") from None


def localize(value: datetime, tz: tzinfo, *, existing: bool = False) -> datetime:
    """Reject ambiguous/nonexistent tool input. Existing floating folds use fold=0."""
    candidates = [value.replace(tzinfo=tz, fold=f) for f in (0, 1)]
    valid = [
        c for c in candidates if c.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value
    ]
    if not valid:
        raise invalid(
            "This local time does not exist during a DST transition; choose a valid time."
        )
    if not existing and len({c.utcoffset() for c in valid}) > 1:
        raise invalid(
            "This local time is ambiguous during a DST transition; include an explicit UTC offset."
        )
    return valid[0]


def timestamp(value: str, tz: ZoneInfo) -> datetime:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", value
    ):
        raise invalid(
            "Use an ISO-8601 timestamp with T and optional offset; "
            "date-only values are for all-day events."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise invalid("Invalid ISO-8601 timestamp.") from None
    return localize(parsed, tz) if parsed.tzinfo is None else parsed


def day(value: str) -> date:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise invalid("All-day events require YYYY-MM-DD dates and an exclusive end date.")


def as_aware(value: date | datetime, tz: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        return localize(datetime.combine(value, time.min), tz, existing=True)
    return value if value.tzinfo is not None else localize(value, tz, existing=True)


def bounded_range(start: str, end: str, tz: ZoneInfo, max_days: int) -> Interval:
    a, b = timestamp(start, tz).astimezone(UTC), timestamp(end, tz).astimezone(UTC)
    if not timedelta(0) < b - a <= timedelta(days=max_days):
        raise invalid(f"end must follow start and the range must not exceed {max_days} days.")
    return a, b


def event_times(
    start: str, end: str | None, duration_minutes: int | None, all_day: bool, tz: ZoneInfo
) -> tuple[date | datetime, date | datetime]:
    if end is not None and duration_minutes is not None:
        raise invalid("Supply end or duration_minutes, not both.")
    if all_day:
        if duration_minutes is not None:
            raise invalid("Use an exclusive end date for an all-day event, not duration_minutes.")
        a: date | datetime = day(start)
        b: date | datetime = day(end) if end is not None else a + timedelta(days=1)
        valid = b > a
    else:
        a = timestamp(start, tz)
        if end is None:
            if (
                duration_minutes is None
                or isinstance(duration_minutes, bool)
                or not 1 <= duration_minutes <= 527040
            ):
                raise invalid("Provide end or a positive duration_minutes (at most 366 days).")
            b = (a.astimezone(UTC) + timedelta(minutes=duration_minutes)).astimezone(a.tzinfo)
        else:
            b = timestamp(end, tz)
        valid = b.astimezone(UTC) > a.astimezone(UTC)
    if not valid:
        raise invalid("Event end must be later than start.")
    if as_aware(b, tz).astimezone(UTC) - as_aware(a, tz).astimezone(UTC) > timedelta(days=366):
        raise invalid("Event duration must not exceed 366 days.")
    return a, b


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(b, merged[-1][1])
        else:
            merged.append((a, b))
    return merged


def free_intervals(busy: list[Interval], windows: list[Interval], minutes: int) -> list[Interval]:
    result = []
    for a, b in windows:
        cursor = a
        for left, right in busy:
            if right <= cursor or left >= b:
                continue
            if left - cursor >= timedelta(minutes=minutes):
                result.append((cursor, left))
            cursor = max(cursor, right)
        if b - cursor >= timedelta(minutes=minutes):
            result.append((cursor, b))
    return result


def working_windows(
    a: datetime,
    b: datetime,
    tz: ZoneInfo,
    working_start: str | None,
    working_end: str | None,
    weekdays: list[int] | None,
) -> list[Interval]:
    if (working_start is None) != (working_end is None):
        raise invalid("Provide both working_start and working_end (HH:MM).")
    if weekdays is not None and (
        not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays)
    ):
        raise invalid("weekdays must contain integers 0 (Monday) through 6 (Sunday).")
    if working_start is None and weekdays is None:
        return [(a, b)]
    try:
        if working_start is not None:
            if not all(re.fullmatch(r"\d{2}:\d{2}", v or "") for v in (working_start, working_end)):
                raise ValueError
            left, right = time.fromisoformat(working_start), time.fromisoformat(working_end or "")
            if left >= right:
                raise ValueError
        else:
            left, right = time.min, time.min
    except ValueError:
        raise invalid(
            "Working hours must be HH:MM with start before end; overnight windows are unsupported."
        ) from None
    result = []
    current = a.astimezone(tz).date()
    while current <= b.astimezone(tz).date():
        if weekdays is None or current.weekday() in weekdays:
            stop_day = current if working_start is not None else current + timedelta(days=1)
            lo = localize(datetime.combine(current, left), tz).astimezone(UTC)
            hi = localize(datetime.combine(stop_day, right), tz).astimezone(UTC)
            if max(lo, a) < min(hi, b):
                result.append((max(lo, a), min(hi, b)))
        current += timedelta(days=1)
    return result
