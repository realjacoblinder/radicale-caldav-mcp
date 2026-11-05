"""Administrator-owned process configuration; never accepted by a tool."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import CalendarError


@dataclass(frozen=True)
class Config:
    url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    verify_ssl: bool = True
    timeout: int = 30
    timezone: str = "UTC"
    max_range_days: int = 93
    max_events: int = 5000
    log_level: str = "WARNING"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "")
            if not value or "${" in value:
                raise CalendarError("configuration", f"Set {name} in the process environment.")
            return value

        def integer(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(env.get(name, str(default)))
                if low <= value <= high:
                    return value
            except ValueError:
                pass
            raise CalendarError("configuration", f"{name} must be {low}–{high}.")

        url = required("CALDAV_URL").rstrip("/") + "/"
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(c.isspace() or ord(c) < 32 for c in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise CalendarError(
                "configuration",
                "CALDAV_URL must be an HTTP(S) URL without credentials, query, or fragment.",
            )
        username, password = required("CALDAV_USERNAME"), required("CALDAV_PASSWORD")
        verify = env.get("CALDAV_VERIFY_SSL", "true").lower()
        if verify not in ("true", "false"):
            raise CalendarError("configuration", "CALDAV_VERIFY_SSL must be true or false.")
        timezone = env.get("CALDAV_DEFAULT_TIMEZONE", "UTC")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise CalendarError(
                "configuration", "CALDAV_DEFAULT_TIMEZONE must be an IANA timezone."
            ) from None
        level = env.get("LOG_LEVEL", "WARNING").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise CalendarError(
                "configuration", "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
            )
        return cls(
            url,
            username,
            password,
            verify == "true",
            integer("CALDAV_TIMEOUT", 30, 1, 120),
            timezone,
            integer("CALDAV_MAX_RANGE_DAYS", 93, 1, 366),
            integer("CALDAV_MAX_EVENTS", 5000, 1, 10000),
            level,
        )
