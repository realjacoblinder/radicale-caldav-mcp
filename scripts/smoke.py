"""Exercise the installed server via a real MCP stdio connection.

Default is discovery and bounded event/task reads. --write-calendar opts into
temporary VEVENT and VTODO lifecycle checks in that collection. No credentials on argv.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def run(args: argparse.Namespace) -> None:
    env = {k: v for k, v in os.environ.items() if k.startswith("CALDAV_") or k == "LOG_LEVEL"}
    params = StdioServerParameters(command=args.command, env=env)
    async with Client(params) as client:

        async def call(name: str, **kwargs: Any) -> dict[str, Any]:
            result = await client.call_tool(name, kwargs)
            if result.is_error:
                raise RuntimeError(str(result.content))
            if result.structured_content is None:
                raise RuntimeError("Server returned no structured result.")
            return result.structured_content

        tools = (await client.list_tools()).tools
        print(f"MCP connected; {len(tools)} tools discovered.")
        calendars = await call("list_calendars")
        print(json.dumps(calendars, indent=2))
        a = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
        b = a + timedelta(days=7)
        events = await call("list_events", start=a.isoformat(), end=b.isoformat(), limit=10)
        print(f"Next seven days: {events['total']} occurrences.")
        tasks = await call(
            "list_tasks", start=a.isoformat(), end=b.isoformat(), include_undated=False, limit=10
        )
        print(f"Next seven days: {tasks['total']} dated tasks.")
        if args.write_calendar is None:
            return
        cid = args.write_calendar
        event = None
        owned_etag = None
        task = None
        task_etag = None
        try:
            event = (
                await call(
                    "create_event",
                    calendar_id=cid,
                    summary="Radicale MCP smoke " + str(uuid4()),
                    start=a.isoformat(),
                    duration_minutes=15,
                )
            )["event"]
            owned_etag = event["etag"]
            print("Created temporary event: " + event["event_id"])
            current = (await call("get_event", calendar_id=cid, event_id=event["event_id"]))[
                "event"
            ]
            if current["etag"] != owned_etag or not owned_etag:
                raise RuntimeError(
                    "Temporary event has no known matching ETag; inspect it manually."
                )
            updated = await call(
                "update_event",
                calendar_id=cid,
                event_id=event["event_id"],
                etag=owned_etag,
                location="MCP smoke test",
            )
            owned_etag = updated["event"]["etag"]
            print("Read and conditional update succeeded.")

            task = (
                await call(
                    "create_task",
                    calendar_id=cid,
                    summary="Radicale MCP task smoke " + str(uuid4()),
                    due=b.isoformat(),
                    categories=["MCP smoke test"],
                )
            )["task"]
            task_etag = task["etag"]
            current_task = (await call("get_task", calendar_id=cid, task_id=task["task_id"]))[
                "task"
            ]
            if current_task["etag"] != task_etag or not task_etag:
                raise RuntimeError(
                    "Temporary task has no known matching ETag; inspect it manually."
                )
            completed = await call(
                "complete_task", calendar_id=cid, task_id=task["task_id"], etag=task_etag
            )
            task_etag = completed["task"]["etag"]
            reopened = await call(
                "reopen_task", calendar_id=cid, task_id=task["task_id"], etag=task_etag
            )
            task_etag = reopened["task"]["etag"]
            print("VTODO create/read/complete/reopen succeeded.")
        finally:
            if task is not None and task_etag is not None:
                await call("delete_task", calendar_id=cid, task_id=task["task_id"], etag=task_etag)
                print("Temporary task deleted.")
            if event is not None and owned_etag is not None:
                # Use our last known version, never fetch-and-delete a third party's new edit.
                # If update failed, cleanup is best effort and must respect concurrency.
                await call(
                    "delete_event", calendar_id=cid, event_id=event["event_id"], etag=owned_etag
                )
                print("Temporary event deleted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="radicale-mcp", help="Installed server executable")
    parser.add_argument(
        "--write-calendar", help="Opt in to temporary CRUD in this discovered calendar_id"
    )
    asyncio.run(run(parser.parse_args()))
