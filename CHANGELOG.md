# Changelog

## 0.4.0 — V4

- Added eight typed VTODO tools: list, search, get, create, partial update,
  complete, reopen, and conditional delete.
- Normalize DTSTART/DUE, all-day/floating/zoned values, categories, priority,
  status, progress, UTC completion timestamps, recurrence metadata, and ETags.
- Preserve unknown VTODO properties and alarms; validate RFC 5545 date, duration,
  status, priority, progress, and completion constraints.
- Added deterministic DUE/DTSTART range filters, completed/open/all states,
  undated handling, pagination, response budgets, and local text search.
- Expand well-defined DTSTART-based recurring VTODOs for bounded reads and
  explicitly reject recurring-task mutation instead of guessing client semantics.
- Added real Radicale task lifecycle, owner-only isolation, remote subpath proxy,
  malformed-data, recurrence, time-zone, metadata, and stale-ETag tests.
- Added VEVENT categories, priority, geo, attach, comment, resources, URL,
  organizer, attendee, and contact properties. ATTACH currently accepts URI
  values only; binary attachment support is deferred.
- Preserved the single-identity security boundary and event behavior from v3.

## 0.3.0 — V3

- create_event accepts validated DAILY/WEEKLY/MONTHLY/YEARLY recurrence rules.
- update_event supports explicit future scope with an original occurrence boundary.
- Splits retain earlier exceptions and remaining COUNT/original UNTIL, and return successor IDs.
- Future text changes retain exceptions; schedule changes refuse ambiguous future remapping.
- delete_event supports future truncation with a single conditional mutation.
- Two-resource writes use conditional creation, ETag truncation, conditional rollback,
  deterministic successor identities for identical requests, and explicit recovery errors.
- Correct floating UNTIL boundaries and preserve UTC recurrence cadence across DST.
- Preserved one-account process isolation, stdio transport and remote Radicale support.
- Added real Radicale, MCP, split arithmetic and failure-injection coverage; see VALIDATION.md.

# Changes

## 0.2.0 — recurring mutations (v2)

- Explicit `event`, `occurrence`, and `series` mutation scopes. Recurring resources
  refuse the default scope rather than silently modifying a whole series.
- `get_event` resolves an optional original occurrence ID and returns its current
  effective representation plus the resource ETag.
- Occurrence updates create/replace RECURRENCE-ID overrides; occurrence deletions
  use EXDATE and remove any matching override, preserving DTSTART, COUNT, and UID.
- Whole-series text edits propagate requested fields to existing overrides.
- Simple series support schedule and RRULE changes, with consistent anchor/rule
  validation. Series with existing exceptions refuse schedule rewrites.
- ETag checks protect every mutation, including separate edits to different
  occurrences in the same resource.
- New real Radicale/MCP tests, examples, and upgrade documentation. Existing
  one-identity isolation, tool names, non-recurring calls and deployment config
  remain compatible.

Scope exclusions: this-and-following; recurrence creation; timezone/type changes
for series; complex recurrence rewrites; existing documented DST engine limits.

## 0.1.0 — initial version (v1)

Nine stdio tools, authenticated CalDAV discovery, non-recurring CRUD, recurrence
listing, free/busy and availability, owner_only integration tests, and Hermes docs.
