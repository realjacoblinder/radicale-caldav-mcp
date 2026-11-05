# radicale-mcp v4

This project was built with AI assistance, including code and documentation.

A Python 3.11+ Model Context Protocol (MCP) server for accessing a Radicale server
through CalDAV. It provides structured tools for iCalendar events (`VEVENT`) and
tasks (`VTODO`), including recurrence, conditional writes, and availability.
Calendar data follows RFC 5545 (iCalendar), with access through RFC 4791 (CalDAV).
The supported operations and implementation limits are documented below; this is
not a complete implementation of every feature in those RFCs.
It exposes 17 structured tools over **stdio**, using one fixed CalDAV identity per process.
There is no MCP HTTP service, credential-switching tool, database, or raw-ICS tool.

If `PYTHONPATH` points at a different Python installation, run project commands
with `env -u PYTHONPATH` (as shown below). This removes the inherited path for that
command only, preventing foreign packages from shadowing the project virtualenv;
it does not change the venv or the parent shell.

## Install

Extract `radicale-mcp-v4.zip`, then run from its `radicale-mcp-v4` directory:

```bash
env -u PYTHONPATH uv tool install .
env -u PYTHONPATH radicale-mcp --version
command -v radicale-mcp
```

Or use `env -u PYTHONPATH pipx install .`. Both install the `radicale-mcp` executable
into the tool manager's bin directory, typically `~/.local/bin`. Use its **absolute path** in
your MCP client's server configuration. This project has not been published to PyPI; install from this source tree.

For an environment with the exact tested dependencies, including development tools:

```bash
env -u PYTHONPATH uv sync --locked
env -u PYTHONPATH uv run --locked radicale-mcp --version
```

For a deployment using the lockfile without test dependencies, run
`env -u PYTHONPATH uv sync --locked --no-dev` and point your MCP client at this project's absolute
`.venv/bin/radicale-mcp` path. `uv tool install .` and `pipx install .` resolve the
compatible ranges in `pyproject.toml`; they do not automatically consume `uv.lock`.

## Upgrade from v1, v2, or v3

V4 is package version **0.4.0**. After extracting this archive:

```bash
cd radicale-mcp-v4
env -u PYTHONPATH uv tool install --force .
env -u PYTHONPATH radicale-mcp --version
```

For pipx use `env -u PYTHONPATH pipx install --force .`. Existing credentials
and tool names remain the same. Restart the MCP server connection in your client
so it discovers the new input schemas. For deployments pointing directly at a
project `.venv/bin/radicale-mcp`, run `env -u PYTHONPATH uv sync --locked --no-dev`
in the new directory and update the executable path in your client configuration.

Existing event calls remain compatible. V4 adds eight task tools; recurring event
mutations still require explicit scope, so there is no default that could
accidentally affect an entire series.

Events also support CATEGORIES, PRIORITY (0–9, where 0 is distinct from absent),
GEO (`{"latitude": 0.0, "longitude": 0.0}`), URI-only ATTACH, COMMENT,
RESOURCES, URL, ORGANIZER, ATTENDEE, and CONTACT. Repeated text and URI fields
are JSON arrays; organizer is `{address, cn?}` and attendee is an array of
`{address, cn?, role?, partstat?, rsvp?}`. Normalized reads use these same shapes.
Omitted/null update fields are preserved; pass `[]` to clear a repeated field.
Text is limited to 16,000 characters per value and 100 values per repeated
property; URIs are limited to 2,048 characters. ATTACH accepts absolute URIs and
does not fetch them. Binary attachments are not supported.

## Configuration

The executable reads process environment variables at startup. It does not
auto-load a `.env` file or read secrets from tool arguments.

| Variable | Default | Meaning |
|---|---|---|
| `CALDAV_URL` | required | Final Radicale HTTP(S) base URL, e.g. `http://127.0.0.1:5232` |
| `CALDAV_USERNAME` | required | One authenticated Radicale account |
| `CALDAV_PASSWORD` | required | That account's password |
| `CALDAV_VERIFY_SSL` | `true` | TLS certificate verification; literal `true`/`false` |
| `CALDAV_TIMEOUT` | `30` | Per-HTTP-request timeout in seconds, 1–120 |
| `CALDAV_DEFAULT_TIMEZONE` | `UTC` | IANA timezone for naive input, floating events and all-day busy time |
| `CALDAV_MAX_RANGE_DAYS` | `93` | Maximum query span; administrator may set 1–366 |
| `CALDAV_MAX_EVENTS` | `5000` | Resource/occurrence safety cap per query; 1–10000 |
| `LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

Missing required values and unresolved `${VARIABLE}` placeholders fail at startup
with a concise message and exit status 2. Connection/authentication errors appear
on first tool use or in `--check`. No network connection is needed to list MCP
tool schemas. Use loopback HTTP when both processes share a trusted machine;
use HTTPS for network access. Set the final URL directly: redirects and DNS-based
CalDAV endpoint discovery are disabled. Principal/calendar discovery is enabled.
An nginx subpath such as `https://calendar.example.org/radicale/` is supported
when its DAV responses preserve that same origin and base path.

