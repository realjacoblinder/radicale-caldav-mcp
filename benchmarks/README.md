# Local read-only baseline

`baseline.py` benchmarks the existing service API against an isolated Radicale subprocess. It
accepts no server URL or credentials, binds Radicale to loopback, creates synthetic credentials and
calendar data in a temporary directory, performs only read operations during measurement, and
removes the fixture on exit. It does not load `.env` files or query a configured/real calendar.

Run the reproducible default workload from the repository root:

```console
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/baseline.py \
  --warmup 2 --samples 30 --resources 24 \
  > benchmarks/baseline-results.json
```

The JSON output contains median and linearly interpolated p95 wall time. Instrumented DAV request
time is divided into discovery (`PROPFIND`) and resource transfer (`REPORT`/`GET`); the remainder of
operation wall time is reported as `local_service_ms`. This remainder includes Python parsing and
filtering plus measurement overhead, so it is an attribution estimate rather than CPU profiling.
Method counts, decoded response-body byte totals, and DAV multistatus response-element counts are
reported per sample. A successful GET counts as one response resource. URLs, request/authorization
headers, and response/calendar bodies are never recorded.

The fixture has two calendars, each with equal numbers of VEVENT and VTODO resources. The measured
task range is narrower than the seeded dates, intentionally exposing whole-calendar VTODO transfer.
Event listing and free/busy each use a two-day short window and a 24-day long window; both windows
exercise the event query's one-day server-side padding. Each operation is warmed independently
before its recorded samples.

## Threshold for a later optimization phase

Any later optimization must return identical results and satisfy at least one of these thresholds:
eliminate a redundant CalDAV round trip, reduce response bytes/resources by at least 25%, improve
median wall time by at least 10%, or improve p95 wall time by at least 20%. A cold-path p95 regression
greater than 5% is a no-go without approval. Compare on the same fixture and machine with this exact
command. Local loopback results remain sensitive to host load and do not represent real-network or
Hermes transport latency.
