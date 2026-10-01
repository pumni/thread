# Observability and readiness runbook (#72)

This runbook records the structured lifecycle logs and readiness snapshot from
#72, bounded metrics from #89, and bounded opt-in OpenTelemetry tracing from
#91. PostgreSQL remains the source of durable state. No collector, dashboards,
or #62 CRM result transport is deployed by these checkpoints.

## Lifecycle correlation

Command events include the available `command_id`, `correlation_id`,
`account_id`, `command_type`, status, and safe `error_code`. WorkerJob events
include available `worker_job_id`, parent `command_id`, `account_id`,
`worker_id`, capability, status, and safe error code. Scheduler emits one
bounded tick summary with stage counts and duration, alongside existing
stage-failure events. Logs do not include one record per scanned row or per
lease heartbeat.

Correlation context is task-local and bound only for the operation lifetime.
It is reset after each operation, including concurrent Command execution.
Correlation IDs remain visible; ordinary identifiers are not redacted merely
because their key contains `id`.

## Secret and payload handling

The structlog processor chain renders stack/exception information first, then
recursively sanitizes event mappings, lists, and tuples before JSON rendering.
The sanitizer copies the event data and uses the stable marker `[REDACTED]`.
Sensitive key names are matched case-insensitively, including authorization,
token, secret, password, private key, signature, cookie, enrollment code, and
credential reference fields. String values are also checked for Bearer tokens,
`TH|...` values, Threads token environment references, and PEM private-key
blocks. `SecretStr`, `SecretBytes`, exception objects, and raw byte-like values
are never rendered with their contents.

Never log Command payload/result documents, WorkerJob input/checkpoint/result,
Authorization headers, access/session/enrollment tokens, OAuth or proxy
credential references, cookies/browser storage, private keys/signatures,
lease tokens, or raw remote request/response bodies. Existing Worker HTTP
enrollment/session response fields remain part of the protocol; logging
sanitization does not remove or change them.

This is defense in depth, not a general secret detector. It cannot reliably
recognize arbitrary high-entropy values under neutral keys, novel token formats,
or secrets embedded in unstructured values that do not match the known
patterns. Callers must keep secret-bearing and business payload data out of log
events. Exception text is sanitized after normal stack formatting, but callers
should still avoid raising exceptions that include sensitive data.

## Sensitive representations and worker auth responses (#75)

`repr()` hides Command payload/result/checkpoint and execution lease values,
WorkerJob documents and lease values, worker enrollment/session digests,
challenge nonces, issued enrollment/session credentials, and worker network
credential references. This is defense in depth for accidental debugging; it
does not make logging whole request, response, domain, or execution objects
appropriate. Continue to keep all secret-bearing and business payload data out
of log events.

The worker wire contract is unchanged. Authorized enrollment, challenge, and
session exchange responses still contain the values their clients need, and
assigned account context still carries its configured network `credential_ref`.
Those four HTTP responses use `Cache-Control: no-store`. PostgreSQL retains
enrollment/session token digests and configured credential references; it does
not retain raw enrollment codes or session access tokens.

This #75 hardening added no session revocation flow and did not change worker
auth semantics. It added no metrics or tracing; #89 separately adds bounded
operational metrics, and #91 later adds opt-in tracing. Issue #3
remains OPEN as the production/release gate; #62 remains a separate CRM result
transport dependency.

## Bounded operational metrics (#89)

The HTTP Control Plane exposes Prometheus text at `GET /metrics` on the existing
HTTP listener. The endpoint performs a short-lived, read-only PostgreSQL
aggregate with the configured readiness timeout. It emits:

- `threads_platform_workers{status=...}` — persisted count for every existing
  `WorkerStatus`, including `DISABLED`;
- `threads_platform_worker_jobs{status=...}` — persisted count for every
  existing `WorkerJobStatus`;
- `threads_platform_database_up` — `1` when both aggregates complete, otherwise
  `0`;
- `threads_platform_command_execution_duration_seconds` — process-local
  histogram measured with monotonic time around actual `CommandRuntime`
  processing methods (`process` and selected `process_next`). It does not infer
  execution latency from persisted timestamps.