`--check` performs authenticated discovery and exits; it is a standalone
diagnostic mode that prints JSON. Normal `radicale-mcp` reserves stdout for MCP.
Logging goes to stderr. Third-party debug logs and exception tracebacks are
suppressed at the CLI boundary; raising `LOG_LEVEL` does not enable HTTP wire logs.

### Remote Radicale and reverse proxies

The MCP process does not need to run on the Radicale host. Point `CALDAV_URL` at
the final URL that the machine running this MCP server can reach, normally an HTTPS endpoint with
a certificate trusted by that machine:

```dotenv
CALDAV_URL=https://calendar.example.com/radicale/
CALDAV_VERIFY_SSL=true
CALDAV_TIMEOUT=30
```

For a subpath deployment, configure the proxy so Radicale reports public URLs
under the same origin and `/radicale/` base path. This follows Radicale's
documented nginx pattern:

```nginx
location /radicale/ {
    proxy_pass http://127.0.0.1:5232/; # Strip /radicale/ before forwarding.
    proxy_set_header X-Script-Name /radicale;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Port $server_port;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Host $http_host;
}
```

Keep Radicale's own HTTP authentication enabled in this arrangement; nginx
forwards the client's `Authorization` header by default. If authentication is
instead terminated at nginx with Radicale's `http_x_remote_user` backend, block
direct network access to Radicale as its documentation requires.

Redirects are deliberately rejected before they can move credentials. Therefore
use the final HTTPS hostname and CalDAV base path in `CALDAV_URL`; do not use an
HTTP URL that redirects to HTTPS or a `/.well-known/caldav` discovery URL. It is
normal for a browser's **GET** of the Radicale root to redirect to its web UI. The
MCP does not use that browser request for discovery: it sends WebDAV `PROPFIND` to
the configured base, reads `current-user-principal`, then discovers the account's
`calendar-home-set` and its calendar collections. Thus a configuration such as
`CALDAV_URL=https://calendar.example.com/` is correct when nginx exposes Radicale
at that root, even if opening that URL in a browser lands on `/web` or `/.web`.
The `PROPFIND` itself must return a DAV multistatus response without redirecting.
A public-CA or
otherwise system-trusted certificate works with the default verification. The
boolean configuration does not accept a private CA bundle path; install a private
CA in the MCP server host's trust store, or use `CALDAV_VERIFY_SSL=false` only for a
controlled diagnostic.

From the MCP server host, verify the remote route and identity before connecting
your client:

```bash
read -rsp "CalDAV password: " PERSONAL_CALDAV_PASSWORD; echo
export CALDAV_URL=https://calendar.example.com/radicale/
export CALDAV_USERNAME=alice
export CALDAV_PASSWORD="$PERSONAL_CALDAV_PASSWORD"
export CALDAV_VERIFY_SSL=true
env -u PYTHONPATH radicale-mcp --check
unset CALDAV_PASSWORD PERSONAL_CALDAV_PASSWORD
```

The command should return discovered calendars as JSON.

## Connect an MCP client

Configure your MCP client to launch `radicale-mcp` as a **stdio** server. Supply
its absolute executable path and the environment variables listed above. The
client starts the process and communicates through stdin/stdout; Radicale can run
locally or on a remote host.

A common JSON configuration shape is shown below. Configuration keys, file
locations, secret storage, and environment-variable expansion depend on your
client; adapt this example to its documented format.

```json
{
  "mcpServers": {
    "radicale": {
      "command": "/absolute/path/to/radicale-mcp",
      "env": {
        "CALDAV_URL": "https://calendar.example.com/radicale/",
        "CALDAV_USERNAME": "alice",
        "CALDAV_PASSWORD": "REPLACE_WITH_YOUR_PASSWORD",
        "CALDAV_DEFAULT_TIMEZONE": "UTC"
      }
    }
  }
}
```

Use your client's secret store or inherited process environment where supported.
Protect any configuration file containing credentials with owner-only permissions.
The server does not load `.env` files or expand `${VARIABLE}` references itself;
any substitution must happen in the client before the process starts. Restart the
server connection after changing its environment or executable.

Each process uses one fixed Radicale identity. To access multiple accounts,
configure a separate server entry for each account, with its own environment.
Tool names or prefixes depend on the client. Calls without `calendar_id` read all
calendars discovered for that identity. Select a calendar deliberately before
creation, since display names may collide. **Availability from one MCP process
covers only that identity.** To combine availability across accounts, the caller
must fetch and merge their busy intervals.

