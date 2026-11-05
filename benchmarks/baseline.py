"""Reproducible, local-only performance baseline for read-only service operations."""

from __future__ import annotations

import argparse
import json
import math
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

import niquests

from radicale_mcp.client import CalDAVStore, resource_id
from radicale_mcp.config import Config
from radicale_mcp.service import CalendarService

SHORT_START = "2026-09-10T00:00:00-04:00"
SHORT_END = "2026-09-12T00:00:00-04:00"
LONG_START = "2026-09-01T00:00:00-04:00"
LONG_END = "2026-09-25T00:00:00-04:00"
_DAV_RESPONSE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?response(?:\s|>)", re.IGNORECASE)


def percentile(values: Sequence[float], percent: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: Sequence[float], digits: int = 3) -> dict[str, float]:
    return {
        "median": round(percentile(values, 50), digits),
        "p95": round(percentile(values, 95), digits),
    }


@dataclass(frozen=True)
class RequestMetric:
    method: str
    category: str
    elapsed_ms: float
    response_bytes: int
    response_resources: int


class RequestRecorder:
    """Observe aggregate response metadata without retaining URLs, headers, or bodies."""

    def __init__(self, store: CalDAVStore) -> None:
        self.metrics: list[RequestMetric] = []
        self._session = store.dav.session
        self._original = self._session.request

    def install(self) -> None:
        def recorded(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter_ns()
            response = self._original(method, url, *args, **kwargs)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            body = response.content
            if isinstance(body, str):
                body = body.encode()
            method_name = method.upper()
            if method_name == "PROPFIND":
                category = "discovery"
            elif method_name in {"REPORT", "GET"}:
                category = "resource_transfer"
            else:
                category = "other"
            resources = len(_DAV_RESPONSE.findall(body))
            if method_name == "GET" and 200 <= response.status_code < 300:
                resources = max(resources, 1)
            self.metrics.append(
                RequestMetric(method_name, category, elapsed_ms, len(body), resources)
            )
            return response

        self._session.request = recorded

    def reset(self) -> None:
        self.metrics.clear()

    def close(self) -> None:
        self._session.request = self._original


def summarize(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    methods = sorted({method for sample in samples for method in sample["methods"]})
    categories = sorted({category for sample in samples for category in sample["categories"]})
    return {
        "wall_ms": distribution([sample["wall_ms"] for sample in samples]),
        "http_ms": distribution([sample["http_ms"] for sample in samples]),
        "local_service_ms": distribution([sample["local_service_ms"] for sample in samples]),
        "network_category_ms": {
            category: distribution([sample["categories"].get(category, 0.0) for sample in samples])
            for category in categories
        },
        "requests_per_sample": {
            method: distribution([sample["methods"].get(method, 0) for sample in samples], 1)
            for method in methods
        },
        "response_bytes_per_sample": distribution(
            [sample["response_bytes"] for sample in samples], 1
        ),
        "response_resources_per_sample": distribution(
            [sample["response_resources"] for sample in samples], 1
        ),
        "result_items": distribution([sample["result_items"] for sample in samples], 1),
    }


def measure(
    recorder: RequestRecorder,
    operation: Callable[[], dict[str, Any]],
    result_key: str,
) -> dict[str, Any]:
    recorder.reset()
    started = time.perf_counter_ns()
    result = operation()
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    http_ms = sum(metric.elapsed_ms for metric in recorder.metrics)
    categories: defaultdict[str, float] = defaultdict(float)
    for metric in recorder.metrics:
        categories[metric.category] += metric.elapsed_ms
    items = result[result_key]
    return {
        "wall_ms": wall_ms,
        "http_ms": http_ms,
        # This is an attributable remainder, not a CPU profiler measurement.
        "local_service_ms": max(0.0, wall_ms - http_ms),
        "categories": dict(categories),
        "methods": dict(Counter(metric.method for metric in recorder.metrics)),
        "response_bytes": sum(metric.response_bytes for metric in recorder.metrics),
        "response_resources": sum(metric.response_resources for metric in recorder.metrics),
        "result_items": len(items) if isinstance(items, list) else 1,
    }


@contextmanager
def local_radicale() -> Iterator[tuple[str, str, str]]:
    """Start an isolated loopback Radicale with synthetic credentials and storage."""
    with tempfile.TemporaryDirectory(prefix="radicale-mcp-benchmark-") as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        username = "benchmark"
        password = "synthetic-local-only-password"
        users = root / "users"
        users.write_text(f"{username}:{password}\n")
        users.chmod(0o600)
        config = root / "config"
        config.write_text(
            "[server]\n"
            f"hosts = 127.0.0.1:{port}\n"
            "[auth]\n"
            "type = htpasswd\n"
            f"htpasswd_filename = {users}\n"
            "htpasswd_encryption = plain\n"
            "delay = 0\n"
            "[rights]\n"
            "type = owner_only\n"
            "[storage]\n"
            f"filesystem_folder = {root / 'collections'}\n"
            "[logging]\n"
            "level = warning\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "radicale", "--config", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            url = f"http://127.0.0.1:{port}/"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"local Radicale failed to start (exit code {process.returncode})"
                    )
                try:
                    with niquests.Session() as session:
                        session.trust_env = False
                        response = session.get(url, timeout=0.3)
                        if response.status_code:
                            break
                except niquests.exceptions.RequestException:
                    time.sleep(0.05)
            else:
                raise RuntimeError("local Radicale did not start within 15 seconds")
            yield url, username, password
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def event_body(uid: str, index: int, calendar_index: int) -> str:
    day = 1 + index
    recur = "RRULE:FREQ=DAILY;COUNT=3\r\n" if index % 10 == 0 else ""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Benchmark//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:202609{day:02d}T140000Z\r\nDTEND:202609{day:02d}T150000Z\r\n"
        f"SUMMARY:Synthetic event {calendar_index}-{index}\r\n{recur}"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def task_body(uid: str, index: int, calendar_index: int) -> str:
    day = 1 + index
    marker = " benchmark-needle" if index % 4 == 0 else ""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Benchmark//EN\r\n"
        "BEGIN:VTODO\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:202609{day:02d}T090000Z\r\nDUE:202609{day:02d}T170000Z\r\n"
        f"SUMMARY:Synthetic task {calendar_index}-{index}\r\n"
        f"DESCRIPTION:Local fixture{marker}\r\nSTATUS:NEEDS-ACTION\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


def seed(store: CalDAVStore, resources: int) -> tuple[list[str], str]:
    calendars = []
    task_id = ""
    for calendar_index in range(2):
        calendar = store.dav.principal().make_calendar(
            name=f"Synthetic {calendar_index + 1}", cal_id=str(uuid4())
        )
        calendar_url = str(calendar.url).rstrip("/") + "/"
        calendars.append(calendar_url)
        for index in range(resources):
            event_uid = f"event-{calendar_index}-{index}"
            store.dav.request(
                calendar_url + event_uid + ".ics",
                "PUT",
                event_body(event_uid, index, calendar_index),
                {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            )
            task_uid = f"task-{calendar_index}-{index}"
            store.dav.request(
                calendar_url + task_uid + ".ics",
                "PUT",
                task_body(task_uid, index, calendar_index),
                {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            )
            if not task_id:
                task_id = resource_id(calendar_url + task_uid + ".ics", calendar_url, "t")
    discovered = store.discover()
    calendar_ids = [
        next(info.calendar_id for info, _ in discovered.values() if info.url == url)
        for url in calendars
    ]
    return calendar_ids, task_id


def run(warmup: int, samples: int, resources: int) -> dict[str, Any]:
    if warmup < 0 or samples < 2 or not 10 <= resources <= 28:
        raise ValueError("warmup must be nonnegative, samples >= 2, resources must be 10-28")
    with local_radicale() as (url, username, password):
        config = Config.from_env(
            {
                "CALDAV_URL": url,
                "CALDAV_USERNAME": username,
                "CALDAV_PASSWORD": password,
                "CALDAV_DEFAULT_TIMEZONE": "America/New_York",
            }
        )
        store = CalDAVStore(config)
        try:
            calendar_ids, task_id = seed(store, resources)
            service = CalendarService(config, store)
            one = calendar_ids[0]
            workloads: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
                "list_calendars": (service.list_calendars, "calendars"),
                "get_task.one": (lambda: service.get_task(one, task_id), "task"),
                "list_tasks.one": (
                    lambda: service.list_tasks(one, start=SHORT_START, end=SHORT_END),
                    "tasks",
                ),
                "list_tasks.all": (
                    lambda: service.list_tasks(start=SHORT_START, end=SHORT_END),
                    "tasks",
                ),
                "search_tasks.one": (
                    lambda: service.search_tasks(
                        "benchmark-needle", one, start=SHORT_START, end=SHORT_END
                    ),
                    "tasks",
                ),
                "search_tasks.all": (
                    lambda: service.search_tasks(
                        "benchmark-needle", start=SHORT_START, end=SHORT_END
                    ),
                    "tasks",
                ),
                "list_events.short.one": (
                    lambda: service.list_events(SHORT_START, SHORT_END, one),
                    "events",
                ),
                "list_events.short.all": (
                    lambda: service.list_events(SHORT_START, SHORT_END),
                    "events",
                ),
                "list_events.long.one": (
                    lambda: service.list_events(LONG_START, LONG_END, one),
                    "events",
                ),
                "list_events.long.all": (
                    lambda: service.list_events(LONG_START, LONG_END),
                    "events",
                ),
                "get_free_busy.short.one": (
                    lambda: service.get_free_busy(SHORT_START, SHORT_END, one),
                    "busy",
                ),
                "get_free_busy.short.all": (
                    lambda: service.get_free_busy(SHORT_START, SHORT_END),
                    "busy",
                ),
                "get_free_busy.long.one": (
                    lambda: service.get_free_busy(LONG_START, LONG_END, one),
                    "busy",
                ),
                "get_free_busy.long.all": (
                    lambda: service.get_free_busy(LONG_START, LONG_END),
                    "busy",
                ),
            }
            recorder = RequestRecorder(store)
            recorder.install()
            try:
                results = {}
                for name, (operation, result_key) in workloads.items():
                    for _ in range(warmup):
                        measure(recorder, operation, result_key)
                    results[name] = summarize(
                        [measure(recorder, operation, result_key) for _ in range(samples)]
                    )
            finally:
                recorder.close()
        finally:
            store.close()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture": {
            "endpoint": "isolated loopback Radicale; no real endpoint queried",
            "calendars": 2,
            "event_resources_per_calendar": resources,
            "task_resources_per_calendar": resources,
            "short_query_start": SHORT_START,
            "short_query_end": SHORT_END,
            "long_query_start": LONG_START,
            "long_query_end": LONG_END,
        },
        "sampling": {"warmup": warmup, "samples": samples},
        "versions": {
            "python": sys.version.split()[0],
            "radicale": version("radicale"),
            "caldav": version("caldav"),
        },
        "measurement": {
            "http_ms": "sum of instrumented DAV request wall times",
            "local_service_ms": "operation wall time minus instrumented DAV request time",
            "response_bytes": "decoded HTTP response body bytes; bodies are not retained",
            "response_resources": "DAV multistatus response elements; successful GET counts as one",
        },
        "workloads": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--resources", type=int, default=24)
    args = parser.parse_args()
    print(json.dumps(run(args.warmup, args.samples, args.resources), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