If PostgreSQL is unavailable or times out, `/metrics` still returns a valid
Prometheus response with `threads_platform_database_up 0`. Worker and WorkerJob
status samples are omitted because their durable counts could not be read. The
failure response contains no DSN or exception message. This diagnostic gauge
does not replace or change `/ready`.

The standalone scheduler owns a separate process-local registry and optional
private listener at `GET /metrics`. It exports:

- `threads_platform_scheduler_ticks_total{outcome=...}` with only `success` or
  `error`;
- `threads_platform_scheduler_tick_duration_seconds`;
- `threads_platform_scheduler_worker_presences_expired_total`;
- `threads_platform_scheduler_worker_job_lease_reclaims_total`;
- `threads_platform_scheduler_stage_failures_total{stage=...}` with only
  `worker_presence_expiry`, `activity_recurrence_generation`,
  `conversation_sync_dispatch`, `activity_materialization`,
  `command_processing`, `worker_job_recovery`, or `outbox_delivery`;
- `threads_platform_command_execution_duration_seconds` for CommandRuntime
  processing in this process. `process_next` records a sample only when a
  command was selected.

Presence-expiry and lease-reclaim counters consume the actual scheduler tick
result fields `worker_presences_expired` and `worker_jobs_recovered`, including
partial results from a tick that later reports a stage error. They do not run
parallel recovery logic. Existing tick order, failure handling, and backoff are
unchanged.

Metric names have the `threads_platform_` prefix. Labels are limited to the
Worker/WorkerJob status enums, scheduler outcome, and fixed scheduler stage.
Command, correlation, Worker, WorkerJob, and account IDs; hostnames; profile or
session identifiers; raw command/capability values; URLs; payloads; errors;
DSNs; credentials; and request headers never enter the metric registry. No
browser-session metric is exported: persisted `active_browser_sessions` is the
last Worker report and may remain nonzero after scheduler-owned presence
expiry. Reporting it as live capacity would require defining or duplicating
freshness semantics, so it remains deferred.

Scheduler metrics are disabled by default. Configure
`THREADS_PLATFORM_SCHEDULER_METRICS_ENABLED`,
`THREADS_PLATFORM_SCHEDULER_METRICS_HOST` (`127.0.0.1` or `0.0.0.0`), and
`THREADS_PLATFORM_SCHEDULER_METRICS_PORT` (1–65535; default 9101). Committed
Compose enables the listener on `0.0.0.0:9101` inside the private Compose
network and publishes no scheduler metrics port to the host. The HTTP endpoint
uses the normal HTTP listener.

Gauges are re-read from PostgreSQL on every HTTP scrape and recover immediately
after HTTP process recreation. Histograms and scheduler counters are process
local and reset when their owning process restarts; they are never stored in
PostgreSQL. Metrics are diagnostic only, not business state, a queue, or a
readiness decision. No Prometheus server, collector, dashboard, alerting, or
tracing collector is included. Issue #3 remains OPEN and #62 remains separate.

## Bounded OpenTelemetry tracing (#91)

`THREADS_PLATFORM_TRACING_ENABLED` defaults to `false` independently in the
HTTP and scheduler processes. When disabled, the process creates no tracer
provider or exporter and makes no exporter connection. When enabled, that
process creates its own provider, a fixed `service.name` resource
(`threads-platform-http` or `threads-platform-scheduler`), and an OTLP/HTTP
exporter. No global tracer provider is installed. The checked-in Compose
topology explicitly keeps tracing disabled, adds no collector service, and
publishes no OTLP port.