The nine read tools carry `readOnlyHint=true`. Creation is non-idempotent and
non-destructive. Update/delete/complete/reopen tools mutate remote state;
destructive and idempotence hints reflect each operation. Event update is marked
non-idempotent because it can split a series. Annotations describe behavior; the
MCP client controls approval policy.

## Tools and identifiers

| Tool | Behavior |
|---|---|
| `list_calendars` | Discover calendars with stable `calendar_id`, name and URL |
| `list_events` | List expanded occurrences in required bounded `[start,end)` |
| `search_events` | Case-insensitive literal substring search of summary, description and location |
| `get_event` | Read a resource, series master, or selected occurrence and obtain a fresh ETag |
| `create_event` | Create a timed or all-day event, optionally with an RRULE |
| `update_event` | Update an event, occurrence, whole series, or this-and-following with ETags |
| `delete_event` | Delete an event/series, exclude one occurrence, or truncate from a boundary |
| `get_free_busy` | Merge and clip busy intervals from all matching occurrences |
| `find_available_slots` | Return maximal free intervals long enough for the requested duration |
| `list_tasks` | List open/completed/all VTODOs, optionally filtered by DUE/DTSTART range |
| `search_tasks` | Search task summary, description, location and categories locally |
| `get_task` | Read a task or recurring occurrence and obtain a fresh ETag |
| `create_task` | Create a timed, all-day, or undated non-recurring VTODO |
| `update_task` | Partially update a non-recurring VTODO using an ETag |
| `complete_task` | Atomically set STATUS, PERCENT-COMPLETE and UTC COMPLETED |
| `reopen_task` | Return a completed/cancelled task to NEEDS-ACTION |
| `delete_task` | Conditionally delete a non-recurring VTODO |

Calendar discovery is cached in memory for up to 30 seconds per configured store.
Consequently, `list_calendars` may return a snapshot that is up to 30 seconds old.
An unknown `calendar_id` triggers fresh discovery before it is reported as not found.

Calendar IDs derive from the discovered collection URL, not the display name.
They survive renames of display names and process restarts, but change if a
collection moves. Event and task IDs identify resources within that calendar and
survive content edits. Use **both `calendar_id` and `event_id`/`task_id`**, not a UID
alone: UIDs can appear in multiple calendars. An `occurrence_id` is the original
recurrence date or timestamp, so a moved event exception retains its identity.
No tool accepts a destination URL, host, username or password.

Lists return normalized fields, including UID, time zone, all-day/floating flags,
status, recurrence metadata and text. Task output additionally includes DUE,
completion timestamp/percentage, priority and categories. `get_event` returns the
master representation for a series, or the effective occurrence when given
`occurrence_id`; list/search return actual occurrences.
List results need not contain an ETag: call `get_event` or `get_task` immediately
before a write.

List/search default to 200 results, maximum 1000, additionally limited to about
128 KB of normalized JSON per page. Follow `next_offset` with the same query parameters.
`total` describes the complete bounded query; `truncated` describes only the
current page. Paging is recomputed, not a snapshot: concurrent calendar changes
can move results between pages. Text fields are limited to 16000 characters,
with `text_truncated` reporting clipped output. Search fails if text clipping
would make its results incomplete. Raw ICS is not exposed.

Example tool arguments (after discovering `calendar_id`):

```json
{"calendar_id":"c_...","summary":"Dentist","start":"2026-09-15T14:00:00-04:00","duration_minutes":60}
```

```json
{"calendar_id":"c_...","event_id":"e_...","etag":"\"current-etag\"","start":"2026-09-15T15:00:00-04:00"}
```

Omitted or null update fields are preserved; an empty string clears description
or location. A start-only move preserves elapsed duration for timed events and
the number of days for all-day events. Updating preserves unknown properties,
alarms and attendees; the library may normalize ICS formatting. It updates
DTSTAMP, LAST-MODIFIED and SEQUENCE. It does not send invitations.
New/changed event durations are limited to 366 days. Required missing VTIMEZONE
definitions are generated through the iCalendar library.

Writes use CalDAV's conditional HTTP headers through `DAVClient.request`:
`If-None-Match: *` for creation and `If-Match` for update/delete. The server reads
the current resource before editing, checks the supplied ETag, and includes that
ETag in the final request to close the race. Weak, wildcard and missing ETags
are refused. A conflict requires another read and a reviewed decision; writes
are never retried automatically. After an uncertain create result, search first.

## VTODO tasks in v4

