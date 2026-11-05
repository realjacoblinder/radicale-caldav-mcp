"""Transport integration tests for the common remote reverse-proxy deployment."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests

from radicale_mcp.client import CalDAVStore, GuardedSession
from radicale_mcp.config import Config
from radicale_mcp.service import CalendarService


@contextmanager
def subpath_proxy(
    upstream_url: str,
    *,
    redirect_base_get: bool = False,
    calls: list[tuple[str, str]] | None = None,
) -> Iterator[str]:
    """Proxy /radicale/* like Radicale's documented nginx configuration."""
    upstream = urlsplit(upstream_url)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _proxy(self) -> None:
            assert self.path.startswith("/radicale/")
            if calls is not None:
                calls.append((self.command, self.path))
            if redirect_base_get and self.command == "GET" and self.path == "/radicale/":
                self.send_response(302)
                self.send_header("Location", "/radicale/web/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=5)
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"connection", "content-length", "host"}
            }
            headers.update(
                {
                    "Content-Length": str(len(body)),
                    "Host": self.headers["Host"],
                    "X-Script-Name": "/radicale",
                    "X-Forwarded-Host": self.headers["Host"],
                    "X-Forwarded-Proto": "http",
                }
            )
            connection.request(self.command, self.path.removeprefix("/radicale"), body, headers)
            response = connection.getresponse()
            response_body = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "content-length", "transfer-encoding"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
            connection.close()

        do_DELETE = do_GET = do_MKCALENDAR = do_OPTIONS = _proxy
        do_PROPFIND = do_PROPPATCH = do_PUT = do_REPORT = _proxy

        def log_message(self, format: str, *args: object) -> None:
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{proxy.server_port}/radicale/"
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)


def test_remote_subpath_discovery_and_crud(radicale):
    with subpath_proxy(radicale["url"]) as public_url:
        config = Config.from_env(
            {
                "CALDAV_URL": public_url,
                "CALDAV_USERNAME": "alice",
                "CALDAV_PASSWORD": radicale["passwords"]["alice"],
                "CALDAV_DEFAULT_TIMEZONE": "America/New_York",
            }
        )
        store = CalDAVStore(config)
        calendar = store.dav.principal().make_calendar(name="Remote", cal_id="remote-test")
        try:
            service = CalendarService(config, store)
            discovered = service.list_calendars()["calendars"]
            selected = next(item for item in discovered if item["name"] == "Remote")
            assert selected["url"].startswith(public_url)

            created = service.create_event(
                selected["calendar_id"],
                "Remote dentist",
                "2026-09-15T14:00:00-04:00",
                duration_minutes=60,
            )["event"]
            listed = service.list_events(
                "2026-09-15T00:00:00-04:00",
                "2026-09-16T00:00:00-04:00",
                selected["calendar_id"],
            )["events"]
            assert [event["summary"] for event in listed] == ["Remote dentist"]

            updated = service.update_event(
                selected["calendar_id"],
                created["event_id"],
                created["etag"],
                summary="Remote checkup",
            )["event"]
            service.delete_event(selected["calendar_id"], updated["event_id"], updated["etag"])

            task = service.create_task(
                selected["calendar_id"],
                "Remote task",
                due="2026-09-15T17:00:00-04:00",
            )["task"]
            assert [
                item["summary"] for item in service.list_tasks(selected["calendar_id"])["tasks"]
            ] == ["Remote task"]
            task = service.update_task(
                selected["calendar_id"],
                task["task_id"],
                task["etag"],
                summary="Remote task updated",
            )["task"]
            service.delete_task(selected["calendar_id"], task["task_id"], task["etag"])
        finally:
            calendar.delete()
            store.close()


def test_remote_request_keeps_timeout_and_tls_verification(config, monkeypatch):
    class RequestObserved(Exception):
        pass

    seen = {}

    def observe(self, method, url, *args, **kwargs):
        seen.update(kwargs)
        raise RequestObserved

    monkeypatch.setattr(GuardedSession, "request", observe)
    remote = replace(config, timeout=17, verify_ssl=False)
    store = CalDAVStore(remote)
    try:
        with pytest.raises(RequestObserved):
            store.dav.request(remote.url, "GET")
        assert seen["timeout"] == 17
        assert seen["verify"] is False
    finally:
        store.close()


def test_calendar_discovery_uses_propfind_despite_browser_redirect(radicale, accounts):
    assert accounts["alice"]  # The fixture creates Alice's calendar before the proxy test.
    calls: list[tuple[str, str]] = []
    with subpath_proxy(radicale["url"], redirect_base_get=True, calls=calls) as public_url:
        # A browser-like request redirects to the management UI by design.
        response = requests.get(public_url, allow_redirects=False, timeout=5)
        assert response.status_code == 302 and response.headers["Location"].endswith("/web/")
        calls.clear()

        config = Config.from_env(
            {
                "CALDAV_URL": public_url,
                "CALDAV_USERNAME": "alice",
                "CALDAV_PASSWORD": radicale["passwords"]["alice"],
            }
        )
        store = CalDAVStore(config)
        try:
            assert store.list_calendars()
        finally:
            store.close()

        # CalDAV discovery starts at the configured base with PROPFIND, obtains
        # current-user-principal and calendar-home-set, then queries that home.
        assert calls
        assert calls[0] == ("PROPFIND", "/radicale/")
        assert all(method == "PROPFIND" for method, _ in calls)
