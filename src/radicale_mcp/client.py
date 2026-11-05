"""CalDAV discovery and conditional HTTP writes through the established DAV client."""

import base64
import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

import niquests
from caldav.davclient import DAVClient
from caldav.lib import error as dav_error

from .config import Config
from .errors import CalendarError, invalid
from .models import CalendarInfo

_DISCOVERY_TTL_SECONDS = 30.0


def checked_url(url: str, base: str) -> str:
    """Never send this account's credentials to another origin or URL subtree."""
    try:
        target, root = urlsplit(url), urlsplit(base)

        def origin(p: Any) -> tuple[str, str, int]:
            return (p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80))

        path = target.path
        for _ in range(8):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        root_path = unquote(root.path)
        if (
            origin(target) != origin(root)
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)
            or "\\" in path
            or "%" in path
            or any(p in (".", "..") for p in path.split("/"))
            or not (path == root_path.rstrip("/") or path.startswith(root_path))
        ):
            raise ValueError
    except ValueError:
        raise CalendarError(
            "unsafe_url",
            "CalDAV returned a URL outside the configured server path, or an unsafe URL.",
        ) from None
    return url


class GuardedSession(niquests.Session):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.trust_env = False  # No alternate .netrc identity or ambient proxy credentials.

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        checked_url(str(url), self.base)
        kwargs["allow_redirects"] = False
        response = super().request(method, url, *args, **kwargs)
        status = response.status_code or 500
        messages = {
            401: ("authentication_failed", "Radicale rejected the configured credentials."),
            403: ("permission_denied", "This Radicale identity does not have permission."),
            404: ("not_found", "The calendar resource no longer exists."),
            409: ("conflict", "Radicale rejected a conflicting resource change; fetch it again."),
            412: (
                "stale_etag",
                "The calendar resource changed remotely; fetch it again before retrying.",
            ),
            429: ("rate_limited", "Radicale is rate limiting requests; try again later."),
        }
        if status in messages:
            raise CalendarError(*messages[status])
        if 300 <= status < 400:
            raise CalendarError(
                "redirect_refused",
                "Set CALDAV_URL to the final CalDAV endpoint; redirects are disabled.",
            )
        if status >= 400:
            raise CalendarError(
                "server_error", "Radicale rejected the request or is temporarily unavailable."
            )
        return response


@contextmanager
def safe_errors() -> Iterator[None]:
    try:
        yield
    except CalendarError:
        raise
    except (niquests.exceptions.RequestException, OSError):
        raise CalendarError(
            "unreachable", "Cannot reach Radicale; check URL, TLS, and network connectivity."
        ) from None
    except dav_error.NotFoundError:
        raise CalendarError("not_found", "The calendar resource no longer exists.") from None
    except dav_error.AuthorizationError:
        raise CalendarError(
            "permission_denied", "Radicale rejected authentication or authorization."
        ) from None
    except Exception:
        raise CalendarError(
            "caldav_error",
            "CalDAV operation failed; check the server configuration and calendar data.",
        ) from None


def calendar_info(url: str, name: str | None) -> CalendarInfo:
    return CalendarInfo(
        "c_" + hashlib.sha256(url.encode()).hexdigest()[:20], name or "Unnamed calendar", url
    )


def resource_id(url: str, calendar_url: str, prefix: str = "e") -> str:
    checked_url(url, calendar_url)
    leaf = url.removeprefix(calendar_url)
    if not leaf or "/" in leaf or "/" in unquote(leaf):
        raise CalendarError("unsafe_url", "Invalid CalDAV resource path.")
    if prefix not in {"e", "t"}:
        raise ValueError("invalid internal resource prefix")
    return prefix + "_" + base64.urlsafe_b64encode(leaf.encode()).decode().rstrip("=")


def resource_url(item_id: str, calendar_url: str, prefix: str = "e") -> str:
    try:
        if prefix not in {"e", "t"} or not re.fullmatch(
            rf"{prefix}_[A-Za-z0-9_-]{{1,2048}}", item_id
        ):
            raise ValueError
        encoded = item_id[2:]
        leaf = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        url = checked_url(calendar_url + leaf, calendar_url)
        if resource_id(url, calendar_url, prefix) != item_id:
            raise ValueError
        return url
    except (ValueError, UnicodeError, CalendarError):
        noun, tools = (
            ("event_id", "list_events or create_event")
            if prefix == "e"
            else (
                "task_id",
                "list_tasks or create_task",
            )
        )
        raise invalid(f"Invalid {noun}; reuse the identifier returned by {tools}.") from None


class CalDAVStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._discovery_snapshot: tuple[float, dict[str, tuple[CalendarInfo, Any]]] | None = None
        self.dav = DAVClient(
            url=config.url,
            username=config.username,
            password=config.password,
            auth_type="basic",
            timeout=config.timeout,
            ssl_verify_cert=config.verify_ssl,
            enable_rfc6764=False,
            features="radicale",
            rate_limit_handle=False,
        )
        self.dav.session.close()
        self.dav.session = GuardedSession(config.url)

    def close(self) -> None:
        self.dav.close()

    def _fresh_discovery_snapshot(self) -> dict[str, tuple[CalendarInfo, Any]] | None:
        if self._discovery_snapshot is None:
            return None
        expires_at, calendars = self._discovery_snapshot
        return calendars if time.monotonic() < expires_at else None

    def _refresh_discovery(self) -> dict[str, tuple[CalendarInfo, Any]]:
        with safe_errors():
            calendars = self.dav.principal().calendars()
            if len(calendars) > 100:
                raise CalendarError("limit_exceeded", "More than 100 calendars were discovered.")
            result = {}
            for calendar in calendars:
                url = checked_url(str(calendar.url), self.config.url).rstrip("/") + "/"
                info = calendar_info(url, calendar.get_display_name())
                result[info.calendar_id] = (info, calendar)
            self._discovery_snapshot = (time.monotonic() + _DISCOVERY_TTL_SECONDS, result)
            return result

    def discover(self) -> dict[str, tuple[CalendarInfo, Any]]:
        cached = self._fresh_discovery_snapshot()
        return cached if cached is not None else self._refresh_discovery()

    def list_calendars(self) -> list[dict[str, Any]]:
        return [asdict(info) for info, _ in self.discover().values()]

    def select(self, calendar_id: str | None) -> list[tuple[CalendarInfo, Any]]:
        cached = self._fresh_discovery_snapshot()
        calendars = cached if cached is not None else self._refresh_discovery()
        if calendar_id is None:
            return list(calendars.values())
        if calendar_id not in calendars:
            if cached is not None:
                calendars = self._refresh_discovery()
            if calendar_id in calendars:
                return [calendars[calendar_id]]
            raise CalendarError(
                "calendar_not_found", "Unknown calendar_id for this account; call list_calendars."
            )
        return [calendars[calendar_id]]

    def resources(
        self, calendar_id: str | None, a: datetime, b: datetime
    ) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        with safe_errors():
            for info, calendar in self.select(calendar_id):
                # Radicale interprets unzoned REPORT matches in UTC. Pad for local all-day/floating
                # boundaries; recurrence expansion and the exact interval filter happen locally.
                resources = calendar.search(
                    event=True,
                    start=a - timedelta(days=1),
                    end=b + timedelta(days=1),
                    expand=False,
                    post_filter=False,
                )
                if len(resources) + len(result) > self.config.max_events:
                    raise CalendarError(
                        "limit_exceeded",
                        "Too many event resources; use a smaller range or select one calendar.",
                    )
                for resource in resources:
                    url = checked_url(str(resource.url), info.url)
                    result.append((info.calendar_id, resource_id(url, info.url), resource.data))
        return result

    def task_resources(self, calendar_id: str | None) -> list[tuple[str, str, str]]:
        """Fetch VTODO resources; deterministic date/state filtering happens locally."""
        result: list[tuple[str, str, str]] = []
        with safe_errors():
            for info, calendar in self.select(calendar_id):
                resources = calendar.search(
                    todo=True,
                    include_completed=True,
                    expand=False,
                    post_filter=False,
                )
                if len(resources) + len(result) > self.config.max_events:
                    raise CalendarError(
                        "limit_exceeded",
                        "Too many task resources; select one calendar or archive old tasks.",
                    )
                for resource in resources:
                    url = checked_url(str(resource.url), info.url)
                    result.append(
                        (info.calendar_id, resource_id(url, info.url, "t"), resource.data)
                    )
        return result

    def get(self, calendar_id: str, event_id: str) -> tuple[str, str | None]:
        info, _ = self.select(calendar_id)[0]
        with safe_errors():
            response = self.dav.request(resource_url(event_id, info.url), "GET")
            return response.raw, response.headers.get("ETag")

    def get_task(self, calendar_id: str, task_id: str) -> tuple[str, str | None]:
        info, _ = self.select(calendar_id)[0]
        with safe_errors():
            response = self.dav.request(resource_url(task_id, info.url, "t"), "GET")
            return response.raw, response.headers.get("ETag")

    def write(
        self, calendar_id: str, event_id: str, data: str, etag: str | None = None
    ) -> str | None:
        info, _ = self.select(calendar_id)[0]
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers.update({"If-Match": etag} if etag is not None else {"If-None-Match": "*"})
        with safe_errors():
            response = self.dav.request(resource_url(event_id, info.url), "PUT", data, headers)
            return response.headers.get("ETag")

    def write_task(
        self, calendar_id: str, task_id: str, data: str, etag: str | None = None
    ) -> str | None:
        info, _ = self.select(calendar_id)[0]
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers.update({"If-Match": etag} if etag is not None else {"If-None-Match": "*"})
        with safe_errors():
            response = self.dav.request(resource_url(task_id, info.url, "t"), "PUT", data, headers)
            return response.headers.get("ETag")

    def delete(self, calendar_id: str, event_id: str, etag: str) -> None:
        info, _ = self.select(calendar_id)[0]
        with safe_errors():
            self.dav.request(resource_url(event_id, info.url), "DELETE", headers={"If-Match": etag})

    def delete_task(self, calendar_id: str, task_id: str, etag: str) -> None:
        info, _ = self.select(calendar_id)[0]
        with safe_errors():
            self.dav.request(
                resource_url(task_id, info.url, "t"), "DELETE", headers={"If-Match": etag}
            )