V4 uses standard `VTODO` resources, not a private task format. It supports undated,
timed, floating, UTC, IANA-zoned and all-day tasks; `SUMMARY`, `DESCRIPTION`,
`LOCATION`, `CATEGORIES`, `PRIORITY`, `STATUS`, `PERCENT-COMPLETE`, `COMPLETED`,
`DTSTART` and `DUE` are normalized. Unknown properties and nested alarms are
preserved on partial update. `DUE` and `DURATION` imports are validated according
to [RFC 5545 section 3.6.2](https://www.rfc-editor.org/rfc/rfc5545#section-3.6.2).

`list_tasks` defaults to open tasks and accepts `state="open"`, `"completed"`, or
`"all"`. Cancelled tasks appear only under `all`. Optional start/end filters are
half-open and use DUE when present, otherwise DTSTART. `include_undated` controls
tasks with neither field. Both range bounds must be supplied together; no-range
queries are still capped by `CALDAV_MAX_EVENTS` and paginated.

Typical calls after calendar discovery:

```json
{"calendar_id":"c_...","summary":"Submit school form","due":"2026-09-18T17:00:00-04:00","categories":["Family","School"],"priority":1}
```

```json
{"calendar_id":"c_...","state":"open","start":"2026-09-15T00:00:00-04:00","end":"2026-09-22T00:00:00-04:00","include_undated":false}
```

```json
{"calendar_id":"c_...","task_id":"t_...","etag":"\"current-etag\"","completed_at":"2026-09-18T20:30:00Z"}
```

Creation sets `STATUS=NEEDS-ACTION` and `PERCENT-COMPLETE=0`. Use `complete_task`
instead of a generic patch to set `STATUS=COMPLETED`, `PERCENT-COMPLETE=100`, and
the UTC `COMPLETED` timestamp consistently. `reopen_task` removes `COMPLETED` and
resets progress. `update_task` accepts progress 0–99 and the other valid statuses;
empty category lists and explicit `clear_*` flags remove optional values. Every
mutation requires the latest strong ETag.

Recurring VTODOs with DTSTART are expanded for bounded read/search queries using
the same established recurrence library as events. An unbounded query returns the
series master rather than inventing an infinite task list. V4 deliberately rejects
all recurring-task updates, completion, reopening and deletion with
`unsupported_recurrence_mutation`: recurrence completion and exception conventions
are not consistently interoperable across task clients. A recurring VTODO without
DTSTART cannot be expanded safely and is rejected for bounded queries. Use a
dedicated CalDAV task client for those mutations.

VTODOs never contribute busy intervals. This follows iCalendar semantics: a due
task is a deadline, not automatically an occupied calendar interval.

## Recurring event creation and edits

`update_event` and `delete_event` accept these scopes:

| Scope | Target | occurrence_id |
|---|---|---|
| `event` (default) | One non-recurring resource | Must be omitted |
| `occurrence` | Exactly one instance of a recurring resource | Required |
| `series` | Entire recurring resource, including past instances and overrides | Must be omitted |
| `future` | Selected occurrence and all later original recurrence IDs | Required |

For recurring resources, leaving the default scope returns
`recurrence_scope_required`. Unsupported or contradictory scopes fail before any
write. Do not guess whether the user intends one occurrence, the entire series,
or this occurrence and following when their request is ambiguous.

To move one appointment:

1. Find it with `list_events` or `search_events` and retain its `calendar_id`,
   `event_id` and `occurrence_id`.
2. Call `get_event` with those three identifiers for its effective fields and the
   current **whole-resource ETag**.
3. Call `update_event`, reusing the original occurrence ID:

```json
{
  "calendar_id": "c_...",
  "event_id": "e_...",
  "etag": "\"current-etag\"",
  "scope": "occurrence",
  "occurrence_id": "2026-09-15T14:00:00-04:00",
  "start": "2026-09-15T15:00:00-04:00"
}
```

The UID and original `RECURRENCE-ID` remain stable, even when the appointment moves
outside the original week/month. A second edit replaces that override rather than
creating another. Unrequested fields, alarms, attendees, and custom properties
are preserved. Floating series keep floating DTSTART/RECURRENCE-ID values;
all-day recurrence identifiers are dates. A timezone may be specified for a zoned
occurrence move; floating-to-zoned and all-day-to-timed conversions are unsupported.

To delete just that occurrence, fetch its latest ETag, then call:

```json
{
  "calendar_id": "c_...",
  "event_id": "e_...",
  "etag": "\"latest-etag\"",
  "scope": "occurrence",
  "occurrence_id": "2026-09-15T14:00:00-04:00"
}
```

Deletion removes a matching override and adds an `EXDATE` for the **original**
recurrence identifier. It preserves the master DTSTART and COUNT, including when
excluding the first occurrence. The other instances remain intact. Missing,
excluded, or cancelled occurrences return `occurrence_not_found` rather than
creating or resurrecting an appointment. Busy time and availability reflect the
move/exclusion immediately on the next query.

For a whole-series text change, fetch the master with `get_event` (omit
`occurrence_id`), then use `scope: "series"`. Supplied summary, description and
location changes apply to the master **and every existing override**; unrelated
exception fields/times remain intact. Deleting with `scope: "series"` deletes the
entire resource and all its exceptions.

Schedule or repeat-rule changes are supported for a **simple series**: one RRULE,
no overrides, RDATE or EXDATE. This deliberately refuses a rewrite that could
orphan exception identifiers. For example, change a Tuesday series to Wednesday:

```json
{
  "calendar_id": "c_...",
  "event_id": "e_...",
  "etag": "\"current-etag\"",
  "scope": "series",
  "start": "2026-09-02T14:00:00-04:00",
  "recurrence_rule": "FREQ=WEEKLY;BYDAY=WE;COUNT=5"
}
```

The start is the **master's anchor**, not an arbitrary later occurrence. Its date
must match the rule. A time-only change can keep an unconstrained rule; rules
containing UNTIL or explicit clock selectors require a supplied replacement rule
so their bounds cannot change silently. Replacing a rule is supported only for
DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT or UNTIL, BYDAY, BYMONTHDAY,
BYMONTH, BYSETPOS and WKST. COUNT/INTERVAL must be positive and at most 10000.
UNTIL must have the correct DATE/DATE-TIME and UTC/floating form. The series'
timezone and date-vs-timed type cannot be changed.

RANGE/THISANDFUTURE exceptions, duplicate recurrence IDs, EXRULE, multiple RRULEs,
or RDATE periods require a calendar client for edits. Whole-resource series
deletion still works. `create_event` now accepts recurrence_rule for new series. See
`examples/recurrence-tool-calls.json` for additional argument examples.

Occurrence and whole-series mutations use a single conditional write or delete.
Future splits have the two-write behavior described below. Two changes to
different occurrences can conflict because they share a resource ETag. On a stale
ETag, fetch/review the latest resource before retrying. SEQUENCE, DTSTAMP and
LAST-MODIFIED are updated consistently across its VEVENT components. No scheduling
invitations/cancellation messages are sent. This follows the
[RECURRENCE-ID and EXDATE rules in RFC 5545](https://www.rfc-editor.org/rfc/rfc5545).

### Create a repeating event

Pass `recurrence_rule` to `create_event`; omit it for a one-off event. Example:

```json
{
  "calendar_id": "c_DISCOVERED",
  "summary": "Weekly family dinner",
  "start": "2026-09-18T18:00:00-04:00",
  "duration_minutes": 90,
  "timezone": "America/New_York",
  "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR;COUNT=12"
}
```

Creation supports DAILY, WEEKLY, MONTHLY and YEARLY, with INTERVAL, COUNT, UNTIL,
BYDAY, BYMONTHDAY, BYMONTH, BYSETPOS and WKST where valid under RFC 5545. The first
start must match the rule. COUNT includes that first occurrence; COUNT and UNTIL
are mutually exclusive. Omit both to repeat indefinitely. COUNT and INTERVAL must
be 1–10000; query and historical-validation caps still apply to long series.
`UNTIL` is inclusive: use a UTC DATE-TIME for zoned events, or YYYYMMDD for an
all-day event. New timed events use an explicit IANA timezone or the process's
configured default; this is the zone in which the rule repeats. Floating inputs
are resolved under that policy, not stored as new floating series. Existing
floating series remain supported for edits.

For an all-day repeating event, use date-only start and exclusive end, e.g.
`start="2026-09-18", end="2026-09-19", all_day=true`, with
`recurrence_rule="FREQ=WEEKLY;COUNT=12"`. New recurrence rules are validated before
writing. Existing recurrence/DST expansion safeguards below still apply; creating
an unbounded rule does not pre-validate every future occurrence. Subdaily rules,
EXRULE, multiple RRULEs and recurrence creation with explicit RDATE/EXDATE sets
are outside this version's creation API. Individual dates can be excluded later
with occurrence deletion.

### Modify from a date onward

For “move my Friday dinner to 7 starting October 2,” list the relevant dates and
select the first original occurrence on or after October 2. Get that occurrence
for its fresh ETag, then:

```json
{
  "calendar_id": "c_DISCOVERED",
  "event_id": "e_DISCOVERED",
  "etag": "\"fresh-etag\"",
  "scope": "future",
  "occurrence_id": "2026-10-02T18:00:00-04:00",
  "start": "2026-10-02T19:00:00-04:00"
}
```

The boundary is inclusive and uses the **original occurrence ID**, not a moved
instance's current start. A prior occurrence moved to after the boundary stays in
the earlier series; an occurrence originally after the boundary stays on the
future side even if its current date is earlier. Earlier effective event fields
and exceptions remain intact; resource SEQUENCE/DTSTAMP/ETag necessarily change.
A returned `split=true` means a new successor UID and event_id were created.
Reuse `event.event_id`, its ETag, and freshly listed occurrence IDs for later
operations. `original` identifies the retained earlier resource. The successor
carries a standard RELATED-TO link to the original UID, exposed in recurrence
metadata. Clients generally display these as two independent series. Editing
`scope="series"` later affects only the selected resource, not both halves.
If the boundary is the first rule occurrence, no earlier resource is needed and
the update remains one conditional write with `split=false`.

Supported future changes:

- Summary, description and location changes partition and retain existing
  exceptions, apply requested fields to future overrides, and preserve their
  individual times and other fields.
- Time, duration or rule changes permit earlier exceptions, but reject future
  overrides, RDATE or EXDATE instead of guessing how to remap them.
- Omitted `recurrence_rule` keeps the remaining generated COUNT (exclusions do
  not decrement COUNT), or the original inclusive UNTIL, or an unbounded rule.
  Moving a series later can therefore remove its last occurrence if it falls
  after the retained UNTIL. An explicit rule replaces those future bounds;
  its COUNT begins at the successor's start, including that start.
- A replacement rule can change cadence, e.g. `FREQ=WEEKLY;BYDAY=SA;COUNT=8`
  together with a matching Saturday start. Series timezone and timed/all-day
  conversions remain unsupported.
- `delete_event` with `scope="future"` and the boundary ID removes the selected
  and later original recurrence IDs using one conditional truncate (or a whole
  delete at the first occurrence). It does not create a successor.

The boundary must be an existing, noncancelled, nonexcluded occurrence generated
by the RRULE. An extra RDATE-only date cannot be the split anchor. Future edits
require one supported DAILY-or-coarser RRULE, and reject RANGE/THISANDFUTURE,
EXRULE, multiple rules and RDATE periods. Splitting does not emit RANGE exceptions;
it uses ordinary interoperable resources. Historical enumeration is bounded by
CALDAV_MAX_EVENTS. Unsupported shapes fail before writing.

### Split failures and recovery

CalDAV does **not** provide an atomic transaction over the two resources. The
server validates both resources before writing, creates the successor with
`If-None-Match: *`, then shortens the original with the caller's `If-Match` ETag.
A brief overlap is visible to other readers between writes. If the original write
is explicitly rejected, the successor is rolled back only with its own ETag;
a remotely changed successor will never be blindly removed. Losing a response,
failing rollback, or receiving no strong successor ETag returns
`split_recovery_required`, with calendar_id, original_event_id,
successor_event_id and the failed stage. Unknown outcomes are never retried or
rolled back automatically. Identical retries against the same unchanged source
use the same successor identity, preventing duplicate successors for that request.
`update_event` is conservatively annotated non-idempotent because it can split.

A process crash can also leave an incomplete split without returning an error.
There is no persistent transaction journal. After a timeout/crash or recovery
error, inspect both resources (use list/search and RELATED-TO metadata if the IDs
were not returned), then choose a reviewed recovery:

1. Original still includes the future and successor is correct: finish by calling
   `delete_event` on the **original** with `scope="future"`, the original boundary
   and a fresh original ETag. This truncates only the original.
2. Original is already truncated and successor is correct: the split completed;
   reuse the successor without retrying.
3. Original is unchanged and successor is absent: re-read and retry the intended
   edit after confirming that state.
4. To abandon a partial split while the original still contains all occurrences,
   delete only the successor with `scope="series"` and its fresh ETag, after
   reviewing any intervening edits.

Do not perform cleanup based solely on an earlier response. Changed exceptions,
concurrent edits or ambiguous states require review in a calendar client. These
limitations allow callers to handle partial results and recover explicitly.

## Time, recurrence, and availability

- Ranges are half-open: start inclusive, end exclusive. Inputs use ISO-8601
  timestamps with `T`; offsets or `Z` are recommended. Naive timestamps use the
  configured default zone, or the supplied event timezone when creating/updating.
- Explicit-offset times identify an instant. New/changed timed event fields are
  stored in the chosen IANA zone, defaulting to the account zone for creation
  and the existing IANA zone for updates. UTC is fully supported. All-day event
  start/end use `YYYY-MM-DD`; the end is exclusive and defaults to the next day.
- Ambiguous naive times during a fall-back transition require an explicit offset.
  Nonexistent naive times during spring-forward are rejected. Existing floating
  calendar values use the account zone, choosing the first fold for ambiguity;
  nonexistent floating values fail instead of producing a false free interval.
  All-day busy intervals follow local midnight boundaries, including 23/25-hour days.
- RRULE, RDATE, EXDATE and recurrence exceptions are expanded by
  `recurring-ical-events`, using `icalendar` parsing. The CalDAV REPORT fetch is
  bounded and padded for floating/all-day boundary differences, then occurrences
  are filtered to the exact requested range. Recurrence expansion is performed
  in daily chunks. The original DTSTART outside a query does not become a result.
- The recurrence engine can generate nonexistent local times at a spring-forward
  transition. Such series are rejected rather than silently shifted or counted
  incorrectly. For timed COUNT rules, original rule history is also validated
  before returning a later window, even if an EXDATE/override hides the gap.
  This check is capped at `CALDAV_MAX_EVENTS` historical instances and 1200
  monthly chunks; exceptionally long finite series can therefore be refused.
  Timed recurring occurrences whose start/end span a DST offset change are also
  refused because the engine does not reliably preserve their exact duration.
  Ordinary recurring appointments on either side of a DST transition work;
  non-recurring timed events and all-day events can span the transition.
- Recurring mutations use the explicit scopes above. Occurrence lookup uses the
  same expansion/validation engine as listing, while an already stored override
  can be addressed directly by its original recurrence ID.
- Busy time excludes cancelled and transparent events. Tentative/opaque events
  count as busy. Zero-length timed events can appear in lists but occupy no time.
  Free/busy is computed from events, avoiding dependence on native Radicale free/busy.
- Availability returns maximal free windows, not every possible starting time.
  `duration_minutes` is elapsed time. Optional working hours use `HH:MM` in the
  supplied working timezone, and weekdays are `0=Monday` through `6=Sunday`.
  Both working-hour endpoints are required; overnight windows are unsupported.
  Ambiguous/nonexistent local working boundaries are rejected. All returned
  busy/free interval timestamps are UTC. Nothing is reserved by an availability call.

If resource or occurrence limits are exceeded, or data cannot be parsed/expanded,
the query fails explicitly. Busy/availability never uses a truncated page of events.
Individual resources are capped at 2 MB before parsing and discovery at 100
calendars. The CalDAV library still materializes a REPORT response and each daily
recurrence chunk before these result caps can be enforced. These are practical
calendar limits, not a streaming defense against a malicious CalDAV server.
`CALDAV_TIMEOUT` bounds individual HTTP requests, not an entire multi-calendar
query; narrow the range if your MCP client's tool timeout is reached.

## Radicale setup and security

Use authenticated Radicale 3.x with server-side permissions:

```ini
[rights]
type = owner_only
```

Radicale documents this as its recommended backend: an authenticated user can
read/write their own collections under `/USERNAME/`. Authentication must also be
enabled. Collection discovery normally finds calendars directly under that
principal; a rights file granting another user's collection does not necessarily
make it discoverable. The dedicated family account avoids that discovery issue.
See the official [Radicale configuration and rights documentation](https://radicale.org/v3.html).

`examples/radicale-config.ini` includes loopback binding, htpasswd authentication,
owner-only rights and storage. Merge it with your distribution configuration;
keep the systemd service running as the dedicated `radicale` OS user. If using
Ubuntu's packaged installation with nginx, a typical sequence is:

```bash
sudo apt install radicale apache2-utils python3-bcrypt
# Use -c only for a NEW password file; it replaces an existing file.
sudo htpasswd -B -c /etc/radicale/users alice
sudo htpasswd -B /etc/radicale/users partner
sudo htpasswd -B /etc/radicale/users family
sudo chown root:radicale /etc/radicale/users
sudo chmod 640 /etc/radicale/users
sudo systemctl restart radicale
```

Use `htpasswd_encryption = bcrypt`, not `autodetect`; distribution-supported
algorithms vary. If the file already exists, omit `-c` for every command. The
password prompts keep secrets out of shell arguments. Preserve nginx's CalDAV
HTTP methods and conditional headers. Authenticate as each identity in Radicale's
built-in web interface or a CalDAV client and create at least one calendar under
that account. This MCP intentionally does not create/delete collections.

The MCP boundary supplements Radicale permissions; **Radicale is the authority**.
Calendar URLs come only from discovery. Transport checks reject another origin,
base-path escape and redirects before sending credentials. Ambient `.netrc`,
proxy credentials and DNS endpoint discovery cannot select another identity.
The process has no tools for files, shell execution or credential access.

Client configuration and environment variables are not an OS sandbox. An agent
with unrestricted shell/file access as the same OS user could read that user's
credentials. Use separate OS users/containers and file permissions when that
threat is in scope. The one-identity guarantee applies to this MCP's tool and
transport surface; it cannot constrain other tools granted to the client.

## Test against your running Radicale

Use the same host/URL that the MCP server process can reach. Read a password from
the terminal rather than putting it on the command line (Bash):

```bash
export CALDAV_URL=http://127.0.0.1:5232
export CALDAV_USERNAME=alice
export CALDAV_DEFAULT_TIMEZONE=America/New_York
read -rsp 'Radicale password: ' CALDAV_PASSWORD
export CALDAV_PASSWORD
env -u PYTHONPATH radicale-mcp --check
```

Then, from this project, exercise actual stdio MCP discovery and a bounded read:

```bash
env -u PYTHONPATH uv sync --locked
env -u PYTHONPATH uv run --locked python scripts/smoke.py --command "$PWD/.venv/bin/radicale-mcp"
```

Optionally exercise a temporary create/read/update/delete in a calendar you own.
Copy its `calendar_id` from discovery; this is an explicit write test:

```bash
env -u PYTHONPATH uv run --locked python scripts/smoke.py \
  --command "$PWD/.venv/bin/radicale-mcp" \
  --write-calendar c_REPLACE_WITH_DISCOVERED_ID
unset CALDAV_PASSWORD
```

The write smoke test exercises temporary VEVENT and VTODO resources, prints their
opaque IDs, and uses conditional cleanup. If a network failure or concurrent
change prevents cleanup, inspect those resources in your calendar client. To
check family access, repeat with `CALDAV_USERNAME=family` and its password.
Running `radicale-mcp` directly with no flags waits for MCP messages on stdin;
the silent wait is expected.

## Automated tests and architecture

```bash
env -u PYTHONPATH uv sync --locked
env -u PYTHONPATH uv run --locked pytest
env -u PYTHONPATH uv run --locked pytest -m 'not integration'  # pure/unit tests only
env -u PYTHONPATH uv run --locked pytest -m integration        # real Radicale and MCP tests
env -u PYTHONPATH uv run --locked ruff check src tests scripts
env -u PYTHONPATH uv run --locked ruff format --check src tests scripts
env -u PYTHONPATH uv run --locked mypy src
env -u PYTHONPATH uv build
```

Integration tests automatically launch the installed Radicale dependency on a
temporary loopback port with temporary storage, random Alice/Bob test passwords,
real htpasswd authentication and `owner_only`. The test-only password file uses
`plain` in its private temporary directory; production examples use bcrypt.
No Docker, running user server or real secrets are needed. The suite issues
unauthorized HTTP GET/PROPFIND/REPORT/PUT/DELETE requests against Bob as Alice,
so isolation is verified at Radicale itself. Tests also cover conditional writes,
ICS preservation, VTODO lifecycle/completion, recurrence reads and event exceptions,
ranges, busy time, secret-safe errors, CLI startup, and both current and legacy
MCP stdio negotiation. Remote-proxy integration performs both VEVENT and VTODO CRUD.

The implementation uses the current official
[MCP Python SDK v2 `MCPServer` API](https://github.com/modelcontextprotocol/python-sdk),
[python-caldav](https://github.com/python-caldav/caldav),
[icalendar](https://icalendar.readthedocs.io/), and
[recurring-ical-events](https://recurring-ical-events.readthedocs.io/en/latest/user-guide/examples.html).
The VTODO behavior also follows the current
[python-caldav task API](https://caldav.readthedocs.io/stable/tutorial.html).
Dependencies are constrained to compatible major versions, with exact versions in `uv.lock`.
No third-party project implementation was copied.

`server.py` defines typed MCP tools, safe errors, annotations and stdio startup.
`service.py` implements domain operations and calendar math. `client.py` owns
discovery, transport restrictions and conditional requests. `events.py` owns
iCalendar normalization and recurrence expansion; `recurrence.py` owns explicit
mutation scopes, overrides, exclusions and rule validation. `splitting.py` partitions
a series by original recurrence IDs; `service.py` coordinates conditional split writes
and recovery. `timeutils.py` handles
zones and intervals.
`tasks.py` owns VTODO validation, normalization and conservative recurrence reads.
`models.py`, `config.py`, and `errors.py` hold outputs, immutable settings, and
safe error types. Synchronous CalDAV calls run in a worker thread, serialized
per process to protect the shared HTTP session. ETags handle other processes.
No background synchronization or persistent application state is needed.

## Other v4 limitations

- VTODO recurrence is read-only as described above. VTODO dependencies/relations,
  assignees/attendees and client-specific task extensions are preserved but do not
  have dedicated tool arguments. No contacts, calendar administration, attachments,
  or scheduling/invitation workflow. Existing attendees/alarms survive partial edits.
- The chosen Radicale calendar collection must allow VTODO. Some clients create
  VEVENT-only collections through `supported-calendar-component-set`; this MCP does
  not create or change collections, so create/select a task-capable collection in a
  CalDAV client when needed.
- All-day/timed conversion is unsupported on update. Use explicit delete/create
  if you need that change. Changing a non-recurring floating event stores zoned times; recurring edits
  preserve floating times.
- Custom ACL collections outside principal discovery are not exposed; there is
  no arbitrary-URL escape hatch. No automatic retries, offline cache, OAuth,
  Digest authentication, proxy configuration or transport other than MCP stdio.
- Availability is a read-time view; calendars can change before booking. Across
  separate identities, combined free time must be computed by the caller.
