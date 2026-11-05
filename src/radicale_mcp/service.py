"""Calendar operations independent of the MCP protocol."""

import json
import re
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, NotRequired, cast
from unicodedata import category
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from icalendar import Alarm, Calendar, Event, Todo, vRecur
from typing_extensions import TypedDict

from .client import CalDAVStore, resource_id
from .config import Config
from .errors import CalendarError, invalid
from .events import (
    event_span,
    expand,
    interval,
    is_recurring,
    master_event,
    normalize,
    parse_calendar,
    recurrence_info,
    serialize,
    validate_text,
)
from .models import EventInfo, TaskInfo
from .recurrence import (
    MutationScope,
    exclude_occurrence,
    guard_series_schedule,
    occurrence_event,
    replace_occurrence,
    touch,
    validate_changed_event,
    validate_rule,
    validate_scope,
    validate_structure,
)
from .splitting import has_future_exceptions, recovery_details, set_uid, split_calendar
from .tasks import (
    VALID_STATUSES,
    effective_time,
    expand_tasks,
    is_recurring_task,
    master_task,
    normalize_task,
    parse_task_calendar,
    serialize_task_calendar,
    task_dates,
    touch_task,
    validate_task,
    validate_task_text,
)
from .tasks import (
    recurrence_info as task_recurrence_info,
)
from .timeutils import (
    as_aware,
    bounded_range,
    event_times,
    free_intervals,
    merge_intervals,
    timestamp,
    working_windows,
    zone,
)

TaskState = Literal["open", "completed", "all"]


class GeoInput(TypedDict):
    latitude: float
    longitude: float


class AlarmInput(TypedDict):
    trigger: str
    description: NotRequired[str]
    action: NotRequired[Literal["DISPLAY"]]
    related: NotRequired[Literal["START", "END"]]


class OrganizerInput(TypedDict):
    address: str
    cn: NotRequired[str]


class AttendeeInput(TypedDict):
    address: str
    cn: NotRequired[str]
    role: NotRequired[str]
    partstat: NotRequired[str]
    rsvp: NotRequired[bool]


def _event_properties(event: Event, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if value is None:
            continue
        repeated = {"categories", "attach", "comment", "resources", "attendee", "contact"}
        if key in repeated and (not isinstance(value, list) or len(value) > 100):
            raise invalid(f"{key} must be a list containing at most 100 values.")
        event.pop(key.upper(), None)
        if key == "geo":
            if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
                raise invalid("geo must contain latitude and longitude.")
            lat, lon = value["latitude"], value["longitude"]
            if (
                not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon))
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
            ):
                raise invalid("geo coordinates must be latitude -90..90 and longitude -180..180.")
            event.add("GEO", (lat, lon))
        elif key == "priority":
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 9:
                raise invalid("priority must be an integer from 0 through 9.")
            event.add("PRIORITY", value)
        elif key in ("url",):
            _uri(value, key)
            event.add(key.upper(), value)
        elif key == "attach":
            # Binary ATTACH (ENCODING=BASE64;VALUE=BINARY) is intentionally deferred.
            for uri in value:
                _uri(uri, "attach")
                event.add("ATTACH", uri)
        elif key in ("organizer", "attendee"):
            for item in [value] if key == "organizer" else value:
                if (
                    not isinstance(item, dict)
                    or set(item)
                    - (
                        {"address", "cn"}
                        if key == "organizer"
                        else {"address", "cn", "role", "partstat", "rsvp"}
                    )
                    or not isinstance(item.get("address"), str)
                    or not item["address"].strip()
                ):
                    raise invalid(f"{key} entries require a nonempty calendar address.")
                _uri(item["address"], f"{key} address")
                event.add(key.upper(), item["address"])
                prop = event.get(key.upper())
                if isinstance(prop, list):
                    prop = prop[-1]
                allowed = (
                    {"cn": "CN"}
                    if key == "organizer"
                    else {"cn": "CN", "role": "ROLE", "partstat": "PARTSTAT", "rsvp": "RSVP"}
                )
                for name, param in allowed.items():
                    if name in item:
                        if name == "rsvp" and not isinstance(item[name], bool):
                            raise invalid("rsvp must be a boolean.")
                        if name != "rsvp" and (
                            not isinstance(item[name], str)
                            or len(item[name]) > 16000
                            or "\x00" in item[name]
                        ):
                            raise invalid(f"{name} must be text of at most 16000 characters.")
                        prop.params[param] = (
                            "TRUE"
                            if item[name] is True
                            else "FALSE"
                            if item[name] is False
                            else item[name]
                        )
        else:
            vals = value if isinstance(value, list) else [value]
            if len(vals) > 100 or any(
                not isinstance(v, str) or len(v) > 16000 or "\x00" in v for v in vals
            ):
                raise invalid(
                    f"{key} must contain at most 100 text values of at most 16000 characters."
                )
            for v in vals:
                event.add(key.upper(), v)


def _uri(value: Any, field: str) -> None:
    if not isinstance(value, str) or len(value) > 2048:
        raise invalid(f"{field} must be an absolute URI (maximum 2048 characters).")
    try:
        parsed = urlsplit(value)
        _port = parsed.port  # Forces validation of any authority port.
    except ValueError:
        raise invalid(f"{field} must be an absolute URI (maximum 2048 characters).") from None
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", parsed.scheme)
        or not value[len(parsed.scheme) + 1 :]
        or any(ch.isspace() or category(ch) == "Cc" for ch in value)
        or re.search(r"%(?![0-9A-Fa-f]{2})", value)
        or (parsed.scheme.lower() in {"http", "https"} and not parsed.hostname)
        or (parsed.scheme.lower() == "mailto" and not parsed.path)
    ):
        raise invalid(f"{field} must be an absolute URI (maximum 2048 characters).")


