"""Focused behavior tests for the per-store calendar discovery snapshot."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

import radicale_mcp.client as client
from radicale_mcp.client import CalDAVStore, calendar_info
from radicale_mcp.errors import CalendarError


class FakeCalendar:
    def __init__(self, url: str, name: str) -> None:
        self.url = url
        self.name = name

    def get_display_name(self) -> str:
        return self.name


class FakePrincipal:
    def __init__(self, calendars: list[FakeCalendar]) -> None:
        self.available = calendars
        self.discovery_requests = 0

    def calendars(self) -> list[FakeCalendar]:
        self.discovery_requests += 1
        return list(self.available)


def make_store(config, calendars: list[FakeCalendar]) -> tuple[CalDAVStore, FakePrincipal]:
    store = CalDAVStore(config)
    principal = FakePrincipal(calendars)
    store.dav.principal = lambda: principal
    return store, principal


def test_warm_discovery_reuses_one_snapshot(config):
    calendar = FakeCalendar(config.url + "alice/personal/", "Personal")
    store, principal = make_store(config, [calendar])
    try:
        assert store.list_calendars() == store.list_calendars()
        assert principal.discovery_requests == 1
    finally:
        store.close()


def test_expired_discovery_snapshot_is_refreshed(config, monkeypatch):
    now = 100.0
    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: now), raising=False)
    first = FakeCalendar(config.url + "alice/first/", "First")
    second = FakeCalendar(config.url + "alice/second/", "Second")
    store, principal = make_store(config, [first])
    try:
        assert store.list_calendars() == [asdict(calendar_info(first.url, first.name))]
        principal.available = [second]
        now += 29.999
        assert store.list_calendars() == [asdict(calendar_info(first.url, first.name))]
        assert principal.discovery_requests == 1

        now += 0.001
        assert store.list_calendars() == [asdict(calendar_info(second.url, second.name))]
        assert principal.discovery_requests == 2
    finally:
        store.close()


def test_unknown_calendar_id_refreshes_before_not_found(config):
    first = FakeCalendar(config.url + "alice/first/", "First")
    added = FakeCalendar(config.url + "alice/added/", "Added")
    store, principal = make_store(config, [first])
    try:
        store.list_calendars()
        first_id = calendar_info(first.url, first.name).calendar_id
        assert store.select(first_id)[0][0].calendar_id == first_id
        assert principal.discovery_requests == 1

        principal.available.append(added)
        added_id = calendar_info(added.url, added.name).calendar_id
        assert store.select(added_id)[0][0].calendar_id == added_id
        assert principal.discovery_requests == 2

        with pytest.raises(CalendarError, match="calendar_not_found"):
            store.select("c_unknown")
        assert principal.discovery_requests == 3
    finally:
        store.close()


def test_discovery_snapshots_are_isolated_between_stores(config):
    first = FakeCalendar(config.url + "alice/first/", "First")
    second = FakeCalendar(config.url + "alice/second/", "Second")
    store_a, principal_a = make_store(config, [first])
    store_b, principal_b = make_store(config, [second])
    try:
        assert store_a.list_calendars()[0]["name"] == "First"
        assert store_b.list_calendars()[0]["name"] == "Second"
        assert principal_a.discovery_requests == principal_b.discovery_requests == 1
    finally:
        store_a.close()
        store_b.close()
