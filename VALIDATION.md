# V4 validation record

Validated on 2026-09-15 using Linux and Python 3.12.14. The package declares
Python >=3.11; other Python versions were not executed in this workspace.

| Check | Result |
|---|---|
| `pytest -q` | 182 passed |
| `ruff check src tests scripts` | Passed |
| `ruff format --check src tests scripts` | 21 files formatted |
| `mypy src` | No issues in 12 source files |
| `python -m build` | Built 0.4.0 sdist and wheel |
| `radicale-mcp --version` | `0.4.0` |
| Real stdio MCP negotiation | 17 tools; current and legacy modes passed |
| Source/archive review | No real credentials, live `.env`, VCS data, virtualenv, caches, or build junk |

Primary tested versions: MCP 2.2.0, CalDAV 3.3.0, icalendar 7.3.0,
recurring-ical-events 3.8.2, and Radicale 3.8.0. See `uv.lock` for the exact
dependency graph.

The integration suite launches a real authenticated Radicale subprocess with
random Alice/Bob passwords, temporary storage, and `owner_only` authorization.
It verifies calendar discovery plus VEVENT and VTODO create/read/update/delete,
task search, date/undated/state filters, completion/reopening, all-day/floating
time handling, preservation of unknown VTODO properties and VALARMs, recurring
task reads, explicit recurring-task mutation refusal, and stale ETag rejection.
Direct requests prove Alice cannot read Bob's task or event resources; the
security test is enforced by Radicale rather than faked in the MCP layer.

A separate transport test routes authenticated discovery and both VEVENT/VTODO
CRUD through an HTTP reverse proxy mounted at `/radicale/` using Radicale's
documented `X-Script-Name` behavior. A regression makes browser GET of the base
redirect to `/web/` and proves CalDAV discovery instead starts with PROPFIND at
the configured base and follows principal/calendar-home-set DAV properties.
Redirects, cross-origin paths, ambient credentials, and unsafe resource IDs are
still refused by the guarded transport.

VTODO unit tests cover normalized RFC properties, UTC completion timestamps,
priority/progress/status ranges, mismatched DTSTART/DUE types, DUE plus DURATION,
all-day and floating values, text/category search, output paging, malformed input,
long DTSTART-to-DUE recurrence windows, and deterministic recurrence expansion.
VTODO resources do not enter event free/busy calculations.

The existing v1-v3 suite remains intact: event ranges and search, recurrence
creation/expansion, occurrence/series/future mutation, split arithmetic and
failure recovery, all-day/floating/DST behavior, free/busy and slot calculation,
ETag races, safe error redaction, MCP annotations/schemas, CLI checks, and remote
subpath deployments all passed.

No connection was made to the user's own Radicale or Hermes installation. The
included `--check` and `scripts/smoke.py` commands are the live verification path;
the write smoke test creates and conditionally cleans up temporary VEVENT and
VTODO resources.

Recurring VTODO mutation remains deliberately unsupported. CalDAV task clients
use incompatible conventions for advancing/completing repeating tasks, so v4
returns `unsupported_recurrence_mutation` instead of risking a whole-series or
exception corruption. Bounded reads expand DTSTART-based VTODO recurrence;
unbounded reads return the series master.