ALARM_DURATION = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W|(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?)$"
)


def validate_alarms(value: list[AlarmInput] | None) -> list[Alarm] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 10:
        raise invalid("alarms must be a list containing at most 10 alarm objects.")
    result = []
    allowed = {"trigger", "description", "action", "related"}
    for item in value:
        if not isinstance(item, dict) or set(item) - allowed or "trigger" not in item:
            raise invalid(
                "Each alarm requires trigger and accepts only trigger, description, "
                "action, related."
            )
        trigger = item["trigger"]
        if not isinstance(trigger, str) or len(trigger) > 100:
            raise invalid("Alarm trigger must be a relative RFC5545 duration such as -PT15M.")
        match = ALARM_DURATION.fullmatch(trigger) if isinstance(trigger, str) else None
        units = ("weeks", "days", "hours", "minutes", "seconds")
        if not match or not any(match.group(k) for k in units):
            raise invalid("Alarm trigger must be a relative RFC5545 duration such as -PT15M.")
        if match.group("weeks") and any(
            match.group(k) for k in ("days", "hours", "minutes", "seconds")
        ):
            raise invalid("Alarm duration weeks cannot be combined with other units.")
        if "T" in trigger and not any(match.group(k) for k in ("hours", "minutes", "seconds")):
            raise invalid("Alarm duration has an empty time part.")
        if not match.group("weeks") and not any(
            match.group(k) for k in ("days", "hours", "minutes", "seconds")
        ):
            raise invalid("Alarm duration must include a duration unit.")
        description = item.get("description", "Reminder")
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise invalid("Alarm description must contain 1–500 characters.")
        action = item.get("action", "DISPLAY")
        related = item.get("related", "START")
        if action != "DISPLAY":
            raise invalid("Only DISPLAY alarms are supported.")
        if related not in ("START", "END"):
            raise invalid("Alarm related must be START or END.")
        seconds = sum(
            int(match.group(k) or 0) * factor
            for k, factor in (
                ("weeks", 604800),
                ("days", 86400),
                ("hours", 3600),
                ("minutes", 60),
                ("seconds", 1),
            )
        )
        try:
            delta = timedelta(seconds=seconds * (-1 if match.group("sign") == "-" else 1))
        except OverflowError:
            raise invalid("Alarm trigger duration is too large.") from None
        alarm = Alarm()
        alarm.add("ACTION", action)
        alarm.add("DESCRIPTION", description)
        if related == "END":
            alarm.add("TRIGGER", delta, parameters={"RELATED": related})
        else:
            alarm.add("TRIGGER", delta)
        result.append(alarm)
    return result


