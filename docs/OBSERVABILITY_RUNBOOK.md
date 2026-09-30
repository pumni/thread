# Observability and readiness runbook (#72)

This checkpoint adds structured lifecycle logs and an operational readiness
snapshot. PostgreSQL remains the source of durable state. Metrics, dashboards,
OpenTelemetry, distributed tracing, deployment probes, and the #62 CRM result
transport are outside this checkpoint.

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
