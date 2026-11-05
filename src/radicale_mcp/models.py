"""Normalized outputs are ordinary JSON-compatible dataclasses."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CalendarInfo:
    calendar_id: str
    name: str
    url: str


@dataclass(frozen=True)
class EventInfo:
    event_id: str
    calendar_id: str
    uid: str
    summary: str
    start: str
    end: str
    all_day: bool
    timezone: str | None
    floating: bool
    location: str
    description: str
    status: str
    transparent: bool
    etag: str | None
    recurrence: dict[str, Any] | None
    occurrence_id: str | None
    categories: list[str] = field(default_factory=list)
    priority: int | None = None
    geo: dict[str, float] | None = None
    attach: list[str] = field(default_factory=list)
    comment: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    url: str | None = None
    organizer: dict[str, str] | None = None
    attendee: list[dict[str, str]] = field(default_factory=list)
    contact: list[str] = field(default_factory=list)
    alarms: list[dict[str, str]] = field(default_factory=list)
    text_truncated: list[str] = field(default_factory=list)

    def json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskInfo:
    task_id: str
    calendar_id: str
    uid: str
    summary: str
    start: str | None
    due: str | None
    all_day: bool
    timezone: str | None
    floating: bool
    description: str
    location: str
    categories: list[str]
    status: str
    completed: bool
    completed_at: str | None
    percent_complete: int | None
    priority: int | None
    created: str | None
    last_modified: str | None
    etag: str | None
    recurrence: dict[str, Any] | None
    occurrence_id: str | None
    representation: str
    text_truncated: list[str] = field(default_factory=list)

    def json(self) -> dict[str, Any]:
        return asdict(self)