Configure the endpoint with `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or the standard
fallback `OTEL_EXPORTER_OTLP_ENDPOINT`. If neither is set, the OTLP/HTTP
exporter uses its standard localhost endpoint. Authentication may be supplied
through `OTEL_EXPORTER_OTLP_TRACES_HEADERS` or `OTEL_EXPORTER_OTLP_HEADERS`.
These values are environment-only and are never logged or placed in span
resources. Configured endpoints are rejected if the scheme is not HTTP(S), the
host is missing, or user information, query, fragment, or whitespace appears.
The trace-specific endpoint is used as supplied; the generic base endpoint
gets `/v1/traces` appended.

HTTP spans are named `http.server.request`; they contain only `process.role`,
an allowlisted method (or `OTHER`), a registered route template (or
`unmatched`), and response status. `/health`, `/ready`, and `/metrics` are not
traced. `command.receive`, `command.process`, and selected
`command.process_next` spans are children of the active HTTP or scheduler
context and add only bounded process role and Command status. No Command,
account, Worker, or correlation ID, raw command type, payload, result, URL,
header, or database field is exported.

Each scheduler tick has one `scheduler.tick` span and child spans named
`scheduler.stage.<stage>` for the existing fixed stages. The optional
`scheduler.stage.outbox_delivery` span exists only when that stage is
configured. Attributes use only `process.role`, fixed `scheduler.stage`, and
`scheduler.outcome=success` on a returned tick. A failed operation sets OTel
status `ERROR` and the constant `error.type=exception`; it does not record an
exception event, message, or stack. Tick order, partial completion, backoff,
metrics, and durable scheduler behavior are unchanged.

Inbound propagation accepts the W3C `traceparent` header. The optional vendor
`tracestate` header and baggage are ignored. Child spans use only same-process
context. Worker HTTP and WebSocket protocols remain unchanged, and trace IDs
are never written to PostgreSQL or durable Command, WorkerJob, or Worker state.

Structured logs receive lowercase 32-character `trace_id` and 16-character
`span_id` fields only inside an active recording span. They are diagnostic
correlation only. The normal redaction processor still runs after exception
formatting and after trace fields are added. OpenTelemetry SDK and HTTP
transport records from `urllib3`, `requests`, and `http.client` are reduced to
a fixed event before handlers render them, so endpoint paths, headers,
responses, and exception values cannot leak through dependency logs.

The exporter queue holds at most 64 spans, exports batches of at most 64, limits
an OTLP request to 1 MiB, and sets a two-second request timeout. Process
shutdown runs flush and provider close in a daemon cleanup thread and waits at
most three seconds. The SDK's own atexit shutdown hook is disabled so it cannot
add an unbounded synchronous shutdown path. Exporter setup, export, flush, or
shutdown failure is fail-open for Commands, scheduler ticks, readiness,
metrics, and WorkerJob semantics; failures produce only fixed phase/service
logs. Queue contents and trace data are process-local and may be lost on
exporter failure or process restart. The locked application does not
install SQLAlchemy/database or outbound Threads/Meta HTTP auto-instrumentation.
No OTel collector, tracing port, dashboard, or alerting service is deployed.
Issue #3 remains OPEN and #62 remains separate.

## Health and readiness

`GET /health` is process liveness. It returns `{"status":"ok"}` without
contacting PostgreSQL.

`GET /ready` uses a short-lived PostgreSQL interaction with a two-second
default timeout (configurable by `THREADS_PLATFORM_READINESS_TIMEOUT_SECONDS`,
bounded above zero and at 30 seconds). It runs `SELECT 1` and aggregates persisted
`worker_nodes.status` counts, excluding `DISABLED` workers. It does not mutate
state or run presence expiry; the scheduler owns expiry of stale Worker
presence.

The bounded response contains `overall`, `database`, `fleet`,
`workers_available`, and aggregate counts for total, online, degraded, draining,
offline, registering, and upgrade-required Workers. It never contains worker
IDs, hostnames, account IDs, profile references, database URLs, or exception
messages. If the database is unavailable or times out, the response is
`NOT_READY` / `DOWN` with HTTP 503. Its worker counts are zero and
`workers_available` is false because fleet state could not be read.

With the database up, an empty non-disabled fleet is `READY` / `EMPTY` and HTTP
200; an all-online fleet is `READY` / `HEALTHY` and HTTP 200. Any non-disabled
worker that is degraded, draining, offline, registering, or upgrade-required
makes the snapshot `DEGRADED` / `DEGRADED`, still HTTP 200. Fleet degradation
is diagnostic; it must not trigger orchestrator restart loops. Disabled-only
fleets are empty and ready.

Issue #3 remains OPEN as the production/release gate. Phase A is accepted and
Phase B is `NOT_RUN`; this checkpoint makes no production release claim. Issue
#62 remains a separate CRM result transport dependency.