class CalendarService:
    def __init__(self, config: Config, store: CalDAVStore | None = None) -> None:
        self.config = config
        self.store = store or CalDAVStore(config)
        self.tz = zone(config.timezone)
        self.lock = threading.RLock()

    def list_calendars(self) -> dict[str, Any]:
        return {"calendars": self.store.list_calendars(), "default_timezone": self.tz.key}

    def _events(
        self, start: str, end: str, calendar_id: str | None
    ) -> tuple[list[EventInfo], datetime, datetime]:
        a, b = bounded_range(start, end, self.tz, self.config.max_range_days)
        result = []
        for cid, eid, data in self.store.resources(calendar_id, a, b):
            result.extend(
                expand(parse_calendar(data), cid, eid, self.tz, a, b, self.config.max_events)
            )
            if len(result) > self.config.max_events:
                raise CalendarError(
                    "limit_exceeded",
                    "Too many event occurrences; use a smaller range or select one calendar.",
                )
        result.sort(
            key=lambda e: (
                interval(e, self.tz)[0],
                e.calendar_id,
                e.event_id,
                e.occurrence_id or "",
            )
        )
        return result, a, b

    @staticmethod
    def _page(
        events: list[EventInfo], a: datetime, b: datetime, limit: int, offset: int
    ) -> dict[str, Any]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(offset) is not int
            or offset < 0
        ):
            raise invalid("limit must be 1–1000 and offset must be nonnegative.")
        page = []
        size = 0
        for event in events[offset : offset + limit]:
            item = event.json()
            item_size = len(json.dumps(item, ensure_ascii=False).encode())
            if item_size > 128000:
                raise CalendarError("limit_exceeded", "An event is too large for a tool response.")
            if size + item_size > 128000:
                break
            page.append(item)
            size += item_size
        next_offset = offset + len(page) if offset + len(page) < len(events) else None
        return {
            "events": page,
            "start": a.isoformat(),
            "end": b.isoformat(),
            "total": len(events),
            "next_offset": next_offset,
            "truncated": next_offset is not None,
        }

    def list_events(
        self,
        start: str,
        end: str,
        calendar_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        events, a, b = self._events(start, end, calendar_id)
        return self._page(events, a, b, limit, offset)

    def search_events(
        self,
        query: str,
        start: str,
        end: str,
        calendar_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not query.strip() or len(query) > 500:
            raise invalid("query must contain 1–500 characters.")
        events, a, b = self._events(start, end, calendar_id)
        needle = query.casefold()
        matches = [
            e
            for e in events
            if any(needle in v.casefold() for v in (e.summary, e.description, e.location))
        ]
        if any(e.text_truncated for e in events):
            raise CalendarError(
                "limit_exceeded",
                "Some searchable event text exceeds the text limit; "
                "search it in a calendar client.",
            )
        return self._page(matches, a, b, limit, offset)

    def get_event(
        self, calendar_id: str, event_id: str, occurrence_id: str | None = None
    ) -> dict[str, Any]:
        data, etag = self.store.get(calendar_id, event_id)
        calendar = parse_calendar(data)
        event = master_event(calendar)
        if occurrence_id is not None:
            if not is_recurring(calendar):
                raise invalid("occurrence_id requires a recurring resource.")
            event = occurrence_event(calendar, occurrence_id, self.tz, self.config.max_events)
        return {
            "event": normalize(
                event,
                calendar_id,
                event_id,
                self.tz,
                recurrence_info(calendar),
                etag,
                occurrence=occurrence_id is not None,
            ).json(),
            "representation": "occurrence"
            if occurrence_id is not None
            else ("series" if is_recurring(calendar) else "event"),
        }

    def create_event(
        self,
        calendar_id: str,
        summary: str,
        start: str,
        end: str | None = None,
        duration_minutes: int | None = None,
        all_day: bool = False,
        description: str = "",
        location: str = "",
        timezone: str | None = None,
        recurrence_rule: str | None = None,
        alarms: list[AlarmInput] | None = None,
        categories: list[str] | None = None,
        priority: int | None = None,
        geo: GeoInput | None = None,
        attach: list[str] | None = None,
        comment: list[str] | None = None,
        resources: list[str] | None = None,
        url: str | None = None,
        organizer: OrganizerInput | None = None,
        attendee: list[AttendeeInput] | None = None,
        contact: list[str] | None = None,
    ) -> dict[str, Any]:
        validate_text(summary, description, location)
        alarm_components = validate_alarms(alarms)
        tz = zone(timezone) if timezone else self.tz
        a, b = event_times(start, end, duration_minutes, all_day, tz)
        if not all_day:
            a, b = as_aware(a, tz).astimezone(tz), as_aware(b, tz).astimezone(tz)
        uid = str(uuid4())
        calendar = Calendar()
        calendar.add("PRODID", "-//radicale-mcp//EN")
        calendar.add("VERSION", "2.0")
        event = Event()
        _event_properties(
            event,
            {
                "categories": categories,
                "priority": priority,
                "geo": geo,
                "attach": attach,
                "comment": comment,
                "resources": resources,
                "url": url,
                "organizer": organizer,
                "attendee": attendee,
                "contact": contact,
            },
        )
        for key, value in {
            "UID": uid,
            "DTSTAMP": datetime.now(UTC),
            "DTSTART": a,
            "DTEND": b,
            "SUMMARY": summary,
            "DESCRIPTION": description,
            "LOCATION": location,
            "SEQUENCE": 0,
        }.items():
            event.add(key, value)
        if recurrence_rule is not None:
            validate_rule(event, recurrence_rule)
            validate_changed_event(event, tz, True)
        for alarm in alarm_components or []:
            event.add_component(alarm)
        calendar.add_component(event)
        info, _ = self.store.select(calendar_id)[0]
        event_id = resource_id(info.url + uid + ".ics", info.url)
        etag = self.store.write(calendar_id, event_id, serialize(calendar))
        return {
            "event": normalize(
                event, calendar_id, event_id, self.tz, recurrence_info(calendar), etag
            ).json()
        }

    def _mutable(self, calendar_id: str, event_id: str, etag: str) -> tuple[Calendar, Event]:
        if not re.fullmatch(r'"[^"\r\n]{1,512}"', etag):
            raise invalid(
                "Supply the strong, quoted etag returned by get_event; "
                "wildcard/weak ETags are not allowed."
            )
        data, current = self.store.get(calendar_id, event_id)
        if current is None:
            raise CalendarError(
                "missing_etag", "Radicale did not return an ETag; mutation is refused."
            )
        if current != etag:
            raise CalendarError(
                "stale_etag",
                "The event changed remotely; fetch it again and review before retrying.",
            )
        calendar = parse_calendar(data)
        if not is_recurring(calendar) and len(calendar.walk("VEVENT")) != 1:
            raise invalid("A non-recurring resource must contain one event.")
        return calendar, master_event(calendar)

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        etag: str,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        duration_minutes: int | None = None,
        description: str | None = None,
        location: str | None = None,
        timezone: str | None = None,
        occurrence_id: str | None = None,
        scope: MutationScope = "event",
        recurrence_rule: str | None = None,
        alarms: list[AlarmInput] | None = None,
        categories: list[str] | None = None,
        priority: int | None = None,
        geo: GeoInput | None = None,
        attach: list[str] | None = None,
        comment: list[str] | None = None,
        resources: list[str] | None = None,
        url: str | None = None,
        organizer: OrganizerInput | None = None,
        attendee: list[AttendeeInput] | None = None,
        contact: list[str] | None = None,
    ) -> dict[str, Any]:
        validate_text(summary, description, location)
        alarm_components = validate_alarms(alarms)
        if all(
            v is None
            for v in (
                summary,
                start,
                end,
                duration_minutes,
                description,
                location,
                timezone,
                recurrence_rule,
                alarms,
                categories,
                priority,
                geo,
                attach,
                comment,
                resources,
                url,
                organizer,
                attendee,
                contact,
            )
        ):
            raise invalid("Provide at least one field to update.")
        calendar, event = self._mutable(calendar_id, event_id, etag)
        validate_scope(calendar, scope, occurrence_id)
        recurring = is_recurring(calendar)
        schedule_change = any(
            v is not None for v in (start, end, duration_minutes, timezone, recurrence_rule)
        )
        if recurrence_rule is not None and scope not in ("series", "future"):
            raise invalid("recurrence_rule may only be changed with scope='series' or 'future'.")
        if scope == "future":
            assert occurrence_id is not None
            return self._update_future(
                calendar_id,
                event_id,
                etag,
                calendar,
                occurrence_id,
                summary,
                start,
                end,
                duration_minutes,
                description,
                location,
                timezone,
                recurrence_rule,
                schedule_change,
                alarm_components,
                {
                    "categories": categories,
                    "priority": priority,
                    "geo": geo,
                    "attach": attach,
                    "comment": comment,
                    "resources": resources,
                    "url": url,
                    "organizer": organizer,
                    "attendee": attendee,
                    "contact": contact,
                },
            )
        if scope == "series":
            validate_structure(calendar)
            if schedule_change:
                guard_series_schedule(calendar, timezone, recurrence_rule)
        if scope == "occurrence":
            assert occurrence_id is not None
            event = occurrence_event(calendar, occurrence_id, self.tz, self.config.max_events)
        targets = cast(list[Event], calendar.walk("VEVENT")) if scope == "series" else [event]
        for target in targets:
            self._patch_event(
                target,
                summary,
                start,
                end,
                duration_minutes,
                description,
                location,
                timezone,
                preserve_floating=recurring,
            )
            _event_properties(
                target,
                {
                    "categories": categories,
                    "priority": priority,
                    "geo": geo,
                    "attach": attach,
                    "comment": comment,
                    "resources": resources,
                    "url": url,
                    "organizer": organizer,
                    "attendee": attendee,
                    "contact": contact,
                },
            )
            if alarms is not None:
                target.subcomponents = [c for c in target.subcomponents if c.name != "VALARM"]
                for alarm in alarm_components or []:
                    target.add_component(alarm.copy())
            validate_changed_event(target, self.tz, recurring)
        if scope == "series" and schedule_change:
            validate_rule(event, recurrence_rule)
        if scope == "occurrence":
            replace_occurrence(calendar, event)
        touch(calendar)
        new_etag = self.store.write(calendar_id, event_id, serialize(calendar), etag)
        return {
            "event": normalize(
                event,
                calendar_id,
                event_id,
                self.tz,
                recurrence_info(calendar),
                new_etag,
                occurrence=scope == "occurrence",
            ).json(),
            "scope": scope,
        }

    def _update_future(
        self,
        calendar_id: str,
        event_id: str,
        etag: str,
        calendar: Calendar,
        occurrence_id: str,
        summary: str | None,
        start: str | None,
        end: str | None,
        duration_minutes: int | None,
        description: str | None,
        location: str | None,
        timezone: str | None,
        recurrence_rule: str | None,
        schedule_change: bool,
        alarm_components: list[Alarm] | None,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        past, future, prior_count = split_calendar(
            calendar, occurrence_id, self.tz, self.config.max_events
        )
        if schedule_change:
            if has_future_exceptions(future):
                raise CalendarError(
                    "unsupported_recurrence_mutation",
                    "Future schedule changes with future overrides/RDATE/EXDATE are refused; "
                    "earlier exceptions are allowed. Use occurrence edits or a calendar client.",
                )
            # Retaining UNTIL means retaining its original inclusive absolute boundary.
            current_rule = cast(vRecur, master_event(future)["RRULE"]).to_ical().decode()
            guard_series_schedule(future, timezone, recurrence_rule or current_rule)
        for event in cast(list[Event], future.walk("VEVENT")):
            self._patch_event(
                event,
                summary,
                start,
                end,
                duration_minutes,
                description,
                location,
                timezone,
                preserve_floating=True,
            )
            _event_properties(event, properties)
            if alarm_components is not None:
                event.subcomponents = [c for c in event.subcomponents if c.name != "VALARM"]
                for alarm in alarm_components:
                    event.add_component(alarm.copy())
            validate_changed_event(event, self.tz, True)
        validate_rule(master_event(future), recurrence_rule)
        touch(future)
        if past is None:
            new_etag = self.store.write(calendar_id, event_id, serialize(future), etag)
            return {
                "scope": "future",
                "split": False,
                "event": normalize(
                    master_event(future),
                    calendar_id,
                    event_id,
                    self.tz,
                    recurrence_info(future),
                    new_etag,
                ).json(),
            }
        touch(past)
        # Identical retries of an unchanged original must not create multiple successors.
        identity = json.dumps(
            [
                calendar_id,
                event_id,
                etag,
                occurrence_id,
                summary,
                start,
                end,
                duration_minutes,
                description,
                location,
                timezone,
                recurrence_rule,
                properties,
                [a.to_ical().decode() for a in alarm_components]
                if alarm_components is not None
                else None,
            ],
            separators=(",", ":"),
        )
        uid = str(uuid5(NAMESPACE_URL, identity))
        set_uid(future, uid, str(master_event(calendar)["UID"]))
        info, _ = self.store.select(calendar_id)[0]
        successor_id = resource_id(info.url + uid + ".ics", info.url)
        # Serialize and validate both resources before the first remote mutation.
        old_data, new_data = serialize(past), serialize(future)
        details = recovery_details(calendar_id, event_id, successor_id)

        def recovery(stage: str) -> CalendarError:
            return CalendarError(
                "split_recovery_required",
                "The series split may be incomplete. Do not retry blindly. "
                + json.dumps({**details, "stage": stage}),
            )

        try:
            new_etag = self.store.write(calendar_id, successor_id, new_data)
        except Exception:
            # Even a lost response can mean the new resource was committed. Never
            # remove a resource without a known version or repeat with another UID.
            raise recovery("create_successor") from None
        if new_etag is None or not re.fullmatch(r'"[^"\r\n]{1,512}"', new_etag):
            raise recovery("successor_missing_strong_etag")
        try:
            old_etag = self.store.write(calendar_id, event_id, old_data, etag)
        except CalendarError as exc:
            if exc.code in {
                "stale_etag",
                "permission_denied",
                "authentication_failed",
                "not_found",
                "conflict",
                "rate_limited",
                "calendar_not_found",
                "unsafe_url",
                "redirect_refused",
            }:
                try:
                    self.store.delete(calendar_id, successor_id, new_etag)
                except Exception:
                    raise recovery("rollback_successor") from None
                raise
            raise recovery("shorten_original") from None
        except Exception:
            raise recovery("shorten_original") from None
        return {
            "scope": "future",
            "split": True,
            "boundary_occurrence_id": occurrence_id,
            "original": {
                "calendar_id": calendar_id,
                "event_id": event_id,
                "etag": old_etag,
                "generated_occurrences_before_boundary": prior_count,
            },
            "event": normalize(
                master_event(future),
                calendar_id,
                successor_id,
                self.tz,
                recurrence_info(future),
                new_etag,
            ).json(),
        }

    def _patch_event(
        self,
        event: Event,
        summary: str | None,
        start: str | None,
        end: str | None,
        duration_minutes: int | None,
        description: str | None,
        location: str | None,
        timezone: str | None,
        preserve_floating: bool,
    ) -> None:
        for key, value in (
            ("SUMMARY", summary),
            ("DESCRIPTION", description),
            ("LOCATION", location),
        ):
            if value is not None:
                event.pop(key, None)
                event.add(key, value)
        change_time = any(v is not None for v in (start, end, duration_minutes))
        if timezone and not change_time:
            raise invalid("timezone requires an explicit time change.")
        if change_time:
            old_start, old_end = event_span(event, self.tz)
            all_day = not isinstance(old_start, datetime)
            original_tz = getattr(old_start, "tzinfo", None)
            old_zone = getattr(original_tz, "key", None) or (
                "UTC" if str(original_tz) == "UTC" else None
            )
            tz = zone(timezone or old_zone or self.tz.key)
            supplied_start = start or old_start.isoformat()
            supplied_end = end
            if end is None and duration_minutes is None:
                if all_day:
                    from .timeutils import day

                    supplied_end = (
                        day(supplied_start)
                        + (as_aware(old_end, tz).date() - as_aware(old_start, tz).date())
                    ).isoformat()
                else:
                    from .timeutils import timestamp

                    delta = as_aware(old_end, tz).astimezone(UTC) - as_aware(
                        old_start, tz
                    ).astimezone(UTC)
                    new_start = timestamp(supplied_start, tz)
                    supplied_end = (
                        (new_start.astimezone(UTC) + delta).astimezone(new_start.tzinfo).isoformat()
                    )
            a, b = event_times(supplied_start, supplied_end, duration_minutes, all_day, tz)
            if not all_day:
                a, b = as_aware(a, tz).astimezone(tz), as_aware(b, tz).astimezone(tz)
            if preserve_floating and isinstance(old_start, datetime) and old_start.tzinfo is None:
                if timezone is not None:
                    raise invalid("A floating recurrence must remain floating; omit timezone.")
                a, b = as_aware(a, tz).replace(tzinfo=None), as_aware(b, tz).replace(tzinfo=None)
            for key in ("DTSTART", "DTEND", "DURATION"):
                event.pop(key, None)
            event.add("DTSTART", a)
            event.add("DTEND", b)

    def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        etag: str,
        occurrence_id: str | None = None,
        scope: MutationScope = "event",
    ) -> dict[str, Any]:
        calendar, _ = self._mutable(calendar_id, event_id, etag)
        validate_scope(calendar, scope, occurrence_id)
        if scope == "future":
            assert occurrence_id is not None
            past, _, _ = split_calendar(calendar, occurrence_id, self.tz, self.config.max_events)
            new_etag = None
            if past is None:
                self.store.delete(calendar_id, event_id, etag)
            else:
                touch(past)
                new_etag = self.store.write(calendar_id, event_id, serialize(past), etag)
            return {
                "deleted": True,
                "scope": "future",
                "series_deleted": past is None,
                "calendar_id": calendar_id,
                "event_id": event_id,
                "boundary_occurrence_id": occurrence_id,
                "etag": new_etag,
            }
        if scope == "occurrence":
            assert occurrence_id is not None
            event = occurrence_event(calendar, occurrence_id, self.tz, self.config.max_events)
            normalized = normalize(
                event, calendar_id, event_id, self.tz, recurrence_info(calendar), occurrence=True
            )
            exclude_occurrence(calendar, event)
            touch(calendar)
            new_etag = self.store.write(calendar_id, event_id, serialize(calendar), etag)
            return {
                "deleted": True,
                "scope": scope,
                "series_deleted": False,
                "calendar_id": calendar_id,
                "event_id": event_id,
                "occurrence_id": normalized.occurrence_id,
                "etag": new_etag,
            }
        self.store.delete(calendar_id, event_id, etag)
        return {
            "deleted": True,
            "scope": scope,
            "series_deleted": scope == "series",
            "calendar_id": calendar_id,
            "event_id": event_id,
        }

    def _busy(
        self, start: str, end: str, calendar_id: str | None
    ) -> tuple[list[tuple[datetime, datetime]], datetime, datetime]:
        events, a, b = self._events(start, end, calendar_id)
        busy = []
        for event in events:
            if event.transparent or event.status.upper() == "CANCELLED":
                continue
            left, right = interval(event, self.tz)
            busy.append((max(a, left), min(b, right)))
        return merge_intervals(busy), a, b

    @staticmethod
    def _intervals(intervals: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
        return [
            {
                "start": a.isoformat(),
                "end": b.isoformat(),
                "duration_minutes": (b - a).total_seconds() / 60,
            }
            for a, b in intervals
        ]

    def get_free_busy(self, start: str, end: str, calendar_id: str | None = None) -> dict[str, Any]:
        busy, a, b = self._busy(start, end, calendar_id)
        return {
            "start": a.isoformat(),
            "end": b.isoformat(),
            "busy": self._intervals(busy),
            "complete": True,
            "timezone": "UTC",
        }

    def find_available_slots(
        self,
        start: str,
        end: str,
        duration_minutes: int,
        calendar_id: str | None = None,
        working_start: str | None = None,
        working_end: str | None = None,
        weekdays: list[int] | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        if type(duration_minutes) is not int or not 1 <= duration_minutes <= 527040:
            raise invalid("duration_minutes must be a positive integer, at most 366 days.")
        busy, a, b = self._busy(start, end, calendar_id)
        tz = zone(timezone) if timezone else self.tz
        windows = working_windows(a, b, tz, working_start, working_end, weekdays)
        slots = free_intervals(busy, windows, duration_minutes)
        if len(slots) > 1000:
            raise CalendarError(
                "limit_exceeded", "More than 1000 availability windows; use a smaller range."
            )
        return {
            "slots": self._intervals(slots),
            "duration_minutes": duration_minutes,
            "complete": True,
            "timezone": "UTC",
            "working_timezone": tz.key,
            "semantics": "Maximal free windows large enough for the requested duration; "
            "no reservation is made.",
        }

    def _tasks(
        self,
        calendar_id: str | None,
        state: TaskState,
        start: str | None,
        end: str | None,
        include_undated: bool,
    ) -> tuple[list[TaskInfo], datetime | None, datetime | None]:
        if state not in {"open", "completed", "all"}:
            raise invalid("state must be 'open', 'completed', or 'all'.")
        if (start is None) != (end is None):
            raise invalid("Provide both start and end, or neither, when filtering tasks.")
        a: datetime | None = None
        b: datetime | None = None
        if start is not None and end is not None:
            a, b = bounded_range(start, end, self.tz, self.config.max_range_days)
        result: list[TaskInfo] = []
        for cid, task_id, data in self.store.task_resources(calendar_id):
            calendar = parse_task_calendar(data)
            result.extend(
                expand_tasks(calendar, cid, task_id, self.tz, a, b, self.config.max_events)
            )
            if len(result) > self.config.max_events:
                raise CalendarError(
                    "limit_exceeded",
                    "Too many tasks or task occurrences; narrow the filters or select a calendar.",
                )
        filtered: list[TaskInfo] = []
        for task in result:
            if state == "open" and (task.completed or task.status == "CANCELLED"):
                continue
            if state == "completed" and not task.completed:
                continue
            point = effective_time(task, self.tz)
            if a is not None and b is not None:
                if point is None and not include_undated:
                    continue
                if point is not None and not a <= point < b:
                    continue
            filtered.append(task)
        filtered.sort(
            key=lambda task: (
                effective_time(task, self.tz) is None,
                effective_time(task, self.tz) or datetime.max.replace(tzinfo=UTC),
                task.calendar_id,
                task.task_id,
                task.occurrence_id or "",
            )
        )
        return filtered, a, b

    @staticmethod
    def _task_page(
        tasks: list[TaskInfo],
        a: datetime | None,
        b: datetime | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(offset) is not int
            or offset < 0
        ):
            raise invalid("limit must be 1–1000 and offset must be nonnegative.")
        page: list[dict[str, Any]] = []
        size = 0
        for task in tasks[offset : offset + limit]:
            item = task.json()
            item_size = len(json.dumps(item, ensure_ascii=False).encode())
            if item_size > 128000:
                raise CalendarError("limit_exceeded", "A task is too large for a tool response.")
            if size + item_size > 128000:
                break
            page.append(item)
            size += item_size
        next_offset = offset + len(page) if offset + len(page) < len(tasks) else None
        return {
            "tasks": page,
            "start": a.isoformat() if a else None,
            "end": b.isoformat() if b else None,
            "total": len(tasks),
            "next_offset": next_offset,
            "truncated": next_offset is not None,
            "range_semantics": "DUE when present, otherwise DTSTART; [start,end).",
        }

    def list_tasks(
        self,
        calendar_id: str | None = None,
        state: TaskState = "open",
        start: str | None = None,
        end: str | None = None,
        include_undated: bool = True,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        tasks, a, b = self._tasks(calendar_id, state, start, end, include_undated)
        return self._task_page(tasks, a, b, limit, offset)

    def search_tasks(
        self,
        query: str,
        calendar_id: str | None = None,
        state: TaskState = "open",
        start: str | None = None,
        end: str | None = None,
        include_undated: bool = True,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not query.strip() or len(query) > 500:
            raise invalid("query must contain 1–500 characters.")
        tasks, a, b = self._tasks(calendar_id, state, start, end, include_undated)
        needle = query.casefold()
        matches = [
            task
            for task in tasks
            if any(
                needle in value.casefold()
                for value in (task.summary, task.description, task.location, *task.categories)
            )
        ]
        if any(task.text_truncated for task in tasks):
            raise CalendarError(
                "limit_exceeded",
                "Some searchable task text exceeds the text limit; search it in a task client.",
            )
        return self._task_page(matches, a, b, limit, offset)

    def get_task(
        self, calendar_id: str, task_id: str, occurrence_id: str | None = None
    ) -> dict[str, Any]:
        data, etag = self.store.get_task(calendar_id, task_id)
        calendar = parse_task_calendar(data)
        recurrence = task_recurrence_info(calendar)
        task = master_task(calendar)
        if occurrence_id is not None:
            if recurrence is None:
                raise invalid("occurrence_id requires a recurring task resource.")
            try:
                if len(occurrence_id) == 10:
                    anchor = datetime.combine(
                        date.fromisoformat(occurrence_id), datetime.min.time()
                    )
                    anchor = as_aware(anchor, self.tz).astimezone(UTC)
                else:
                    anchor = timestamp(occurrence_id, self.tz).astimezone(UTC)
            except (ValueError, CalendarError):
                raise invalid(
                    "occurrence_id must be the exact value returned by list_tasks."
                ) from None
            source_start, source_due = task_dates(master_task(calendar), self.tz)
            if source_start is not None and source_due is not None:
                anchor += as_aware(source_due, self.tz).astimezone(UTC) - as_aware(
                    source_start, self.tz
                ).astimezone(UTC)
            candidates = expand_tasks(
                calendar,
                calendar_id,
                task_id,
                self.tz,
                anchor - self._task_lookup_padding(),
                anchor + self._task_lookup_padding(),
                self.config.max_events,
            )
            item = next(
                (candidate for candidate in candidates if candidate.occurrence_id == occurrence_id),
                None,
            )
            if item is None:
                raise CalendarError("occurrence_not_found", "That task occurrence does not exist.")
            output = item.json()
            output["etag"] = etag
            return {"task": output, "representation": "occurrence"}
        return {
            "task": normalize_task(task, calendar_id, task_id, self.tz, recurrence, etag).json(),
            "representation": "series" if recurrence else "task",
        }

    @staticmethod
    def _task_lookup_padding() -> timedelta:
        return timedelta(days=2)

    def _task_value(self, value: str, all_day: bool, tz: Any) -> date | datetime:
        if all_day:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                parsed: date | datetime = date.fromisoformat(value)
            except ValueError:
                raise invalid("All-day task dates require YYYY-MM-DD.") from None
        else:
            parsed = timestamp(value, tz)
        if isinstance(parsed, datetime):
            return parsed.astimezone(tz)
        return parsed

    @staticmethod
    def _task_integer(value: int | None, name: str, low: int, high: int) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
        ):
            raise invalid(f"{name} must be an integer from {low} through {high}.")

    def create_task(
        self,
        calendar_id: str,
        summary: str,
        start: str | None = None,
        due: str | None = None,
        all_day: bool = False,
        description: str = "",
        location: str = "",
        categories: list[str] | None = None,
        priority: int | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        validate_task_text(summary, description, location, categories)
        self._task_integer(priority, "priority", 0, 9)
        tz = zone(timezone) if timezone else self.tz
        if all_day and start is None and due is None:
            raise invalid("all_day requires start or due.")
        if timezone is not None and start is None and due is None:
            raise invalid("timezone requires start or due.")
        now = datetime.now(UTC)
        uid = str(uuid4())
        calendar = Calendar()
        calendar.add("PRODID", "-//radicale-mcp//EN")
        calendar.add("VERSION", "2.0")
        task = Todo()
        for key, value in {
            "UID": uid,
            "DTSTAMP": now,
            "CREATED": now,
            "LAST-MODIFIED": now,
            "SUMMARY": summary,
            "DESCRIPTION": description,
            "LOCATION": location,
            "STATUS": "NEEDS-ACTION",
            "PERCENT-COMPLETE": 0,
            "SEQUENCE": 0,
        }.items():
            task.add(key, value)
        if start is not None:
            task.add("DTSTART", self._task_value(start, all_day, tz))
        if due is not None:
            task.add("DUE", self._task_value(due, all_day, tz))
        if categories:
            task.add("CATEGORIES", [value.strip() for value in categories])
        if priority is not None:
            task.add("PRIORITY", priority)
        validate_task(task, self.tz)
        calendar.add_component(task)
        info, _ = self.store.select(calendar_id)[0]
        task_id = resource_id(info.url + uid + ".ics", info.url, "t")
        etag = self.store.write_task(calendar_id, task_id, serialize_task_calendar(calendar))
        return {"task": normalize_task(task, calendar_id, task_id, self.tz, None, etag).json()}

    def _mutable_task(self, calendar_id: str, task_id: str, etag: str) -> tuple[Calendar, Todo]:
        if not re.fullmatch(r'"[^"\r\n]{1,512}"', etag):
            raise invalid(
                "Supply the strong, quoted etag returned by get_task; "
                "wildcard/weak ETags are not allowed."
            )
        data, current = self.store.get_task(calendar_id, task_id)
        if current is None:
            raise CalendarError(
                "missing_etag", "Radicale did not return an ETag; mutation is refused."
            )
        if current != etag:
            raise CalendarError(
                "stale_etag",
                "The task changed remotely; fetch it again and review before retrying.",
            )
        calendar = parse_task_calendar(data)
        if is_recurring_task(calendar):
            raise CalendarError(
                "unsupported_recurrence_mutation",
                "Recurring VTODO mutation is not supported in v4; use a task client.",
            )
        tasks = calendar.walk("VTODO")
        if len(tasks) != 1:
            raise CalendarError(
                "invalid_calendar_data", "A non-recurring task resource must contain one VTODO."
            )
        return calendar, cast(Todo, tasks[0])

    def update_task(
        self,
        calendar_id: str,
        task_id: str,
        etag: str,
        summary: str | None = None,
        start: str | None = None,
        due: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        location: str | None = None,
        categories: list[str] | None = None,
        priority: int | None = None,
        percent_complete: int | None = None,
        status: str | None = None,
        timezone: str | None = None,
        clear_start: bool = False,
        clear_due: bool = False,
        clear_priority: bool = False,
        clear_percent_complete: bool = False,
    ) -> dict[str, Any]:
        validate_task_text(summary, description, location, categories)
        self._task_integer(priority, "priority", 0, 9)
        self._task_integer(percent_complete, "percent_complete", 0, 99)
        if status is not None:
            status = status.upper()
            if status not in VALID_STATUSES:
                raise invalid("status must be NEEDS-ACTION, IN-PROCESS, COMPLETED, or CANCELLED.")
            if status == "COMPLETED":
                raise invalid("Use complete_task to set a task COMPLETED consistently.")
        if start is not None and clear_start or due is not None and clear_due:
            raise invalid("Do not supply and clear the same task date.")
        values = (
            summary,
            start,
            due,
            all_day,
            description,
            location,
            categories,
            priority,
            percent_complete,
            status,
            timezone,
        )
        if not any(value is not None for value in values) and not any(
            (clear_start, clear_due, clear_priority, clear_percent_complete)
        ):
            raise invalid("Provide at least one field to update or clear.")
        calendar, task = self._mutable_task(calendar_id, task_id, etag)
        if str(task.get("STATUS", "NEEDS-ACTION")).upper() == "COMPLETED" and (
            status is not None or percent_complete is not None or clear_percent_complete
        ):
            raise invalid("Use reopen_task before changing a completed task's status or progress.")
        current_start, current_due = task_dates(task, self.tz)
        current_values = [value for value in (current_start, current_due) if value is not None]
        current_all_day = bool(current_values) and all(
            not isinstance(value, datetime) for value in current_values
        )
        change_time = (
            any(value is not None for value in (start, due, all_day, timezone))
            or clear_start
            or clear_due
        )
        if timezone is not None and start is None and due is None:
            raise invalid("timezone requires an explicit start or due update.")
        if (
            all_day is not None
            and start is None
            and due is None
            and not clear_start
            and not clear_due
        ):
            raise invalid("all_day requires an explicit task date update or clear operation.")
        if change_time:
            target_all_day = current_all_day if all_day is None else all_day
            tz = zone(timezone) if timezone else self.tz
            desired_start = (
                None
                if clear_start
                else self._task_value(start, target_all_day, tz)
                if start is not None
                else current_start
            )
            desired_due = (
                None
                if clear_due
                else self._task_value(due, target_all_day, tz)
                if due is not None
                else current_due
            )
            if current_values and target_all_day != current_all_day:
                if (current_start is not None and start is None and not clear_start) or (
                    current_due is not None and due is None and not clear_due
                ):
                    raise invalid(
                        "Changing all_day requires explicit replacement or clearing of every "
                        "existing task date."
                    )
            preserve_floating = (
                not target_all_day
                and timezone is None
                and current_values
                and all(
                    isinstance(value, datetime) and value.tzinfo is None for value in current_values
                )
            )
            if preserve_floating:
                desired_start = (
                    desired_start.replace(tzinfo=None)
                    if isinstance(desired_start, datetime)
                    else desired_start
                )
                desired_due = (
                    desired_due.replace(tzinfo=None)
                    if isinstance(desired_due, datetime)
                    else desired_due
                )
            for key in ("DTSTART", "DUE", "DURATION"):
                task.pop(key, None)
            if desired_start is not None:
                task.add("DTSTART", desired_start)
            if desired_due is not None:
                task.add("DUE", desired_due)
        for key, value in (
            ("SUMMARY", summary),
            ("DESCRIPTION", description),
            ("LOCATION", location),
        ):
            if value is not None:
                task.pop(key, None)
                task.add(key, value)
        if categories is not None:
            task.pop("CATEGORIES", None)
            if categories:
                task.add("CATEGORIES", [value.strip() for value in categories])
        for key, integer_value, clear in (
            ("PRIORITY", priority, clear_priority),
            ("PERCENT-COMPLETE", percent_complete, clear_percent_complete),
        ):
            if integer_value is not None or clear:
                task.pop(key, None)
                if integer_value is not None:
                    task.add(key, integer_value)
        if status is not None:
            if str(task.get("STATUS", "")).upper() == "COMPLETED":
                raise invalid("Use reopen_task before changing a completed task's status.")
            task.pop("STATUS", None)
            task.add("STATUS", status)
        validate_task(task, self.tz)
        touch_task(task)
        new_etag = self.store.write_task(
            calendar_id, task_id, serialize_task_calendar(calendar), etag
        )
        return {"task": normalize_task(task, calendar_id, task_id, self.tz, None, new_etag).json()}

    def complete_task(
        self, calendar_id: str, task_id: str, etag: str, completed_at: str | None = None
    ) -> dict[str, Any]:
        calendar, task = self._mutable_task(calendar_id, task_id, etag)
        if str(task.get("STATUS", "")).upper() == "COMPLETED":
            return {
                "changed": False,
                "task": normalize_task(task, calendar_id, task_id, self.tz, None, etag).json(),
            }
        completed = (
            timestamp(completed_at, self.tz).astimezone(UTC)
            if completed_at is not None
            else datetime.now(UTC)
        )
        for key in ("STATUS", "PERCENT-COMPLETE", "COMPLETED"):
            task.pop(key, None)
        task.add("STATUS", "COMPLETED")
        task.add("PERCENT-COMPLETE", 100)
        task.add("COMPLETED", completed)
        touch_task(task)
        new_etag = self.store.write_task(
            calendar_id, task_id, serialize_task_calendar(calendar), etag
        )
        return {
            "changed": True,
            "task": normalize_task(task, calendar_id, task_id, self.tz, None, new_etag).json(),
        }

    def reopen_task(self, calendar_id: str, task_id: str, etag: str) -> dict[str, Any]:
        calendar, task = self._mutable_task(calendar_id, task_id, etag)
        if (
            str(task.get("STATUS", "NEEDS-ACTION")).upper() == "NEEDS-ACTION"
            and "COMPLETED" not in task
        ):
            return {
                "changed": False,
                "task": normalize_task(task, calendar_id, task_id, self.tz, None, etag).json(),
            }
        for key in ("STATUS", "PERCENT-COMPLETE", "COMPLETED"):
            task.pop(key, None)
        task.add("STATUS", "NEEDS-ACTION")
        task.add("PERCENT-COMPLETE", 0)
        touch_task(task)
        new_etag = self.store.write_task(
            calendar_id, task_id, serialize_task_calendar(calendar), etag
        )
        return {
            "changed": True,
            "task": normalize_task(task, calendar_id, task_id, self.tz, None, new_etag).json(),
        }

    def delete_task(self, calendar_id: str, task_id: str, etag: str) -> dict[str, Any]:
        self._mutable_task(calendar_id, task_id, etag)
        self.store.delete_task(calendar_id, task_id, etag)
        return {"deleted": True, "calendar_id": calendar_id, "task_id": task_id}
