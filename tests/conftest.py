from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from radicale_mcp.client import CalDAVStore
from radicale_mcp.config import Config
from radicale_mcp.service import CalendarService


@pytest.fixture(scope="session")
def radicale(tmp_path_factory):
    """A real authenticated Radicale process with isolated temporary storage."""
    root = tmp_path_factory.mktemp("radicale")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    passwords = {user: secrets.token_urlsafe(24) for user in ("alice", "bob")}
    users = root / "users"
    users.write_text("\n".join(f"{u}:{p}" for u, p in passwords.items()))
    users.chmod(0o600)
    config = root / "config"
    config.write_text(f"""[server]
hosts = 127.0.0.1:{port}
[auth]
type = htpasswd
htpasswd_filename = {users}
htpasswd_encryption = plain
delay = 0
[rights]
type = owner_only
[storage]
filesystem_folder = {root / "collections"}
[logging]
level = warning
""")
    url = f"http://127.0.0.1:{port}/"
    with (root / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "radicale", "--config", str(config)],
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Radicale failed to start: " + (root / "server.log").read_text())
                try:
                    requests.get(url, timeout=0.3)
                    break
                except requests.ConnectionError:
                    time.sleep(0.05)
            else:
                pytest.fail("Radicale did not start within 15 seconds")
            yield {"url": url, "passwords": passwords, "root": root}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def accounts(radicale):
    result = {}
    stores = []
    for user in ("alice", "bob"):
        cfg = Config.from_env(
            {
                "CALDAV_URL": radicale["url"],
                "CALDAV_USERNAME": user,
                "CALDAV_PASSWORD": radicale["passwords"][user],
                "CALDAV_DEFAULT_TIMEZONE": "America/New_York",
            }
        )
        store = CalDAVStore(cfg)
        stores.append(store)
        calendar = store.dav.principal().make_calendar(name="Personal", cal_id=str(uuid4()))
        cid = next(
            info.calendar_id
            for info, _ in store.discover().values()
            if info.url == str(calendar.url)
        )
        result[user] = (CalendarService(cfg, store), cid, calendar)
    try:
        yield result
    finally:
        for _, _, calendar in result.values():
            calendar.delete()
        for store in stores:
            store.close()


@pytest.fixture
def config():
    return Config.from_env(
        {
            "CALDAV_URL": "https://calendar.example.test/radicale/",
            "CALDAV_USERNAME": "alice",
            "CALDAV_PASSWORD": "unit-test-secret",
            "CALDAV_DEFAULT_TIMEZONE": "America/New_York",
        }
    )


@pytest.fixture
def project_root():
    return Path(__file__).resolve().parents[1]
